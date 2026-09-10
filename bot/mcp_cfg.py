"""Bloco MCP no JSONC de config do opencode (ler/escrever)."""
from pathlib import Path
import json
import logging
import os
from . import state


logger = logging.getLogger(__name__)


def _mcp_file() -> Path:
    return Path.home() / ".config" / "opencode" / "opencode.jsonc"


def _load_mcp_cfg() -> dict:
    p = _mcp_file()
    if not p.exists():
        return {}
    try:
        data = json.loads(_strip_jsonc_comments(p.read_text()))
    except Exception:
        return {}
    return (data or {}).get("mcp") or {}


def _mcp_url(name: str) -> str:
    return (_load_mcp_cfg().get(name) or {}).get("url") or ""


def _skip_jsonc_string(text: str, i: int) -> int:
    """text[i] é uma aspa dupla; retorna o índice logo após a aspa de fechamento."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    return n


def _skip_jsonc_ws(text: str, i: int, end: int) -> int:
    """Avança sobre espaços e comentários JSONC até `end` (exclusive)."""
    n = min(len(text), end)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = j + 1 if j != -1 else n
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = j + 2 if j != -1 else n
        else:
            break
    return i


def _strip_jsonc_comments(text: str) -> str:
    """Remove comentários (// e /* */) fora de strings, preservando a estrutura."""
    n = len(text)
    out: list[str] = []
    i = 0
    while i < n:
        c = text[i]
        if c == '"':
            j = _skip_jsonc_string(text, i)
            out.append(text[i:j])
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = j if j != -1 else n
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = j + 2 if j != -1 else n
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _has_jsonc_comments(text: str) -> bool:
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c == '"':
            i = _skip_jsonc_string(text, i)
        elif c == "/" and i + 1 < n and text[i + 1] in ("/", "*"):
            return True
        else:
            i += 1
    return False


def _jsonc_load(raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        data = json.loads(_strip_jsonc_comments(raw))
    except Exception as e:
        logger.debug("opencode.jsonc ilegível: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _jsonc_root_span(raw: str) -> tuple[int | None, int | None]:
    """Retorna (índice do '{' raiz, índice do '}' raiz) do objeto JSONC, ou (None, None)."""
    depth = 0
    i = 0
    n = len(raw)
    open_idx = None
    while i < n:
        c = raw[i]
        if c == '"':
            i = _skip_jsonc_string(raw, i)
            continue
        if c == "{":
            if depth == 0:
                open_idx = i
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return open_idx, i
            i += 1
        elif c == "/" and i + 1 < n and raw[i + 1] == "/":
            j = raw.find("\n", i)
            i = j + 1 if j != -1 else n
        elif c == "/" and i + 1 < n and raw[i + 1] == "*":
            j = raw.find("*/", i + 2)
            i = j + 2 if j != -1 else n
        else:
            i += 1
    return None, None


def _jsonc_last_content(raw: str, start: int, end: int) -> int:
    """Índice do último caractere que não é espaço/comentário em [start, end), ou -1."""
    i = start
    last = -1
    n = min(len(raw), end)
    while i < n:
        c = raw[i]
        if c == '"':
            j = _skip_jsonc_string(raw, i)
            last = j - 1
            i = j
        elif c == "/" and i + 1 < n and raw[i + 1] == "/":
            j = raw.find("\n", i)
            i = j + 1 if j != -1 else n
        elif c == "/" and i + 1 < n and raw[i + 1] == "*":
            j = raw.find("*/", i + 2)
            i = j + 2 if j != -1 else n
        elif c in " \t\r\n":
            i += 1
        else:
            last = i
            i += 1
    return last


def _jsonc_value_end(raw: str, start: int, bound: int) -> int:
    """Índice logo após o valor JSON que começa em `start` (dentro de [start, bound))."""
    n = min(len(raw), bound)
    depth = 0
    i = start
    while i < n:
        c = raw[i]
        if c == '"':
            j = _skip_jsonc_string(raw, i)
            if depth == 0:
                return j
            i = j
        elif c in " \t\r\n" or (c == "/" and i + 1 < n and raw[i + 1] in ("/", "*")):
            i = _skip_jsonc_ws(raw, i, n)
        elif c in "{[":
            depth += 1
            i += 1
        elif c in "}]":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        elif c in ",:":
            if depth == 0:
                return i
            i += 1
        else:
            i += 1
            while i < n and raw[i] not in ",{}[]\" \t\r\n" and not (
                raw[i] == "/" and i + 1 < n and raw[i + 1] in ("/", "*")
            ):
                i += 1
            if depth == 0:
                return i
    return n


def _jsonc_find_prop(raw: str, start: int, end: int, key: str) -> tuple[int, int, int] | None:
    """Retorna (início da chave, início do valor, fim do valor) da propriedade
    `key` no nível zero dentro de [start, end), ou None. Lida com aninhamento,
    strings e comentários."""
    i = start
    depth = 0
    n = min(len(raw), end)
    while i < n:
        c = raw[i]
        if c in " \t\r\n" or (c == "/" and i + 1 < n and raw[i + 1] in ("/", "*")):
            i = _skip_jsonc_ws(raw, i, n)
            continue
        if c == '"':
            if depth == 0:
                j = _skip_jsonc_string(raw, i)
                name = raw[i + 1:j - 1]
                k = _skip_jsonc_ws(raw, j, n)
                if name == key and k < n and raw[k] == ":":
                    vs = _skip_jsonc_ws(raw, k + 1, n)
                    ve = _jsonc_value_end(raw, vs, n)
                    return i, vs, ve
                i = j
            else:
                i = _skip_jsonc_string(raw, i)
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        i += 1
    return None


def _jsonc_parseable(text: str) -> bool:
    try:
        data = json.loads(_strip_jsonc_comments(text))
        return isinstance(data, dict)
    except Exception:
        return False


def _jsonc_upsert_mcp(raw: str, name: str, cfg_json: str) -> str | None:
    """Edita opencode.jsonc cirurgicamente (preservando comentários) para
    adicionar/atualizar mcp.<name>. Retorna o texto novo, ou None se não
    conseguir editar com segurança."""
    open_i, close_i = _jsonc_root_span(raw)
    if open_i is None:
        return None
    mcp = _jsonc_find_prop(raw, open_i + 1, close_i, "mcp")
    name_json = f'"{name}": {cfg_json}'
    if mcp is None:
        last = _jsonc_last_content(raw, open_i + 1, close_i)
        ins = f'\n  "mcp": {{\n    {name_json}\n  }}' if last < 0 else (
            f"{'' if raw[last] == ',' else ','}\n  \"mcp\": {{\n    {name_json}\n  }}"
        )
        return raw[:close_i] + ins + raw[close_i:]
    _, vs, ve = mcp
    if raw[vs] != "{":
        return None
    entry = _jsonc_find_prop(raw, vs + 1, ve - 1, name)
    if entry:
        _, evs, eve = entry
        return raw[:evs] + cfg_json + raw[eve:]
    last = _jsonc_last_content(raw, vs + 1, ve - 1)
    if last < 0:
        ins = f"\n    {name_json}\n  "
    else:
        ins = f"{'' if raw[last] == ',' else ','}\n    {name_json}\n  "
    return raw[:ve - 1] + ins + raw[ve - 1:]


def _jsonc_remove_mcp(raw: str, name: str) -> str | None:
    """Removes cirurgicamente mcp.<name>, preservando comentários. None se falhar."""
    open_i, close_i = _jsonc_root_span(raw)
    if open_i is None:
        return None
    mcp = _jsonc_find_prop(raw, open_i + 1, close_i, "mcp")
    if mcp is None:
        return None
    _, vs, ve = mcp
    entry = _jsonc_find_prop(raw, vs + 1, ve - 1, name)
    if entry is None:
        return None
    ps, _, ee = entry
    prev = _jsonc_last_content(raw, vs + 1, ps)
    if prev >= 0 and raw[prev] == ",":
        return raw[:prev] + raw[ee:]
    k = _skip_jsonc_ws(raw, ee, ve - 1)
    if k < ve - 1 and raw[k] == ",":
        return raw[:ps] + raw[k + 1:]
    return raw[:ps] + raw[ee:]


async def _mcp_set_server(name: str, url: str, headers: dict | None = None) -> str:
    """Adds/updates an MCP server entry in opencode.jsonc.

    opencode.jsonc is JSONC (comments allowed), and Python's json module does
    not round-trip comments — so when the file has comments we edit it
    surgically, in place, preserving everything else. Files without comments
    are rewritten with json.dumps. Either way we keep a .bak of whatever we
    overwrite and we restrict file permissions to the owner (tokens are plain
    text in there).
    """
    p = _mcp_file()
    raw = p.read_text() if p.exists() else ""
    data = _jsonc_load(raw)
    mcp = (data or {}).get("mcp") or {}
    cfg = dict(mcp.get(name) or {})
    cfg["type"] = "remote"
    if url:
        cfg["url"] = url
    cfg["enabled"] = True
    if headers is not None:
        merged = dict(cfg.get("headers") or {})
        merged.update(headers)
        cfg["headers"] = merged
    if not cfg.get("url"):
        return "❌ sem URL — passe `--url <url>`"
    cfg_json = json.dumps(cfg, indent=2, ensure_ascii=False)
    try:
        if not raw.strip():
            as_text = json.dumps({"mcp": {name: cfg}}, indent=2, ensure_ascii=False) + "\n"
        elif _has_jsonc_comments(raw):
            edited = _jsonc_upsert_mcp(raw, name, cfg_json)
            if edited is None or not _jsonc_parseable(edited):
                return (
                    "⚠️ `opencode.jsonc` tem comentários e não consegui "
                    "editá-lo com segurança. Edite o arquivo manualmente."
                )
            as_text = edited
        else:
            data.setdefault("mcp", {})[name] = cfg
            as_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    except Exception as e:
        return f"❌ falha ao preparar config: {e}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if raw and raw != as_text:
            bak = p.with_suffix(p.suffix + ".bak")
            bak.write_text(raw)
            os.chmod(bak, 0o600)
        p.write_text(as_text)
        os.chmod(p, 0o600)
    except Exception as e:
        return f"❌ falha ao gravar config: {e}"
    try:
        r = await state._client.post("/mcp", json={"name": name, "config": cfg})
        r.raise_for_status()
        return f"✅ `{name}` configurado (arquivo + servidor em execução)."
    except Exception as e:
        return f"⚠️ Config salva no arquivo, mas o servidor não aplicou: {e}"


async def _mcp_remove_server(name: str, raw: str) -> str:
    """Removes an MCP server entry from opencode.jsonc (preserving comments)
    and asks the opencode server to drop it too."""
    if not raw.strip():
        return f"❌ `{name}` não está configurado no arquivo."
    data = _jsonc_load(raw)
    if name not in ((data or {}).get("mcp") or {}):
        return f"❌ `{name}` não está configurado."
    try:
        if _has_jsonc_comments(raw):
            edited = _jsonc_remove_mcp(raw, name)
            if edited is None or not _jsonc_parseable(edited):
                return (
                    "⚠️ `opencode.jsonc` tem comentários e não consegui "
                    "editá-lo com segurança. Edite o arquivo manualmente."
                )
            as_text = edited
        else:
            mcp = data["mcp"]
            mcp.pop(name, None)
            if not mcp:
                data.pop("mcp", None)
            as_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        p = _mcp_file()
        if raw != as_text:
            bak = p.with_suffix(p.suffix + ".bak")
            bak.write_text(raw)
            os.chmod(bak, 0o600)
        p.write_text(as_text)
        os.chmod(p, 0o600)
    except Exception as e:
        return f"❌ falha ao remover do arquivo: {e}"
    try:
        r = await state._client.request("DELETE", f"/mcp/{name}")
        r.raise_for_status()
        return f"✅ `{name}` removido."
    except Exception as e:
        return f"⚠️ Removido do arquivo, mas o servidor não confirmou: {e}"
