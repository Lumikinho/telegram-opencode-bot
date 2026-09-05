#!/usr/bin/env python3
import os
import json
import time
import signal
import logging
import asyncio
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
)
from telegram.ext import filters

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CHAT_ID = os.getenv("CHAT_ID", "")
OPENCODE_DIR = os.getenv("OPENCODE_DIR", str(Path.home()))
OC_PORT = int(os.getenv("OPENCODE_SERVER_PORT", "4100"))
OC_URL = os.getenv("OPENCODE_SERVER_URL", f"http://127.0.0.1:{OC_PORT}")

VERSION = "1.0.0"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

_app_ref: Application | None = None
_client: httpx.AsyncClient | None = None
_server_proc: asyncio.subprocess.Process | None = None
_stream_task: asyncio.Task | None = None
TURNS: dict[int, dict] = {}


def _get_owner_chat() -> int | None:
    if CHAT_ID:
        return int(CHAT_ID)
    return OWNER_ID or None


def is_owner(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if OWNER_ID and user.id != OWNER_ID:
        return False
    return True


async def reject_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.warning("Unauthorized: %s (id=%s)", update.effective_user.username or update.effective_user.first_name, update.effective_user.id)
    await update.message.reply_text("Access denied.")


# ---------------------------------------------------------------- opencode server

async def oc_server_ok() -> bool:
    try:
        r = await _client.get("/config")
        return r.status_code == 200
    except Exception:
        return False


async def oc_start_server():
    global _server_proc
    proc = await asyncio.create_subprocess_exec(
        "opencode", "serve", "--port", str(OC_PORT), "--print-logs",
        cwd=OPENCODE_DIR,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    _server_proc = proc
    for _ in range(60):
        if await oc_server_ok():
            logger.info("opencode server pronto na porta %d", OC_PORT)
            return
        await asyncio.sleep(0.5)
    raise RuntimeError("opencode server não respondeu a tempo")


async def oc_ensure_server():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=OC_URL, timeout=None)
    try:
        if await oc_server_ok():
            logger.info("Conectado ao opencode server %s", OC_URL)
            return
    except Exception:
        pass
    await oc_start_server()


async def oc_create_session() -> str:
    r = await _client.post("/session", json={"dir": OPENCODE_DIR})
    r.raise_for_status()
    return r.json()["id"]


async def oc_send_message(sid: str, text: str, model: dict | None = None, agent: str | None = None):
    body: dict = {"parts": [{"type": "text", "text": text}]}
    if model:
        body["model"] = model
    if agent:
        body["agent"] = agent
    await _client.post(
        f"/session/{sid}/prompt_async",
        json=body,
    )


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
        data = json.loads(p.read_text())
    except Exception:
        return {}
    return (data or {}).get("mcp") or {}


def _mcp_url(name: str) -> str:
    return (_load_mcp_cfg().get(name) or {}).get("url") or ""


async def _mcp_set_server(name: str, url: str, headers: dict | None = None) -> str:
    p = _mcp_file()
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except Exception:
            data = {}
    mcp = data.setdefault("mcp", {})
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
    mcp[name] = cfg
    try:
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except Exception as e:
        return f"\u274c falha ao gravar config: {e}"
    try:
        await _client.post("/mcp", json={"name": name, "config": cfg})
        return f"\u2705 `{name}` configurado (arquivo + servidor em execu\u00e7\u00e3o)."
    except Exception as e:
        return f"\u26a0\ufe0f Config salva no arquivo, mas o servidor n\u00e3o aplicou: {e}"


async def oc_answer_permission(sid: str, perm_id: str, response: str):
    await _client.post(
        f"/session/{sid}/permissions/{perm_id}",
        json={"response": response},
    )


async def oc_abort(sid: str):
    try:
        await _client.post(f"/session/{sid}/abort")
    except Exception:
        pass


def _find_turn(sid: str) -> dict | None:
    for t in TURNS.values():
        if t["sid"] == sid:
            return t
    return None


# ---------------------------------------------------------------- rendering

TOOL_ICONS = {
    "read": "\U0001f4d6",     # 📖 leu
    "write": "\u2795",        # ➕ criou/escreveu
    "edit": "\u270f\ufe0f",   # ✏️ editou
    "bash": "\u26a1",         # ⚡ rodou comando
    "glob": "\U0001f50d",     # 🔍 buscou arquivos
    "grep": "\U0001f50d",
    "mcp": "\U0001f916",      # 🤖 mcp
    "todo": "\U0001f4cb",     # 📋
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
    title = p.get("title") or p.get("permission") or "permissão"
    if pat_str:
        return f"{title}: `{pat_str}`"
    return f"`{title}`"


def _code(s: str) -> str:
    return f"`{(s or '').replace('`', "'")}`"


def _tail_out(s: str, n: int = 450) -> str:
    s = (s or "").strip().replace("```", "'''")
    if not s:
        return ""
    if len(s) > n:
        return "\u2026" + s[-n:]
    return s


def _render_running(turn: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    lines = []
    if turn["todo"]:
        lines.append(f"\U0001f4cb *Plano:* {turn['todo']} passo{'s' if turn['todo'] != 1 else ''}")
    kb = None
    if turn["perm_queue"]:
        p = turn["perm_queue"][0]
        lines += ["", "\U0001f512 *Permiss\u00e3o pedida:*", _perm_desc(p)]
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("\u2705 Uma vez", callback_data=f"perm:{p['sid']}:{p['id']}:once"),
                InlineKeyboardButton("\U0001f501 Sempre", callback_data=f"perm:{p['sid']}:{p['id']}:always"),
                InlineKeyboardButton("\u274c Negar", callback_data=f"perm:{p['sid']}:{p['id']}:reject"),
            ]
        ])
    else:
        curr = turn["current"]
        if curr and curr.get("cmd"):
            lines += ["", f"\u26a1 *rodando:* {_code(curr['cmd'])}"]
            out = _tail_out(curr.get("out"), 450)
            if out:
                lines += ["```", out, "```"]
        elif curr and curr.get("label"):
            lines += ["", curr["label"]]
        else:
            lines += ["", "\u23f3 *pensando\u2026*"]
    return "\n".join(lines), kb


def _summary(turn: dict) -> list[str]:
    out = []
    if turn["reads"]:
        out.append(f"\U0001f4d6 *leu:* " + ", ".join(sorted(f"`{_fmt_path(x)}`" for x in turn["reads"])))
    if turn["writes"]:
        out.append(f"\u2795 *criou:* " + ", ".join(sorted(f"`{_fmt_path(x)}`" for x in turn["writes"])))
    if turn["edits"]:
        out.append(f"\u270f\ufe0f *editou:* " + ", ".join(sorted(f"`{_fmt_path(x)}`" for x in turn["edits"])))
    if turn["rejected"]:
        out.append(f"\u274c *negado:* {len(turn['rejected'])} permiss\u00f5e(s)")
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
        lines += ["", f"{i}) {_code('$ ' + step['cmd'])}"]
        out = _tail_out(step.get("out"), max_out)
        if out:
            lines += ["```", out, "```"]
        if step.get("status") == "error":
            lines.append("(\u274c erro)")
    return lines


def _render_done(turn: dict, elapsed: float, answer: str = "") -> str:
    def build(trace_cfg):
        lines = []
        if answer:
            lines.append(answer)
        summaries = _summary(turn)
        if summaries:
            lines += ["", "\u2014\u2014\u2014", *summaries]
        trace = _trace_lines(turn, *trace_cfg)
        if trace:
            lines += ["", *trace]
        return "\n".join(lines)

    total = build((40, 500))
    if len(total) > 3800:
        total = build((4, 220))
    if len(total) > 3950:
        total = total[:3950]
    return total


# ---------------------------------------------------------------- turn plumbing

async def _push_status(turn: dict, force: bool = False):
    app = _app_ref
    now = time.monotonic()
    if not force and now - turn["last_edit"] < 1.0:
        return
    turn["last_edit"] = now
    if turn.get("done"):
        text = _render_done(turn, turn.get("elapsed", 0.0))
        kb = None
    else:
        text, kb = _render_running(turn)
    try:
        await app.bot.edit_message_text(
            chat_id=turn["chat_id"],
            message_id=turn["status_msg_id"],
            text=text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except TelegramError:
        try:
            await app.bot.edit_message_text(
                chat_id=turn["chat_id"],
                message_id=turn["status_msg_id"],
                text=text,
                reply_markup=kb,
            )
        except TelegramError:
            pass


def _start_typing(turn: dict):
    async def loop():
        while turn["busy"]:
            try:
                await _app_ref.bot.send_chat_action(chat_id=turn["chat_id"], action="typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    turn["typing_task"] = asyncio.create_task(loop())


def _stop_typing(turn: dict):
    task = turn.pop("typing_task", None)
    if task:
        task.cancel()


STREAM_MIN = 150


async def _flush_stream(turn: dict):
    """Sends the unanswered tail of out_text, in order, as new messages."""
    text = turn["out_text"]
    tail = text[turn["streamed_len"]:]
    if not tail:
        return
    if not turn["done"] and len(tail.strip()) < STREAM_MIN:
        return
    for part in _split_text(tail, 3500):
        try:
            await _app_ref.bot.send_message(chat_id=turn["chat_id"], text=part, parse_mode="Markdown")
        except TelegramError:
            try:
                await _app_ref.bot.send_message(chat_id=turn["chat_id"], text=part)
            except TelegramError:
                break
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


async def _start_turn(update_or_chat, text: str):
    """Runs one opencode turn for a chat. update can be a message update."""
    chat_id = update_or_chat.effective_chat.id if hasattr(update_or_chat, "effective_chat") else update_or_chat
    user_id = update_or_chat.effective_user.id if hasattr(update_or_chat, "effective_user") else None

    if chat_id in TURNS:
        await update_or_chat.message.reply_text("\u23f3 Ainda estou processando a mensagem anterior\u2026")
        return

    # reuse the persistent session, or create one
    data = _app_ref.bot_data.setdefault("chats", {})
    chat_cfg = data.setdefault(chat_id, {})
    sid = chat_cfg.get("sid")
    if not sid:
        sid = await oc_create_session()
        chat_cfg["sid"] = sid

    placeholder = await update_or_chat.message.reply_text("\u23f3 *opencode pensando\u2026*", parse_mode="Markdown")

    turn = {
        "chat_id": chat_id,
        "sid": sid,
        "status_msg_id": placeholder.message_id,
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
        "user_msg_id": None,
        "streamed_len": 0,
        "perm_queue": deque(),
        "typing_task": None,
        "stream_task": None,
    }
    TURNS[chat_id] = turn
    _start_typing(turn)
    turn["stream_task"] = asyncio.create_task(_stream_loop(turn))

    try:
        await oc_send_message(sid, text, model=chat_cfg.get("model"), agent=chat_cfg.get("agent"))
    except Exception as e:
        logger.exception("Falha ao enviar prompt")
        turn["out_text"] = f"\u274c Falha ao enviar para o opencode: {e}"
        await _finish_turn(chat_id)


async def _finish_turn(chat_id: int):
    turn = TURNS.pop(chat_id, None)
    if not turn:
        return
    turn["busy"] = False
    turn["done"] = True
    turn["elapsed"] = time.monotonic() - turn["started"]
    turn["current"] = None
    _stop_typing(turn)
    _stop_stream(turn)
    await _flush_stream(turn)
    # se o texto já foi transmitido em fluxo, a mensagem final é só o resumo
    answer = "" if turn["streamed_len"] else (turn["out_text"].strip() or "(sem resposta)")
    text = _render_done(turn, turn["elapsed"], answer)
    chunks = _split_text(text)
    app = _app_ref
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id, message_id=turn["status_msg_id"], text=chunks[0], parse_mode="Markdown"
        )
    except TelegramError:
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id, message_id=turn["status_msg_id"], text=chunks[0]
            )
        except TelegramError:
            return
    for chunk in chunks[1:]:
        try:
            await app.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="Markdown")
        except TelegramError:
            await app.bot.send_message(chat_id=chat_id, text=chunk)


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
    elif et == "message.updated":
        info = props.get("info") or {}
        if info.get("role") == "user":
            turn["user_msg_id"] = info.get("id")
    elif et == "session.status":
        st = props.get("status", {})
        if st.get("type") == "busy":
            turn["busy"] = True
            if not turn["typing_task"]:
                _start_typing(turn)
        elif st.get("type") == "idle":
            pass
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
        if part.get("type") == "tool":
            _record_tool(turn, part)
            await _push_status(turn)
    elif et == "message.part.delta":
        if props.get("field") == "text" and props.get("messageID") != turn.get("user_msg_id"):
            turn["out_text"] += props.get("delta", "")
    elif et == "file.edited":
        f = props.get("file")
        if f:
            turn["edits"].add(f)
            await _push_status(turn)


# ---------------------------------------------------------------- handlers

async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await _start_turn(update, text)


async def cb_permission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if OWNER_ID and query.from_user.id != OWNER_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    _, sid, perm_id, response = query.data.split(":")
    chat_id = update.effective_chat.id
    turn = TURNS.get(chat_id)
    if not turn:
        await query.answer("Sess\u00e3o encerrada.", show_alert=True)
        return
    if not turn["perm_queue"] or turn["perm_queue"][0]["id"] != perm_id:
        await query.answer("Permiss\u00e3o j\u00e1 respondida.", show_alert=True)
        return
    turn["perm_queue"].popleft()
    await oc_answer_permission(sid, perm_id, response)
    await query.answer()
    if turn["perm_queue"]:
        await _push_status(turn, force=True)
    else:
        # remove the keyboard, keep status
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=turn["status_msg_id"], reply_markup=None)
        except TelegramError:
            pass
        await _push_status(turn, force=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    await update.message.reply_text(
        "\U0001f916 *opencode no Telegram*\n\n"
        "Envie qualquer mensagem e eu respondo com o opencode.\n\n"
        "Digite /help para ver todos os comandos.",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    await update.message.reply_text(
        "*Comandos do opencode:*\n\n"
        "  /models \u2014 lista modelos; `/models opencode/xx` define\n"
        "  /agents \u2014 lista agentes; `/agents <nome>` define\n"
        "  /sessions \u2014 lista sess\u00f5es; `/sessions <id>` retoma\n"
        "  /new \u2014 nova conversa (alias /novo)\n"
        "  /cancel \u2014 interrompe (alias /cancelar)\n"
        "  /summarize \u2014 dispara o resumo da sess\u00e3o\n"
        "  /stats \u2014 uso e custo do opencode\n"
        "  /mcp \u2014 gerencia servidores MCP (list/add/auth/logout/debug)\n"
        "  /version \u2014 vers\u00e3o instalada\n"
        "  /status \u2014 estado do servidor\n"
        "  /help \u2014 esta ajuda (alias /ajuda)",
        parse_mode="Markdown",
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
    lines = [l for l in out.splitlines() if l.strip()]
    cur = chat_cfg.get("model")
    head = f"*Modelo atual:* `{(cur['providerID'] + '/' + cur['modelID']) if cur else '\u00e0 definir'}`\n\nModelos dispon\u00edveis:\n"
    if len(lines) > 40:
        lines = lines[:40] + ["\u2026 (+%d)" % (len(lines) - 40)]
    await update.message.reply_text(head + "\n".join(f"`{l}`" for l in lines), parse_mode="Markdown")


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
        await update.message.reply_text(msg)
        return

    if sub == "token":
        if len(args) < 3:
            await update.message.reply_text(
                "Uso: `/mcp token <nome> <TOKEN>`\n\n"
                "Grava o header `Authorization: Bearer <TOKEN>` no servidor MCP. Para o Todoist, pegue seu API token em Todoist \u2192 Settings \u2192 Integrations \u2192 Developer.",
                parse_mode="Markdown",
            )
            return
        name, token = args[1], args[2]
        msg = await _mcp_set_server(name, _mcp_url(name), headers={"Authorization": f"Bearer {token}"})
        await update.message.reply_text(msg + "\n\n_(o token fica salvo em ~/.config/opencode/opencode.jsonc)_")
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


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    chat_id = update.effective_chat.id
    turn = TURNS.get(chat_id)
    if not turn:
        await update.message.reply_text("Nada em andamento.")
        return
    await oc_abort(turn["sid"])
    turn["out_text"] = "\u26d4 *Interrompido pelo dono.*"
    await _finish_turn(chat_id)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    ok = await oc_server_ok()
    busy = sum(1 for t in TURNS.values() if t["busy"])
    chat_id = update.effective_chat.id
    chat_cfg = context.bot_data.setdefault("chats", {}).setdefault(chat_id, {})
    model = chat_cfg.get("model")
    agent = chat_cfg.get("agent")
    sid = chat_cfg.get("sid")
    lines = [
        f"*Servidor opencode:* {'\u2705 online' if ok else '\u274c offline'}",
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
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
        BotCommand("sessions", "Listar/retomar sessões"),
        BotCommand("summarize", "Resumo da sessão"),
        BotCommand("stats", "Uso e custo"),
        BotCommand("mcp", "Servidores MCP"),
        BotCommand("version", "Versão do opencode"),
        BotCommand("status", "Estado do servidor"),
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


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN n\u00e3o configurado. Crie um .env com BOT_TOKEN=seu_token")
        return
    request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=10)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
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
    app.add_handler(CallbackQueryHandler(cb_permission, pattern=r"^perm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))
    logger.info("=" * 44)
    logger.info("  Telegram opencode bot  v%s", VERSION)
    logger.info("  url=%s  dir=%s", OC_URL, OPENCODE_DIR)
    logger.info("=" * 44)
    logger.info("Iniciando opencode bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()