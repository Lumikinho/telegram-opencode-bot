"""Anexo a sessões em andamento (attach_turn + backfill da resposta)."""
import asyncio
from types import SimpleNamespace
from urllib.parse import urlencode

from bot import state
from bot.opencode import oc_active_sessions, oc_last_assistant_text
from bot.turns import _finish_turn, attach_turn


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, routes):
        self.routes = routes

    async def get(self, path, params=None):
        key = path
        if params:
            key += "?" + urlencode(sorted(params.items()))
        if key not in self.routes:
            raise RuntimeError(f"GET inesperado: {key}")
        return self.routes[key]

    async def aclose(self):
        pass


class _Bot:
    def __init__(self):
        self.sent = []
        self._next_id = 100

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self._next_id += 1
        self.sent.append(text)
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None):
        self.sent.append(("edit", message_id, text))
        return True

    async def delete_message(self, chat_id, message_id):
        return True

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        return True

    async def send_chat_action(self, chat_id, action):
        return True


def _setup(client):
    state._client = client
    bot = _Bot()
    state._app_ref = SimpleNamespace(bot=bot)
    state.TURNS.clear()
    return bot


def _teardown():
    for turn in list(state.TURNS.values()):
        for key in ("typing_task", "stream_task"):
            task = turn.get(key)
            if task is not None:
                task.cancel()
    state.TURNS.clear()
    state._client = None
    state._app_ref = None


def test_active_sessions_parse():
    _setup(_Client({"/api/session/active": _Resp({"data": {"ses_1": {"type": "running"}}}),
                    "/api/session/ses_1/message?limit=50&order=desc": _Resp({"data": []})}))
    try:
        assert asyncio.run(oc_active_sessions()) == {"ses_1": "running"}
    finally:
        _teardown()


def test_active_sessions_falha_retorna_vazio():
    _setup(_Client({}))
    try:
        assert asyncio.run(oc_active_sessions()) == {}
    finally:
        _teardown()


def test_last_assistant_text_pega_ultimo_texto():
    msgs = {"data": [
        {"type": "user", "text": "oi"},
        {"type": "assistant", "content": [{"type": "tool", "tool": "read"}]},
        {"type": "assistant", "content": [{"type": "text", "text": "resposta final"}]},
    ]}
    _setup(_Client({"/api/session/active": _Resp({"data": {}}),
                    "/api/session/ses_1/message?limit=50&order=desc": _Resp(msgs)}))
    try:
        assert asyncio.run(oc_last_assistant_text("ses_1")) == "resposta final"
    finally:
        _teardown()


def test_attach_so_quando_ativa():
    bot = _setup(_Client({"/api/session/active": _Resp({"data": {}}),
                          "/api/session/ses_1/message?limit=50&order=desc": _Resp({"data": []})}))
    try:
        assert asyncio.run(attach_turn(7, "ses_1")) is False
        assert 7 not in state.TURNS
        assert bot.sent == []
    finally:
        _teardown()


def test_attach_cria_turno_e_finaliza_com_backfill():
    msgs = {"data": [{"type": "assistant",
                      "content": [{"type": "text", "text": "resposta que já estava lá"}]}]}
    bot = _setup(_Client({"/api/session/active": _Resp({"data": {"ses_9": {"type": "running"}}}),
                          "/api/session/ses_9/message?limit=50&order=desc": _Resp(msgs)}))
    try:
        assert asyncio.run(attach_turn(7, "ses_9")) is True
        turn = state.TURNS[7]
        assert turn["sid"] == "ses_9" and turn["attached"] is True
        assert any("em andamento" in (m if isinstance(m, str) else "") for m in bot.sent)
        # Sem deltas capturados: o finish usa o backfill persistido.
        asyncio.run(_finish_turn(7))
        assert 7 not in state.TURNS
        assert any("resposta que já estava lá" in (m if isinstance(m, str) else m[2] if isinstance(m, tuple) else "") for m in bot.sent)
    finally:
        _teardown()


def test_attach_recusa_com_turno_ativo():
    _setup(_Client({"/api/session/active": _Resp({"data": {"ses_9": {"type": "running"}}}),
                    "/api/session/ses_9/message?limit=50&order=desc": _Resp({"data": []})}))
    try:
        state.TURNS[7] = {"sid": "ses_old"}
        assert asyncio.run(attach_turn(7, "ses_9")) is False
        assert state.TURNS[7]["sid"] == "ses_old"
    finally:
        _teardown()
