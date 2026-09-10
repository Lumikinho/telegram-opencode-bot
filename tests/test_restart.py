"""Testes do /restart (mapa de alvos) e do registro de handlers."""
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from bot.handlers import register_handlers
from bot.render import _RESTART_ALIASES, _RESTART_LABELS


def test_restart_aliases_validos():
    assert set(_RESTART_ALIASES.values()) <= {"bot", "server", "both"}
    assert _RESTART_ALIASES["bot"] == "bot"
    assert _RESTART_ALIASES["servidor"] == "server"
    assert _RESTART_ALIASES["ambos"] == "both"
    assert set(_RESTART_LABELS) == {"bot", "server", "both"}


class _FakeApp:
    def __init__(self):
        self.added = []

    def add_handler(self, handler, group=0):
        self.added.append(handler)


def test_register_handlers_cobre_comandos():
    app = _FakeApp()
    register_handlers(app)
    cmds = set()
    n_cb = n_msg = 0
    for h in app.added:
        if isinstance(h, CommandHandler):
            cmds.update(h.commands)
        elif isinstance(h, CallbackQueryHandler):
            n_cb += 1
        elif isinstance(h, MessageHandler):
            n_msg += 1
    for esperado in ("start", "help", "new", "cancel", "status", "restart",
                     "bateria", "models", "agents", "sessions", "mcp",
                     "version", "summarize", "stats"):
        assert esperado in cmds, esperado
    assert n_cb >= 4 and n_msg >= 2
