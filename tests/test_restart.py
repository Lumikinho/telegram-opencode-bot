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
                     "bateria", "funnel", "unfunnel", "models", "agents",
                     "sessions", "mcp", "version", "summarize", "stats"):
        assert esperado in cmds, esperado
    assert n_cb >= 4 and n_msg >= 2


def test_format_signal_trace_contem_pai_e_stack(tmp_path):
    import inspect
    from bot.restart import format_signal_trace
    frame = inspect.currentframe()
    out = format_signal_trace(15, frame)
    assert "SIG=15" in out and "PPID_CMDLINE=" in out and "STACK:" in out


def test_install_signal_tracer_envolve_so_term_int(tmp_path):
    import inspect
    import signal
    from bot.restart import install_signal_tracer
    real = signal.signal
    seen = {}
    def fake(signum, handler):
        seen[signum] = handler
        return handler
    signal.signal = fake
    try:
        install_signal_tracer(tmp_path / "signals.log")
        assert signal.signal is not fake  # foi envelopado

        def inner_term(*a):
            return "term"

        def inner_usr(*a):
            return "usr"

        signal.signal(15, inner_term)
        signal.signal(10, inner_usr)
        # SIGTERM foi envelopado, SIGUSR1 passou direto
        assert seen[15] is not inner_term
        assert seen[10] is inner_usr
        log = tmp_path / "signals.log"
        seen[15](15, inspect.currentframe())
        assert log.exists() and "SIG=15" in log.read_text()
    finally:
        signal.signal = real
