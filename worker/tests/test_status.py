"""Status de ferramentas: nome curto, emoji único, contexto e colapso."""
from app.services import turns as T


def _turn(**kw):
    base = {
        "todos": [], "questions": [], "perm_queue": [],
        "current": None, "reasoning_active": False, "reasoning_text": "",
        "tool_cards": {}, "reads": set(), "writes": set(), "edits": set(),
        "rejected": 0,
    }
    base.update(kw)
    return base


def test_short_tira_prefixo_mcp():
    assert T.tool_short("mcp__todoist__list-tasks") == "list-tasks"
    assert T.tool_short("grep") == "grep"
    assert T.tool_short("") == "ferramenta"


def test_meta_conhecida_e_fallback():
    assert T.tool_meta("grep") == ("🔍", "Buscando")
    assert T.tool_meta("read") == ("📖", "Lendo")
    emoji, verb = T.tool_meta("mcp__todoist__list-tasks")
    assert emoji == "🔧" and verb == "list-tasks"


def test_arg_por_ferramenta_e_truncate():
    assert T.tool_arg("read", {"path": "/home/u/src/bot.ts"}, "/home/u") == "src/bot.ts"
    assert T.tool_arg("grep", {"pattern": "callWorker"}, "") == "callWorker"
    long_cmd = "python3 " + "x" * 100
    assert T.tool_arg("bash", {"command": long_cmd}, "").endswith("…")
    assert len(T.tool_arg("bash", {"command": long_cmd}, "")) == 40


def test_header_mostra_acao_atual():
    turn = _turn(current={"tool": "edit", "label": "src/bot.ts", "out": ""})
    text = T.render_running(turn, "")["text"]
    assert "⏳ Editando src/bot.ts" in text
    assert "[CFG]" not in text


def test_recentes_com_contexto_e_colapso():
    cards = {}
    for i in range(3):
        cards[f"g{i}"] = {"name": "grep", "input": {"pattern": "x"}, "status": "completed"}
    cards["r"] = {"name": "read", "input": {"path": "/a/b.ts"}, "status": "completed"}
    cards["e"] = {"name": "bash", "input": {"command": "false"}, "status": "error",
                  "output": "", "error": "exit 1"}
    turn = _turn(tool_cards=cards)
    text = T.render_running(turn, "")["text"]
    assert "Recentes:" in text
    assert "✅ 🔍 grep ×3" in text
    assert "✅ 📖 read /a/b.ts" in text
    assert "❌ ⚙️ bash false: exit 1" in text
    assert "[OK]" not in text and "[FIND]" not in text


def test_bash_mostra_retorno():
    cards = {"b": {"name": "bash", "input": {"command": "ls /tmp"},
                   "status": "completed", "output": "a.txt\nb.txt", "error": ""}}
    text = T.render_running(_turn(tool_cards=cards), "")["text"]
    assert "✅ ⚙️ bash ls /tmp: a.txt b.txt" in text
