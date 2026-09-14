"""Cliente HTTP do opencode server **v2** e ciclo de vida do `opencode serve`.

Norma v2 (levantada contra `packages/protocol/openapi.json`):
- toda rota exige `Authorization: Basic base64("opencode:<senha>")`;
- rotas sob `/api/*` com envelopes `{data: ...}`;
- eventos SSE em `GET /api/event` com envelope `{type, data, ...}`;
- prompt via `POST /api/session/{id}/prompt {text, files}`;
- modelo/agente são estado da sessão (`/model`, `/agent`), não do prompt;
- permissões em `.../permission/{id}/reply {reply: once|always|reject}`;
- perguntas viraram forms (`form.created` + `.../form/{id}/reply|cancel`).
"""
from pathlib import Path
import asyncio
import base64
import logging
import os
import re
import secrets
import subprocess
import httpx
from . import config, state


logger = logging.getLogger(__name__)


def _effective_password() -> str:
    """Senha vigente: configurada no env ou gerada no spawn desta execução."""
    return config.OC_PASSWORD or getattr(state, "_server_password", "") or ""


def _auth_header() -> dict:
    pw = _effective_password()
    if not pw:
        return {}
    raw = base64.b64encode(f"opencode:{pw}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.OC_URL,
        timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=10.0),
        headers=_auth_header(),
    )


async def oc_server_ok() -> bool:
    try:
        r = await asyncio.wait_for(state._client.get("/api/status"), timeout=7.0)
        return r.status_code == 200
    except Exception:
        return False


async def oc_server_info() -> dict:
    """Identifica se o opencode está ATIVO ou DESATIVADO.

    Retorna dict com: ok, status ("ativo"/"desativado"), latency_ms,
    url, port, version, error. Usado pelo /status e pelo painel de botões para
    mostrar 🟢/🔴 sem duplicar a lógica de checagem."""
    import time
    info: dict = {
        "ok": False,
        "status": "desativado",
        "latency_ms": None,
        "url": config.OC_URL,
        "port": config.OC_PORT,
        "version": None,
        "error": None,
    }
    if state._client is None:
        info["error"] = "cliente HTTP não inicializado"
        return info
    t0 = time.monotonic()
    try:
        r = await asyncio.wait_for(state._client.get("/api/status"), timeout=7.0)
        info["latency_ms"] = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            info["ok"] = True
            info["status"] = "ativo"
            try:
                info["version"] = (r.json() or {}).get("version")
            except Exception:
                pass
        elif r.status_code == 401:
            info["error"] = "HTTP 401: senha do servidor (OPENCODE_SERVER_PASSWORD) incorreta"
        else:
            info["error"] = f"HTTP {r.status_code}"
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}".strip()[:200]
    return info


def _kill_stale_servers() -> list[int]:
    """Derruba `opencode serve --port <OC_PORT>` órfãos (ex.: de um boot anterior
    do bot, cuja senha gerada se perdeu). Nunca toca no próprio processo."""
    pattern = f"opencode serve --port {config.OC_PORT}"
    killed: list[int] = []
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10)
    except Exception as e:
        logger.debug("pgrep falhou: %s", e)
        return killed
    me = os.getpid()
    for line in (out.stdout or "").splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if "opencode serve" not in cmdline or f"--port {config.OC_PORT}" not in cmdline:
            continue
        # Evita matar o wrapper do próprio pgrep/bot (não contém o padrão exato).
        if "opencode_bot" in cmdline or "python" in cmdline.split(" ")[0]:
            # Processo python não é o binário opencode; pula por segurança.
            continue
        try:
            os.kill(pid, 15)
            killed.append(pid)
        except Exception as e:
            logger.debug("Falha ao sinalizar pid %d: %s", pid, e)
    return killed


async def oc_start_server():
    """Spawns `opencode serve` v2, logging its output to a file instead of discarding it,
    so a failed boot can actually be diagnosed."""
    stale = _kill_stale_servers()
    if stale:
        logger.info("Servidores órfãos encerrados: %s", stale)
        await asyncio.sleep(1.0)
    pw = config.OC_PASSWORD or secrets.token_urlsafe(24)
    state._server_password = pw
    # Cliente precisa da senha vigente antes do primeiro health check.
    if state._client is not None:
        try:
            await state._client.aclose()
        except Exception:
            pass
    state._client = _build_client()
    log_path = Path(config.OPENCODE_DIR) / ".opencode_bot_server.log"
    log_fh = open(log_path, "ab", buffering=0)
    env = dict(os.environ, OPENCODE_PASSWORD=pw)
    proc = await asyncio.create_subprocess_exec(
        "opencode", "serve", "--port", str(config.OC_PORT), "--print-logs",
        cwd=config.OPENCODE_DIR,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
        env=env,
    )
    state._server_proc = proc
    state._we_started_server = True
    for _ in range(200):
        if await oc_server_ok():
            logger.info("opencode server v2 pronto na porta %d", config.OC_PORT)
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
        state._client = _build_client()
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
    r = await state._client.post("/api/session", json={"directory": config.OPENCODE_DIR})
    r.raise_for_status()
    return r.json()["data"]["id"]


async def oc_list_sessions() -> list[dict]:
    """Lista as sessões do servidor (vazia se falhar)."""
    try:
        r = await state._client.get("/api/session")
        r.raise_for_status()
        data = (r.json() or {}).get("data")
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("Falha ao listar sessões: %s", e)
        return []


def pick_session_id(stored_sid: str | None, sessions: list[dict]) -> str | None:
    """Escolhe qual sessão retomar, sem rede (pura, testável).

    1) se o sid guardado ainda existe no servidor, mantém;
    2) senão, pega a mais recente (time.updated);
    3) se não há nenhuma, retorna None (quem chama cria uma nova)."""
    ids = {s.get("id") for s in sessions if s.get("id")}
    if stored_sid and stored_sid in ids:
        return stored_sid
    if not sessions:
        return None

    def _updated(s: dict) -> int:
        try:
            return int((s.get("time") or {}).get("updated", 0) or 0)
        except (TypeError, ValueError):
            return 0

    ordered = sorted(sessions, key=_updated, reverse=True)
    for s in ordered:
        if s.get("id"):
            return s["id"]
    return None


async def oc_restore_chat_session(chat_cfg: dict, sessions: list[dict]) -> tuple[str | None, bool]:
    """Volta para a sessão anterior do chat; cria uma se não houver.

    Retorna (sid, created). Atualiza chat_cfg["sid"] quando resolve."""
    sid = pick_session_id(chat_cfg.get("sid"), sessions)
    if sid:
        chat_cfg["sid"] = sid
        return sid, False
    try:
        sid = await oc_create_session()
    except Exception as e:
        logger.warning("Falha ao criar sessão: %s", e)
        return None, False
    chat_cfg["sid"] = sid
    sessions.append({"id": sid, "time": {"updated": 0}})
    return sid, True


def _v2_model_ref(model: dict | None) -> dict | None:
    if not model:
        return None
    return {"providerID": model.get("providerID"), "id": model.get("modelID")}


async def oc_send_message(sid: str, text: str = "", model: dict | None = None, agent: str | None = None, parts: list | None = None):
    # Modelo/agente são estado da sessão no v2: ajusta antes do prompt.
    if model:
        r = await state._client.post(f"/api/session/{sid}/model", json={"model": _v2_model_ref(model)})
        r.raise_for_status()
    if agent:
        r = await state._client.post(f"/api/session/{sid}/agent", json={"agent": agent})
        r.raise_for_status()
    files: list[dict] = []
    for p in parts or []:
        if isinstance(p, dict) and p.get("type") == "file" and p.get("url"):
            entry: dict = {"uri": p["url"]}
            if p.get("filename"):
                entry["name"] = p["filename"]
            files.append(entry)
        elif isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
            text = (text + "\n" + p["text"]).strip() if text else p["text"]
    body: dict = {"text": text}
    if files:
        body["files"] = files
    r = await state._client.post(
        f"/api/session/{sid}/prompt",
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
        f"/api/session/{sid}/permission/{perm_id}/reply",
        json={"reply": response},
    )


async def oc_answer_question(sid: str, request_id: str, answer: dict) -> bool:
    """Responde um form v2 (ex-pergunta). answer: {nome_do_campo: valor}."""
    try:
        r = await state._client.post(
            f"/api/session/{sid}/form/{request_id}/reply",
            json={"answer": answer},
        )
        return r.status_code < 400
    except Exception as e:
        logger.warning("Falha ao responder form %s: %s", request_id, e)
        return False


async def oc_reject_question(sid: str, request_id: str) -> bool:
    try:
        r = await state._client.post(f"/api/session/{sid}/form/{request_id}/cancel")
        return r.status_code < 400
    except Exception as e:
        logger.warning("Falha ao rejeitar form %s: %s", request_id, e)
        return False


async def oc_list_agents() -> list[str]:
    """Nomes dos agentes (via HTTP; o CLI v2 não tem `agent list`).

    O catálogo do servidor pode levar alguns segundos após o boot;
    tenta de novo antes de devolver vazio."""
    for attempt in (1, 2, 3):
        try:
            r = await state._client.get("/api/agent")
            r.raise_for_status()
            out = []
            for a in (r.json() or {}).get("data") or []:
                name = a.get("name") or a.get("id")
                if name and not a.get("hidden"):
                    out.append(name)
            if out or attempt == 3:
                return out
        except Exception as e:
            logger.warning("Falha ao listar agentes: %s", e)
            return []
        await asyncio.sleep(3.0)
    return []


async def oc_list_models() -> list[str]:
    """Modelos como `providerID/modelID` (via HTTP, fonte = o serve em uso)."""
    for attempt in (1, 2, 3):
        try:
            r = await state._client.get("/api/model")
            r.raise_for_status()
            out = []
            for m in (r.json() or {}).get("data") or []:
                if m.get("enabled") is False:
                    continue
                pid, mid = m.get("providerID"), m.get("modelID")
                if pid and mid:
                    out.append(f"{pid}/{mid}")
            if out or attempt == 3:
                return out
        except Exception as e:
            logger.warning("Falha ao listar modelos: %s", e)
            return []
        await asyncio.sleep(3.0)
    return []


async def oc_mcp_remove(name: str) -> bool:
    try:
        r = await state._client.request("DELETE", f"/api/mcp/{name}")
        return r.status_code < 400
    except Exception as e:
        logger.warning("Falha ao remover MCP %s: %s", name, e)
        return False


async def oc_mcp_set(name: str, cfg: dict) -> bool:
    try:
        r = await state._client.put(f"/api/mcp/{name}", json={"config": cfg})
        return r.status_code < 400
    except Exception as e:
        logger.warning("Falha ao configurar MCP %s: %s", name, e)
        return False
