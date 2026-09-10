"""Ciclo de vida: servidor opencode + processo do bot (/restart)."""
from pathlib import Path
import asyncio
import logging
import os
import signal
import subprocess
import time
from dotenv import dotenv_values
from . import config, state
from .turns import _kill_all_turns
from .opencode import oc_ensure_server


logger = logging.getLogger(__name__)


def _read_oc_port_from_env() -> tuple[int, str]:
    """Lê a porta/URL do opencode server direto do .env (sem mexer em os.environ),
    para que o /restart respeite uma mudança de porta feita em disco."""
    port, url = config.OC_PORT, config.OC_URL
    try:
        vals = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
        raw = str(vals.get("OPENCODE_SERVER_PORT") or "").strip()
        if raw.isdigit():
            port = int(raw)
        url = str(vals.get("OPENCODE_SERVER_URL") or "").strip() or f"http://127.0.0.1:{port}"
    except Exception:
        pass
    return port, url


async def _find_opencode_pids(ports: set[int], uid: int) -> list[int]:
    """Retorna os PIDs (do usuário atual, excluindo o próprio processo) que
    correspondem a `opencode serve --port <porta>` para as portas dadas."""
    pids: list[int] = []
    for port in sorted(ports):
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-u", str(uid), "-f", f"opencode serve --port {port}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        for x in out.decode().split():
            pid = int(x)
            if pid != os.getpid() and pid not in pids:
                pids.append(pid)
    return pids


def _pids_alive(pids: list[int]) -> list[int]:
    """Filtra `pids` mantendo só os que ainda existem (ou cujo estado não dá
    pra confirmar por falta de permissão — tratados como vivos por segurança)."""
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            alive.append(pid)
    return alive


async def _kill_opencode_servers(ports: set[int] | None = None, timeout: float = 15.0, max_rounds: int = 3) -> bool:
    """Derruba apenas os processos `opencode serve --port <porta>` do próprio
    usuário (incluindo órfãos da porta antiga), sem matar por padrão global
    outras instâncias do opencode na máquina. Retorna True se nenhum sobrou.

    Faz até `max_rounds` ciclos de SIGTERM -> espera -> SIGKILL antes de
    desistir. Isso é deliberadamente um loop com teto fixo (e não recursão
    infinita): se algum PID nunca puder ser confirmado como morto (ex.
    permissão negada, processo zumbi preso em D-state), o código antigo
    recursava sem limite a cada rodada e podia estourar o limite de
    recursão do Python numa máquina onde o processo simplesmente não morre.
    """
    ports = ports or {config.OC_PORT}
    uid = os.getuid()

    for round_num in range(1, max_rounds + 1):
        targets: list[int] = []
        for _ in range(3):
            targets = await _find_opencode_pids(ports, uid)
            if targets:
                break
            await asyncio.sleep(0.3)
        if not targets:
            return True

        for pid in targets:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pids_alive(targets):
                return True
            await asyncio.sleep(0.25)

        for pid in targets:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        await asyncio.sleep(0.5)

        still_alive = _pids_alive(targets)
        if not still_alive:
            return True
        if round_num == max_rounds:
            logger.error(
                "Não foi possível encerrar opencode serve nas portas %s após %d rodada(s); pids ainda vivos: %s",
                sorted(ports), max_rounds, still_alive,
            )
    return False


def _spawn_bot_process():
    """Relaça o bot num processo destacado, com o mesmo run.sh com que foi iniciado,
    para sobreviver ao encerramento do processo atual."""
    bot_dir = Path(__file__).parent
    log_path = bot_dir / "bot.log"
    log_fh = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            ["bash", "run.sh"],
            cwd=str(bot_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        logger.info("Processo do bot relaçado (pid=%d)", proc.pid)
    finally:
        log_fh.close()


async def _cycle_server_locked() -> None:
    """Derruba o `opencode serve` atual (inclusive zumbis) e sobe um novo,
    recriando o cliente HTTP. O processo do bot continua o mesmo."""
    new_port, new_url = _read_oc_port_from_env()
    config.OC_PORT, config.OC_URL = new_port, new_url
    if state._client is not None:
        try:
            await state._client.aclose()
        except Exception:
            pass
        state._client = None
    await _kill_opencode_servers({config.OC_PORT})
    if state._server_proc is not None:
        try:
            await asyncio.wait_for(state._server_proc.wait(), timeout=5)
        except Exception:
            pass
        state._server_proc = None
    state._we_started_server = False
    await oc_ensure_server()
    logger.info("Servidor opencode reciclado (porta=%d)", config.OC_PORT)


async def _respawn_bot_and_exit() -> None:
    """Sobe um processo novo do bot via run.sh e encerra este via SIGTERM
    (o run_polling faz o shutdown gracioso). Quem chama deve antes desarmar
    o oc_stop_server (via state._we_started_server=False) se o servidor deve ficar no ar."""
    _spawn_bot_process()
    await asyncio.sleep(1.0)
    os.kill(os.getpid(), signal.SIGTERM)


async def _restart_server_only(chat_id: int):
    """Reinicia só o servidor opencode. O bot continua no ar."""
    app = state._app_ref
    try:
        await _kill_all_turns()
        await _cycle_server_locked()
        logger.info("Restart do servidor concluído (porta=%d)", config.OC_PORT)
        if app:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ *Servidor reiniciado* — servidor `{config.OC_URL}` OK. O bot continuou no ar.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    except Exception as e:
        logger.exception("Falha no restart do servidor")
        if app:
            try:
                msg = f"❌ *Falha no restart do servidor:* `{e}`\n\nVerifique o log: `.opencode_bot_server.log`"
                await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            except Exception:
                pass


async def _restart_bot_only(chat_id: int):
    """Reinicia só o bot do Telegram (novo processo). O servidor continua no ar."""
    app = state._app_ref
    if app:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text="🔁 *Reiniciando o bot...* volto em segundos. O servidor continua no ar.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    await _kill_all_turns()
    state._we_started_server = False  # post_shutdown não pode matar o servidor ao sair
    await _respawn_bot_and_exit()


async def _restart_both(chat_id: int):
    """Reinicia o servidor e o bot. O processo novo sobe um servidor fresco no boot."""
    app = state._app_ref
    if app:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text="🔁 *Reiniciando bot + servidor...* volto em segundos.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    await _kill_all_turns()
    try:
        await _kill_opencode_servers({config.OC_PORT})
    except Exception:
        logger.exception("Falha ao derrubar o servidor no restart duplo")
    if state._server_proc is not None:
        try:
            await asyncio.wait_for(state._server_proc.wait(), timeout=5)
        except Exception:
            pass
        state._server_proc = None
    state._we_started_server = False
    await _respawn_bot_and_exit()


async def _perform_restart(chat_id: int, target: str = "both"):
    """Despacha o /restart: `bot`, `server` ou `both`."""
    if target == "bot":
        await _restart_bot_only(chat_id)
    elif target == "server":
        await _restart_server_only(chat_id)
    else:
        await _restart_both(chat_id)
