"""Callbacks de botoes inline: permissoes, perguntas, comandos."""
import logging
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from .. import config, state
from .commands import cmd_agents, cmd_bateria, cmd_cancel, cmd_funnel, cmd_help, cmd_mcp, cmd_menu, cmd_models, cmd_new, cmd_sessions, cmd_stats, cmd_status, cmd_summarize, cmd_version
from ..opencode import oc_answer_permission, oc_reject_question, oc_server_info
from ..render import _RESTART_LABELS, _kb_menu, _kb_menu_opencode, _kb_menu_server, _menu_main_text
from ..restart import _kill_all_turns, _perform_restart
from ..turns import _drop_questions, _push_status, _refresh_after_question, _submit_question


logger = logging.getLogger(__name__)


def _find_qitem(turn: dict, rid: str, qi: int | None = None) -> dict | None:
    for q in turn["questions"]:
        if q["request_id"] == rid and (qi is None or q["qidx"] == qi):
            return q
    return None


async def cb_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != config.OWNER_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    chat_id = update.effective_chat.id
    turn = state.TURNS.get(chat_id)
    if not turn:
        await query.answer("Sessão encerrada.", show_alert=True)
        return
    parts = query.data.split(":")
    op, rid = parts[0], parts[1]

    if op == "qo":
        _, _, qi_s, idx_s = parts
        qi, idx = int(qi_s), int(idx_s)
        item = _find_qitem(turn, rid, qi)
        if not item:
            await query.answer("Pergunta já respondida.", show_alert=True)
            return
        item["answer"] = [item["options"][idx].get("value", item["options"][idx].get("label"))]
        ok = await _submit_question(turn, rid)
        await query.answer("✅ Enviado!" if ok else "❌ Falha ao enviar.")
        await _refresh_after_question(turn)
    elif op == "qt":
        _, _, qi_s, idx_s = parts
        qi, idx = int(qi_s), int(idx_s)
        item = _find_qitem(turn, rid, qi)
        if not item:
            await query.answer("Pergunta já respondida.", show_alert=True)
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
            q["answer"] = [q["options"][i].get("value", q["options"][i].get("label")) for i in sorted(sel)] if sel else None
            if not q["answer"]:
                missing = True
        if missing:
            await query.answer("Selecione ao menos uma opção em cada pergunta.", show_alert=True)
            return
        ok = await _submit_question(turn, rid)
        await query.answer("✅ Enviado!" if ok else "❌ Falha ao enviar.")
        await _refresh_after_question(turn)
    elif op == "qc":
        _, _, qi_s = parts
        qi = int(qi_s)
        item = _find_qitem(turn, rid, qi)
        if not item:
            await query.answer("Pergunta já respondida.", show_alert=True)
            return
        turn["awaiting_custom"] = (rid, qi)
        await query.answer("✏️ Digite sua resposta")
        await update.effective_chat.send_message(
            "✏️ *Digite sua resposta:* mande o texto agora.",
            parse_mode="Markdown",
        )
    elif op == "qr":
        ok = await oc_reject_question(turn.get("sid") or "", rid)
        _drop_questions(turn, rid)
        await query.answer("❌ Rejeitada." if ok else "❌ Falha ao rejeitar.")
        await _refresh_after_question(turn)


async def cb_permission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != config.OWNER_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    _, perm_id, response = query.data.split(":", 2)
    chat_id = update.effective_chat.id
    turn = state.TURNS.get(chat_id)
    if not turn:
        await query.answer("Sessão encerrada.", show_alert=True)
        return
    if not turn["perm_queue"] or turn["perm_queue"][0]["id"] != perm_id:
        await query.answer("Permissão já respondida.", show_alert=True)
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
    """Cria um 'update' com .message apontando para a mensagem do botão."""
    query = update.callback_query
    message = query.message

    class _Proxy:
        # Handlers usam update.message (reply_text/delete) e update.effective_*.
        # Em CallbackQuery updates o campo .message é None, então injetamos a
        # mensagem onde o botão foi clicado e repassamos todo o resto ao update real.
        def __init__(self):
            self.message = message

        def __getattr__(self, name):
            return getattr(update, name)

    return _Proxy()


async def cb_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manipula botões de seleção de modelo (`mod:`)."""
    query = update.callback_query
    if query.from_user.id != config.OWNER_ID:
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
            f"*Modelo atual:* `{spec}`\n\n✅ Modelo definido!",
            parse_mode="Markdown",
        )
    except TelegramError:
        await query.message.reply_text(f"✅ Modelo definido: `{spec}`", parse_mode="Markdown")


async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Painel interativo: menu:main|status|refresh|opencode|server|funnel|battery|summarize."""
    query = update.callback_query
    if query.from_user.id != config.OWNER_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    data = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    await query.answer()
    injected = await _btn_invoked(update, context)

    if data in ("main", "refresh"):
        info = await oc_server_info()
        try:
            await query.message.edit_text(
                _menu_main_text(info["ok"], info["latency_ms"]),
                parse_mode="Markdown",
                reply_markup=_kb_menu(info["ok"]),
            )
        except TelegramError:
            pass
        return
    if data == "status":
        context.args = []
        try:
            await cmd_status(injected, context)
        finally:
            context.args = []
        return
    if data == "opencode":
        try:
            await query.message.edit_text(
                "🤖 *Opencode* — escolha uma função:",
                parse_mode="Markdown",
                reply_markup=_kb_menu_opencode(),
            )
        except TelegramError:
            pass
        return
    if data == "server":
        info = await oc_server_info()
        dot = "🟢 ATIVO" if info["ok"] else "🔴 DESATIVADO"
        extra = f" ({info['latency_ms']}ms)" if info["latency_ms"] is not None else ""
        try:
            await query.message.edit_text(
                f"🖥️ *Servidor:* {dot}{extra}\n`{info['url']}`",
                parse_mode="Markdown",
                reply_markup=_kb_menu_server(info["ok"]),
            )
        except TelegramError:
            pass
        return
    if data == "funnel":
        context.args = []
        try:
            await cmd_funnel(injected, context)
        finally:
            context.args = []
        return
    if data == "battery":
        context.args = []
        try:
            await cmd_bateria(injected, context)
        finally:
            context.args = []
        return
    if data == "summarize":
        context.args = []
        try:
            await cmd_summarize(injected, context)
        finally:
            context.args = []
        return


async def cb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Roteia os botões inline para os comandos correspondentes."""
    query = update.callback_query
    if query.from_user.id != config.OWNER_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    data = query.data or ""
    await query.answer()

    if data == "__restart_confirm" or data.startswith("__restart:"):
        chat_id = update.effective_chat.id
        # teclado antigo (só "Confirmar restart") equivale a tudo
        target = data.split(":", 1)[1] if ":" in data else "both"
        if target not in ("bot", "server", "both"):
            return
        await _kill_all_turns()
        try:
            await query.message.edit_text(
                f"🔁 *Reiniciando {_RESTART_LABELS[target]}...*",
                parse_mode="Markdown",
            )
        except TelegramError:
            await query.message.reply_text(
                f"🔁 *Reiniciando {_RESTART_LABELS[target]}...*",
                parse_mode="Markdown",
            )
        await _perform_restart(chat_id, target)
        return

    cmd = data.split(" ", 1)[0]
    if not cmd.startswith("/"):
        return
    handler = {
        "/help": cmd_help,
        "/menu": cmd_menu,
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


async def cb_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botão de sessão (`ses:<6 últimos do id>`): retoma sem digitar id."""
    query = update.callback_query
    if query.from_user.id != config.OWNER_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    suffix = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    chat_id = update.effective_chat.id
    chat_cfg = context.bot_data.setdefault("chats", {}).setdefault(chat_id, {})
    try:
        all_sessions = await state._client.get("/api/session")
        found = next((s for s in (all_sessions.json() or {}).get("data") or []
                      if s.get("id", "").endswith(suffix)), None)
    except Exception:
        found = None
    if not found:
        await query.answer("Sessão não encontrada.", show_alert=True)
        return
    chat_cfg["sid"] = found["id"]
    name = (found.get("title") or "sem título").strip()
    await query.answer("Conversa retomada.")
    try:
        await query.message.edit_text(f"🔁 Conversa retomada: *{name}*",
                                      parse_mode="Markdown")
    except TelegramError:
        pass
