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
import difflib
import html
import json
import re
import sys
import time
from pathlib import Path

STREAM_MIN = 150


# Nome da ferramenta -> (emoji, verbo PT). Nomes com prefixo de MCP
# (`mcp__server__tool`) são normalizados por tool_short antes da busca,
# então nunca caem no fallback sem dizer qual ferramenta é.
_TOOL_VERBS = {
    "read": ("📖", "Lendo"),
    "write": ("📝", "Criando"),
    "edit": ("✏️", "Editando"),
    "bash": ("⚙️", "Rodando"),
    "shell": ("⚙️", "Rodando"),
    "glob": ("🗂️", "Buscando"),
    "grep": ("🔍", "Buscando"),
    "webfetch": ("🌐", "Pesquisando"),
    "websearch": ("🌐", "Pesquisando"),
    "todo": ("☑️", "Tarefas"),
    "todowrite": ("☑️", "Tarefas"),
    "todo_write": ("☑️", "Tarefas"),
    "todo_read": ("☑️", "Tarefas"),
    "task": ("🤖", "Executando"),
}


def tool_short(name: str) -> str:
    """`mcp__todoist__list-tasks` -> `list-tasks` (último segmento)."""
    base = (name or "").strip()
    if "__" in base:
        base = base.split("__")[-1]
    return base or "ferramenta"


def tool_meta(name: str) -> tuple:
    """(emoji, verbo) para o status; desconhecidas ganham 🔧 + nome curto."""
    short = tool_short(name).lower()
    if short in _TOOL_VERBS:
        return _TOOL_VERBS[short]
    if short.startswith("web"):
        return ("🌐", "Pesquisando")
    if short.startswith("todo"):
        return ("☑️", "Tarefas")
    return ("🔧", short)


def _shorten(s: str, n: int = 40) -> str:
    s = (s or "").strip().replace("\n", " ")
    return (s[: n - 1] + "…") if len(s) > n else s


def tool_arg(name: str, inp: dict | None, opencode_dir: str, title: str = "") -> str:
    """Argumento principal da ferramenta (o que dá utilidade ao status)."""
    short = tool_short(name).lower()
    inp = inp if isinstance(inp, dict) else {}
    arg = ""
    if short in ("read", "write", "edit"):
        p = _tool_file_path(inp)
        arg = fmt_path(p, opencode_dir) if p else ""
    elif short in ("grep", "glob"):
        arg = str(inp.get("pattern") or inp.get("path") or "")
    elif short in ("bash", "shell"):
        arg = _tool_cmd_preview(inp)
    elif short.startswith("web"):
        arg = _research_query(inp) or _tool_cmd_preview(inp)
    else:
        arg = (title or "").strip() or _tool_cmd_preview(inp)
    return _shorten(redact_secrets(arg))

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
    # Compat com turnos antigos: novos campos de diferenciação text/reasoning/tool.
    turn.setdefault("reasoning_text", "")
    turn.setdefault("reasoning_active", False)
    turn.setdefault("tool_input", {})
    turn.setdefault("out_text", "")
    turn.setdefault("tool_names", {})
    turn.setdefault("tool_cards", {})
    turn.setdefault("research", [])
    turn.setdefault("filediffs", {})
    turn.setdefault("_file_before", {})
    turn.setdefault("process", [])
    turn.setdefault("current", None)
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
        # Resposta final (session.text.*) — único campo que vira balão de resposta.
        "out_text": "",
        # Pensamento (session.reasoning.*) — nunca mistura com out_text.
        "reasoning_text": "",
        "reasoning_active": False,
        # Preview de input de ferramenta em streaming (session.tool.input.*).
        "tool_input": {},
        "reasoning_part_ids": set(),
        "user_msg_id": None,
        "streamed_len": 0,
        "perm_queue": [],
        "questions": [],
        "qsel": {},
        "awaiting_custom": None,
        "tool_names": {},
        # Balões por ferramenta: id -> {name, input, status, output, error}
        "tool_cards": {},
        # Resumo estruturado: pesquisas web e diffs de arquivos.
        "research": [],
        "filediffs": {},
        "_file_before": {},
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
    inp = state.get("input") or {}
    path = inp.get("filePath") or inp.get("path")

    if tool in ("bash", "shell"):
        cmd = _cmd_of_part(state)
        out = _clean_output(state.get("output") or "")[:2000]
        if status == "running":
            turn["current"] = {"tool": tool, "cmd": cmd or "comando", "out": out}
            return
        if status in ("completed", "error"):
            if part_id:
                if part_id in turn["process_seen"]:
                    return
                turn["process_seen"].add(part_id)
            turn["process"].append({
                "cmd": cmd or "comando",
                "out": out,
                "status": status,
            })
            turn["current"] = None
            if status == "error" and "permission" in (state.get("error") or "").lower():
                turn["rejected"] += 1
            return

    if tool == "read":
        if status == "running":
            label = fmt_path(path, turn.get("_opencode_dir") or "") if path else ""
            turn["current"] = {"tool": tool, "label": label.replace("`", ""), "out": ""}
        elif status in ("completed", "error") and path:
            turn["reads"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    elif tool == "write":
        if status == "running":
            label = fmt_path(path, turn.get("_opencode_dir") or "") if path else ""
            turn["current"] = {"tool": tool, "label": label.replace("`", ""), "out": ""}
        elif status in ("completed", "error") and path:
            turn["writes"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    elif tool == "edit":
        if status == "running":
            label = fmt_path(path, turn.get("_opencode_dir") or "") if path else ""
            turn["current"] = {"tool": tool, "label": label.replace("`", ""), "out": ""}
        elif status in ("completed", "error") and path:
            turn["edits"].add(path)
            turn["current"] = None
        else:
            turn["current"] = None
    else:
        # Nome curto (sem prefixo mcp__) + título limpo; o render monta o resto.
        title = (state.get("title") or "").strip().replace("\n", " ").replace("`", "")
        if status == "error" and "permission" in (state.get("error") or "").lower():
            turn["rejected"] += 1
            title = "permissão negada"
        turn["current"] = {"tool": tool_short(tool), "label": title, "out": ""} if status == "running" else None


def _tool_content_text(content) -> str:
    """Extrai texto de session.tool.success/failed content[] (histórico)."""
    if not isinstance(content, list):
        return ""
    parts: list = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            parts.append(str(item["text"]))
        elif isinstance(item.get("text"), str) and item.get("text"):
            parts.append(str(item["text"]))
    return "\n".join(parts).strip()


_URL_RE = re.compile(r"https?://[^\s)>\]\"']+")


def _extract_urls(text, limit: int = 6) -> list:
    seen: set = set()
    out: list = []
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?")
        if url and url not in seen:
            seen.add(url)
            out.append(url[:120])
        if len(out) >= limit:
            break
    return out


def _diff_added_removed(before, after, max_lines: int = 30):
    if before is None and after is None:
        return [], []
    added: list = []
    removed: list = []
    for line in difflib.unified_diff((before or "").splitlines(), (after or "").splitlines(), lineterm=""):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
        if len(added) >= max_lines and len(removed) >= max_lines:
            break
    return added[:max_lines], removed[:max_lines]


def _diff_fence_lines(d: dict, max_lines: int = 30, line_cap: int = 300) -> list:
    """Bloco ```diff (suporte Telegram: clientes colorem +verde/-vermelho).

    Por arquivo alterado: `+` adicionadas, `-` removidas, com segredos
    mascarados e linhas cortadas em `line_cap` (uma linha minificada não
    pode estourar o balão sozinha). Retorna [] sem mudanças."""
    added = [str(a) for a in (d.get("added") or [])]
    removed = [str(r) for r in (d.get("removed") or [])]
    if not added and not removed and not d.get("note"):
        return []

    def _clean(s: str, prefix: str) -> str:
        s = redact_secrets(s)
        if len(s) > line_cap:
            s = s[: line_cap - 1] + "…"
        return (prefix + s).replace("```", "'''")

    body = [_clean(a, "+") for a in added[:max_lines]]
    body += [_clean(r, "-") for r in removed[:max_lines]]
    if d.get("note"):
        body.append(str(d["note"])[:300])
    return ["```diff", *body, "```"]


_SNAPSHOT_MAX = 100_000


def _tool_file_path(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("filePath", "path", "file", "file_path"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _read_text_capped(path):
    try:
        with open(path, "r", encoding="utf-8", errors="strict") as fh:
            return fh.read(_SNAPSHOT_MAX + 1)[: _SNAPSHOT_MAX + 1]
    except (OSError, ValueError, UnicodeError):
        return None


def _research_query(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("query", "url", "urls", "question", "prompt", "text"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:200]
        if isinstance(val, list) and val and isinstance(val[0], str):
            return val[0].strip()[:200]
    return ""


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
        turn["out_text"] = f"[ERR] *Erro no opencode:* {err}"
        return {"action": "finish", "detail": {"reason": "failed", "error": str(err)}}
    if et == "session.error":
        err = data.get("error") or data.get("message") or "desconhecido"
        turn["out_text"] = f"[ERR] *Erro no opencode:* {err}"
        return {"action": "finish", "detail": {"reason": "error", "error": str(err)}}
    if et in ("session.text.started",):
        return {"action": "none", "detail": None}
    if et == "session.text.delta":
        turn["out_text"] += data.get("delta", "")
        return {"action": "none", "detail": None}
    if et == "session.text.ended":
        # Fonte da verdade do provider para o span; reconcilia sem duplicar.
        full = data.get("text")
        if isinstance(full, str) and len(full) >= len(turn.get("out_text") or ""):
            turn["out_text"] = full
        return {"action": "none", "detail": None}
    if et == "session.reasoning.started":
        turn["reasoning_active"] = True
        return {"action": "none", "detail": None}
    if et in ("session.reasoning.delta", "session.tool.progress"):
        if et == "session.reasoning.delta":
            turn["reasoning_text"] += data.get("delta", "")
        return {"action": "none", "detail": None}
    if et == "session.reasoning.ended":
        full = data.get("text")
        if isinstance(full, str) and full:
            turn["reasoning_text"] = full
        turn["reasoning_active"] = False
        return {"action": "none", "detail": None}
    if et == "session.tool.input.started":
        if data.get("id"):
            turn.setdefault("tool_input", {})[data["id"]] = ""
            # Servidor v2.0.3+: o nome da ferramenta vem aqui
            # (o `session.tool.called` não traz mais `name`).
            if data.get("name"):
                entry = turn["tool_names"].get(data["id"]) or {}
                entry["name"] = data["name"]
                turn["tool_names"][data["id"]] = entry
        return {"action": "none", "detail": None}
    if et == "session.tool.input.delta":
        tid = data.get("id")
        if tid:
            buf = turn.setdefault("tool_input", {})
            buf[tid] = (buf.get(tid) or "") + str(data.get("delta", ""))
        return {"action": "none", "detail": None}
    if et == "session.tool.input.ended":
        tid = data.get("id")
        full = data.get("text")
        if tid and isinstance(full, str):
            turn.setdefault("tool_input", {})[tid] = full
            # O input completo vem aqui como JSON; guarda para o called.
            try:
                parsed = json.loads(full)
                if isinstance(parsed, dict):
                    entry = turn["tool_names"].get(tid) or {}
                    entry["input"] = parsed
                    turn["tool_names"][tid] = entry
            except ValueError:
                pass
        return {"action": "none", "detail": None}
    if et == "session.tool.called":
        seen = turn["tool_names"].get(data.get("id") or "") or {}
        name = data.get("name") or seen.get("name") or "ferramenta"
        inp = data.get("input") or seen.get("input") or {}
        tid = data.get("id") or ""
        if tid:
            turn["tool_names"][tid] = {"name": name, "input": inp}
            if name in ("write", "edit"):
                path = _tool_file_path(inp)
                if path:
                    turn.setdefault("_file_before", {})[tid] = {"path": path, "text": _read_text_capped(path)}
            elif name.startswith("web"):
                turn.setdefault("research", []).append({"id": tid, "tool": name, "query": _research_query(inp),
                                                        "sites": [], "status": "running"})
            turn.setdefault("tool_cards", {})[tid] = {
                "name": name,
                "input": inp,
                "status": "running",
                "output": "",
                "error": "",
            }
        _record_tool(turn, {
            "tool": name,
            "id": tid,
            "state": {"status": "running", "input": inp},
        })
        return {"action": "push", "detail": {"tool_phase": "started", "tool_id": tid}}
    if et in ("session.tool.success", "session.tool.failed"):
        tid = data.get("id") or ""
        seen = turn["tool_names"].pop(tid, None) or {}
        if tid:
            turn.get("tool_input", {}).pop(tid, None)
        name = seen.get("name") or data.get("name") or "ferramenta"
        status = "completed" if et == "session.tool.success" else "error"
        err_text = ""
        if status == "error":
            err = data.get("error")
            err_text = (err.get("message") if isinstance(err, dict) else str(err or "")) or ""
            if "permission" in str(data.get("error") or "").lower():
                turn["rejected"] += 1
        out_text = _clean_output(_tool_content_text(data.get("content")))[:1000]
        diff_path = ""
        if name in ("write", "edit"):
            before = turn.get("_file_before", {}).pop(tid, None) or {}
            path = before.get("path") or _tool_file_path(seen.get("input") or {})
            if path:
                added, removed = _diff_added_removed(before.get("text"), _read_text_capped(path))
                prev = turn.setdefault("filediffs", {}).get(path) or {"added": [], "removed": []}
                prev["added"] = ((prev.get("added") or []) + added)[:30]
                prev["removed"] = ((prev.get("removed") or []) + removed)[:30]
                if not prev["added"] and not prev["removed"] and not prev.get("note"):
                    prev["note"] = out_text[:200] or "sem alterações detectadas"
                turn["filediffs"][path] = prev
                if prev.get("added") or prev.get("removed"):
                    diff_path = path
        elif name.startswith("web"):
            for r in turn.get("research") or []:
                if tid and r.get("id") != tid:
                    continue
                if r.get("status") != "running":
                    continue
                r["status"] = status
                if not r.get("query"):
                    r["query"] = _research_query(seen.get("input") or {})
                sites = list(r.get("sites") or [])
                query = r.get("query") or ""
                if query.startswith("http") and query not in sites:
                    sites.append(query[:120])
                for u in _extract_urls(out_text):
                    if u not in sites:
                        sites.append(u)
                r["sites"] = sites[:8]
                break
        if tid:
            turn.setdefault("tool_cards", {})[tid] = {
                "name": name,
                "input": seen.get("input") or {},
                "status": status,
                "output": out_text,
                "error": err_text,
            }
        _record_tool(turn, {
            "tool": name,
            "id": tid,
            "state": {"status": status, "input": seen.get("input") or {}, "output": out_text},
        })
        detail = {"tool_phase": "ended", "tool_id": tid}
        if diff_path:
            detail["diff_path"] = diff_path
        return {"action": "push", "detail": detail}
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


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clean_output(s: str) -> str:
    """Retorno sem poluição: sem ANSI, sem barra de progresso (\r),
    sem linhas em branco em sequência."""
    lines: list = []
    for raw in _ANSI_RE.sub("", s or "").split("\n"):
        seg = raw.split("\r")[-1].rstrip()
        if seg or (lines and lines[-1]):
            lines.append(seg)
    return "\n".join(lines).strip()


def tail_out(s: str, n: int = 450) -> str:
    s = redact_secrets(_clean_output(s).replace("```", "'''"))
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
        return "[OK]"
    if s == "in_progress":
        return "[RELOAD]"
    return "[ ]"


def todo_lines(todos: list, max_items: int = 20) -> list:
    items = [t for t in (todos or []) if (t.get("status") or "") != "cancelled"][:max_items]
    if not items:
        return []
    lines = ["[TODO] *To-do's:*"]
    for t in items:
        content = (t.get("content") or "").strip().replace("\n", " ")
        if len(content) > 80:
            content = content[:79] + "…"
        lines.append(f"{todo_icon(t.get('status') or '')} {content}")
    return lines


def q_lines(turn: dict, max_opts: int = 10) -> list:
    """Perguntas do turno, compactas (texto + opções numeradas)."""
    lines: list = []
    for qi, q in enumerate(turn["questions"]):
        sel = set(turn["qsel"].get((q["request_id"], q["qidx"])) or ())
        if qi == 0:
            lines += ["", "[?] *Escolha:*"]
        if q.get("header"):
            lines.append(f"*{q['header']}*")
        if q.get("question"):
            lines.append(q["question"])
        opts = q["options"] or []
        for i, opt in enumerate(opts[:max_opts]):
            mark = "[OK]" if i in sel else f"{i + 1}."
            desc = (opt.get("description") or "").strip().replace("\n", " ")
            if len(desc) > 80:
                desc = desc[:79] + "…"
            line = f"{mark} {opt.get('label')}"
            if desc:
                line += f" — {desc}"
            lines.append(line)
        if len(opts) > max_opts:
            lines.append(f"_…+{len(opts) - max_opts} opções_")
        if q.get("multiple"):
            lines.append("_Alterne e envie._")
    return lines


def q_keyboard(turn: dict) -> list:
    """Teclado de perguntas/permissões como [[{text, data}]]."""
    rows: list = []
    for q in turn["questions"]:
        rid, qi = q["request_id"], q["qidx"]
        sel = set(turn["qsel"].get((rid, qi)) or ())
        if q.get("multiple"):
            for i, opt in enumerate(q["options"]):
                prefix = "[OK] " if i in sel else ""
                rows.append([{"text": prefix + btn_label(opt.get("label")), "data": f"qt:{rid}:{qi}:{i}"}])
            rows.append([
                {"text": "[OK] Enviar", "data": f"qs:{rid}"},
                {"text": "[ERR] Rejeitar", "data": f"qr:{rid}"},
            ])
        else:
            for i, opt in enumerate(q["options"]):
                rows.append([{"text": btn_label(opt.get("label")), "data": f"qo:{rid}:{qi}:{i}"}])
            row: list = []
            if q.get("custom"):
                row.append({"text": "[EDIT] Digitar resposta", "data": f"qc:{rid}:{qi}"})
            row.append({"text": "[ERR] Rejeitar", "data": f"qr:{rid}"})
            rows.append(row)
    if turn["perm_queue"]:
        p = turn["perm_queue"][0]
        rows.append([
            {"text": "[OK] Uma vez", "data": f"perm:{p['id']}:once"},
            {"text": "[REPEAT] Sempre", "data": f"perm:{p['id']}:always"},
            {"text": "[ERR] Negar", "data": f"perm:{p['id']}:reject"},
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
        lines += ["", f"[LOCK] *Permissão:* {perm_desc(p)}"]
    if not has_prompt:
        # Cabeçalho = ação atual (⏳); histórico = concluídas (✅/❌).
        # Texto puro + emoji: sem *code* misturado, sem tags triplas.
        curr = turn["current"]
        if isinstance(curr, dict) and (curr.get("cmd") or curr.get("label") is not None or curr.get("tool")):
            tname = curr.get("tool") or "ferramenta"
            emoji, verb = tool_meta(tname)
            if curr.get("cmd"):
                arg = _shorten(redact_secrets(curr["cmd"]))
            else:
                arg = _shorten((curr.get("label") or "").replace("`", ""))
            head = f"⏳ {verb} {arg}".rstrip()
            lines += ["", head]
        elif turn.get("reasoning_active") or (turn.get("reasoning_text") or "").strip():
            lines += ["", "⏳ pensando…"]
        else:
            lines += ["", "⏳ pensando…"]
        hist = _history_lines(turn, opencode_dir)
        if hist:
            lines += ["", "Recentes:", *hist]
    rows = q_keyboard(turn)
    if not rows and not has_prompt:
        rows = [[{"text": "Cancelar", "data": "/cancel"}]]
    return {"text": "\n".join(lines), "keyboard": rows}


def _history_lines(turn: dict, opencode_dir: str, max_items: int = 5) -> list:
    """Últimas ações concluídas, uma por linha (ou colapsadas: `✅ 🔍 grep ×3`).

    Um emoji por linha (✅/❌ + ferramenta), nome curto sempre visível,
    argumento truncado em 40 chars. Sem tags `[OK] [FIND]` triplas."""
    cards = turn.get("tool_cards") or {}
    done = [c for c in cards.values()
            if isinstance(c, dict) and (c.get("status") or "") in ("completed", "error")]
    groups: list = []  # [ok, emoji, short, arg, count, last_card]
    for card in done[-20:]:
        name = tool_short(card.get("name") or "ferramenta")
        emoji, _verb = tool_meta(name)
        inp = card.get("input") if isinstance(card.get("input"), dict) else {}
        arg = tool_arg(name, inp, opencode_dir)
        ok = (card.get("status") or "") == "completed"
        key = (ok, emoji, name)
        if groups and tuple(groups[-1][:3]) == key:
            groups[-1][4] += 1
            groups[-1][3] = arg  # arg mais recente do grupo
            groups[-1][5] = card
        else:
            groups.append([ok, emoji, name, arg, 1, card])
    out = []
    for ok, emoji, name, arg, n, card in groups[-max_items:]:
        mark = "✅" if ok else "❌"
        if n > 1:
            out.append(f"{mark} {emoji} {name} ×{n}")
            continue
        line = f"{mark} {emoji} {name} {arg}".rstrip()
        # Retorno no formato `comando: retorno` (só onde agrega):
        # bash concluída mostra a saída; qualquer erro mostra a mensagem.
        if not ok:
            err = _shorten(redact_secrets(card.get("error") or card.get("output") or ""), 80)
            if err:
                line += f": {err}"
        elif name in ("bash", "shell"):
            ret = _shorten(redact_secrets(card.get("output") or ""), 80)
            if ret:
                line += f": {ret}"
        out.append(line)
    return out


def _tool_cmd_preview(inp: dict) -> str:
    cmd = inp.get("command") if isinstance(inp, dict) else ""
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(c) for c in cmd)
    cmd = redact_secrets(str(cmd or "")).strip().replace("\n", " ")
    if isinstance(inp, dict) and not cmd:
        q = _research_query(inp)
        if q:
            return q[:50]
    return (cmd[:60] + "…") if len(cmd) > 60 else cmd


def render_file_diff(turn: dict, path: str, opencode_dir: str) -> str:
    """HTML do diff de um arquivo (mensagem própria por alteração)."""
    d = ((turn.get("filediffs") or {}).get(path) or {})
    fence = _diff_fence_lines(d)
    if not fence:
        return ""
    md = f"[DIFF] *diff `{fmt_path(path, opencode_dir)}`:*\n" + "\n".join(fence)
    if len(md) > 3950:
        md = md[:3950] + "\n…"
    return expandable_html(telegram_html(md, max_len=3950))


def summary_lines(turn: dict, opencode_dir: str) -> list:
    out = []
    out += todo_lines(turn.get("todos") or [])
    if turn["reads"]:
        out.append("[READ] *leu:* " + ", ".join(sorted(f"`{fmt_path(x, opencode_dir)}`" for x in turn["reads"])))
    if turn["writes"]:
        out.append("[ADD] *criou:* " + ", ".join(sorted(f"`{fmt_path(x, opencode_dir)}`" for x in turn["writes"])))
    if turn["edits"]:
        out.append("[EDIT] *editou:* " + ", ".join(sorted(f"`{fmt_path(x, opencode_dir)}`" for x in turn["edits"])))
    if turn["rejected"]:
        out.append(f"[ERR] *negado:* {turn['rejected']} permissõe(s)")
    return out


def report_lines(turn: dict, opencode_dir: str) -> list:
    """Resumo estruturado do turno (Pesquisa/Ação/Arquivos).

    Fence ```diff para as mudanças de arquivo: clientes Telegram que
    suportam colorem +verde/-vermelho."""
    lines: list = []
    research = [r for r in (turn.get("research") or []) if isinstance(r, dict)]
    if research:
        subject = next(
            (str(r.get("query") or "").strip() for r in research if str(r.get("query") or "").strip()),
            "web",
        )
        lines += ["", f"[FIND] *Pesquisa:* {code_span(redact_secrets(subject))}"]
        sites: list = []
        for r in research:
            for s in (r.get("sites") or []):
                if s and s not in sites:
                    sites.append(s)
        for s in sites[:8]:
            lines.append(f"- {s}")
        if not sites:
            lines.append("- (sem sites)")
    proc = turn.get("process") or []
    if proc:
        skipped = max(0, len(proc) - 5)
        if skipped:
            lines.append(f"({skipped} ações anteriores omitidas)")
        for step in proc[-5:]:
            cmd = redact_secrets(str(step.get("cmd") or "comando"))
            tag = " [ERR]" if step.get("status") == "error" else ""
            lines += ["", f"» *Ação:* {code_span('$ ' + cmd)}{tag}"]
            out = tail_out(step.get("out"), 400)
            if out:
                lines += ["```", out, "```"]
            elif step.get("status") == "error":
                lines.append("_sem retorno_")
    diffs = turn.get("filediffs") or {}
    if diffs:
        for path in sorted(diffs):
            d = diffs[path] or {}
            fence = _diff_fence_lines(d)
            if not fence:
                continue
            lines += ["", f"[DIFF] *Arquivo:* {code_span(fmt_path(path, opencode_dir))}", *fence]
    return lines


def render_think(turn: dict, elapsed: float, opencode_dir: str) -> str:
    mins, secs = int(elapsed) // 60, int(elapsed) % 60
    lines = [f"[...] *pensou em {mins}m{secs:02d}s:*"]
    summaries = summary_lines(turn, opencode_dir)
    if summaries:
        lines += ["", "———", *summaries]
    thinking = (turn.get("reasoning_text") or "").strip()
    if thinking:
        lines += ["", "[THINK] *pensamento:*", "```", tail_out(thinking, 500), "```"]
    report = report_lines(turn, opencode_dir)
    if report:
        lines += report
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
    elif action == "render_diff":
        turn = _deser_turn(req.get("turn") or {})
        json.dump({"text": render_file_diff(turn, req.get("path") or "", opencode_dir)},
                  sys.stdout, ensure_ascii=False)
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
