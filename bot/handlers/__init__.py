"""Registro central de handlers (comandos, callbacks, mensagens)."""

from telegram.ext import Application
from telegram.ext import CallbackQueryHandler
from telegram.ext import CommandHandler
from telegram.ext import MessageHandler
from telegram.ext import filters
from .callbacks import cb_command, cb_permission, cb_question, cb_reply
from .chat import handle_chat, handle_media
from .commands import cmd_agents, cmd_bateria, cmd_cancel, cmd_help, cmd_mcp, cmd_models, cmd_new, cmd_restart, cmd_sessions, cmd_start, cmd_stats, cmd_status, cmd_summarize, cmd_version


def register_handlers(app: Application) -> None:
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
    app.add_handler(CommandHandler("bateria", cmd_bateria))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CallbackQueryHandler(cb_permission, pattern=r"^perm:"))
    app.add_handler(CallbackQueryHandler(cb_question, pattern=r"^q[ostcr]:"))
    app.add_handler(CallbackQueryHandler(cb_reply, pattern=r"^mod:"))
    app.add_handler(CallbackQueryHandler(cb_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))
    app.add_handler(MessageHandler(filters.ATTACHMENT, handle_media))
