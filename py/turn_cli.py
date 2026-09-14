#!/usr/bin/env python3
"""Worker de turnos: dobra de eventos v2 + render, sem Telegram.

É o port puro de `bot/turns.py` (dobra de eventos) e das funções puras de
`bot/render.py` (teclados como JSON, markdown->HTML, redact). Stateless:
o gateway Bun guarda o dict do turno e o passa a cada chamada.

stdin:  {"action": ..., ...}
stdout: JSON com o resultado (turno serializado de volta quando aplicável).

Ações:
  new_turn      {chat_id} -> {turn}
  fold          {turn, event} -> {turn, action, detail}
                  action: none | push | push_force | finish
  select_option {turn, request_id, qidx, opt} -> {turn, changed}
  submit_form   {turn, request_id} -> {turn, complete, answer, missing}
  drop_form     {turn, request_id} -> {turn}
  render_running {turn, opencode_dir} -> {text, keyboard}
  render_think  {turn, elapsed} -> {text}
  render_result {turn} -> {text}
  telegram_html {text, max_len} -> {html}
  plain_text    {html} -> {text}
  split         {text, limit} -> {chunks}
  redact        {text} -> {text}
"""
import html
import json
import re
import sys
import time
from pathlib import Path

TOOL_ICONS = {
    "read": "📖",
    "write": "➕",
    "edit": "✏️",
    "bash": "⚡",
    "glob": "🔍",
    "grep": "🔍",
    "mcp": "🤖",
    "todo": "📋",
}

STREAM_MIN = 150

_SET_FIELDS = ("reads", "writes", "edits", "process_seen", "reasoning_part_ids")


def _ser_turn(turn: dict) -> dict:
    out = dict(turn)
    for f in _SET_FIELDS:
        out[f] = sorted(turn.get(f) or [])
    out["perm_queue"] = list(turn.get("perm_queue") or [])
    qsel = {}
    for (rid, qi), sel in (turn.get("qsel") or {}).items():
        qsel[f"{rid}\x1f{qi}"] = sorted(sel)
    out["qsel"] = qsel
    if turn.get("awaiting_custom") is not None:
        out["awaiting_custom"] = list(turn["awaiting_custom"])
    return out


def _deser_turn(d: dict) -> dict:
    turn = dict(d)
    for f in _SET_FIELDS:
        turn[f] = set(d.get(f) or [])
    turn["perm_queue"] = list(d.get("perm_queue") or [])
    qsel = {}
    for k, sel in (d.get("qsel") or {}).items():
        if "\x1f" in k:
            rid, qi = k.split("\x1f", 1)
            qsel[(rid, int(qi))] = set(sel)
    turn["qsel"] = qsel
    if d.get("awaiting_custom") is not None:
        turn["awaiting_custom"] = tuple(d["awaiting_custom"])
    return turn


def new_turn(chat_id: int) -> dict:
    return {
        "chat_id": chat_id,
        "sid": None,
        "status_msg_id": None,
        "result_msg_id": None,
        "result_last": "",
        "started": time.monotonic(),
        "last_edit": 0.0,
        "busy": True,
        "done": False,
        "todo": 0,
        "todos": [],
        "reads": set(),
        "writes": set(),
        "edits": set(),
        "rejected": 0,
        "process": [],
        "process_seen": set(),
        "current": None,
        "out_text": "",
        "reasoning_part_ids": set(),
        "user_msg_id": None,
        "streamed_len": 0,
        "perm_queue": [],
        "questions": [],
        "qsel": {},
        "awaiting_custom": None,
        "tool_names": {},
    }


def _cmd_of_part(state: dict) -> str:
    inp = state.get("input") or {}
    cmd = inp.get("command")
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(c) for c in cmd)
    return str(cmd or "")


def _record_tool(turn: dict, part: dict):
    tool = part.get("tool")
    state = part.get("state", {})
    status = state.get("status")
    if status not in ("running", "completed", "error"):
        return
    part_id = part.get("id")
    path = (state.get("input") or {}).get("filePath")

    if tool in ("bash", "shell"):
        cmd = _cmd_of_part(state)
        if status == "running":
            turn["current"] = {"cmd": cmd or "comando", "out": state.get("output") or ""}
            return
        if status in ("completed", "error"):
            if part_id:
                if part_id in turn["process_seen"]:
                    return
                turn["process_seen"].add(part_id)
            turn["process"].append({
                "cmd": cmd or "comando",
                "out": state.get("output") or "",
                "status": status,
            })
            turn["current"] = None
            if status == "error" and "permission" in (state.get("error") or "").lower():
                turn["rejected"] += 1
            return

    if tool == "read":
        if status == "running":
            turn["current"] = {"label": f"📖 lendo `{fmt_path(path, turn.get('_opencode_dir') or '')}`" if path else "📖 lendo arquivo", "out": ""}
        elif status in ("completed", "error") and path:
            turn["reads"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    elif tool == "write":
        if status == "running":
            turn["current"] = {"label": f"➕ criando `{fmt_path(path, turn.get('_opencode_dir') or '')}`" if path else "➕ criando arquivo", "out": ""}
        elif status in ("completed", "error") and path:
            turn["writes"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    elif tool == "edit":
        if status == "running":
            turn["current"] = {"label": f"✏️ editando `{fmt_path(path, turn.get('_opencode_dir') or '')}`" if path else "✏️ editando arquivo", "out": ""}
        elif status in ("completed", "error") and path:
            turn["edits"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    else:
        title = state.get("title") or ""
        label = f"{TOOL_ICONS.get(tool, '⚙️')} {tool}" + (f": `{title}`" if title else "")
        if status == "error" and "permission" in (state.get("error") or "").lower():
            turn["rejected"] += 1
            label = "❌ permissão negada"
        turn["current"] = {"label": label, "out": ""} if status == "running" else None


def add_form_questions(turn: dict, form: dict):
    fid = form.get("id") or ""
    sid = form.get("sessionID") or turn.get("sid")
    fields = form.get("fields") or []
    title = form.get("title") or ""
    for idx, f in enumerate(fields):
        ftype = f.get("type") or "string"
        fname = f.get("name") or f.get("title") or f"field_{idx}"
        flabel = f.get("title") or fname
        fdesc = f.get("description") or ""
        question = f"{flabel}\n{fdesc}".strip() if fdesc else (flabel or "Responda:")
        options: list = []
        multiple = False
        custom = False
        if ftype == "multiselect":
            multiple = True
            for o in f.get("options") or []:
                options.append({"label": o.get("label") or o.get("value"), "value": o.get("value", o.get("label"))})
        elif ftype == "boolean":
            options = [{"label": "Sim", "value": True}, {"label": "Não", "value": False}]
        elif isinstance(f.get("options"), list) and f.get("options"):
            for o in f["options"]:
                options.append({"label": o.get("label") or o.get("value"), "value": o.get("value", o.get("label"))})
            custom = bool(f.get("custom", False))
        else:
            custom = True
        if ftype == "external" and f.get("url"):
            question = f"{question}\n{f['url']}".strip()
            custom = True
        turn["questions"].append({
            "request_id": fid,
            "sid": sid,
            "qidx": idx,
            "qlen": len(fields),
            "header": title,
            "question": question,
            "options": options,
            "multiple": multiple,
            "custom": custom,
            "answer": None,
            "field": fname,
            "ftype": ftype,
        })


def drop_questions(turn: dict, request_id: str):
    rid = request_id or ""
    turn["questions"] = [q for q in turn["questions"] if q["request_id"] != rid]
    turn["qsel"] = {k: v for k, v in turn["qsel"].items() if k[0] != rid}


def fold_event(turn: dict, event: dict) -> dict:
    """Dobra um evento v2 no turno. Retorna {"action", "detail"}.

    action: none (só estado) | push (status, com throttle) |
            push_force (status imediato) | finish (turno encerrou).
    """
    et = event.get("type")
    data = event.get("data") or event.get("properties") or {}

    if et == "permission.asked":
        turn["perm_queue"].append({
            "id": data.get("id"),
            "sid": turn.get("sid"),
            "permission": data.get("action") or "permissão",
            "title": data.get("message") or data.get("action") or "",
            "pattern": ", ".join(data.get("resources") or []),
        })
        return {"action": "push_force", "detail": None}
    if et == "permission.replied":
        rid = data.get("requestID") or data.get("id") or ""
        turn["perm_queue"] = [p for p in turn["perm_queue"] if p.get("id") != rid]
        return {"action": "push_force", "detail": None}
    if et == "form.created":
        add_form_questions(turn, data.get("form") or {})
        return {"action": "push_force", "detail": None}
    if et in ("form.cancelled", "form.replied"):
        drop_questions(turn, (data.get("form") or {}).get("id") or data.get("id") or "")
        return {"action": "push_force", "detail": None}
    if et == "session.status":
        stype = (data.get("status") or {}).get("type")
        if stype == "busy":
            turn["busy"] = True
            return {"action": "none", "detail": None}
        if stype == "idle":
            return {"action": "finish", "detail": {"reason": "idle"}}
        return {"action": "none", "detail": None}
    if et == "session.idle":
        return {"action": "finish", "detail": {"reason": "idle"}}
    if et in ("session.execution.succeeded", "session.execution.interrupted"):
        return {"action": "finish",
                "detail": {"reason": "succeeded" if et.endswith("succeeded") else "interrupted"}}
    if et == "session.execution.failed":
        err = data.get("error") or data.get("message") or "desconhecido"
        turn["out_text"] = f"❌ *Erro no opencode:* {err}"
        return {"action": "finish", "detail": {"reason": "failed", "error": str(err)}}
    if et == "session.error":
        err = data.get("error") or data.get("message") or "desconhecido"
        turn["out_text"] = f"❌ *Erro no opencode:* {err}"
        return {"action": "finish", "detail": {"reason": "error", "error": str(err)}}
    if et == "session.text.delta":
        turn["out_text"] += data.get("delta", "")
        return {"action": "none", "detail": None}
    if et in ("session.reasoning.delta", "session.tool.progress"):
        return {"action": "none", "detail": None}
    if et == "session.tool.called":
        name = data.get("name") or "ferramenta"
        if data.get("id"):
            turn["tool_names"][data["id"]] = {"name": name, "input": data.get("input") or {}}
        _record_tool(turn, {
            "tool": name,
            "id": data.get("id"),
            "state": {"status": "running", "input": data.get("input") or {}},
        })
        return {"action": "push", "detail": None}
    if et in ("session.tool.success", "session.tool.failed"):
        seen = turn["tool_names"].pop(data.get("id") or "", None) or {}
        name = seen.get("name") or "ferramenta"
        status = "completed" if et == "session.tool.success" else "error"
        if status == "error" and "permission" in str(data.get("error") or "").lower():
            turn["rejected"] += 1
        _record_tool(turn, {
            "tool": name,
            "id": data.get("id"),
            "state": {"status": status, "input": seen.get("input") or {}, "output": ""},
        })
        return {"action": "push", "detail": None}
    if et == "file.edited":
        f = data.get("file")
        if f:
            turn["edits"].add(f)
            return {"action": "push", "detail": None}
        return {"action": "none", "detail": None}
    return {"action": "none", "detail": None}


def build_form_answer(turn: dict, request_id: str) -> dict:
    """Monta {complete, answer} para POST .../form/{id}/reply (port de _submit_question)."""
    items = sorted(
        (q for q in turn["questions"] if q["request_id"] == request_id),
        key=lambda q: q["qidx"],
    )
    if not items:
        return {"complete": True, "answer": {}, "missing": []}
    missing = [q["qidx"] for q in items if not q.get("answer")]
    if missing:
        return {"complete": False, "answer": {}, "missing": missing}
    form_answer: dict = {}
    for q in items:
        ftype = q.get("ftype") or "string"
        a = q.get("answer") or []
        if ftype == "multiselect":
            form_answer[q["field"]] = list(a)
            continue
        val = a[0] if a else None
        if ftype == "boolean":
            form_answer[q["field"]] = bool(val) if not isinstance(val, bool) else val
        elif ftype == "number":
            try:
                form_answer[q["field"]] = float(val)
            except (TypeError, ValueError):
                form_answer[q["field"]] = val
        elif ftype == "integer":
            try:
                form_answer[q["field"]] = int(val)
            except (TypeError, ValueError):
                form_answer[q["field"]] = val
        else:
            form_answer[q["field"]] = val
    return {"complete": True, "answer": form_answer, "missing": []}


# ---- render (port puro de bot/render.py) ----

def fmt_path(path: str | None, opencode_dir: str) -> str:
    if not path:
        return "(?)"
    try:
        return str(Path(path).relative_to(Path(opencode_dir or Path.home())))
    except Exception:
        return path


_SECRET_PATTERNS = [
    re.compile(r"(?i)\b((?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|password|passwd|token)\s*[:=]\s*['\"]?)([A-Za-z0-9\-_\.]{6,})"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-_\.]{10,})"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9\-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
]
_MASKED = "███MASKED███"
_SECRET_HEX = re.compile(r"\b[0-9A-Fa-f]{32,64}\b")
_SECRET_KW = re.compile(r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|secret|passwd|password|steam|token)", re.I)


def redact_secrets(s: str) -> str:
    if not s:
        return s
    out = s
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(1) + _MASKED, out)
        else:
            out = pat.sub(_MASKED, out)

    def _sub(m):
        window = out[max(0, m.start() - 60):m.start()]
        return _MASKED if _SECRET_KW.search(window) else m.group(0)

    return _SECRET_HEX.sub(_sub, out)


def tail_out(s: str, n: int = 450) -> str:
    s = redact_secrets((s or "").strip().replace("```", "'''"))
    if not s:
        return ""
    if len(s) > n:
        return "…" + s[-n:]
    return s


def btn_label(s: str, n: int = 40) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def perm_desc(p: dict) -> str:
    pat = p.get("pattern")
    pat_str = ", ".join(pat) if isinstance(pat, list) else (pat or "")
    title = p.get("title") or p.get("permission") or "permissão"
    if pat_str:
        return f"{title}: `{pat_str}`"
    return f"`{title}`"


def code_span(s: str) -> str:
    return "`" + (s or "").replace("`", "'") + "`"


def todo_icon(status: str) -> str:
    s = (status or "").lower().replace("-", "_")
    if s == "completed":
        return "✅"
    if s == "in_progress":
        return "🔄"
    return "⬜"


def todo_lines(todos: list, max_items: int = 20) -> list:
    items = [t for t in (todos or []) if (t.get("status") or "") != "cancelled"][:max_items]
    if not items:
        return []
    lines = ["📋 *To-do's:*"]
    for t in items:
        content = (t.get("content") or "").strip().replace("\n", " ")
        if len(content) > 80:
            content = content[:79] + "…"
        lines.append(f"{todo_icon(t.get('status') or '')} {content}")
    return lines


def q_lines(turn: dict) -> list:
    lines: list = []
    first = True
    for q in turn["questions"]:
        sel = set(turn["qsel"].get((q["request_id"], q["qidx"])) or ())
        if first:
            lines += ["", "❓ *Escolha do opencode:*"]
            first = False
        if q.get("header"):
            lines.append(f"*— {q['header']} —*")
        lines.append(q["question"] or "Selecione uma opção:")
        for i, opt in enumerate(q["options"]):
            mark = "✅" if i in sel else f"{i + 1}."
            line = f"{mark} {opt.get('label')}"
            if opt.get("description"):
                line += f" — {opt['description']}"
            lines.append(line)
        if q.get("multiple"):
            lines.append("_Toque para alternar e envie para confirmar._")
    return lines


def q_keyboard(turn: dict) -> list:
    """Teclado de perguntas/permissões como [[{text, data}]]."""
    rows: list = []
    for q in turn["questions"]:
        rid, qi = q["request_id"], q["qidx"]
        sel = set(turn["qsel"].get((rid, qi)) or ())
        if q.get("multiple"):
            for i, opt in enumerate(q["options"]):
                prefix = "✅ " if i in sel else ""
                rows.append([{"text": prefix + btn_label(opt.get("label")), "data": f"qt:{rid}:{qi}:{i}"}])
            rows.append([
                {"text": "✅ Enviar", "data": f"qs:{rid}"},
                {"text": "❌ Rejeitar", "data": f"qr:{rid}"},
            ])
        else:
            for i, opt in enumerate(q["options"]):
                rows.append([{"text": btn_label(opt.get("label")), "data": f"qo:{rid}:{qi}:{i}"}])
            row: list = []
            if q.get("custom"):
                row.append({"text": "✏️ Digitar resposta", "data": f"qc:{rid}:{qi}"})
            row.append({"text": "❌ Rejeitar", "data": f"qr:{rid}"})
            rows.append(row)
    if turn["perm_queue"]:
        p = turn["perm_queue"][0]
        rows.append([
            {"text": "✅ Uma vez", "data": f"perm:{p['id']}:once"},
            {"text": "🔁 Sempre", "data": f"perm:{p['id']}:always"},
            {"text": "❌ Negar", "data": f"perm:{p['id']}:reject"},
        ])
    return rows


def render_running(turn: dict, opencode_dir: str) -> dict:
    turn = dict(turn)
    turn["_opencode_dir"] = opencode_dir
    lines = []
    lines += todo_lines(turn.get("todos") or [])
    has_prompt = bool(turn["questions"]) or bool(turn["perm_queue"])
    if turn["questions"]:
        lines += q_lines(turn)
    if turn["perm_queue"]:
        p = turn["perm_queue"][0]
        lines += ["", "🔒 *Permissão pedida:*", perm_desc(p)]
    if not has_prompt:
        curr = turn["current"]
        if curr and curr.get("cmd"):
            lines += ["", f"⚡ *rodando:* {code_span(redact_secrets(curr['cmd']))}"]
            out = tail_out(curr.get("out"), 450)
            if out:
                lines += ["```", out, "```"]
        elif curr and curr.get("label"):
            lines += ["", curr["label"]]
        else:
            lines += ["", "⏳ *pensando…*"]
    rows = q_keyboard(turn)
    if not rows and not has_prompt:
        rows = [[{"text": "Cancelar", "data": "/cancel"}]]
    return {"text": "\n".join(lines), "keyboard": rows}


def summary_lines(turn: dict, opencode_dir: str) -> list:
    out = []
    out += todo_lines(turn.get("todos") or [])
    if turn["reads"]:
        out.append("📖 *leu:* " + ", ".join(sorted(f"`{fmt_path(x, opencode_dir)}`" for x in turn["reads"])))
    if turn["writes"]:
        out.append("➕ *criou:* " + ", ".join(sorted(f"`{fmt_path(x, opencode_dir)}`" for x in turn["writes"])))
    if turn["edits"]:
        out.append("✏️ *editou:* " + ", ".join(sorted(f"`{fmt_path(x, opencode_dir)}`" for x in turn["edits"])))
    if turn["rejected"]:
        out.append(f"❌ *negado:* {turn['rejected']} permissõe(s)")
    return out


def trace_lines(turn: dict, max_steps: int = 40, max_out: int = 500) -> list:
    proc = turn["process"]
    if not proc:
        return []
    skipped = max(0, len(proc) - max_steps)
    show = proc[-max_steps:]
    lines = ["📄 *processo:*"]
    if skipped:
        lines.append(f"({skipped} passo{'s' if skipped != 1 else ''} anterior{'is' if skipped != 1 else ''} omitido{'s' if skipped != 1 else ''})")
    for i, step in enumerate(show, 1 + skipped):
        lines += ["", f"{i}) {code_span(redact_secrets('$ ' + step['cmd']))}"]
        out = tail_out(step.get("out"), max_out)
        if out:
            lines += ["```", out, "```"]
        if step.get("status") == "error":
            lines.append("(❌ erro)")
    return lines


def render_think(turn: dict, elapsed: float, opencode_dir: str) -> str:
    mins, secs = int(elapsed) // 60, int(elapsed) % 60
    lines = [f"💭 *pensou em {mins}m{secs:02d}s:*"]
    summaries = summary_lines(turn, opencode_dir)
    if summaries:
        lines += ["", "———", *summaries]
    trace = trace_lines(turn)
    if trace:
        lines += ["", *trace]
    total = "\n".join(lines)
    if len(total) > 3950:
        total = total[:3950] + "\n…"
    return expandable_html(telegram_html(total, max_len=3950))


_MD_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+\-.]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITAL_A_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_MD_ITAL_U_RE = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+)$", re.MULTILINE)

_TAG_TOKEN_RE = re.compile(r"(</?[a-zA-Z][a-zA-Z0-9-]*(?:\s+[^<>]*?)?/?>)|([^<]+)", re.DOTALL)
_ENTITY_RE = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{1,31});")

_ALLOWED_TAGS = {
    "b": "b", "i": "i", "u": "u", "s": "s",
    "strong": "b", "em": "i", "ins": "u", "strike": "s", "del": "s",
    "code": "code", "pre": "pre", "blockquote": "blockquote",
    "p": "p", "br": "br", "a": "a", "tg-spoiler": "tg-spoiler",
}
_VOID_TAGS = {"br"}


def escape_html_text(s: str) -> str:
    out, i = [], 0
    for m in _ENTITY_RE.finditer(s):
        out.append(html.escape(s[i:m.start()], quote=False))
        out.append(m.group(0))
        i = m.end()
    out.append(html.escape(s[i:], quote=False))
    return "".join(out)


def telegram_html(text: str, max_len: int | None = None) -> str:
    def _fence(m):
        lang = m.group(1)
        cls = f' class="language-{lang}"' if lang else ""
        return f"<pre><code{cls}>{html.escape(m.group(2), quote=False)}</code></pre>"

    def _link(m):
        url = m.group(2).strip()
        if not re.match(r"^https?://", url, re.I):
            return m.group(0)
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(m.group(1), quote=False)}</a>'

    h = _MD_FENCE_RE.sub(_fence, text)
    h = _MD_INLINE_CODE_RE.sub(lambda m: f"<code>{html.escape(m.group(1), quote=False)}</code>", h)
    h = _MD_LINK_RE.sub(_link, h)
    h = _MD_STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", h)
    h = _MD_BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", h)
    h = _MD_ITAL_A_RE.sub(lambda m: f"<i>{m.group(1)}</i>", h)
    h = _MD_ITAL_U_RE.sub(lambda m: f"<i>{m.group(1)}</i>", h)
    h = _MD_HEADING_RE.sub(lambda m: f"<b>{m.group(2)}</b>", h)

    out: list = []
    stack: list = []
    clipped = False
    for m in _TAG_TOKEN_RE.finditer(h):
        tag_part, text_part = m.group(1), m.group(2)
        if text_part is not None:
            rendered = escape_html_text(text_part)
            if max_len is not None and len("".join(out)) + len(rendered) > max_len:
                out.append("…")
                clipped = True
                break
            out.append(rendered)
            continue
        tm = re.match(r"</?([a-zA-Z][a-zA-Z0-9-]*)", tag_part)
        if not tm:
            out.append(escape_html_text(tag_part))
            continue
        name = tm.group(1).lower()
        canon = _ALLOWED_TAGS.get(name)
        closing = tag_part.startswith("</")
        if canon is None:
            out.append(escape_html_text(tag_part))
            continue
        if closing:
            if canon in stack:
                while stack and stack[-1] != canon:
                    out.append(f"</{stack.pop()}>")
                if stack:
                    stack.pop()
                out.append(f"</{canon}>")
            else:
                out.append(escape_html_text(tag_part))
            continue
        rendered_tag = f"<{canon}>"
        if name == "a":
            href = re.search(r'\bhref\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag_part, re.I)
            url = (href.group(2) or href.group(3) or href.group(4)) if href else ""
            if not re.match(r"^https?://", url or "", re.I):
                out.append(escape_html_text(tag_part))
                continue
            rendered_tag = f'<a href="{html.escape(url, quote=True)}">'
        if max_len is not None and len("".join(out)) + len(rendered_tag) > max_len:
            out.append("…")
            clipped = True
            break
        out.append(rendered_tag)
        if canon not in _VOID_TAGS:
            stack.append(canon)

    for canon in reversed(stack):
        out.append(f"</{canon}>")
    result = "".join(out)
    if clipped and max_len is not None and len(result) > max_len:
        cut = result.rfind("<", 0, max_len)
        if cut > max_len - 64:
            result = result[:cut] + "…"
        else:
            result = result[:max_len].rstrip() + "…"
    return result


def plain_text(html_text: str) -> str:
    if not html_text:
        return ""
    cleaned = html.unescape(re.sub(r"<[^>]*>", "", html_text))
    return re.sub(r"</?[a-zA-Z][^>]*>", "", cleaned)


def expandable_html(html_text: str) -> str:
    return f"<blockquote expandable>{html_text}</blockquote>"


def result_text(turn: dict) -> str:
    body = (turn["out_text"] or "").strip()
    if not body:
        return "escrevendo…"
    if len(body) > 3800:
        body = "…" + body[-3800:]
    return telegram_html(body, max_len=3800)


def split_text(text: str, limit: int = 4000) -> list:
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for line in text.split("\n"):
        if buf and len(buf) + len(line) + 1 > limit:
            out.append(buf.rstrip())
            buf = line
        else:
            buf += line + "\n"
    if buf.rstrip():
        out.append(buf.rstrip())
    return out


_MEDIA_FALLBACK_MIME = {
    "document": "application/octet-stream",
    "audio": "audio/mpeg",
    "voice": "audio/ogg",
    "video": "video/mp4",
    "video_note": "video/mp4",
    "animation": "video/mp4",
    "photo": "image/jpeg",
}
MEDIA_MAX_BYTES = 20 * 1024 * 1024


def safe_filename(name: str, default: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[^\w.\-()+\[\] ]", "_", name)[:120].strip()
    return name or default


def media_note(kind: str, filename: str, size_mb: int = 0) -> str:
    if kind == "oversize":
        return f"[anexo ignorado ({size_mb} MiB, limite 20 MiB): {filename}]"
    if kind == "inaccessible":
        return f"[anexo não acessível: {filename}]"
    return f"[falha ao baixar anexo: {filename}]"


def main() -> None:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        json.dump({"error": f"stdin inválido: {e}"}, sys.stdout)
        return
    action = req.get("action") or ""
    opencode_dir = req.get("opencode_dir") or str(Path.home())

    if action == "new_turn":
        json.dump({"turn": _ser_turn(new_turn(int(req.get("chat_id") or 0)))},
                  sys.stdout, ensure_ascii=False)
    elif action == "fold":
        turn = _deser_turn(req.get("turn") or {})
        res = fold_event(turn, req.get("event") or {})
        res["turn"] = _ser_turn(turn)
        json.dump(res, sys.stdout, ensure_ascii=False)
    elif action == "select_option":
        turn = _deser_turn(req.get("turn") or {})
        rid, qi, opt = req.get("request_id") or "", int(req.get("qidx") or 0), int(req.get("opt") or 0)
        item = next((q for q in turn["questions"]
                     if q["request_id"] == rid and q["qidx"] == qi), None)
        changed = False
        if item:
            if item.get("multiple"):
                sel = set(turn["qsel"].get((rid, qi)) or ())
                if opt in sel:
                    sel.discard(opt)
                else:
                    sel.add(opt)
                turn["qsel"][(rid, qi)] = sel
            else:
                item["answer"] = [item["options"][opt]["value"]] if opt < len(item["options"]) else []
            changed = True
        json.dump({"turn": _ser_turn(turn), "changed": changed},
                  sys.stdout, ensure_ascii=False)
    elif action == "set_custom":
        turn = _deser_turn(req.get("turn") or {})
        rid, qi = req.get("request_id") or "", int(req.get("qidx") or 0)
        turn["awaiting_custom"] = (rid, qi)
        json.dump({"turn": _ser_turn(turn)}, sys.stdout, ensure_ascii=False)
    elif action == "answer_custom":
        turn = _deser_turn(req.get("turn") or {})
        rid, qi, text = req.get("request_id") or "", int(req.get("qidx") or 0), req.get("text") or ""
        item = next((q for q in turn["questions"]
                     if q["request_id"] == rid and q["qidx"] == qi), None)
        if item:
            item["answer"] = [text]
        turn["awaiting_custom"] = None
        json.dump({"turn": _ser_turn(turn), "res": build_form_answer(turn, rid)},
                  sys.stdout, ensure_ascii=False)
    elif action == "submit_form":
        turn = _deser_turn(req.get("turn") or {})
        rid = req.get("request_id") or ""
        res = build_form_answer(turn, rid)
        if res["complete"]:
            drop_questions(turn, rid)
        res["turn"] = _ser_turn(turn)
        json.dump(res, sys.stdout, ensure_ascii=False)
    elif action == "drop_form":
        turn = _deser_turn(req.get("turn") or {})
        drop_questions(turn, req.get("request_id") or "")
        json.dump({"turn": _ser_turn(turn)}, sys.stdout, ensure_ascii=False)
    elif action == "render_running":
        turn = _deser_turn(req.get("turn") or {})
        json.dump(render_running(turn, opencode_dir), sys.stdout, ensure_ascii=False)
    elif action == "render_think":
        turn = _deser_turn(req.get("turn") or {})
        json.dump({"text": render_think(turn, float(req.get("elapsed") or 0), opencode_dir)},
                  sys.stdout, ensure_ascii=False)
    elif action == "render_result":
        turn = _deser_turn(req.get("turn") or {})
        json.dump({"text": result_text(turn)}, sys.stdout, ensure_ascii=False)
    elif action == "telegram_html":
        json.dump({"html": telegram_html(req.get("text") or "",
                                          req.get("max_len"))},
                  sys.stdout, ensure_ascii=False)
    elif action == "plain_text":
        json.dump({"text": plain_text(req.get("html") or "")},
                  sys.stdout, ensure_ascii=False)
    elif action == "split":
        json.dump({"chunks": split_text(req.get("text") or "", int(req.get("limit") or 4000))},
                  sys.stdout, ensure_ascii=False)
    elif action == "redact":
        json.dump({"text": redact_secrets(req.get("text") or "")},
                  sys.stdout, ensure_ascii=False)
    elif action == "safe_filename":
        json.dump({"filename": safe_filename(req.get("name") or "",
                                              req.get("default") or "arquivo.bin")},
                  sys.stdout, ensure_ascii=False)
    elif action == "media_note":
        json.dump({"text": media_note(req.get("kind") or "download_failed",
                                       req.get("filename") or "anexo",
                                       int(req.get("size_mb") or 0))},
                  sys.stdout, ensure_ascii=False)
    elif action == "media_limits":
        json.dump({"max_bytes": MEDIA_MAX_BYTES,
                   "fallback_mime": _MEDIA_FALLBACK_MIME},
                  sys.stdout, ensure_ascii=False)
    else:
        json.dump({"error": f"ação desconhecida: {action}"}, sys.stdout)


if __name__ == "__main__":
    main()
