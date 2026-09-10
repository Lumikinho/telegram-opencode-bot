"""Ciclo de vida dos turnos: stream SSE, permissoes, perguntas, midia."""
from collections import deque
import asyncio
import base64
import json
import logging
import re
import time
from telegram import Update
from telegram.error import TelegramError
from . import state
from .opencode import oc_answer_question, oc_create_session, oc_send_with_retry
from .render import TOOL_ICONS, _fmt_path, _kb_after_turn, _render_running, _render_think, _result_text, _safe_edit_message, _safe_send_message, _split_text, _telegram_html


logger = logging.getLogger(__name__)


async def oc_abort(sid: str):
    try:
        await state._client.post(f"/session/{sid}/abort")
    except Exception as e:
        logger.debug("Falha ao abortar sessão %s: %s", (sid or "")[-6:], e)


async def _kill_all_turns(chat_id: int | None = None):
    for cid in [chat_id] if chat_id else list(state.TURNS.keys()):
        turn = state.TURNS.pop(cid, None)
        if turn and turn.get("sid"):
            try:
                await oc_abort(turn["sid"])
            except Exception as e:
                logger.debug("Falha ao abortar turno %s: %s", cid, e)


def _find_turn(sid: str) -> dict | None:
    for t in state.TURNS.values():
        if t.get("sid") == sid:
            return t
    return None


async def _push_status(turn: dict, force: bool = False):
    app = state._app_ref
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

    ok = await _safe_edit_message(
        app.bot, turn["chat_id"], turn["status_msg_id"], text,
        parse_mode=parse_mode, reply_markup=kb,
    )
    if not ok and kb is not None:
        # Both attempts failed with the same reply_markup -- if kb itself is
        # the problem (e.g. a callback_data over Telegram's 64-byte limit),
        # retrying with it again would just fail identically and the status
        # message would silently stop updating, which looks to the user like
        # the bot hung. Drop the buttons and show the text anyway so the
        # state is at least visible.
        logger.warning("Falha ao editar status mesmo sem parse_mode; tentando sem botões")
        await _safe_edit_message(
            app.bot, turn["chat_id"], turn["status_msg_id"],
            text + "\n\n_(botões indisponíveis nesta atualização)_",
        )


def _start_typing(turn: dict):
    async def loop():
        while turn["busy"]:
            try:
                await state._app_ref.bot.send_chat_action(chat_id=turn["chat_id"], action="typing")
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
        msg = await _safe_send_message(state._app_ref.bot, turn["chat_id"], text, parse_mode="HTML")
        if msg is not None:
            turn["result_msg_id"] = msg.message_id
        else:
            return
    else:
        await _safe_edit_message(state._app_ref.bot, turn["chat_id"], turn["result_msg_id"], text, parse_mode="HTML")
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
            turn["current"] = {"label": f"📖 lendo `{_fmt_path(path)}`" if path else "📖 lendo arquivo", "out": ""}
        elif status in ("completed", "error") and path:
            turn["reads"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    elif tool == "write":
        if status == "running":
            turn["current"] = {"label": f"➕ criando `{_fmt_path(path)}`" if path else "➕ criando arquivo", "out": ""}
        elif status in ("completed", "error") and path:
            turn["writes"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    elif tool == "edit":
        if status == "running":
            turn["current"] = {"label": f"✏️ editando `{_fmt_path(path)}`" if path else "✏️ editando arquivo", "out": ""}
        elif status in ("completed", "error") and path:
            turn["edits"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    else:
        title = state.get("title") or ""
        label = f"{TOOL_ICONS.get(tool, '⚙️')} {tool}" + (f": `{title}`" if title else "")
        if status == "error" and "permission" in (state.get("error") or "").lower():
            turn["rejected"] += 1
            label = "❌ permissão negada"
        turn["current"] = {"label": label, "out": ""} if status == "running" else None


def _new_turn_skeleton(chat_id: int) -> dict:
    """A turn dict with everything needed to occupy state.TURNS[chat_id] synchronously,
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

    if chat_id in state.TURNS:
        await update_or_chat.message.reply_text("⏳ Ainda estou processando a mensagem anterior…")
        return

    # Reserve the slot for this chat *before* any await, closing the race where
    # two fast messages both see "no turn running" and both start one.
    turn = _new_turn_skeleton(chat_id)
    state.TURNS[chat_id] = turn

    try:
        data = state._app_ref.bot_data.setdefault("chats", {})
        chat_cfg = data.setdefault(chat_id, {})
        sid = chat_cfg.get("sid")
        if not sid:
            sid = await oc_create_session()
            chat_cfg["sid"] = sid
        turn["sid"] = sid

        placeholder = await update_or_chat.message.reply_text("💭 *opencode pensando…*", parse_mode="Markdown")
        turn["status_msg_id"] = placeholder.message_id
        turn["started"] = time.monotonic()

        _start_typing(turn)
        turn["stream_task"] = asyncio.create_task(_stream_loop(turn))

        await oc_send_with_retry(chat_cfg, turn, text, model=chat_cfg.get("model"), agent=chat_cfg.get("agent"), parts=parts)
    except Exception as e:
        logger.exception("Falha ao iniciar turn")
        turn["out_text"] = f"❌ Falha ao enviar para o opencode: {e}"
        if turn["status_msg_id"] is None:
            # We never even got the placeholder message out -- send one now so
            # _finish_turn has something to edit.
            try:
                placeholder = await update_or_chat.message.reply_text("💭 ...")
                turn["status_msg_id"] = placeholder.message_id
            except Exception:
                pass
        await _finish_turn(chat_id)


async def _finish_turn(chat_id: int):
    turn = state.TURNS.pop(chat_id, None)
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

    app = state._app_ref
    # ---- inverted order: answer on top, think below ----
    answer = (turn["out_text"] or "").strip()
    if not answer:
        answer = "(sem resposta)"
    bodies = [_telegram_html(c, max_len=3500) for c in _split_text(answer, limit=3500)]
    first_body = bodies[0]

    # the answer takes over the top balloon (the "pensando…" placeholder)
    answer_msg_id = turn["status_msg_id"]
    if answer_msg_id is not None:
        ok = await _safe_edit_message(app.bot, chat_id, answer_msg_id, first_body, parse_mode="HTML")
        if not ok:
            answer_msg_id = None
    if answer_msg_id is None:
        # unlikely fallback (placeholder never sent, or its edit failed): answer as a new message
        msg = await _safe_send_message(app.bot, chat_id, first_body, parse_mode="HTML")
        answer_msg_id = msg.message_id if msg is not None else None

    last_msg_id = answer_msg_id
    for body in bodies[1:]:
        msg = await _safe_send_message(app.bot, chat_id, body, parse_mode="HTML")
        if msg is not None:
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
        think_placed = await _safe_edit_message(app.bot, chat_id, think_msg_id, think_text, parse_mode="HTML")
    if not think_placed:
        await _safe_send_message(app.bot, chat_id, think_text, parse_mode="HTML")

    # ---- botões de ação pós-turno ----
    if last_msg_id is not None:
        try:
            await app.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=last_msg_id,
                reply_markup=_kb_after_turn(),
            )
        except TelegramError:
            pass


async def _consume_events():
    while True:
        try:
            async with state._client.stream("GET", "/event") as resp:
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
            # here too (finish is idempotent via state.TURNS.pop) avoids a turn that
            # never closes.
            await _finish_turn(turn["chat_id"])
    elif et == "session.idle":
        await _finish_turn(turn["chat_id"])
    elif et == "session.error":
        turn["out_text"] = f"❌ *Erro no opencode:* {props.get('error') or props.get('message') or 'desconhecido'}"
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
            await state._app_ref.bot.edit_message_reply_markup(
                chat_id=turn["chat_id"], message_id=turn["status_msg_id"], reply_markup=None
            )
        except TelegramError:
            pass
        await _push_status(turn, force=True)


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
    turn = state.TURNS.get(chat_id)
    if not turn or not turn.get("awaiting_custom"):
        return False
    rid, qi = turn.pop("awaiting_custom")
    item = next((q for q in turn["questions"] if q["request_id"] == rid and q["qidx"] == qi), None)
    if not item:
        return False
    item["answer"] = [text]
    ok = await _submit_question(turn, rid)
    await update.message.reply_text(
        "✅ *Resposta enviada ao opencode.*" if ok else
        "❌ *Falha ao enviar resposta ao opencode.*",
        parse_mode="Markdown",
    )
    await _refresh_after_question(turn)
    return True


# ---------------------------------------------------------------- handlers
