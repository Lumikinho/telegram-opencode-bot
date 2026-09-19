#!/usr/bin/env python3
"""Worker de render: markdown do bot -> HTML seguro do Telegram.

stdin:  {"text": "..."}
stdout: {"html": "..."}
"""
import html
import json
import re
import sys

_CODE_RE = re.compile(r"```(\w*)\n?(.*?)```", re.DOTALL)
_INLINE_RE = re.compile(r"`([^`\n]+)`")


def markdown_to_html(text: str) -> str:
    parts: list = []
    pos = 0
    for m in _CODE_RE.finditer(text or ""):
        parts.append(html.escape((text[pos:m.start()])))
        lang = (m.group(1) or "").strip()
        code = html.escape(m.group(2).rstrip())
        parts.append(f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>' if lang else f"<pre>{code}</pre>")
        pos = m.end()
    parts.append(html.escape((text[pos:] if text else "")))
    out = "".join(parts)
    out = _INLINE_RE.sub(lambda m: f"<code>{html.escape(m.group(1))}</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.DOTALL)
    return out


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    json.dump({"html": markdown_to_html(payload.get("text") or "")},
              sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
