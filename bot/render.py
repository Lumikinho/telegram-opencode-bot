"""Renderizacao Telegram: Markdown->HTML, teclados, resumos de turno."""
from pathlib import Path
import html
import logging
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from . import config


logger = logging.getLogger(__name__)


# Todo lugar que edita ou manda mensagem no Telegram precisa lidar com o
# mesmo problema: o parse_mode pedido (Markdown/HTML) pode ser rejeitado
# pela API se o texto tiver alguma entidade malformada. O padrão espalhado
# pelo arquivo (try com parse_mode, except tenta de novo em texto puro) foi
# centralizado aqui pra não duplicar a mesma lógica em ~8 lugares.

async def _safe_edit_message(bot, chat_id: int, message_id: int, text: str,
                              parse_mode: str | None = None,
                              reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    """Tenta editar a mensagem com o parse_mode pedido; se o Telegram rejeitar
    o texto formatado, tenta de novo em texto puro (mantendo o reply_markup).
    Retorna True se alguma das duas tentativas teve sucesso."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
            parse_mode=parse_mode, reply_markup=reply_markup,
        )
        return True
    except TelegramError as e:
        logger.debug("Falha ao editar mensagem (parse_mode=%s): %s", parse_mode, e)
    try:
        fallback_text = _plain_text(text) if parse_mode else text
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=fallback_text,
            reply_markup=reply_markup,
        )
        return True
    except TelegramError as e:
        logger.debug("Falha ao editar mensagem em texto puro: %s", e)
        return False


async def _safe_send_message(bot, chat_id: int, text: str,
                              parse_mode: str | None = None,
                              reply_markup: InlineKeyboardMarkup | None = None):
    """Mesma ideia de `_safe_edit_message`, mas para mandar mensagem nova.
    Retorna o objeto Message em caso de sucesso, ou None."""
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup,
        )
    except TelegramError as e:
        logger.debug("Falha ao enviar mensagem (parse_mode=%s): %s", parse_mode, e)
    try:
        fallback_text = _plain_text(text) if parse_mode else text
        return await bot.send_message(chat_id=chat_id, text=fallback_text, reply_markup=reply_markup)
    except TelegramError as e:
        logger.warning("Falha ao enviar mensagem: %s", e)
        return None



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


def _fmt_path(path: str | None) -> str:
    if not path:
        return "(?)"
    try:
        return str(Path(path).relative_to(Path(config.OPENCODE_DIR)))
    except Exception:
        return path


def _perm_desc(p: dict) -> str:
    pat = p.get("pattern")
    pat_str = ", ".join(pat) if isinstance(pat, list) else (pat or "")
    title = p.get("title") or p.get("permission") or "permissão"
    if pat_str:
        return f"{title}: `{pat_str}`"
    return f"`{title}`"


def _code(s: str) -> str:
    # Kept as a plain statement (not a nested-quote f-string) so this runs on
    # Python < 3.12 too -- PEP 701 (same-quote nesting in f-strings) is 3.12+.
    escaped = (s or "").replace("`", "'")
    return "`" + escaped + "`"


# ---- inline buttons ---------------------------------------------------

def _btn_help() -> InlineKeyboardButton:
    return InlineKeyboardButton("Ajuda", callback_data="/help")


def _btn_new() -> InlineKeyboardButton:
    return InlineKeyboardButton("Nova conversa", callback_data="/new")


def _btn_status() -> InlineKeyboardButton:
    return InlineKeyboardButton("Status", callback_data="/status")


def _btn_cancel() -> InlineKeyboardButton:
    return InlineKeyboardButton("Cancelar", callback_data="/cancel")


def _kb_quick() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn_help(), _btn_new(), _btn_status()]])


def _kb_after_turn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn_new(), _btn_help(), _btn_status()]])


def _kb_status(busy_turns: int) -> InlineKeyboardMarkup:
    row1 = [_btn_new(), _btn_help()]
    row2 = [InlineKeyboardButton("Mudar modelo", callback_data="/models")]
    if busy_turns:
        row2.append(_btn_cancel())
    return InlineKeyboardMarkup([row1, row2])


def _kb_restart() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Só o bot", callback_data="__restart:bot"),
         InlineKeyboardButton("🖥️ Só o servidor", callback_data="__restart:server")],
        [InlineKeyboardButton("🔁 Bot + servidor", callback_data="__restart:both")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="/status")],
    ])


_RESTART_ALIASES = {
    "bot": "bot",
    "server": "server", "servidor": "server", "srv": "server",
    "both": "both", "ambos": "both", "tudo": "both", "all": "both",
}

_RESTART_LABELS = {"bot": "o bot", "server": "o servidor", "both": "o bot + o servidor"}


def _models_kb(models: list[str]) -> list[list[InlineKeyboardButton]]:
    """Converte a lista de modelos do CLI em botões (até 2 por linha).
    O callback leva o spec `providerID/modelID`; fica bem abaixo do limite
    de 64 bytes do Telegram."""
    rows: list[list[InlineKeyboardButton]] = []
    pending: list[InlineKeyboardButton] = []
    for spec in models:
        pending.append(InlineKeyboardButton(spec, callback_data=f"mod:{spec}"))
        if len(pending) == 2:
            rows.append(pending)
            pending = []
    if pending:
        rows.append(pending)
    return rows


# ---- secret redaction -------------------------------------------------
# Anything the agent runs (grep/curl/etc) gets echoed into Telegram, which
# means a raw API key or token found on disk ends up permanently in the
# chat's message history. This is a best-effort mask, not a guarantee --
# it catches the common shapes (key=..., Bearer ..., known provider prefixes)
# but a determined secret in an unusual format can still slip through. Treat
# the Telegram history as sensitive regardless.
#
# Bare hex blobs used to be masked blindly, which false-positived on any
# SHA/MD5 hash, git commit, or hyphenless UUID. Now we only mask a bare hex
# blob when a secret-ish keyword appears nearby (e.g. `key=`, `token`, ...),
# so unrelated hashes stay readable.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b((?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|password|passwd|token)\s*[:=]\s*['\"]?)([A-Za-z0-9\-_\.]{6,})"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-_\.]{10,})"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),             # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),   # GitHub tokens
    re.compile(r"\bsk-[A-Za-z0-9\-]{20,}\b"),       # OpenAI/Anthropic-style keys
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),  # Slack tokens
]
_MASKED = "███MASKED███"
_SECRET_HEX = re.compile(r"\b[0-9A-Fa-f]{32,64}\b")
_SECRET_KW = re.compile(r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|secret|passwd|password|steam|token)", re.I)


def _mask_hex_with_context(s: str) -> str:
    """Mascara hex 32–64 só quando perto de uma palavra-chave de segredo,
    deixando hashes/commits/UUIDs comuns legíveis."""
    def _sub(m):
        window = s[max(0, m.start() - 60):m.start()]
        return _MASKED if _SECRET_KW.search(window) else m.group(0)
    return _SECRET_HEX.sub(_sub, s)


def _redact_secrets(s: str) -> str:
    if not s:
        return s
    out = s
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(1) + _MASKED, out)
        else:
            out = pat.sub(_MASKED, out)
    return _mask_hex_with_context(out)


def _tail_out(s: str, n: int = 450) -> str:
    s = _redact_secrets((s or "").strip().replace("```", "'''"))
    if not s:
        return ""
    if len(s) > n:
        return "…" + s[-n:]
    return s


def _btn_label(s: str, n: int = 40) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _q_lines(turn: dict) -> list[str]:
    lines: list[str] = []
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


def _q_kb(turn: dict) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for q in turn["questions"]:
        rid, qi = q["request_id"], q["qidx"]
        sel = set(turn["qsel"].get((rid, qi)) or ())
        if q.get("multiple"):
            for i, opt in enumerate(q["options"]):
                prefix = "✅ " if i in sel else ""
                rows.append([InlineKeyboardButton(prefix + _btn_label(opt.get("label")), callback_data=f"qt:{rid}:{qi}:{i}")])
            rows.append([
                InlineKeyboardButton("✅ Enviar", callback_data=f"qs:{rid}"),
                InlineKeyboardButton("❌ Rejeitar", callback_data=f"qr:{rid}"),
            ])
        else:
            for i, opt in enumerate(q["options"]):
                rows.append([InlineKeyboardButton(_btn_label(opt.get("label")), callback_data=f"qo:{rid}:{qi}:{i}")])
            row: list[InlineKeyboardButton] = []
            if q.get("custom"):
                row.append(InlineKeyboardButton("✏️ Digitar resposta", callback_data=f"qc:{rid}:{qi}"))
            row.append(InlineKeyboardButton("❌ Rejeitar", callback_data=f"qr:{rid}"))
            rows.append(row)
    if turn["perm_queue"]:
        p = turn["perm_queue"][0]
        rows.append([
            # Note: only the permission id goes in callback_data. turn["sid"]
            # is already known server-side -- putting it here too used to push
            # this well past Telegram's 64-byte callback_data limit, which
            # made Telegram silently reject the button and the whole permission
            # prompt would never render (looked like the bot just hung).
            InlineKeyboardButton("✅ Uma vez", callback_data=f"perm:{p['id']}:once"),
            InlineKeyboardButton("🔁 Sempre", callback_data=f"perm:{p['id']}:always"),
            InlineKeyboardButton("❌ Negar", callback_data=f"perm:{p['id']}:reject"),
        ])
    return rows


def _render_running(turn: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    lines = []
    if turn["todo"]:
        lines.append(f"📋 *Plano:* {turn['todo']} passo{'s' if turn['todo'] != 1 else ''}")
    has_prompt = bool(turn["questions"]) or bool(turn["perm_queue"])
    if turn["questions"]:
        lines += _q_lines(turn)
    if turn["perm_queue"]:
        p = turn["perm_queue"][0]
        lines += ["", "🔒 *Permissão pedida:*", _perm_desc(p)]
    if not has_prompt:
        curr = turn["current"]
        if curr and curr.get("cmd"):
            lines += ["", f"⚡ *rodando:* {_code(_redact_secrets(curr['cmd']))}"]
            out = _tail_out(curr.get("out"), 450)
            if out:
                lines += ["```", out, "```"]
        elif curr and curr.get("label"):
            lines += ["", curr["label"]]
        else:
            lines += ["", "⏳ *pensando…*"]
    rows = _q_kb(turn)
    if not rows and not has_prompt:
        rows = [[_btn_cancel()]]
    kb = InlineKeyboardMarkup(rows) if rows else None
    return "\n".join(lines), kb


def _summary(turn: dict) -> list[str]:
    out = []
    if turn["reads"]:
        out.append("📖 *leu:* " + ", ".join(sorted(f"`{_fmt_path(x)}`" for x in turn["reads"])))
    if turn["writes"]:
        out.append("➕ *criou:* " + ", ".join(sorted(f"`{_fmt_path(x)}`" for x in turn["writes"])))
    if turn["edits"]:
        out.append("✏️ *editou:* " + ", ".join(sorted(f"`{_fmt_path(x)}`" for x in turn["edits"])))
    if turn["rejected"]:
        out.append(f"❌ *negado:* {turn['rejected']} permissõe(s)")
    return out


def _trace_lines(turn: dict, max_steps: int = 40, max_out: int = 500) -> list[str]:
    proc = turn["process"]
    if not proc:
        return []
    skipped = max(0, len(proc) - max_steps)
    show = proc[-max_steps:]
    lines = ["📄 *processo:*"]
    if skipped:
        lines.append(f"({skipped} passo{'s' if skipped != 1 else ''} anterior{'is' if skipped != 1 else ''} omitido{'s' if skipped != 1 else ''})")
    for i, step in enumerate(show, 1 + skipped):
        lines += ["", f"{i}) {_code(_redact_secrets('$ ' + step['cmd']))}"]
        out = _tail_out(step.get("out"), max_out)
        if out:
            lines += ["```", out, "```"]
        if step.get("status") == "error":
            lines.append("(❌ erro)")
    return lines


def _render_think(turn: dict, elapsed: float) -> str:
    mins, secs = int(elapsed) // 60, int(elapsed) % 60
    lines = [f"💭 *pensou em {mins}m{secs:02d}s:*"]
    summaries = _summary(turn)
    if summaries:
        lines += ["", "———", *summaries]
    trace = _trace_lines(turn)
    if trace:
        lines += ["", *trace]
    total = "\n".join(lines)
    if len(total) > 3950:
        total = total[:3950] + "\n…"
    return _expandable_html(_telegram_html(total, max_len=3950))


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


def _escape_html_text(s: str) -> str:
    """Escapa & < > para entidades, preservando entidades já válidas."""
    out, i = [], 0
    for m in _ENTITY_RE.finditer(s):
        out.append(html.escape(s[i:m.start()], quote=False))
        out.append(m.group(0))
        i = m.end()
    out.append(html.escape(s[i:], quote=False))
    return "".join(out)


def _telegram_html(text: str, max_len: int | None = None) -> str:
    """Converte markdown + HTML do assistente em HTML seguro e balanceado
    para o parse_mode='HTML' do Telegram.

    Etapas:
      1) markdown -> HTML (fenced code, `código`, **negrito**, *itálico*,
         ~~riscado~~, [link](url) só http/https, cabeçalhos #..######);
      2) sanitização com whitelist de tags Telegram, normalização de nomes,
         descarte de atributos (excepto <a href> seguro), balanceamento de
         tags (fechando/descartando órfãs) e corte seguro em max_len.
    """
    def _fence(m):
        lang = m.group(1)
        cls = f' class="language-{lang}"' if lang else ""
        return f"<pre><code{cls}>{html.escape(m.group(2), quote=False)}</code></pre>"

    def _link(m):
        url = m.group(2).strip()
        if not re.match(r"^https?://", url, re.I):
            return m.group(0)
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(m.group(1), quote=False)}</a>'

    # 1) markdown -> html (sem escapar o texto inteiro antes)
    h = _MD_FENCE_RE.sub(_fence, text)
    h = _MD_INLINE_CODE_RE.sub(lambda m: f"<code>{html.escape(m.group(1), quote=False)}</code>", h)
    h = _MD_LINK_RE.sub(_link, h)
    h = _MD_STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", h)
    h = _MD_BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", h)
    h = _MD_ITAL_A_RE.sub(lambda m: f"<i>{m.group(1)}</i>", h)
    h = _MD_ITAL_U_RE.sub(lambda m: f"<i>{m.group(1)}</i>", h)
    h = _MD_HEADING_RE.sub(lambda m: f"<b>{m.group(2)}</b>", h)

    # 2) sanitizar + balancear + cortar
    out: list[str] = []
    stack: list[str] = []
    clipped = False
    for m in _TAG_TOKEN_RE.finditer(h):
        tag_part, text_part = m.group(1), m.group(2)
        if text_part is not None:
            rendered = _escape_html_text(text_part)
            if max_len is not None and len("".join(out)) + len(rendered) > max_len:
                out.append("…")
                clipped = True
                break
            out.append(rendered)
            continue
        tm = re.match(r"</?([a-zA-Z][a-zA-Z0-9-]*)", tag_part)
        if not tm:
            out.append(_escape_html_text(tag_part))
            continue
        name = tm.group(1).lower()
        canon = _ALLOWED_TAGS.get(name)
        closing = tag_part.startswith("</")
        if canon is None:
            out.append(_escape_html_text(tag_part))
            continue
        if closing:
            if canon in stack:
                while stack and stack[-1] != canon:
                    out.append(f"</{stack.pop()}>")
                if stack:
                    stack.pop()
                out.append(f"</{canon}>")
            else:
                out.append(_escape_html_text(tag_part))
            continue
        rendered_tag = f"<{canon}>"
        if name == "a":
            href = re.search(r'\bhref\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag_part, re.I)
            url = (href.group(2) or href.group(3) or href.group(4)) if href else ""
            if not re.match(r"^https?://", url or "", re.I):
                out.append(_escape_html_text(tag_part))
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


def _plain_text(html_text: str) -> str:
    """Remove tags e desfaz entidades: texto limpo, sem HTML cru."""
    if not html_text:
        return ""
    cleaned = html.unescape(re.sub(r"<[^>]*>", "", html_text))
    return re.sub(r"</?[a-zA-Z][^>]*>", "", cleaned)


def _expandable_html(html_text: str) -> str:
    return f"<blockquote expandable>{html_text}</blockquote>"


def _result_text(turn: dict) -> tuple[str, str]:
    body = (turn["out_text"] or "").strip()
    if not body:
        return "escrevendo…", ""
    if len(body) > 3800:
        body = "…" + body[-3800:]
    return _telegram_html(body, max_len=3800), ""


def _split_text(text: str, limit: int = 4000) -> list[str]:
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


async def _reply_or_edit(update, text: str, parse_mode=None, reply_markup=None):
    """Botão transforma o próprio balão (edit); comando digitado manda msg nova."""
    query = update.callback_query
    if query is None or query.message is None:
        await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    try:
        await query.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramError as e:
        if "not modified" in str(e).lower():
            return
        await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
