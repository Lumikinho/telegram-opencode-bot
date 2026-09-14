"""Restauração de sessão no boot: mantém a anterior, senão a mais recente, senão None."""
from bot.opencode import pick_session_id


def _s(sid, updated):
    return {"id": sid, "time": {"updated": updated}}


def test_mantem_anterior_se_existe():
    sessions = [_s("aaa", 3), _s("bbb", 9)]
    assert pick_session_id("aaa", sessions) == "aaa"


def test_cai_para_mais_recente():
    sessions = [_s("aaa", 3), _s("bbb", 9), _s("ccc", 5)]
    assert pick_session_id("zzz-sumiu", sessions) == "bbb"


def test_sem_anterior_pega_mais_recente():
    sessions = [_s("aaa", 3), _s("bbb", 9)]
    assert pick_session_id(None, sessions) == "bbb"


def test_sem_sessoes_retorna_none():
    assert pick_session_id("aaa", []) is None
    assert pick_session_id(None, []) is None
