#!/usr/bin/env python3
import os
import json
import time
import signal
import subprocess
import logging
import asyncio
import base64
import html
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
)
from telegram.ext import filters

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
if not OWNER_ID:
    logger.error("OWNER_ID não configurado — recusando iniciar por segurança.")
    raise SystemExit("OWNER_ID obrigatório. Defina-o no .env.")
CHAT_ID = os.getenv("CHAT_ID", "")
OPENCODE_DIR = os.getenv("OPENCODE_DIR", str(Path.home()))
OC_PORT = int(os.getenv("OPENCODE_SERVER_PORT", "4100"))
OC_URL = os.getenv("OPENCODE_SERVER_URL", f"http://127.0.0.1:{OC_PORT}")

VERSION = "1.9.1"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

_app_ref: Application | None = None
_client: httpx.AsyncClient | None = None
_server_proc: asyncio.subprocess.Process | None = None
_we_started_server = False
_stream_task: asyncio.Task | None = None
TURNS: dict[int, dict] = {}


def _get_owner_chat() -> int | None:
    if CHAT_ID:
        return int(CHAT_ID)
    return OWNER_ID or None


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id == OWNER_ID


async def reject_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.warning("Unauthorized: %s (id=%s)", update.effective_user.username or update.effective_user.first_name, update.effective_user.id)
    await update.message.reply_text("Access denied.")


# ---------------------------------------------------------------- opencode server

async def oc_server_ok() -> bool:
    try:
        r = await asyncio.wait_for(_client.get("/config"), timeout=7.0)
        return r.status_code == 200
    except Exception:
        return False


async def oc_start_server():
    """Spawns `opencode serve`, logging its output to a file instead of discarding it,
    so a failed boot can actually be diagnosed."""
    global _server_proc, _we_started_server
    log_path = Path(OPENCODE_DIR) / ".opencode_bot_server.log"
    log_fh = open(log_path, "ab", buffering=0)
    proc = await asyncio.create_subprocess_exec(
        "opencode", "serve", "--port", str(OC_PORT), "--print-logs",
        cwd=OPENCODE_DIR,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    _server_proc = proc
    _we_started_server = True
    for _ in range(200):
        if await oc_server_ok():
            logger.info("opencode server pronto na porta %d", OC_PORT)
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
    global _client
    if _client is None:
        # Finite timeouts: infinite ones let a hung server wedge every command forever.
        # Read timeout is generous because prompts can legitimately take a while.
        _client = httpx.AsyncClient(
            base_url=OC_URL,
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=10.0),
        )
    try:
        if await oc_server_ok():
            logger.info("Conectado ao opencode server %s", OC_URL)
            return
    except Exception:
        pass
    await oc_start_server()


async def oc_stop_server():
    """Terminate the opencode server process only if we're the ones who spawned it."""
    global _server_proc
    if _server_proc and _we_started_server and _server_proc.returncode is None:
        try:
            _server_proc.terminate()
            await asyncio.wait_for(_server_proc.wait(), timeout=10)
        except Exception:
            try:
                _server_proc.kill()
            except Exception as e:
                logger.debug("Falha ao matar o servidor opencode: %s", e)
    _server_proc = None


# ---------------------------------------------------------------- restart

def _read_oc_port_from_env() -> tuple[int, str]:
    """Lê a porta/URL do opencode server direto do .env (sem mexer em os.environ),
    para que o /restart respeite uma mudança de porta feita em disco."""
    port, url = OC_PORT, OC_URL
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(Path(__file__).parent / ".env")
        raw = str(vals.get("OPENCODE_SERVER_PORT") or "").strip()
        if raw.isdigit():
            port = int(raw)
        url = str(vals.get("OPENCODE_SERVER_URL") or "").strip() or f"http://127.0.0.1:{port}"
    except Exception:
        pass
    return port, url


async def _kill_opencode_servers(ports: set[int] | None = None, timeout: float = 15.0) -> bool:
    """Derruba apenas os processos `opencode serve --port <porta>` do próprio
    usuário (incluindo órfãos da porta antiga), sem matar por padrão global
    outras instâncias do opencode na máquina. Retorna True se nenhum sobrou."""
    ports = ports or {OC_PORT}
    targets: list[int] = []
    uid = os.getuid()
    for _ in range(3):
        targets = []
        for port in sorted(ports):
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-u", str(uid), "-f", f"opencode serve --port {port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            for x in out.decode().split():
                pid = int(x)
                if pid != os.getpid() and pid not in targets:
                    targets.append(pid)
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
        alive = []
        for pid in targets:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                alive.append(pid)
        if not alive:
            return True
        await asyncio.sleep(0.25)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    await asyncio.sleep(0.5)
    return not targets or await _kill_opencode_servers(ports, 0.5)


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
    global OC_PORT, OC_URL, _client, _server_proc, _we_started_server
    new_port, new_url = _read_oc_port_from_env()
    OC_PORT, OC_URL = new_port, new_url
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None
    await _kill_opencode_servers({OC_PORT})
    if _server_proc is not None:
        try:
            await asyncio.wait_for(_server_proc.wait(), timeout=5)
        except Exception:
            pass
        _server_proc = None
    _we_started_server = False
    await oc_ensure_server()
    logger.info("Servidor opencode reciclado (porta=%d)", OC_PORT)


async def _respawn_bot_and_exit() -> None:
    """Sobe um processo novo do bot via run.sh e encerra este via SIGTERM
    (o run_polling faz o shutdown gracioso). Quem chama deve antes desarmar
    o oc_stop_server (via _we_started_server=False) se o servidor deve ficar no ar."""
    _spawn_bot_process()
    await asyncio.sleep(1.0)
    os.kill(os.getpid(), signal.SIGTERM)


async def _restart_server_only(chat_id: int):
    """Reinicia s\u00f3 o servidor opencode. O bot continua no ar."""
    app = _app_ref
    try:
        await _kill_all_turns()
        await _cycle_server_locked()
        logger.info("Restart do servidor conclu\u00eddo (porta=%d)", OC_PORT)
        if app:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"\u2705 *Servidor reiniciado* \u2014 servidor `{OC_URL}` OK. O bot continuou no ar.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    except Exception as e:
        logger.exception("Falha no restart do servidor")
        if app:
            try:
                msg = f"\u274c *Falha no restart do servidor:* `{e}`\n\nVerifique o log: `.opencode_bot_server.log`"
                await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            except Exception:
                pass


async def _restart_bot_only(chat_id: int):
    """Reinicia s\u00f3 o bot do Telegram (novo processo). O servidor continua no ar."""
    global _we_started_server
    app = _app_ref
    if app:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text="\U0001f501 *Reiniciando o bot...* volto em segundos. O servidor continua no ar.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    await _kill_all_turns()
    _we_started_server = False  # post_shutdown n\u00e3o pode matar o servidor ao sair
    await _respawn_bot_and_exit()


async def _restart_both(chat_id: int):
    """Reinicia o servidor e o bot. O processo novo sobe um servidor fresco no boot."""
    global _server_proc, _we_started_server
    app = _app_ref
    if app:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text="\U0001f501 *Reiniciando bot + servidor...* volto em segundos.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    await _kill_all_turns()
    try:
        await _kill_opencode_servers({OC_PORT})
    except Exception:
        logger.exception("Falha ao derrubar o servidor no restart duplo")
    if _server_proc is not None:
        try:
            await asyncio.wait_for(_server_proc.wait(), timeout=5)
        except Exception:
            pass
        _server_proc = None
    _we_started_server = False
    await _respawn_bot_and_exit()


async def _perform_restart(chat_id: int, target: str = "both"):
    """Despacha o /restart: `bot`, `server` ou `both`."""
    if target == "bot":
        await _restart_bot_only(chat_id)
    elif target == "server":
        await _restart_server_only(chat_id)
    else:
        await _restart_both(chat_id)


async def oc_create_session() -> str:
    r = await _client.post("/session", json={"dir": OPENCODE_DIR})
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
    r = await _client.post(
        f"/session/{sid}/prompt_async",
        json=body,
    )
    r.raise_for_status()


async def oc_send_with_retry(chat_cfg: dict, turn: dict, text: str, model: dict | None = None, agent: str | None = None, parts: list | None = None):
    for attempt in (1, 2):
        try:
            await oc_send_message(turn["sid"], text, model=model, agent=agent, parts=parts)
            return
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404 or attempt == 2:
                raise
            logger.warning("Sessão %s sumiu do servidor (404); recriando sessão", (turn.get("sid") or "")[-6:])
        except httpx.ConnectError:
            if attempt == 2:
                raise
            logger.warning("Servidor opencode fora durante o envio; religando e tentando de novo")
            await oc_ensure_server()
        sid = await oc_create_session()
        turn["sid"] = sid
        chat_cfg["sid"] = sid
        logger.info("Nova sessão criada: %s", sid[-6:])
    raise RuntimeError("não foi possível enviar a mensagem para o opencode")


async def _run_cli(*args: str, timeout: int = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "opencode", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=OPENCODE_DIR,
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
        return "\u23f1 comando excedeu o tempo limite"
    except Exception as e:
        return f"\u274c {e}"


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s or "")


def _mcp_file() -> Path:
    return Path.home() / ".config" / "opencode" / "opencode.jsonc"


def _load_mcp_cfg() -> dict:
    p = _mcp_file()
    if not p.exists():
        return {}
    try:
        data = json.loads(_strip_jsonc_comments(p.read_text()))
    except Exception:
        return {}
    return (data or {}).get("mcp") or {}


def _mcp_url(name: str) -> str:
    return (_load_mcp_cfg().get(name) or {}).get("url") or ""


def _skip_jsonc_string(text: str, i: int) -> int:
    """text[i] é uma aspa dupla; retorna o índice logo após a aspa de fechamento."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    return n


def _skip_jsonc_ws(text: str, i: int, end: int) -> int:
    """Avança sobre espaços e comentários JSONC até `end` (exclusive)."""
    n = min(len(text), end)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = j + 1 if j != -1 else n
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = j + 2 if j != -1 else n
        else:
            break
    return i


def _strip_jsonc_comments(text: str) -> str:
    """Remove comentários (// e /* */) fora de strings, preservando a estrutura."""
    n = len(text)
    out: list[str] = []
    i = 0
    while i < n:
        c = text[i]
        if c == '"':
            j = _skip_jsonc_string(text, i)
            out.append(text[i:j])
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = j if j != -1 else n
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = j + 2 if j != -1 else n
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _has_jsonc_comments(text: str) -> bool:
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c == '"':
            i = _skip_jsonc_string(text, i)
        elif c == "/" and i + 1 < n and text[i + 1] in ("/", "*"):
            return True
        else:
            i += 1
    return False


def _jsonc_load(raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        data = json.loads(_strip_jsonc_comments(raw))
    except Exception as e:
        logger.debug("opencode.jsonc ilegível: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _jsonc_root_span(raw: str) -> tuple[int | None, int | None]:
    """Retorna (índice do '{' raiz, índice do '}' raiz) do objeto JSONC, ou (None, None)."""
    depth = 0
    i = 0
    n = len(raw)
    open_idx = None
    while i < n:
        c = raw[i]
        if c == '"':
            i = _skip_jsonc_string(raw, i)
            continue
        if c == "{":
            if depth == 0:
                open_idx = i
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return open_idx, i
            i += 1
        elif c == "/" and i + 1 < n and raw[i + 1] == "/":
            j = raw.find("\n", i)
            i = j + 1 if j != -1 else n
        elif c == "/" and i + 1 < n and raw[i + 1] == "*":
            j = raw.find("*/", i + 2)
            i = j + 2 if j != -1 else n
        else:
            i += 1
    return None, None


def _jsonc_last_content(raw: str, start: int, end: int) -> int:
    """Índice do último caractere que não é espaço/comentário em [start, end), ou -1."""
    i = start
    last = -1
    n = min(len(raw), end)
    while i < n:
        c = raw[i]
        if c == '"':
            j = _skip_jsonc_string(raw, i)
            last = j - 1
            i = j
        elif c == "/" and i + 1 < n and raw[i + 1] == "/":
            j = raw.find("\n", i)
            i = j + 1 if j != -1 else n
        elif c == "/" and i + 1 < n and raw[i + 1] == "*":
            j = raw.find("*/", i + 2)
            i = j + 2 if j != -1 else n
        elif c in " \t\r\n":
            i += 1
        else:
            last = i
            i += 1
    return last


def _jsonc_value_end(raw: str, start: int, bound: int) -> int:
    """Índice logo após o valor JSON que começa em `start` (dentro de [start, bound))."""
    n = min(len(raw), bound)
    depth = 0
    i = start
    while i < n:
        c = raw[i]
        if c == '"':
            j = _skip_jsonc_string(raw, i)
            if depth == 0:
                return j
            i = j
        elif c in " \t\r\n" or (c == "/" and i + 1 < n and raw[i + 1] in ("/", "*")):
            i = _skip_jsonc_ws(raw, i, n)
        elif c in "{[":
            depth += 1
            i += 1
        elif c in "}]":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        elif c in ",:":
            if depth == 0:
                return i
            i += 1
        else:
            i += 1
            while i < n and raw[i] not in ",{}[]\" \t\r\n" and not (
                raw[i] == "/" and i + 1 < n and raw[i + 1] in ("/", "*")
            ):
                i += 1
            if depth == 0:
                return i
    return n


def _jsonc_find_prop(raw: str, start: int, end: int, key: str) -> tuple[int, int, int] | None:
    """Retorna (início da chave, início do valor, fim do valor) da propriedade
    `key` no nível zero dentro de [start, end), ou None. Lida com aninhamento,
    strings e comentários."""
    i = start
    depth = 0
    n = min(len(raw), end)
    while i < n:
        c = raw[i]
        if c in " \t\r\n" or (c == "/" and i + 1 < n and raw[i + 1] in ("/", "*")):
            i = _skip_jsonc_ws(raw, i, n)
            continue
        if c == '"':
            if depth == 0:
                j = _skip_jsonc_string(raw, i)
                name = raw[i + 1:j - 1]
                k = _skip_jsonc_ws(raw, j, n)
                if name == key and k < n and raw[k] == ":":
                    vs = _skip_jsonc_ws(raw, k + 1, n)
                    ve = _jsonc_value_end(raw, vs, n)
                    return i, vs, ve
                i = j
            else:
                i = _skip_jsonc_string(raw, i)
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        i += 1
    return None


def _jsonc_parseable(text: str) -> bool:
    try:
        data = json.loads(_strip_jsonc_comments(text))
        return isinstance(data, dict)
    except Exception:
        return False


def _jsonc_upsert_mcp(raw: str, name: str, cfg_json: str) -> str | None:
    """Edita opencode.jsonc cirurgicamente (preservando comentários) para
    adicionar/atualizar mcp.<name>. Retorna o texto novo, ou None se não
    conseguir editar com segurança."""
    open_i, close_i = _jsonc_root_span(raw)
    if open_i is None:
        return None
    mcp = _jsonc_find_prop(raw, open_i + 1, close_i, "mcp")
    name_json = f'"{name}": {cfg_json}'
    if mcp is None:
        last = _jsonc_last_content(raw, open_i + 1, close_i)
        ins = f'\n  "mcp": {{\n    {name_json}\n  }}' if last < 0 else (
            f"{'' if raw[last] == ',' else ','}\n  \"mcp\": {{\n    {name_json}\n  }}"
        )
        return raw[:close_i] + ins + raw[close_i:]
    _, vs, ve = mcp
    if raw[vs] != "{":
        return None
    entry = _jsonc_find_prop(raw, vs + 1, ve - 1, name)
    if entry:
        _, evs, eve = entry
        return raw[:evs] + cfg_json + raw[eve:]
    last = _jsonc_last_content(raw, vs + 1, ve - 1)
    if last < 0:
        ins = f"\n    {name_json}\n  "
    else:
        ins = f"{'' if raw[last] == ',' else ','}\n    {name_json}\n  "
    return raw[:ve - 1] + ins + raw[ve - 1:]


def _jsonc_remove_mcp(raw: str, name: str) -> str | None:
    """Remova cirurgicamente mcp.<name>, preservando comentários. None se falhar."""
    open_i, close_i = _jsonc_root_span(raw)
    if open_i is None:
        return None
    mcp = _jsonc_find_prop(raw, open_i + 1, close_i, "mcp")
    if mcp is None:
        return None
    _, vs, ve = mcp
    entry = _jsonc_find_prop(raw, vs + 1, ve - 1, name)
    if entry is None:
        return None
    ps, _, ee = entry
    prev = _jsonc_last_content(raw, vs + 1, ps)
    if prev >= 0 and raw[prev] == ",":
        return raw[:prev] + raw[ee:]
    k = _skip_jsonc_ws(raw, ee, ve - 1)
    if k < ve - 1 and raw[k] == ",":
        return raw[:ps] + raw[k + 1:]
    return raw[:ps] + raw[ee:]


async def _mcp_set_server(name: str, url: str, headers: dict | None = None) -> str:
    """Adds/updates an MCP server entry in opencode.jsonc.

    opencode.jsonc is JSONC (comments allowed), and Python's json module does
    not round-trip comments — so when the file has comments we edit it
    surgically, in place, preserving everything else. Files without comments
    are rewritten with json.dumps. Either way we keep a .bak of whatever we
    overwrite and we restrict file permissions to the owner (tokens are plain
    text in there).
    """
    p = _mcp_file()
    raw = p.read_text() if p.exists() else ""
    data = _jsonc_load(raw)
    mcp = (data or {}).get("mcp") or {}
    cfg = dict(mcp.get(name) or {})
    cfg["type"] = "remote"
    if url:
        cfg["url"] = url
    cfg["enabled"] = True
    if headers is not None:
        merged = dict(cfg.get("headers") or {})
        merged.update(headers)
        cfg["headers"] = merged
    if not cfg.get("url"):
        return "\u274c sem URL \u2014 passe `--url <url>`"
    cfg_json = json.dumps(cfg, indent=2, ensure_ascii=False)
    try:
        if not raw.strip():
            as_text = json.dumps({"mcp": {name: cfg}}, indent=2, ensure_ascii=False) + "\n"
        elif _has_jsonc_comments(raw):
            edited = _jsonc_upsert_mcp(raw, name, cfg_json)
            if edited is None or not _jsonc_parseable(edited):
                return (
                    "\u26a0\ufe0f `opencode.jsonc` tem coment\u00e1rios e n\u00e3o consegui "
                    "edit\u00e1-lo com seguran\u00e7a. Edite o arquivo manualmente."
                )
            as_text = edited
        else:
            data.setdefault("mcp", {})[name] = cfg
            as_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    except Exception as e:
        return f"\u274c falha ao preparar config: {e}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if raw and raw != as_text:
            bak = p.with_suffix(p.suffix + ".bak")
            bak.write_text(raw)
            os.chmod(bak, 0o600)
        p.write_text(as_text)
        os.chmod(p, 0o600)
    except Exception as e:
        return f"\u274c falha ao gravar config: {e}"
    try:
        r = await _client.post("/mcp", json={"name": name, "config": cfg})
        r.raise_for_status()
        return f"\u2705 `{name}` configurado (arquivo + servidor em execu\u00e7\u00e3o)."
    except Exception as e:
        return f"\u26a0\ufe0f Config salva no arquivo, mas o servidor n\u00e3o aplicou: {e}"


async def _mcp_remove_server(name: str, raw: str) -> str:
    """Removes an MCP server entry from opencode.jsonc (preserving comments)
    and asks the opencode server to drop it too."""
    if not raw.strip():
        return f"\u274c `{name}` n\u00e3o est\u00e1 configurado no arquivo."
    data = _jsonc_load(raw)
    if name not in ((data or {}).get("mcp") or {}):
        return f"\u274c `{name}` n\u00e3o est\u00e1 configurado."
    try:
        if _has_jsonc_comments(raw):
            edited = _jsonc_remove_mcp(raw, name)
            if edited is None or not _jsonc_parseable(edited):
                return (
                    "\u26a0\ufe0f `opencode.jsonc` tem coment\u00e1rios e n\u00e3o consegui "
                    "edit\u00e1-lo com seguran\u00e7a. Edite o arquivo manualmente."
                )
            as_text = edited
        else:
            mcp = data["mcp"]
            mcp.pop(name, None)
            if not mcp:
                data.pop("mcp", None)
            as_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        p = _mcp_file()
        if raw != as_text:
            bak = p.with_suffix(p.suffix + ".bak")
            bak.write_text(raw)
            os.chmod(bak, 0o600)
        p.write_text(as_text)
        os.chmod(p, 0o600)
    except Exception as e:
        return f"\u274c falha ao remover do arquivo: {e}"
    try:
        r = await _client.request("DELETE", f"/mcp/{name}")
        r.raise_for_status()
        return f"\u2705 `{name}` removido."
    except Exception as e:
        return f"\u26a0\ufe0f Removido do arquivo, mas o servidor n\u00e3o confirmou: {e}"


async def oc_answer_permission(sid: str, perm_id: str, response: str):
    await _client.post(
        f"/session/{sid}/permissions/{perm_id}",
        json={"response": response},
    )


async def oc_answer_question(request_id: str, answers: list) -> bool:
    try:
        r = await _client.post(
            f"/question/{request_id}/reply",
            json={"answers": answers},
        )
        return r.status_code < 400
    except Exception as e:
        logger.warning("Falha ao responder pergunta %s: %s", request_id, e)
        return False


async def oc_reject_question(request_id: str) -> bool:
    try:
        r = await _client.post(f"/question/{request_id}/reject")
        return r.status_code < 400
    except Exception as e:
        logger.warning("Falha ao rejeitar pergunta %s: %s", request_id, e)
        return False


async def oc_abort(sid: str):
    try:
        await _client.post(f"/session/{sid}/abort")
    except Exception as e:
        logger.debug("Falha ao abortar sessão %s: %s", (sid or "")[-6:], e)


async def _kill_all_turns(chat_id: int | None = None):
    for cid in [chat_id] if chat_id else list(TURNS.keys()):
        turn = TURNS.pop(cid, None)
        if turn and turn.get("sid"):
            try:
                await oc_abort(turn["sid"])
            except Exception as e:
                logger.debug("Falha ao abortar turno %s: %s", cid, e)


def _find_turn(sid: str) -> dict | None:
    for t in TURNS.values():
        if t.get("sid") == sid:
            return t
    return None


# ---------------------------------------------------------------- rendering

TOOL_ICONS = {
    "read": "\U0001f4d6",     # \U0001f4d6 leu
    "write": "\u2795",        # \u2795 criou/escreveu
    "edit": "\u270f\ufe0f",   # \u270f\ufe0f editou
    "bash": "\u26a1",         # \u26a1 rodou comando
    "glob": "\U0001f50d",     # \U0001f50d buscou arquivos
    "grep": "\U0001f50d",
    "mcp": "\U0001f916",      # \U0001f916 mcp
    "todo": "\U0001f4cb",     # \U0001f4cb
}


def _fmt_path(path: str | None) -> str:
    if not path:
        return "(?)"
    try:
        return str(Path(path).relative_to(Path(OPENCODE_DIR)))
    except Exception:
        return path


def _perm_desc(p: dict) -> str:
    pat = p.get("pattern")
    pat_str = ", ".join(pat) if isinstance(pat, list) else (pat or "")
    title = p.get("title") or p.get("permission") or "permiss\u00e3o"
    if pat_str:
        return f"{title}: `{pat_str}`"
    return f"`{title}`"


def _code(s: str) -> str:
    # Kept as a plain statement (not a nested-quote f-string) so this runs on
    # Python < 3.12 too -- PEP 701 (same-quote nesting in f-strings) is 3.12+.
    escaped = (s or "").replace("`", "'")
    return "`" + escaped + "`"


# ---- inline buttons ---------------------------------------------------

def _btn_help() -> InlineKeyboardButton:
    return InlineKeyboardButton("Ajuda", callback_data="/help")


def _btn_new() -> InlineKeyboardButton:
    return InlineKeyboardButton("Nova conversa", callback_data="/new")


def _btn_status() -> InlineKeyboardButton:
    return InlineKeyboardButton("Status", callback_data="/status")


def _btn_cancel() -> InlineKeyboardButton:
    return InlineKeyboardButton("Cancelar", callback_data="/cancel")


def _kb_quick() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn_help(), _btn_new(), _btn_status()]])


def _kb_after_turn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn_new(), _btn_help(), _btn_status()]])


def _kb_status(busy_turns: int) -> InlineKeyboardMarkup:
    row1 = [_btn_new(), _btn_help()]
    row2 = [InlineKeyboardButton("Mudar modelo", callback_data="/models")]
    if busy_turns:
        row2.append(_btn_cancel())
    return InlineKeyboardMarkup([row1, row2])


def _kb_restart() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f916 S\u00f3 o bot", callback_data="__restart:bot"),
         InlineKeyboardButton("\U0001f5a5\ufe0f S\u00f3 o servidor", callback_data="__restart:server")],
        [InlineKeyboardButton("\U0001f501 Bot + servidor", callback_data="__restart:both")],
        [InlineKeyboardButton("\u274c Cancelar", callback_data="/status")],
    ])


_RESTART_ALIASES = {
    "bot": "bot",
    "server": "server", "servidor": "server", "srv": "server",
    "both": "both", "ambos": "both", "tudo": "both", "all": "both",
}

_RESTART_LABELS = {"bot": "o bot", "server": "o servidor", "both": "o bot + o servidor"}


def _models_kb(models: list[str]) -> list[list[InlineKeyboardButton]]:
    """Converte a lista de modelos do CLI em botões (até 2 por linha).
    O callback leva o spec `providerID/modelID`; fica bem abaixo do limite
    de 64 bytes do Telegram."""
    rows: list[list[InlineKeyboardButton]] = []
    pending: list[InlineKeyboardButton] = []
    for spec in models:
        pending.append(InlineKeyboardButton(spec, callback_data=f"mod:{spec}"))
        if len(pending) == 2:
            rows.append(pending)
            pending = []
    if pending:
        rows.append(pending)
    return rows


# ---- secret redaction -------------------------------------------------
# Anything the agent runs (grep/curl/etc) gets echoed into Telegram, which
# means a raw API key or token found on disk ends up permanently in the
# chat's message history. This is a best-effort mask, not a guarantee --
# it catches the common shapes (key=..., Bearer ..., known provider prefixes)
# but a determined secret in an unusual format can still slip through. Treat
# the Telegram history as sensitive regardless.
#
# Bare hex blobs used to be masked blindly, which false-positived on any
# SHA/MD5 hash, git commit, or hyphenless UUID. Now we only mask a bare hex
# blob when a secret-ish keyword appears nearby (e.g. `key=`, `token`, ...),
# so unrelated hashes stay readable.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b((?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|password|passwd|token)\s*[:=]\s*['\"]?)([A-Za-z0-9\-_\.]{6,})"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-_\.]{10,})"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),             # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),   # GitHub tokens
    re.compile(r"\bsk-[A-Za-z0-9\-]{20,}\b"),       # OpenAI/Anthropic-style keys
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),  # Slack tokens
]
_MASKED = "\u2588\u2588\u2588MASKED\u2588\u2588\u2588"
_SECRET_HEX = re.compile(r"\b[0-9A-Fa-f]{32,64}\b")
_SECRET_KW = re.compile(r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|secret|passwd|password|steam|token)", re.I)


def _mask_hex_with_context(s: str) -> str:
    """Mascara hex 32–64 só quando perto de uma palavra-chave de segredo,
    deixando hashes/commits/UUIDs comuns legíveis."""
    def _sub(m):
        window = s[max(0, m.start() - 60):m.start()]
        return _MASKED if _SECRET_KW.search(window) else m.group(0)
    return _SECRET_HEX.sub(_sub, s)


def _redact_secrets(s: str) -> str:
    if not s:
        return s
    out = s
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(1) + _MASKED, out)
        else:
            out = pat.sub(_MASKED, out)
    return _mask_hex_with_context(out)


def _tail_out(s: str, n: int = 450) -> str:
    s = _redact_secrets((s or "").strip().replace("```", "'''"))
    if not s:
        return ""
    if len(s) > n:
        return "\u2026" + s[-n:]
    return s


def _btn_label(s: str, n: int = 40) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _q_lines(turn: dict) -> list[str]:
    lines: list[str] = []
    first = True
    for q in turn["questions"]:
        sel = set(turn["qsel"].get((q["request_id"], q["qidx"])) or ())
        if first:
            lines += ["", "\u2753 *Escolha do opencode:*"]
            first = False
        if q.get("header"):
            lines.append(f"*\u2014 {q['header']} \u2014*")
        lines.append(q["question"] or "Selecione uma op\u00e7\u00e3o:")
        for i, opt in enumerate(q["options"]):
            mark = "\u2705" if i in sel else f"{i + 1}."
            line = f"{mark} {opt.get('label')}"
            if opt.get("description"):
                line += f" \u2014 {opt['description']}"
            lines.append(line)
        if q.get("multiple"):
            lines.append("_Toque para alternar e envie para confirmar._")
    return lines


def _q_kb(turn: dict) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for q in turn["questions"]:
        rid, qi = q["request_id"], q["qidx"]
        sel = set(turn["qsel"].get((rid, qi)) or ())
        if q.get("multiple"):
            for i, opt in enumerate(q["options"]):
                prefix = "\u2705 " if i in sel else ""
                rows.append([InlineKeyboardButton(prefix + _btn_label(opt.get("label")), callback_data=f"qt:{rid}:{qi}:{i}")])
            rows.append([
                InlineKeyboardButton("\u2705 Enviar", callback_data=f"qs:{rid}"),
                InlineKeyboardButton("\u274c Rejeitar", callback_data=f"qr:{rid}"),
            ])
        else:
            for i, opt in enumerate(q["options"]):
                rows.append([InlineKeyboardButton(_btn_label(opt.get("label")), callback_data=f"qo:{rid}:{qi}:{i}")])
            row: list[InlineKeyboardButton] = []
            if q.get("custom"):
                row.append(InlineKeyboardButton("\u270f\ufe0f Digitar resposta", callback_data=f"qc:{rid}:{qi}"))
            row.append(InlineKeyboardButton("\u274c Rejeitar", callback_data=f"qr:{rid}"))
            rows.append(row)
    if turn["perm_queue"]:
        p = turn["perm_queue"][0]
        rows.append([
            # Note: only the permission id goes in callback_data. turn["sid"]
            # is already known server-side -- putting it here too used to push
            # this well past Telegram's 64-byte callback_data limit, which
            # made Telegram silently reject the button and the whole permission
            # prompt would never render (looked like the bot just hung).
            InlineKeyboardButton("\u2705 Uma vez", callback_data=f"perm:{p['id']}:once"),
            InlineKeyboardButton("\U0001f501 Sempre", callback_data=f"perm:{p['id']}:always"),
            InlineKeyboardButton("\u274c Negar", callback_data=f"perm:{p['id']}:reject"),
        ])
    return rows


def _render_running(turn: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    lines = []
    if turn["todo"]:
        lines.append(f"\U0001f4cb *Plano:* {turn['todo']} passo{'s' if turn['todo'] != 1 else ''}")
    has_prompt = bool(turn["questions"]) or bool(turn["perm_queue"])
    if turn["questions"]:
        lines += _q_lines(turn)
    if turn["perm_queue"]:
        p = turn["perm_queue"][0]
        lines += ["", "\U0001f512 *Permiss\u00e3o pedida:*", _perm_desc(p)]
    if not has_prompt:
        curr = turn["current"]
        if curr and curr.get("cmd"):
            lines += ["", f"\u26a1 *rodando:* {_code(_redact_secrets(curr['cmd']))}"]
            out = _tail_out(curr.get("out"), 450)
            if out:
                lines += ["```", out, "```"]
        elif curr and curr.get("label"):
            lines += ["", curr["label"]]
        else:
            lines += ["", "\u23f3 *pensando\u2026*"]
    rows = _q_kb(turn)
    if not rows and not has_prompt:
        rows = [[_btn_cancel()]]
    kb = InlineKeyboardMarkup(rows) if rows else None
    return "\n".join(lines), kb


def _summary(turn: dict) -> list[str]:
    out = []
    if turn["reads"]:
        out.append("\U0001f4d6 *leu:* " + ", ".join(sorted(f"`{_fmt_path(x)}`" for x in turn["reads"])))
    if turn["writes"]:
        out.append("\u2795 *criou:* " + ", ".join(sorted(f"`{_fmt_path(x)}`" for x in turn["writes"])))
    if turn["edits"]:
        out.append("\u270f\ufe0f *editou:* " + ", ".join(sorted(f"`{_fmt_path(x)}`" for x in turn["edits"])))
    if turn["rejected"]:
        out.append(f"\u274c *negado:* {turn['rejected']} permiss\u00f5e(s)")
    return out


def _trace_lines(turn: dict, max_steps: int = 40, max_out: int = 500) -> list[str]:
    proc = turn["process"]
    if not proc:
        return []
    skipped = max(0, len(proc) - max_steps)
    show = proc[-max_steps:]
    lines = ["\U0001f4c4 *processo:*"]
    if skipped:
        lines.append(f"({skipped} passo{'s' if skipped != 1 else ''} anterior{'is' if skipped != 1 else ''} omitido{'s' if skipped != 1 else ''})")
    for i, step in enumerate(show, 1 + skipped):
        lines += ["", f"{i}) {_code(_redact_secrets('$ ' + step['cmd']))}"]
        out = _tail_out(step.get("out"), max_out)
        if out:
            lines += ["```", out, "```"]
        if step.get("status") == "error":
            lines.append("(\u274c erro)")
    return lines


def _render_think(turn: dict, elapsed: float) -> str:
    mins, secs = int(elapsed) // 60, int(elapsed) % 60
    lines = [f"\U0001f4ad *pensou em {mins}m{secs:02d}s:*"]
    summaries = _summary(turn)
    if summaries:
        lines += ["", "\u2014\u2014\u2014", *summaries]
    trace = _trace_lines(turn)
    if trace:
        lines += ["", *trace]
    total = "\n".join(lines)
    if len(total) > 3950:
        total = total[:3950] + "\n\u2026"
    return _expandable_html(_telegram_html(total, max_len=3950))


_MD_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+\-.]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITAL_A_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_MD_ITAL_U_RE = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+)$", re.MULTILINE)

_TAG_TOKEN_RE = re.compile(r"(</?[a-zA-Z][a-zA-Z0-9-]*(?:\s+[^<>]*?)?/?>)|([^<]+)", re.DOTALL)
_ENTITY_RE = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{1,31});")

_ALLOWED_TAGS = {
    "b": "b", "i": "i", "u": "u", "s": "s",
    "strong": "b", "em": "i", "ins": "u", "strike": "s", "del": "s",
    "code": "code", "pre": "pre", "blockquote": "blockquote",
    "p": "p", "br": "br", "a": "a", "tg-spoiler": "tg-spoiler",
}
_VOID_TAGS = {"br"}


def _escape_html_text(s: str) -> str:
    """Escapa & < > para entidades, preservando entidades já válidas."""
    out, i = [], 0
    for m in _ENTITY_RE.finditer(s):
        out.append(html.escape(s[i:m.start()], quote=False))
        out.append(m.group(0))
        i = m.end()
    out.append(html.escape(s[i:], quote=False))
    return "".join(out)


def _telegram_html(text: str, max_len: int | None = None) -> str:
    """Converte markdown + HTML do assistente em HTML seguro e balanceado
    para o parse_mode='HTML' do Telegram.

    Etapas:
      1) markdown -> HTML (fenced code, `código`, **negrito**, *itálico*,
         ~~riscado~~, [link](url) só http/https, cabeçalhos #..######);
      2) sanitização com whitelist de tags Telegram, normalização de nomes,
         descarte de atributos (excepto <a href> seguro), balanceamento de
         tags (fechando/descartando órfãs) e corte seguro em max_len.
    """
    def _fence(m):
        lang = m.group(1)
        cls = f' class="language-{lang}"' if lang else ""
        return f"<pre><code{cls}>{html.escape(m.group(2), quote=False)}</code></pre>"

    def _link(m):
        url = m.group(2).strip()
        if not re.match(r"^https?://", url, re.I):
            return m.group(0)
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(m.group(1), quote=False)}</a>'

    # 1) markdown -> html (sem escapar o texto inteiro antes)
    h = _MD_FENCE_RE.sub(_fence, text)
    h = _MD_INLINE_CODE_RE.sub(lambda m: f"<code>{html.escape(m.group(1), quote=False)}</code>", h)
    h = _MD_LINK_RE.sub(_link, h)
    h = _MD_STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", h)
    h = _MD_BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", h)
    h = _MD_ITAL_A_RE.sub(lambda m: f"<i>{m.group(1)}</i>", h)
    h = _MD_ITAL_U_RE.sub(lambda m: f"<i>{m.group(1)}</i>", h)
    h = _MD_HEADING_RE.sub(lambda m: f"<b>{m.group(2)}</b>", h)

    # 2) sanitizar + balancear + cortar
    out: list[str] = []
    stack: list[str] = []
    clipped = False
    for m in _TAG_TOKEN_RE.finditer(h):
        tag_part, text_part = m.group(1), m.group(2)
        if text_part is not None:
            rendered = _escape_html_text(text_part)
            if max_len is not None and len("".join(out)) + len(rendered) > max_len:
                out.append("\u2026")
                clipped = True
                break
            out.append(rendered)
            continue
        tm = re.match(r"</?([a-zA-Z][a-zA-Z0-9-]*)", tag_part)
        if not tm:
            out.append(_escape_html_text(tag_part))
            continue
        name = tm.group(1).lower()
        canon = _ALLOWED_TAGS.get(name)
        closing = tag_part.startswith("</")
        if canon is None:
            out.append(_escape_html_text(tag_part))
            continue
        if closing:
            if canon in stack:
                while stack and stack[-1] != canon:
                    out.append(f"</{stack.pop()}>")
                if stack:
                    stack.pop()
                out.append(f"</{canon}>")
            else:
                out.append(_escape_html_text(tag_part))
            continue
        rendered_tag = f"<{canon}>"
        if name == "a":
            href = re.search(r'\bhref\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag_part, re.I)
            url = (href.group(2) or href.group(3) or href.group(4)) if href else ""
            if not re.match(r"^https?://", url or "", re.I):
                out.append(_escape_html_text(tag_part))
                continue
            rendered_tag = f'<a href="{html.escape(url, quote=True)}">'
        if max_len is not None and len("".join(out)) + len(rendered_tag) > max_len:
            out.append("\u2026")
            clipped = True
            break
        out.append(rendered_tag)
        if canon not in _VOID_TAGS:
            stack.append(canon)

    for canon in reversed(stack):
        out.append(f"</{canon}>")
    result = "".join(out)
    if clipped and max_len is not None and len(result) > max_len:
        cut = result.rfind("<", 0, max_len)
        if cut > max_len - 64:
            result = result[:cut] + "\u2026"
        else:
            result = result[:max_len].rstrip() + "\u2026"
    return result


def _plain_text(html_text: str) -> str:
    """Remove tags e desfaz entidades: texto limpo, sem HTML cru."""
    if not html_text:
        return ""
    cleaned = html.unescape(re.sub(r"<[^>]*>", "", html_text))
    return re.sub(r"</?[a-zA-Z][^>]*>", "", cleaned)


def _expandable_html(html_text: str) -> str:
    return f"<blockquote expandable>{html_text}</blockquote>"


def _result_text(turn: dict) -> tuple[str, str]:
    body = (turn["out_text"] or "").strip()
    if not body:
        return "escrevendo\u2026", ""
    if len(body) > 3800:
        body = "\u2026" + body[-3800:]
    return _telegram_html(body, max_len=3800), ""


# ---------------------------------------------------------------- turn plumbing

async def _push_status(turn: dict, force: bool = False):
    app = _app_ref
    now = time.monotonic()
    if not force and now - turn["last_edit"] < 1.0:
        return
    turn["last_edit"] = now
    if turn.get("done"):
        text = _render_think(turn, turn.get("elapsed", 0.0))
        kb = None
        parse_mode = "HTML"
    else:
        text, kb = _render_running(turn)
        parse_mode = "Markdown"
    try:
        await app.bot.edit_message_text(
            chat_id=turn["chat_id"],
            message_id=turn["status_msg_id"],
            text=text,
            parse_mode=parse_mode,
            reply_markup=kb,
        )
    except TelegramError as e1:
        try:
            await app.bot.edit_message_text(
                chat_id=turn["chat_id"],
                message_id=turn["status_msg_id"],
                text=_plain_text(text),
                reply_markup=kb,
            )
        except TelegramError as e2:
            # Both attempts failed with the same reply_markup -- if kb itself
            # is the problem (e.g. a callback_data over Telegram's 64-byte
            # limit), retrying with it again would just fail identically and
            # the status message would silently stop updating, which looks
            # to the user like the bot hung. Drop the buttons and show the
            # text anyway so the state is at least visible.
            logger.warning("Falha ao editar status (com botões): %s / %s", e1, e2)
            if kb is not None:
                try:
                    await app.bot.edit_message_text(
                        chat_id=turn["chat_id"],
                        message_id=turn["status_msg_id"],
                        text=text + "\n\n_(bot\u00f5es indispon\u00edveis nesta atualiza\u00e7\u00e3o)_",
                    )
                except TelegramError:
                    pass


def _start_typing(turn: dict):
    async def loop():
        while turn["busy"]:
            try:
                await _app_ref.bot.send_chat_action(chat_id=turn["chat_id"], action="typing")
            except Exception as e:
                logger.debug("Falha ao enviar indicador de digitação: %s", e)
            await asyncio.sleep(4)

    turn["typing_task"] = asyncio.create_task(loop())


def _stop_typing(turn: dict):
    task = turn.pop("typing_task", None)
    if task:
        task.cancel()


STREAM_MIN = 150


async def _update_result(turn: dict, force: bool = False):
    """Updates the result balloon, creating it lazily once there's enough text."""
    text, _ = _result_text(turn)
    if text == turn.get("result_last"):
        return
    if turn["result_msg_id"] is None:
        if not force and not turn["done"] and len(text.strip()) < STREAM_MIN:
            return
        msg = await _app_ref.bot.send_message(turn["chat_id"], text, parse_mode="HTML")
        turn["result_msg_id"] = msg.message_id
    else:
        try:
            await _app_ref.bot.edit_message_text(
                chat_id=turn["chat_id"], message_id=turn["result_msg_id"], text=text, parse_mode="HTML"
            )
        except TelegramError:
            try:
                await _app_ref.bot.edit_message_text(
                    chat_id=turn["chat_id"], message_id=turn["result_msg_id"],
                    text=_plain_text(text),
                )
            except TelegramError:
                pass
    turn["result_last"] = text


async def _flush_stream(turn: dict, force: bool = False):
    """Feeds the tail of out_text into the result balloon as it grows."""
    text = turn["out_text"]
    tail = text[turn["streamed_len"]:]
    if not tail and not force:
        return
    if turn["result_msg_id"] is None and not turn["done"] and len(tail.strip()) < STREAM_MIN:
        return
    await _update_result(turn, force=force)
    turn["streamed_len"] = len(text)


async def _stream_loop(turn: dict):
    try:
        while turn["busy"]:
            await asyncio.sleep(0.8)
            await _flush_stream(turn)
    except asyncio.CancelledError:
        pass


def _stop_stream(turn: dict):
    task = turn.pop("stream_task", None)
    if task:
        task.cancel()


def _cmd_of_part(state: dict) -> str:
    inp = state.get("input") or {}
    cmd = inp.get("command")
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(c) for c in cmd)
    return str(cmd or "")


def _record_tool(turn: dict, part: dict):
    tool = part.get("tool")
    state = part.get("state", {})
    status = state.get("status")
    if status not in ("running", "completed", "error"):
        return
    part_id = part.get("id")
    path = (state.get("input") or {}).get("filePath")

    if tool in ("bash", "shell"):
        cmd = _cmd_of_part(state)
        if status == "running":
            turn["current"] = {"cmd": cmd or "comando", "out": state.get("output") or ""}
            return
        if status in ("completed", "error"):
            if part_id:
                if part_id in turn["process_seen"]:
                    return
                turn["process_seen"].add(part_id)
            turn["process"].append({
                "cmd": cmd or "comando",
                "out": state.get("output") or "",
                "status": status,
            })
            turn["current"] = None
            if status == "error" and "permission" in (state.get("error") or "").lower():
                turn["rejected"] += 1
            return

    if tool == "read":
        if status == "running":
            turn["current"] = {"label": f"\U0001f4d6 lendo `{_fmt_path(path)}`" if path else "\U0001f4d6 lendo arquivo", "out": ""}
        elif status in ("completed", "error") and path:
            turn["reads"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    elif tool == "write":
        if status == "running":
            turn["current"] = {"label": f"\u2795 criando `{_fmt_path(path)}`" if path else "\u2795 criando arquivo", "out": ""}
        elif status in ("completed", "error") and path:
            turn["writes"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    elif tool == "edit":
        if status == "running":
            turn["current"] = {"label": f"\u270f\ufe0f editando `{_fmt_path(path)}`" if path else "\u270f\ufe0f editando arquivo", "out": ""}
        elif status in ("completed", "error") and path:
            turn["edits"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    else:
        title = state.get("title") or ""
        label = f"{TOOL_ICONS.get(tool, '\u2699\ufe0f')} {tool}" + (f": `{title}`" if title else "")
        if status == "error" and "permission" in (state.get("error") or "").lower():
            turn["rejected"] += 1
            label = "\u274c permiss\u00e3o negada"
        turn["current"] = {"label": label, "out": ""} if status == "running" else None


def _new_turn_skeleton(chat_id: int) -> dict:
    """A turn dict with everything needed to occupy TURNS[chat_id] synchronously,
    before any `await` happens, so a second incoming message can't race past the
    'is a turn already running' check."""
    return {
        "chat_id": chat_id,
        "sid": None,
        "status_msg_id": None,
        "result_msg_id": None,
        "result_last": "",
        "started": time.monotonic(),
        "last_edit": 0.0,
        "busy": True,
        "done": False,
        "todo": 0,
        "reads": set(),
        "writes": set(),
        "edits": set(),
        "rejected": 0,
        "process": [],
        "process_seen": set(),
        "current": None,
        "out_text": "",
        "reasoning_part_ids": set(),
        "user_msg_id": None,
        "streamed_len": 0,
        "perm_queue": deque(),
        "questions": [],
        "qsel": {},
        "awaiting_custom": None,
        "typing_task": None,
        "stream_task": None,
    }


async def _start_turn(update_or_chat, text: str, parts: list | None = None):
    """Runs one opencode turn for a chat. update can be a message update."""
    chat_id = update_or_chat.effective_chat.id if hasattr(update_or_chat, "effective_chat") else update_or_chat

    if chat_id in TURNS:
        await update_or_chat.message.reply_text("\u23f3 Ainda estou processando a mensagem anterior\u2026")
        return

    # Reserve the slot for this chat *before* any await, closing the race where
    # two fast messages both see "no turn running" and both start one.
    turn = _new_turn_skeleton(chat_id)
    TURNS[chat_id] = turn

    try:
        data = _app_ref.bot_data.setdefault("chats", {})
        chat_cfg = data.setdefault(chat_id, {})
        sid = chat_cfg.get("sid")
        if not sid:
            sid = await oc_create_session()
            chat_cfg["sid"] = sid
        turn["sid"] = sid

        placeholder = await update_or_chat.message.reply_text("\U0001f4ad *opencode pensando\u2026*", parse_mode="Markdown")
        turn["status_msg_id"] = placeholder.message_id
        turn["started"] = time.monotonic()

        _start_typing(turn)
        turn["stream_task"] = asyncio.create_task(_stream_loop(turn))

        await oc_send_with_retry(chat_cfg, turn, text, model=chat_cfg.get("model"), agent=chat_cfg.get("agent"), parts=parts)
    except Exception as e:
        logger.exception("Falha ao iniciar turn")
        turn["out_text"] = f"\u274c Falha ao enviar para o opencode: {e}"
        if turn["status_msg_id"] is None:
            # We never even got the placeholder message out -- send one now so
            # _finish_turn has something to edit.
            try:
                placeholder = await update_or_chat.message.reply_text("\U0001f4ad ...")
                turn["status_msg_id"] = placeholder.message_id
            except Exception:
                pass
        await _finish_turn(chat_id)


async def _finish_turn(chat_id: int):
    turn = TURNS.pop(chat_id, None)
    if not turn:
        return
    turn["busy"] = False
    turn["done"] = True
    turn["elapsed"] = time.monotonic() - turn["started"]
    turn["current"] = None
    turn["questions"] = []
    turn["qsel"] = {}
    turn["awaiting_custom"] = None
    _stop_typing(turn)
    _stop_stream(turn)

    app = _app_ref
    # ---- inverted order: answer on top, think below ----
    answer = (turn["out_text"] or "").strip()
    if not answer:
        answer = "(sem resposta)"
    bodies = [_telegram_html(c, max_len=3500) for c in _split_text(answer, limit=3500)]
    first_body = bodies[0]

    # the answer takes over the top balloon (the "pensando…" placeholder)
    answer_msg_id = turn["status_msg_id"]
    if answer_msg_id is not None:
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id, message_id=answer_msg_id, text=first_body, parse_mode="HTML"
            )
        except TelegramError:
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id, message_id=answer_msg_id,
                    text=_plain_text(first_body),
                )
            except TelegramError:
                pass
    else:
        # unlikely fallback (placeholder never sent): answer as a new message
        try:
            msg = await app.bot.send_message(chat_id=chat_id, text=first_body, parse_mode="HTML")
        except TelegramError:
            try:
                msg = await app.bot.send_message(chat_id=chat_id, text=_plain_text(first_body))
            except TelegramError:
                msg = None
        answer_msg_id = msg.message_id if msg is not None else None

    last_msg_id = answer_msg_id
    for body in bodies[1:]:
        try:
            msg = await app.bot.send_message(chat_id=chat_id, text=body, parse_mode="HTML")
        except TelegramError:
            try:
                msg = await app.bot.send_message(chat_id=chat_id, text=_plain_text(body))
            except TelegramError:
                continue
        last_msg_id = msg.message_id

    # ---- think balloon (expandable blockquote) at the bottom ----
    # reuses the streaming balloon (already below) or sends a new one
    think_text = _render_think(turn, turn["elapsed"])
    think_msg_id = turn["result_msg_id"]
    if think_msg_id is not None and think_msg_id != answer_msg_id and len(bodies) > 1:
        # with extra chunks, drop the partial streaming balloon to keep the
        # order answer -> extras -> think
        try:
            await app.bot.delete_message(chat_id=chat_id, message_id=think_msg_id)
            think_msg_id = None
        except TelegramError:
            pass
    think_placed = False
    if think_msg_id is not None and think_msg_id != answer_msg_id:
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id, message_id=think_msg_id, text=think_text, parse_mode="HTML"
            )
            think_placed = True
        except TelegramError:
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id, message_id=think_msg_id, text=_plain_text(think_text)
                )
                think_placed = True
            except TelegramError:
                pass
    if not think_placed:
        try:
            await app.bot.send_message(chat_id=chat_id, text=think_text, parse_mode="HTML")
        except TelegramError:
            try:
                await app.bot.send_message(chat_id=chat_id, text=_plain_text(think_text))
            except TelegramError:
                pass

    # ---- bot\u00f5es de a\u00e7\u00e3o p\u00f3s-turno ----
    if last_msg_id is not None:
        try:
            await app.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=last_msg_id,
                reply_markup=_kb_after_turn(),
            )
        except TelegramError:
            pass


def _split_text(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for line in text.split("\n"):
        if buf and len(buf) + len(line) + 1 > limit:
            out.append(buf.rstrip())
            buf = line
        else:
            buf += line + "\n"
    if buf.rstrip():
        out.append(buf.rstrip())
    return out


# ---------------------------------------------------------------- event loop

async def _consume_events():
    global _stream_task
    while True:
        try:
            async with _client.stream("GET", "/event") as resp:
                async for raw in resp.aiter_lines():
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    await _dispatch(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Stream de eventos caiu: %s", e)
            await asyncio.sleep(3)


async def _dispatch(event: dict):
    et = event.get("type")
    props = event.get("properties") or {}
    sid = props.get("sessionID")
    turn = _find_turn(sid) if sid else None
    if not turn:
        return

    if et == "permission.asked":
        p = {"id": props.get("id"), "sid": sid, "permission": props.get("permission"), "title": props.get("title") or "", "pattern": props.get("pattern")}
        turn["perm_queue"].append(p)
        await _push_status(turn, force=True)
    elif et == "question.asked":
        rid = props.get("id") or ""
        questions = props.get("questions") or []
        qlen = len(questions)
        for idx, q in enumerate(questions):
            turn["questions"].append({
                "request_id": rid,
                "sid": sid,
                "qidx": idx,
                "qlen": qlen,
                "header": q.get("header") or "",
                "question": q.get("question") or "",
                "options": q.get("options") or [],
                "multiple": bool(q.get("multiple")),
                "custom": q.get("custom", True),
                "answer": None,
            })
        await _push_status(turn, force=True)
    elif et == "question.rejected":
        _drop_questions(turn, props.get("requestID") or props.get("id") or "")
        await _push_status(turn, force=True)
    elif et == "message.updated":
        info = props.get("info") or {}
        if info.get("role") == "user":
            turn["user_msg_id"] = info.get("id")
    elif et == "session.status":
        st = props.get("status", {})
        stype = st.get("type")
        if stype == "busy":
            turn["busy"] = True
            if not turn["typing_task"]:
                _start_typing(turn)
        elif stype == "idle":
            # Some opencode versions signal completion only via session.status
            # (idle) and never emit a separate session.idle event. Finishing
            # here too (finish is idempotent via TURNS.pop) avoids a turn that
            # never closes.
            await _finish_turn(turn["chat_id"])
    elif et == "session.idle":
        await _finish_turn(turn["chat_id"])
    elif et == "session.error":
        turn["out_text"] = f"\u274c *Erro no opencode:* {props.get('error') or props.get('message') or 'desconhecido'}"
        await _finish_turn(turn["chat_id"])
    elif et == "todo.updated":
        todos = props.get("todos") or []
        turn["todo"] = len([t for t in todos if t.get("status") not in ("cancelled", "completed")])
        await _push_status(turn)
    elif et == "message.part.updated":
        part = props.get("part") or {}
        ptype = part.get("type")
        if ptype == "tool":
            _record_tool(turn, part)
            await _push_status(turn)
        elif ptype == "reasoning":
            # Remember this part's id so the delta handler below can tell the
            # model's internal chain-of-thought apart from its real answer --
            # both can arrive as field="text" deltas, and without this the
            # reasoning gets appended straight into the final answer bubble.
            pid = part.get("id")
            if pid:
                turn["reasoning_part_ids"].add(pid)
    elif et == "message.part.delta":
        part_id = props.get("partID") or props.get("partId") or props.get("id")
        if part_id and part_id in turn["reasoning_part_ids"]:
            return
        if props.get("field") == "text" and props.get("messageID") != turn.get("user_msg_id"):
            turn["out_text"] += props.get("delta", "")
    elif et == "file.edited":
        f = props.get("file")
        if f:
            turn["edits"].add(f)
            await _push_status(turn)


def _drop_questions(turn: dict, request_id: str):
    rid = request_id or ""
    turn["questions"] = [q for q in turn["questions"] if q["request_id"] != rid]
    turn["qsel"] = {k: v for k, v in turn["qsel"].items() if k[0] != rid}


async def _submit_question(turn: dict, request_id: str) -> bool:
    items = sorted(
        (q for q in turn["questions"] if q["request_id"] == request_id),
        key=lambda q: q["qidx"],
    )
    if not items:
        return True
    qlen = max(q["qlen"] for q in items)
    answers: list = [None] * qlen
    for q in items:
        a = q.get("answer")
        if not a:
            return False
        answers[q["qidx"]] = list(a)
    if any(a is None for a in answers):
        return False
    ok = await oc_answer_question(request_id, answers)
    _drop_questions(turn, request_id)
    return ok


async def _refresh_after_question(turn: dict):
    if turn["questions"] or turn["perm_queue"]:
        await _push_status(turn, force=True)
    else:
        try:
            await _app_ref.bot.edit_message_reply_markup(
                chat_id=turn["chat_id"], message_id=turn["status_msg_id"], reply_markup=None
            )
        except TelegramError:
            pass
        await _push_status(turn, force=True)


# ---------------------------------------------------------------- media

_MEDIA_TYPES = ("document", "audio", "voice", "video", "animation")
_MEDIA_FALLBACK_MIME = {
    "document": "application/octet-stream",
    "audio": "audio/mpeg",
    "voice": "audio/ogg",
    "video": "video/mp4",
    "animation": "video/mp4",
}
_MEDIA_MAX_BYTES = 20 * 1024 * 1024


def _safe_filename(name: str, default: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[^\w.\-()+\[\] ]", "_", name)[:120].strip()
    return name or default


def _media_entries(update: Update) -> list[tuple[str, str, str]]:
    """Returns (file_id, filename, mime) for every attachment on the message."""
    msg = update.message or update.edited_message
    if not msg:
        return []
    out: list[tuple[str, str, str]] = []
    if msg.photo:
        photo = max(msg.photo, key=lambda p: p.file_size or 0)
        out.append((photo.file_id, "photo.jpg", "image/jpeg"))
    for name in _MEDIA_TYPES:
        f = getattr(msg, name, None)
        if not f:
            continue
        mime = getattr(f, "mime_type", None) or _MEDIA_FALLBACK_MIME[name]
        fname = _safe_filename(getattr(f, "file_name", "") or "", f"{name}.bin")
        out.append((f.file_id, fname, mime))
    return out


async def _media_to_file_parts(bot, entries: list[tuple[str, str, str]]) -> list[dict]:
    """Downloads each attachment and turns it into an opencode `file` part."""
    parts: list[dict] = []
    for file_id, filename, mime in entries:
        try:
            f = await bot.get_file(file_id)
        except Exception as e:
            logger.warning("Falha ao obter mídia %s: %s", file_id, e)
            parts.append({"type": "text", "text": f"[anexo não acessível: {filename}]"})
            continue
        if (f.file_size or 0) > _MEDIA_MAX_BYTES:
            parts.append({"type": "text", "text": f"[anexo ignorado ({(f.file_size or 0) // (1024 * 1024)} MiB, limite 20 MiB): {filename}]"})
            continue
        try:
            raw = await f.download_as_bytearray()
        except Exception as e:
            logger.warning("Falha ao baixar mídia %s: %s", file_id, e)
            parts.append({"type": "text", "text": f"[falha ao baixar anexo: {filename}]"})
            continue
        b64 = base64.b64encode(bytes(raw)).decode("ascii")
        parts.append({
            "type": "file",
            "url": f"data:{mime};base64,{b64}",
            "filename": filename,
            "mime": mime,
        })
    return parts


async def _consume_custom_answer(update: Update, text: str) -> bool:
    """Routes a plain-text message to a pending opencode question (custom
    answer). Returns True when the message was consumed by that flow."""
    if not text:
        return False
    chat_id = update.effective_chat.id
    turn = TURNS.get(chat_id)
    if not turn or not turn.get("awaiting_custom"):
        return False
    rid, qi = turn.pop("awaiting_custom")
    item = next((q for q in turn["questions"] if q["request_id"] == rid and q["qidx"] == qi), None)
    if not item:
        return False
    item["answer"] = [text]
    ok = await _submit_question(turn, rid)
    await update.message.reply_text(
        "\u2705 *Resposta enviada ao opencode.*" if ok else
        "\u274c *Falha ao enviar resposta ao opencode.*",
        parse_mode="Markdown",
    )
    await _refresh_after_question(turn)
    return True


# ---------------------------------------------------------------- handlers

async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    if await _consume_custom_answer(update, text):
        return
    await _start_turn(update, text)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    text = (update.message.caption or "").strip()
    if await _consume_custom_answer(update, text):
        return
    entries = _media_entries(update)
    if not entries and not text:
        await update.message.reply_text(
            "\u26a0\ufe0f Anexos suportados: foto, documento, \u00e1udio, voz, v\u00eddeo e GIF.\n"
            "Envie junto um texto ou legenda com a instru\u00e7\u00e3o.",
        )
        return
    parts = await _media_to_file_parts(context.bot, entries) if entries else None
    await _start_turn(update, text, parts=parts)


def _find_qitem(turn: dict, rid: str, qi: int | None = None) -> dict | None:
    for q in turn["questions"]:
        if q["request_id"] == rid and (qi is None or q["qidx"] == qi):
            return q
    return None


async def cb_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    chat_id = update.effective_chat.id
    turn = TURNS.get(chat_id)
    if not turn:
        await query.answer("Sess\u00e3o encerrada.", show_alert=True)
        return
    parts = query.data.split(":")
    op, rid = parts[0], parts[1]

    if op == "qo":
        _, _, qi_s, idx_s = parts
        qi, idx = int(qi_s), int(idx_s)
        item = _find_qitem(turn, rid, qi)
        if not item:
            await query.answer("Pergunta j\u00e1 respondida.", show_alert=True)
            return
        item["answer"] = [item["options"][idx]["label"]]
        ok = await _submit_question(turn, rid)
        await query.answer("\u2705 Enviado!" if ok else "\u274c Falha ao enviar.")
        await _refresh_after_question(turn)
    elif op == "qt":
        _, _, qi_s, idx_s = parts
        qi, idx = int(qi_s), int(idx_s)
        item = _find_qitem(turn, rid, qi)
        if not item:
            await query.answer("Pergunta j\u00e1 respondida.", show_alert=True)
            return
        sel = turn["qsel"].setdefault((rid, qi), set())
        if idx in sel:
            sel.discard(idx)
        else:
            sel.add(idx)
        await query.answer()
        await _push_status(turn, force=True)
    elif op == "qs":
        items = [q for q in turn["questions"] if q["request_id"] == rid]
        missing = False
        for q in items:
            sel = turn["qsel"].get((rid, q["qidx"])) or set()
            q["answer"] = [q["options"][i]["label"] for i in sorted(sel)] if sel else None
            if not q["answer"]:
                missing = True
        if missing:
            await query.answer("Selecione ao menos uma op\u00e7\u00e3o em cada pergunta.", show_alert=True)
            return
        ok = await _submit_question(turn, rid)
        await query.answer("\u2705 Enviado!" if ok else "\u274c Falha ao enviar.")
        await _refresh_after_question(turn)
    elif op == "qc":
        _, _, qi_s = parts
        qi = int(qi_s)
        item = _find_qitem(turn, rid, qi)
        if not item:
            await query.answer("Pergunta j\u00e1 respondida.", show_alert=True)
            return
        turn["awaiting_custom"] = (rid, qi)
        await query.answer("\u270f\ufe0f Digite sua resposta")
        await update.effective_chat.send_message(
            "\u270f\ufe0f *Digite sua resposta:* mande o texto agora.",
            parse_mode="Markdown",
        )
    elif op == "qr":
        ok = await oc_reject_question(rid)
        _drop_questions(turn, rid)
        await query.answer("\u274c Rejeitada." if ok else "\u274c Falha ao rejeitar.")
        await _refresh_after_question(turn)


async def cb_permission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    _, perm_id, response = query.data.split(":", 2)
    chat_id = update.effective_chat.id
    turn = TURNS.get(chat_id)
    if not turn:
        await query.answer("Sess\u00e3o encerrada.", show_alert=True)
        return
    if not turn["perm_queue"] or turn["perm_queue"][0]["id"] != perm_id:
        await query.answer("Permiss\u00e3o j\u00e1 respondida.", show_alert=True)
        return
    turn["perm_queue"].popleft()
    await oc_answer_permission(turn["sid"], perm_id, response)
    await query.answer()
    if turn["perm_queue"]:
        await _push_status(turn, force=True)
    else:
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=turn["status_msg_id"], reply_markup=None)
        except TelegramError:
            pass
        await _push_status(turn, force=True)


async def _btn_invoked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cria um 'update' com .message apontando para a mensagem do bot\u00e3o."""
    query = update.callback_query
    message = query.message

    class _Proxy:
        # Handlers usam update.message (reply_text/delete) e update.effective_*.
        # Em CallbackQuery updates o campo .message \u00e9 None, ent\u00e3o injetamos a
        # mensagem onde o bot\u00e3o foi clicado e repassamos todo o resto ao update real.
        def __init__(self):
            self.message = message

        def __getattr__(self, name):
            return getattr(update, name)

    return _Proxy()


async def cb_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manipula bot\u00f5es de sele\u00e7\u00e3o de modelo (`mod:`)."""
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    data = query.data or ""
    if not data.startswith("mod:"):
        await query.answer()
        return
    await query.answer()
    spec = data[4:]
    if "/" not in spec:
        return
    providerID, modelID = spec.rsplit("/", 1)
    chat_cfg = context.bot_data.setdefault("chats", {}).setdefault(update.effective_chat.id, {})
    chat_cfg["model"] = {"providerID": providerID, "modelID": modelID}
    try:
        await query.message.edit_text(
            f"*Modelo atual:* `{spec}`\n\n\u2705 Modelo definido!",
            parse_mode="Markdown",
        )
    except TelegramError:
        await query.message.reply_text(f"\u2705 Modelo definido: `{spec}`", parse_mode="Markdown")


async def cb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Roteia os bot\u00f5es inline para os comandos correspondentes."""
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    data = query.data or ""
    await query.answer()

    if data == "__restart_confirm" or data.startswith("__restart:"):
        chat_id = update.effective_chat.id
        # teclado antigo (s\u00f3 "Confirmar restart") equivale a tudo
        target = data.split(":", 1)[1] if ":" in data else "both"
        if target not in ("bot", "server", "both"):
            return
        await _kill_all_turns()
        try:
            await query.message.edit_text(
                f"\U0001f501 *Reiniciando {_RESTART_LABELS[target]}...*",
                parse_mode="Markdown",
            )
        except TelegramError:
            await query.message.reply_text(
                f"\U0001f501 *Reiniciando {_RESTART_LABELS[target]}...*",
                parse_mode="Markdown",
            )
        await _perform_restart(chat_id, target)
        return

    cmd = data.split(" ", 1)[0]
    if not cmd.startswith("/"):
        return
    handler = {
        "/help": cmd_help,
        "/new": cmd_new,
        "/cancel": cmd_cancel,
        "/status": cmd_status,
        "/models": cmd_models,
        "/agents": cmd_agents,
        "/sessions": cmd_sessions,
        "/mcp": cmd_mcp,
        "/version": cmd_version,
        "/stats": cmd_stats,
    }.get(cmd)
    if not handler:
        return
    injected = await _btn_invoked(update, context)
    saved = context.args
    if cmd == "/mcp":
        context.args = [data.split(" ", 1)[1] if " " in data else "list"]
    else:
        context.args = []
    try:
        await handler(injected, context)
    finally:
        context.args = saved


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    await update.message.reply_text(
        "\U0001f916 *opencode no Telegram*\n\n"
        "Envie qualquer mensagem e eu respondo com o opencode.\n\n"
        "Toque nos bot\u00f5es abaixo para navegar:",
        parse_mode="Markdown",
        reply_markup=_kb_quick(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    await update.message.reply_text(
        "*Comandos do opencode:*\n\n"
        "Envie fotos, \u00e1udios, v\u00eddeos e arquivos junto com um texto (\u00e0s vezes a legenda).\n\n"
        "  /models \u2014 lista modelos; `/models opencode/xx` define\n"
        "  /agents \u2014 lista agentes; `/agents <nome>` define\n"
        "  /sessions \u2014 lista sess\u00f5es; `/sessions <id>` retoma\n"
        "  /new \u2014 nova conversa (alias /novo)\n"
        "  /cancel \u2014 interrompe (alias /cancelar)\n"
        "  /summarize \u2014 dispara o resumo da sess\u00e3o\n"
        "  /stats \u2014 uso e custo do opencode\n"
        "  /mcp \u2014 gerencia servidores MCP (list/add/token/remove/auth/logout)\n"
        "  /version \u2014 vers\u00e3o instalada\n"
        "  /status \u2014 estado do servidor\n"
        "  /restart [bot|servidor|ambos] \u2014 reinicia o bot, o servidor ou os dois (respostas em andamento s\u00e3o interrompidas)\n"
        "  /help \u2014 esta ajuda (alias /ajuda)",
        parse_mode="Markdown",
        reply_markup=_kb_quick(),
    )


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    chat_id = update.effective_chat.id
    chat_cfg = context.bot_data.setdefault("chats", {}).setdefault(chat_id, {})
    if context.args:
        spec = context.args[0]
        if "/" not in spec:
            await update.message.reply_text("Formato: `/models opencode/nome`", parse_mode="Markdown")
            return
        providerID, modelID = spec.rsplit("/", 1)
        chat_cfg["model"] = {"providerID": providerID, "modelID": modelID}
        await update.message.reply_text(f"\u2705 Modelo definido: `{providerID}/{modelID}`", parse_mode="Markdown")
        return
    out = await _run_cli("models", timeout=30)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    models = [l for l in lines if "/" in l]
    cur = chat_cfg.get("model")
    head = f"*Modelo atual:* `{(cur['providerID'] + '/' + cur['modelID']) if cur else '\u00e0 definir'}`\n\nEscolha o modelo:"
    if not models:
        await update.message.reply_text("Nenhum modelo listado pelo CLI.", parse_mode="Markdown")
        return
    kb = InlineKeyboardMarkup(_models_kb(models))
    await update.message.reply_text(head, parse_mode="Markdown", reply_markup=kb)


async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    chat_id = update.effective_chat.id
    chat_cfg = context.bot_data.setdefault("chats", {}).setdefault(chat_id, {})
    if context.args:
        name = context.args[0]
        chat_cfg["agent"] = name
        await update.message.reply_text(f"\u2705 Agente definido: `{name}`", parse_mode="Markdown")
        return
    out = await _run_cli("agent", "list", timeout=30)
    names = []
    for line in out.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:\(.*\))?\s*$", line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    cur = chat_cfg.get("agent")
    text = f"*Agente atual:* `{cur or '\u00e0 definir'}`\n\n"
    text += "Dispon\u00edveis:\n" + "\n".join(f"`{n}`" for n in names) if names else "\u274c nenhum listado"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    chat_id = update.effective_chat.id
    chat_cfg = context.bot_data.setdefault("chats", {}).setdefault(chat_id, {})
    if context.args:
        target = context.args[0]
        try:
            all_sessions = await _client.get("/session")
            found = None
            for s in all_sessions.json():
                if s.get("id") == target or s.get("slug") == target or s.get("id", "").endswith(target):
                    found = s
                    break
        except Exception:
            found = None
        if not found:
            await update.message.reply_text("\u274c Sess\u00e3o n\u00e3o encontrada.")
            return
        chat_cfg["sid"] = found["id"]
        await update.message.reply_text(f"\U0001f501 Conversa retomada: *{found.get('title') or found.get('slug')}* (`{found['id'][-6:]}`)", parse_mode="Markdown")
        return
    try:
        all_sessions = await _client.get("/session")
        sessions = sorted(all_sessions.json(), key=lambda s: s.get("time", {}).get("updated", 0), reverse=True)[:10]
    except Exception as e:
        await update.message.reply_text(f"\u274c Falha ao listar sess\u00f5es: {e}")
        return
    if not sessions:
        await update.message.reply_text("Nenhuma sess\u00e3o no servidor.")
        return
    cur = chat_cfg.get("sid")
    lines = []
    for s in sessions:
        t = s.get("time", {}).get("updated", 0) / 1000
        when = datetime.fromtimestamp(t, timezone.utc).strftime("%d/%m %H:%M") if t else "?"
        mark = "\u25b6\ufe0f " if s.get("id") == cur else ""
        name = (s.get("title") or s.get("slug") or "sem t\u00edtulo").strip()
        lines.append(f"{mark}`{s.get('id', '')[-6:]}` {when} \u2014 {name[:50]}")
    await update.message.reply_text("*Sess\u00f5es recentes:*\n" + "\n".join(lines) + "\n\n_Use /sessions <id> para retomar. O id completo \u00e9 mostrado por /status._", parse_mode="Markdown")


async def cmd_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    chat_id = update.effective_chat.id
    chat_cfg = context.bot_data.setdefault("chats", {}).setdefault(chat_id, {})
    sid = chat_cfg.get("sid")
    if not sid:
        await update.message.reply_text("Nenhuma conversa ainda \u2014 envie uma mensagem primeiro.")
        return
    model = chat_cfg.get("model")
    body = {}
    if model:
        body["providerID"] = model["providerID"]
        body["modelID"] = model["modelID"]
    try:
        await _client.post(f"/session/{sid}/summarize", json=body if body else None)
        await update.message.reply_text("\U0001f4ca *Resumo disparado* \u2014 o resultado ser\u00e1 gravado na sess\u00e3o.")
    except Exception as e:
        await update.message.reply_text(f"\u274c Falha: {e}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    out = await _run_cli("stats", timeout=40)
    await update.message.reply_text(f"```\n{out[:3500]}\n```" if out.strip() else "\u274c sem dados")


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    v = await _run_cli("--version", timeout=20)
    await update.message.reply_text(
        f"*opencode bot* `v{VERSION}`\n*opencode cli* `{v}`",
        parse_mode="Markdown",
    )


async def cmd_mcp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    args = context.args or []
    sub = args[0] if args else "list"

    if sub in ("list",):
        out = _strip_ansi(await _run_cli("mcp", "list", timeout=30))
        await update.message.reply_text(f"```\n{out[:3500]}\n```" if out.strip() else "\u274c sem sa\u00edda", parse_mode="Markdown")
        return

    if sub == "add":
        if len(args) < 2:
            await update.message.reply_text("Uso: `/mcp add <nome> [--url <url>]`", parse_mode="Markdown")
            return
        name = args[1]
        url = _mcp_url(name)
        rest = args[2:]
        if "--url" in rest:
            i = rest.index("--url")
            if i + 1 < len(rest):
                url = rest[i + 1]
        msg = await _mcp_set_server(name, url)
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    if sub == "token":
        if len(args) < 3:
            await update.message.reply_text(
                "Uso: `/mcp token <nome> <TOKEN>`\n\n"
                "Grava o header `Authorization: Bearer <TOKEN>` no servidor MCP. Para o Todoist, pegue seu API token em Todoist \u2192 Settings \u2192 Integrations \u2192 Developer.\n\n"
                "\u26a0\ufe0f O token passa pelo Telegram (fica no backend deles mesmo depois que voc\u00ea apaga a mensagem). "
                "Alternativa mais segura: edite `~/.config/opencode/opencode.jsonc` direto no servidor e `chmod 600` no arquivo.",
                parse_mode="Markdown",
            )
            return
        name, token = args[1], args[2]
        msg = await _mcp_set_server(name, _mcp_url(name), headers={"Authorization": f"Bearer {token}"})
        await update.message.reply_text(msg + "\n\n_(o token fica salvo em ~/.config/opencode/opencode.jsonc)_", parse_mode="Markdown")
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    if sub == "auth":
        if len(args) < 2:
            await update.message.reply_text("Uso: `/mcp auth <nome>`", parse_mode="Markdown")
            return
        try:
            r = await _client.post(f"/mcp/{args[1]}/auth")
            url = (r.json() or {}).get("authorizationUrl")
        except Exception as e:
            await update.message.reply_text(f"\u274c Falha ao iniciar OAuth: {e}")
            return
        if not url:
            await update.message.reply_text("\u274c O servidor n\u00e3o devolveu URL de autoriza\u00e7\u00e3o.")
            return
        await update.message.reply_text(
            f"\U0001f510 *Autoriza\u00e7\u00e3o MCP* (`{args[1]}`)\n"
            "1) Abra o link abaixo e autorize no Todoist\n"
            "2) Copie o c\u00f3digo da sua URL de retorno e envie:\n"
            "`/mcp callback <nome> <c\u00f3digo>`\n\n"
            f"{url}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    if sub in ("callback", "code"):
        if len(args) < 3:
            await update.message.reply_text(f"Uso: `/mcp {sub} <nome> <c\u00f3digo>`", parse_mode="Markdown")
            return
        try:
            r = await _client.post(f"/mcp/{args[1]}/auth/callback", json={"code": args[2]})
            await update.message.reply_text(f"\U0001f7e2 OAuth conclu\u00eddo: `{json.dumps(r.json(), ensure_ascii=False)[:400]}`")
        except Exception as e:
            await update.message.reply_text(f"\u274c Falha no callback: {e}")
        return

    if sub in ("logout", "signout"):
        if len(args) < 2:
            await update.message.reply_text(f"Uso: `/mcp {sub} <nome>`", parse_mode="Markdown")
            return
        try:
            await _client.request("DELETE", f"/mcp/{args[1]}/auth")
            await update.message.reply_text("\u2705 Credenciais OAuth removidas.")
        except Exception as e:
            await update.message.reply_text(f"\u274c Falha: {e}")
        return

    if sub in ("remove", "rm", "del"):
        if len(args) < 2:
            await update.message.reply_text("Uso: `/mcp remove <nome>`", parse_mode="Markdown")
            return
        p = _mcp_file()
        raw = p.read_text() if p.exists() else ""
        msg = await _mcp_remove_server(args[1], raw)
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    if sub in ("connect", "disconnect"):
        if len(args) < 2:
            await update.message.reply_text(f"Uso: `/mcp {sub} <nome>`", parse_mode="Markdown")
            return
        try:
            await _client.post(f"/mcp/{args[1]}/{sub}")
            await update.message.reply_text(f"\u2705 `{args[1]}` {sub}.")
        except Exception as e:
            await update.message.reply_text(f"\u274c Falha: {e}")
        return

    await update.message.reply_text(
        "Subcomandos de `/mcp`:\n"
        "  `/mcp` \u2014 lista servidores\n"
        "  `/mcp add <nome> --url <url>` \u2014 adiciona remoto\n"
        "  `/mcp token <nome> <TOKEN>` \u2014 define token Bearer\n"
        "  `/mcp remove <nome>` \u2014 remove servidor\n"
        "  `/mcp auth <nome>` \u2014 inicia OAuth\n"
        "  `/mcp callback <nome> <c\u00f3digo>` \u2014 conclui OAuth\n"
        "  `/mcp logout <nome>` \u2014 remove credenciais\n"
        "  `/mcp connect|disconnect <nome>`",
        parse_mode="Markdown",
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    chat_id = update.effective_chat.id
    sid = await oc_create_session()
    context.bot_data.setdefault("chats", {}).setdefault(chat_id, {})["sid"] = sid
    await update.message.reply_text(f"\U0001f504 *Nova conversa iniciada* (sess\u00e3o `{sid[-6:]}`).", parse_mode="Markdown")


async def _reply_or_edit(update, text: str, parse_mode=None, reply_markup=None):
    """Bot\u00e3o transforma o pr\u00f3prio bal\u00e3o (edit); comando digitado manda msg nova."""
    query = update.callback_query
    if query is None or query.message is None:
        await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    try:
        await query.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramError as e:
        if "not modified" in str(e).lower():
            return
        await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    chat_id = update.effective_chat.id
    turn = TURNS.get(chat_id)
    if not turn:
        await _reply_or_edit(update, "Nada em andamento.")
        return
    await oc_abort(turn["sid"])
    turn["out_text"] = "\u26d4 *Interrompido pelo dono.*"
    await _finish_turn(chat_id)


async def _ensure_server_bg(chat_id: int):
    """Sobe o servidor opencode em background e avisa o chat do resultado."""
    app = _app_ref
    try:
        await oc_ensure_server()
    except Exception as e:
        logger.exception("Falha ao inicializar o servidor via /status")
        if app:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"\u274c *Servidor falhou ao inicializar:* `{e}`",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        return
    if app:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text="\u2705 *Servidor opencode funcionando.*",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    ok = await oc_server_ok()
    if not ok:
        asyncio.create_task(_ensure_server_bg(update.effective_chat.id))
        await _reply_or_edit(
            update,
            "\U0001f501 *Servidor est\u00e1 sendo inicializado...*",
            parse_mode="Markdown",
        )
        return
    busy = sum(1 for t in TURNS.values() if t["busy"])
    chat_id = update.effective_chat.id
    chat_cfg = context.bot_data.setdefault("chats", {}).setdefault(chat_id, {})
    model = chat_cfg.get("model")
    agent = chat_cfg.get("agent")
    sid = chat_cfg.get("sid")
    lines = [
        f"*Servidor opencode:* \u2705 funcionando",
        f"*Sess\u00f5es em uso:* {len(TURNS)}",
        f"*Em processamento:* {busy}",
        f"*Diret\u00f3rio:* `{OPENCODE_DIR}`",
    ]
    if sid:
        lines.append(f"*Sess\u00e3o:* `{sid}`")
    if model:
        lines.append(f"*Modelo:* `{model['providerID']}/{model['modelID']}`")
    if agent:
        lines.append(f"*Agente:* `{agent}`")
    lines.append("_Use /sessions para ver as sess\u00f5es do servidor._")
    kb = _kb_status(busy)
    await _reply_or_edit(update, "\n".join(lines), parse_mode="Markdown", reply_markup=kb)


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    if context.args:
        target = _RESTART_ALIASES.get(context.args[0].lower())
        if target is None:
            await update.message.reply_text(
                "Uso: `/restart` ou `/restart bot|servidor|ambos`",
                parse_mode="Markdown",
            )
            return
        await _perform_restart(update.effective_chat.id, target)
        return
    await update.message.reply_text(
        "\u26a0\ufe0f *Reiniciar o qu\u00ea?*\n\n"
        "Respostas em andamento ser\u00e3o interrompidas.",
        parse_mode="Markdown",
        reply_markup=_kb_restart(),
    )


# ---------------------------------------------------------------- bootstrap

async def post_init(app: Application):
    global _app_ref, _stream_task
    _app_ref = app
    await oc_ensure_server()
    if _stream_task is None:
        _stream_task = asyncio.create_task(_consume_events())
    await app.bot.set_my_commands([
        BotCommand("help", "Lista de comandos"),
        BotCommand("new", "Nova conversa"),
        BotCommand("cancel", "Interrompe a resposta"),
        BotCommand("models", "Modelos (ou define modelo)"),
        BotCommand("agents", "Agentes (ou define agente)"),
        BotCommand("sessions", "Listar/retomar sess\u00f5es"),
        BotCommand("summarize", "Resumo da sess\u00e3o"),
        BotCommand("stats", "Uso e custo"),
        BotCommand("mcp", "Servidores MCP"),
        BotCommand("version", "Vers\u00e3o do opencode"),
        BotCommand("status", "Estado do servidor"),
        BotCommand("restart", "Reiniciar bot / servidor"),
    ])
    chat_id = _get_owner_chat()
    if chat_id:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text="\u2705 *opencode bot online v" + VERSION + "* \u2014 conectado ao servidor.",
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("Falha ao enviar mensagem de boot")


async def post_shutdown(app: Application):
    """Cleans up the background event stream task and, if we spawned the
    opencode server ourselves, terminates it instead of leaving it orphaned."""
    global _stream_task
    if _stream_task:
        _stream_task.cancel()
        try:
            await _stream_task
        except (asyncio.CancelledError, Exception):
            pass
        _stream_task = None
    for turn in list(TURNS.values()):
        _stop_typing(turn)
        _stop_stream(turn)
    await oc_stop_server()
    if _client:
        await _client.aclose()


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN n\u00e3o configurado. Crie um .env com BOT_TOKEN=seu_token")
        return
    request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=10)
    persistence = PicklePersistence(filepath=str(Path(OPENCODE_DIR) / ".opencode_bot_persistence.pkl"))
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .persistence(persistence)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["help", "ajuda"], cmd_help))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("summarize", cmd_summarize))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("mcp", cmd_mcp))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler(["new", "novo"], cmd_new))
    app.add_handler(CommandHandler(["cancel", "cancelar"], cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CallbackQueryHandler(cb_permission, pattern=r"^perm:"))
    app.add_handler(CallbackQueryHandler(cb_question, pattern=r"^q[ostcr]:"))
    app.add_handler(CallbackQueryHandler(cb_reply, pattern=r"^mod:"))
    app.add_handler(CallbackQueryHandler(cb_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))
    app.add_handler(MessageHandler(filters.ATTACHMENT, handle_media))
    logger.info("=" * 44)
    logger.info("  Telegram opencode bot  v%s", VERSION)
    logger.info("  url=%s  dir=%s", OC_URL, OPENCODE_DIR)
    logger.info("=" * 44)
    logger.info("Iniciando opencode bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
