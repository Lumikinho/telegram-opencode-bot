"""Cascata de health e backfill de cauda (aprendizados do fonte do servidor)."""
from bot.opencode import HEALTH_PATHS, latest_assistant_text


def _assistant(*texts):
    return {
        "type": "assistant",
        "content": [{"type": "text", "text": t} for t in texts],
    }


def test_health_paths_ordem():
    assert HEALTH_PATHS == ("/api/health", "/global/health", "/api/status")


def test_latest_pega_primeiro_da_pagina_desc():
    # página desc = mais nova primeiro; o primeiro assistant da lista é o latest.
    desc = [_assistant("novo"), {"type": "user", "text": "oi"}, _assistant("velho")]
    assert latest_assistant_text(desc) == "novo"


def test_latest_pula_vazios_e_nao_assistant():
    msgs = [
        {"type": "assistant", "content": [{"type": "text", "text": "   "}]},
        {"type": "assistant", "content": [{"type": "tool", "tool": "read"}]},
        _assistant("achou"),
    ]
    assert latest_assistant_text(msgs) == "achou"


def test_latest_sem_assistant_retorna_vazio():
    assert latest_assistant_text([{"type": "user", "text": "oi"}]) == ""
    assert latest_assistant_text([]) == ""
    assert latest_assistant_text(None) == ""


def test_latest_respeita_limit():
    assert latest_assistant_text([_assistant("abcdefgh")], limit=4) == "abcd"
