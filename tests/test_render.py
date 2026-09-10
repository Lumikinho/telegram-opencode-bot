"""Testes das funcoes puras de renderizacao Telegram."""
from bot.opencode import _strip_ansi
from bot.render import (
    _escape_html_text,
    _plain_text,
    _redact_secrets,
    _split_text,
    _telegram_html,
)


def test_strip_ansi():
    assert _strip_ansi("\x1b[32mok\x1b[0m") == "ok"


def test_escape_html_text():
    assert _escape_html_text("<b>&") == "&lt;b&gt;&amp;"


def test_telegram_html_bold_and_code():
    out = _telegram_html("**oi** `x`")
    assert "<b>oi</b>" in out
    assert "<code>x</code>" in out


def test_plain_text_strips_tags():
    assert _plain_text("<b>oi</b>") == "oi"


def test_redact_secrets_masks_token():
    token = "ghp_" + "a1b2" * 9
    out = _redact_secrets(f"Authorization Bearer {token} fim")
    assert token not in out


def test_split_text_splits_long():
    lines = ["x" * 100 for _ in range(50)]
    chunks = _split_text("\n".join(lines), limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)


def test_split_text_short_passthrough():
    assert _split_text("curto") == ["curto"]
