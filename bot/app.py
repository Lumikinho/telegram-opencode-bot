"""Bootstrap: post_init/post_shutdown/main."""
from pathlib import Path
import asyncio
import logging
from telegram import BotCommand
from telegram.ext import Application, PicklePersistence
from telegram.request import HTTPXRequest
from . import config, state
from .auth import _get_owner_chat
from .battery import _battery_watch
from .config import BOT_TOKEN
from .handlers import register_handlers
from .opencode import oc_ensure_server, oc_list_sessions, oc_restore_chat_session, oc_stop_server
from .render import _safe_send_message
from .turns import _consume_events, _stop_stream, _stop_typing


logger = logging.getLogger(__name__)


async def post_init(app: Application):
    state._app_ref = app
    await oc_ensure_server()
    if state._stream_task is None:
        state._stream_task = asyncio.create_task(_consume_events())
    if state._battery_task is None:
        state._battery_task = asyncio.create_task(_battery_watch())
    await app.bot.set_my_commands([
        BotCommand("menu", "Painel com botões"),
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
        BotCommand("bateria", "Bateria e alertas de nível baixo"),
        BotCommand("funnel", "Liga/desliga o funnel Tailscale"),
        BotCommand("restart", "Reiniciar bot / servidor"),
    ])
    chat_id = _get_owner_chat()
    if chat_id:
        chats = app.bot_data.setdefault("chats", {})
        chats.setdefault(chat_id, {})
        sessions = await oc_list_sessions()
        lines = []
        for cid, cfg in chats.items():
            sid, created = await oc_restore_chat_session(cfg, sessions)
            if not sid:
                continue
            verbo = "criada" if created else "retomada"
            lines.append(f"🔌 conectado a sessão ({verbo}): `{sid}`")
        text = "✅ *opencode bot online v" + config.VERSION + "* — conectado ao servidor."
        if lines:
            text += "\n" + "\n".join(lines)
        await _safe_send_message(app.bot, chat_id, text, parse_mode="Markdown")


async def post_shutdown(app: Application):
    """Cleans up the background event stream task and, if we spawned the
    opencode server ourselves, terminates it instead of leaving it orphaned."""
    if state._battery_task:
        state._battery_task.cancel()
        try:
            await state._battery_task
        except (asyncio.CancelledError, Exception):
            pass
        state._battery_task = None
    if state._stream_task:
        state._stream_task.cancel()
        try:
            await state._stream_task
        except (asyncio.CancelledError, Exception):
            pass
        state._stream_task = None
    for turn in list(state.TURNS.values()):
        _stop_typing(turn)
        _stop_stream(turn)
    await oc_stop_server()
    if state._client:
        await state._client.aclose()


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN não configurado. Crie um .env com BOT_TOKEN=seu_token")
        return
    request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=10)
    persistence = PicklePersistence(filepath=str(Path(config.OPENCODE_DIR) / ".opencode_bot_persistence.pkl"))
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .persistence(persistence)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    register_handlers(app)
    logger.info("=" * 44)
    logger.info("  Telegram opencode bot  v%s", config.VERSION)
    logger.info("  url=%s  dir=%s", config.OC_URL, config.OPENCODE_DIR)
    logger.info("=" * 44)
    logger.info("Iniciando opencode bot...")
    app.run_polling(drop_pending_updates=True)
