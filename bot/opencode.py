"""Cliente HTTP do opencode server e ciclo de vida do `opencode serve`."""
from pathlib import Path
import asyncio
import httpx
import logging
import re
from . import config, state


logger = logging.getLogger(__name__)


async def oc_server_ok() -> bool:
    try:
        r = await asyncio.wait_for(state._client.get("/config"), timeout=7.0)
        return r.status_code == 200
    except Exception:
        return False


async def oc_start_server():
    """Spawns `opencode serve`, logging its output to a file instead of discarding it,
    so a failed boot can actually be diagnosed."""
    log_path = Path(config.OPENCODE_DIR) / ".opencode_bot_server.log"
    log_fh = open(log_path, "ab", buffering=0)
    proc = await asyncio.create_subprocess_exec(
        "opencode", "serve", "--port", str(config.OC_PORT), "--print-logs",
        cwd=config.OPENCODE_DIR,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    state._server_proc = proc
    state._we_started_server = True
    for _ in range(200):
        if await oc_server_ok():
            logger.info("opencode server pronto na porta %d", config.OC_PORT)
            return
        if proc.returncode is not None:
            log_fh.close()
            tail = ""
            try:
                tail = log_path.read_text(errors="replace")[-2000:]
            except Exception:
                pass
            raise RuntimeError(f"opencode serve encerrou sozinho (code={proc.returncode}). Log:\n{tail}")
        await asyncio.sleep(0.5)
    log_fh.close()
    tail = ""
    try:
        tail = log_path.read_text(errors="replace")[-2000:]
    except Exception:
        pass
    raise RuntimeError(f"opencode server não respondeu a tempo. Log:\n{tail}")


async def oc_ensure_server():
    if state._client is None:
        # Finite timeouts: infinite ones let a hung server wedge every command forever.
        # Read timeout is generous because prompts can legitimately take a while.
        state._client = httpx.AsyncClient(
            base_url=config.OC_URL,
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=10.0),
        )
    try:
        if await oc_server_ok():
            logger.info("Conectado ao opencode server %s", config.OC_URL)
            return
    except Exception:
        pass
    await oc_start_server()


async def oc_stop_server():
    """Terminate the opencode server process only if we're the ones who spawned it."""
    if state._server_proc and state._we_started_server and state._server_proc.returncode is None:
        try:
            state._server_proc.terminate()
            await asyncio.wait_for(state._server_proc.wait(), timeout=10)
        except Exception:
            try:
                state._server_proc.kill()
            except Exception as e:
                logger.debug("Falha ao matar o servidor opencode: %s", e)
    state._server_proc = None


async def oc_create_session() -> str:
    r = await state._client.post("/session", json={"dir": config.OPENCODE_DIR})
    r.raise_for_status()
    return r.json()["id"]


async def oc_send_message(sid: str, text: str = "", model: dict | None = None, agent: str | None = None, parts: list | None = None):
    body_parts: list[dict] = []
    if text:
        body_parts.append({"type": "text", "text": text})
    if parts:
        body_parts.extend(parts)
    body: dict = {"parts": body_parts}
    if model:
        body["model"] = model
    if agent:
        body["agent"] = agent
    r = await state._client.post(
        f"/session/{sid}/prompt_async",
        json=body,
    )
    r.raise_for_status()


async def oc_send_with_retry(chat_cfg: dict, turn: dict, text: str, model: dict | None = None, agent: str | None = None, parts: list | None = None):
    """Envia a mensagem, tentando de novo uma vez se a sessão sumiu (404) ou
    se o servidor caiu (ConnectError).

    Importante: só recria a sessão no caso de 404 (a sessão realmente não
    existe mais no servidor). No caso de ConnectError, o servidor só estava
    indisponível — a sessão antiga continua válida assim que ele volta, então
    reenviamos para o MESMO sid em vez de descartar a conversa à toa.
    """
    for attempt in (1, 2):
        try:
            await oc_send_message(turn["sid"], text, model=model, agent=agent, parts=parts)
            return
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404 or attempt == 2:
                raise
            logger.warning("Sessão %s sumiu do servidor (404); recriando sessão", (turn.get("sid") or "")[-6:])
            sid = await oc_create_session()
            turn["sid"] = sid
            chat_cfg["sid"] = sid
            logger.info("Nova sessão criada: %s", sid[-6:])
        except httpx.ConnectError:
            if attempt == 2:
                raise
            logger.warning("Servidor opencode fora durante o envio; religando e tentando de novo")
            await oc_ensure_server()
    raise RuntimeError("não foi possível enviar a mensagem para o opencode")


async def _run_cli(*args: str, timeout: int = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "opencode", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.OPENCODE_DIR,
            start_new_session=True,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = (out or b"").decode(errors="replace").strip()
        errs = (err or b"").decode(errors="replace").strip()
        return text or errs
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "⏱ comando excedeu o tempo limite"
    except Exception as e:
        return f"❌ {e}"


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s or "")


async def oc_answer_permission(sid: str, perm_id: str, response: str):
    await state._client.post(
        f"/session/{sid}/permissions/{perm_id}",
        json={"response": response},
    )


async def oc_answer_question(request_id: str, answers: list) -> bool:
    try:
        r = await state._client.post(
            f"/question/{request_id}/reply",
            json={"answers": answers},
        )
        return r.status_code < 400
    except Exception as e:
        logger.warning("Falha ao responder pergunta %s: %s", request_id, e)
        return False


async def oc_reject_question(request_id: str) -> bool:
    try:
        r = await state._client.post(f"/question/{request_id}/reject")
        return r.status_code < 400
    except Exception as e:
        logger.warning("Falha ao rejeitar pergunta %s: %s", request_id, e)
        return False
