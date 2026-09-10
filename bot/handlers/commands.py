"""Comandos do bot (/new, /status, /restart, /bateria, ...)."""
from datetime import datetime, timezone
import asyncio
import json
import logging
import re
from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from .. import config, state
from ..auth import is_owner, reject_unauthorized
from ..battery import _BAT_STATUS_PT, format_bateria, read_battery
from ..mcp_cfg import _mcp_file, _mcp_remove_server, _mcp_set_server, _mcp_url
from ..opencode import _run_cli, _strip_ansi, oc_create_session, oc_ensure_server, oc_server_ok
from ..render import _kb_quick, _kb_restart, _kb_status, _models_kb, _reply_or_edit, _safe_send_message
from ..render import _RESTART_ALIASES
from ..restart import _perform_restart
from ..turns import _finish_turn, oc_abort


logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    await update.message.reply_text(
        "🤖 *opencode no Telegram*\n\n"
        "Envie qualquer mensagem e eu respondo com o opencode.\n\n"
        "Toque nos botões abaixo para navegar:",
        parse_mode="Markdown",
        reply_markup=_kb_quick(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    await update.message.reply_text(
        "*Comandos do opencode:*\n\n"
        "Envie fotos, áudios, vídeos e arquivos junto com um texto (às vezes a legenda).\n\n"
        "  /models — lista modelos; `/models opencode/xx` define\n"
        "  /agents — lista agentes; `/agents <nome>` define\n"
        "  /sessions — lista sessões; `/sessions <id>` retoma\n"
        "  /new — nova conversa (alias /novo)\n"
        "  /cancel — interrompe (alias /cancelar)\n"
        "  /summarize — dispara o resumo da sessão\n"
        "  /stats — uso e custo do opencode\n"
        "  /mcp — gerencia servidores MCP (list/add/token/remove/auth/logout)\n"
        "  /version — versão instalada\n"
        "  /status — estado do servidor (+ bateria)\n"
        "  /bateria [on|off] — nível da bateria e alertas de bateria baixa\n"
        "  /restart [bot|servidor|ambos] — reinicia o bot, o servidor ou os dois (respostas em andamento são interrompidas)\n"
        "  /help — esta ajuda (alias /ajuda)",
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
        await update.message.reply_text(f"✅ Modelo definido: `{providerID}/{modelID}`", parse_mode="Markdown")
        return
    out = await _run_cli("models", timeout=30)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    models = [ln for ln in lines if "/" in ln]
    cur = chat_cfg.get("model")
    head = f"*Modelo atual:* `{(cur['providerID'] + '/' + cur['modelID']) if cur else 'à definir'}`\n\nEscolha o modelo:"
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
        await update.message.reply_text(f"✅ Agente definido: `{name}`", parse_mode="Markdown")
        return
    out = await _run_cli("agent", "list", timeout=30)
    names = []
    for line in out.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:\(.*\))?\s*$", line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    cur = chat_cfg.get("agent")
    text = f"*Agente atual:* `{cur or 'à definir'}`\n\n"
    text += "Disponíveis:\n" + "\n".join(f"`{n}`" for n in names) if names else "❌ nenhum listado"
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
            all_sessions = await state._client.get("/session")
            found = None
            for s in all_sessions.json():
                if s.get("id") == target or s.get("slug") == target or s.get("id", "").endswith(target):
                    found = s
                    break
        except Exception:
            found = None
        if not found:
            await update.message.reply_text("❌ Sessão não encontrada.")
            return
        chat_cfg["sid"] = found["id"]
        await update.message.reply_text(f"🔁 Conversa retomada: *{found.get('title') or found.get('slug')}* (`{found['id'][-6:]}`)", parse_mode="Markdown")
        return
    try:
        all_sessions = await state._client.get("/session")
        sessions = sorted(all_sessions.json(), key=lambda s: s.get("time", {}).get("updated", 0), reverse=True)[:10]
    except Exception as e:
        await update.message.reply_text(f"❌ Falha ao listar sessões: {e}")
        return
    if not sessions:
        await update.message.reply_text("Nenhuma sessão no servidor.")
        return
    cur = chat_cfg.get("sid")
    lines = []
    for s in sessions:
        t = s.get("time", {}).get("updated", 0) / 1000
        when = datetime.fromtimestamp(t, timezone.utc).strftime("%d/%m %H:%M") if t else "?"
        mark = "▶️ " if s.get("id") == cur else ""
        name = (s.get("title") or s.get("slug") or "sem título").strip()
        lines.append(f"{mark}`{s.get('id', '')[-6:]}` {when} — {name[:50]}")
    await update.message.reply_text("*Sessões recentes:*\n" + "\n".join(lines) + "\n\n_Use /sessions <id> para retomar. O id completo é mostrado por /status._", parse_mode="Markdown")


async def cmd_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    chat_id = update.effective_chat.id
    chat_cfg = context.bot_data.setdefault("chats", {}).setdefault(chat_id, {})
    sid = chat_cfg.get("sid")
    if not sid:
        await update.message.reply_text("Nenhuma conversa ainda — envie uma mensagem primeiro.")
        return
    model = chat_cfg.get("model")
    body = {}
    if model:
        body["providerID"] = model["providerID"]
        body["modelID"] = model["modelID"]
    try:
        await state._client.post(f"/session/{sid}/summarize", json=body if body else None)
        await update.message.reply_text("📊 *Resumo disparado* — o resultado será gravado na sessão.")
    except Exception as e:
        await update.message.reply_text(f"❌ Falha: {e}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    out = await _run_cli("stats", timeout=40)
    await update.message.reply_text(f"```\n{out[:3500]}\n```" if out.strip() else "❌ sem dados")


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    v = await _run_cli("--version", timeout=20)
    await update.message.reply_text(
        f"*opencode bot* `v{config.VERSION}`\n*opencode cli* `{v}`",
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
        await update.message.reply_text(f"```\n{out[:3500]}\n```" if out.strip() else "❌ sem saída", parse_mode="Markdown")
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
                "Grava o header `Authorization: Bearer <TOKEN>` no servidor MCP. Para o Todoist, pegue seu API token em Todoist → Settings → Integrations → Developer.\n\n"
                "⚠️ O token passa pelo Telegram (fica no backend deles mesmo depois que você apaga a mensagem). "
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
            r = await state._client.post(f"/mcp/{args[1]}/auth")
            url = (r.json() or {}).get("authorizationUrl")
        except Exception as e:
            await update.message.reply_text(f"❌ Falha ao iniciar OAuth: {e}")
            return
        if not url:
            await update.message.reply_text("❌ O servidor não devolveu URL de autorização.")
            return
        await update.message.reply_text(
            f"🔐 *Autorização MCP* (`{args[1]}`)\n"
            "1) Abra o link abaixo e autorize no Todoist\n"
            "2) Copie o código da sua URL de retorno e envie:\n"
            "`/mcp callback <nome> <código>`\n\n"
            f"{url}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    if sub in ("callback", "code"):
        if len(args) < 3:
            await update.message.reply_text(f"Uso: `/mcp {sub} <nome> <código>`", parse_mode="Markdown")
            return
        try:
            r = await state._client.post(f"/mcp/{args[1]}/auth/callback", json={"code": args[2]})
            await update.message.reply_text(f"🟢 OAuth concluído: `{json.dumps(r.json(), ensure_ascii=False)[:400]}`")
        except Exception as e:
            await update.message.reply_text(f"❌ Falha no callback: {e}")
        return

    if sub in ("logout", "signout"):
        if len(args) < 2:
            await update.message.reply_text(f"Uso: `/mcp {sub} <nome>`", parse_mode="Markdown")
            return
        try:
            await state._client.request("DELETE", f"/mcp/{args[1]}/auth")
            await update.message.reply_text("✅ Credenciais OAuth removidas.")
        except Exception as e:
            await update.message.reply_text(f"❌ Falha: {e}")
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
            await state._client.post(f"/mcp/{args[1]}/{sub}")
            await update.message.reply_text(f"✅ `{args[1]}` {sub}.")
        except Exception as e:
            await update.message.reply_text(f"❌ Falha: {e}")
        return

    await update.message.reply_text(
        "Subcomandos de `/mcp`:\n"
        "  `/mcp` — lista servidores\n"
        "  `/mcp add <nome> --url <url>` — adiciona remoto\n"
        "  `/mcp token <nome> <TOKEN>` — define token Bearer\n"
        "  `/mcp remove <nome>` — remove servidor\n"
        "  `/mcp auth <nome>` — inicia OAuth\n"
        "  `/mcp callback <nome> <código>` — conclui OAuth\n"
        "  `/mcp logout <nome>` — remove credenciais\n"
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
    await update.message.reply_text(f"🔄 *Nova conversa iniciada* (sessão `{sid[-6:]}`).", parse_mode="Markdown")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    chat_id = update.effective_chat.id
    turn = state.TURNS.get(chat_id)
    if not turn:
        await _reply_or_edit(update, "Nada em andamento.")
        return
    await oc_abort(turn["sid"])
    turn["out_text"] = "⛔ *Interrompido pelo dono.*"
    await _finish_turn(chat_id)


async def _ensure_server_bg(chat_id: int):
    """Sobe o servidor opencode em background e avisa o chat do resultado."""
    app = state._app_ref
    try:
        await oc_ensure_server()
    except Exception as e:
        logger.exception("Falha ao inicializar o servidor via /status")
        if app:
            await _safe_send_message(app.bot, chat_id, f"❌ *Servidor falhou ao inicializar:* `{e}`", parse_mode="Markdown")
        return
    if app:
        await _safe_send_message(app.bot, chat_id, "✅ *Servidor opencode funcionando.*", parse_mode="Markdown")


async def cmd_bateria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    if context.args:
        arg = context.args[0].lower()
        if arg in ("on", "ligar", "ativar"):
            state._battery["alerts"] = True
            await _reply_or_edit(
                update,
                "🔋 *Alertas de bateria ativados* "
                f"(aviso a cada {config.BATTERY_CHECK_INTERVAL}s se estiver abaixo de {config.BATTERY_LOW_PCT}%).",
                parse_mode="Markdown",
            )
            return
        if arg in ("off", "desligar", "desativar"):
            state._battery["alerts"] = False
            await _reply_or_edit(update, "🔋 Alertas de bateria desativados.",
                                 parse_mode="Markdown")
            return
        await _reply_or_edit(update, "Uso: `/bateria` ou `/bateria on|off`.",
                             parse_mode="Markdown")
        return
    await _reply_or_edit(update, format_bateria(read_battery()), parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reject_unauthorized(update, context)
        return
    ok = await oc_server_ok()
    if not ok:
        asyncio.create_task(_ensure_server_bg(update.effective_chat.id))
        await _reply_or_edit(
            update,
            "🔁 *Servidor está sendo inicializado...*",
            parse_mode="Markdown",
        )
        return
    busy = sum(1 for t in state.TURNS.values() if t["busy"])
    chat_id = update.effective_chat.id
    chat_cfg = context.bot_data.setdefault("chats", {}).setdefault(chat_id, {})
    model = chat_cfg.get("model")
    agent = chat_cfg.get("agent")
    sid = chat_cfg.get("sid")
    lines = [
        "*Servidor opencode:* ✅ funcionando",
        f"*Sessões em uso:* {len(state.TURNS)}",
        f"*Em processamento:* {busy}",
        f"*Diretório:* `{config.OPENCODE_DIR}`",
    ]
    bat = read_battery()
    bat_pct = bat.get("capacity") or "?"
    bat_st = _BAT_STATUS_PT.get(bat.get("status") or "", bat.get("status") or "?")
    lines.append(f"*Bateria:* `{bat_pct}% {bat_st}`")
    if sid:
        lines.append(f"*Sessão:* `{sid}`")
    if model:
        lines.append(f"*Modelo:* `{model['providerID']}/{model['modelID']}`")
    if agent:
        lines.append(f"*Agente:* `{agent}`")
    lines.append("_Use /sessions para ver as sessões do servidor._")
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
        "⚠️ *Reiniciar o quê?*\n\n"
        "Respostas em andamento serão interrompidas.",
        parse_mode="Markdown",
        reply_markup=_kb_restart(),
    )
