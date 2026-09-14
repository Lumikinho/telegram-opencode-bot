"""Testes do controle do funnel Tailscale."""
from bot.funnel import FUNNEL_TARGETS, parse_active_funnels


def test_targets_preservam_rotas():
    paths = dict(FUNNEL_TARGETS)
    assert paths["/"] == "http://127.0.0.1:8888"
    assert paths["/ai"] == "http://127.0.0.1:8081"


def test_parse_vazio():
    assert parse_active_funnels("") == []
    assert parse_active_funnels("# Funnel on:\n#     - https://x.ts.net") == []


def test_parse_agrupa_mapeamentos():
    out = (
        "https://server.tail380a9e.ts.net (Funnel on)\n"
        "|-- / proxy http://127.0.0.1:8888\n"
        "|-- /ai proxy http://127.0.0.1:8081\n"
    )
    funnels = parse_active_funnels(out)
    assert len(funnels) == 1
    assert funnels[0]["on"] is True
    assert funnels[0]["mappings"] == [
        "/ proxy http://127.0.0.1:8888",
        "/ai proxy http://127.0.0.1:8081",
    ]


def test_parse_multiplos():
    out = "https://a.ts.net (Funnel on)\nhttps://b.ts.net (Funnel off)\n"
    funnels = parse_active_funnels(out)
    assert [f["on"] for f in funnels] == [True, False]
