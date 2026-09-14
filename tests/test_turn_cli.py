"""Worker de turnos (py/turn_cli.py): dobra de eventos v2 + render puro."""
import json
import subprocess
import sys
from pathlib import Path

WORKER = [sys.executable, str(Path(__file__).resolve().parent.parent / "py" / "turn_cli.py")]


def call(turn, action, **kw):
    kw.update(action=action, turn=turn)
    r = subprocess.run(WORKER, input=json.dumps(kw), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "error" not in out, out["error"]
    return out


def fresh_turn(sid="ses_abc"):
    t = call(None, "new_turn", chat_id=1)["turn"]
    t["sid"] = sid
    return t


def test_new_turn_shape():
    t = fresh_turn()
    assert t["chat_id"] == 1 and t["busy"] is True and t["done"] is False
    assert t["out_text"] == "" and t["perm_queue"] == [] and t["questions"] == []


def test_text_delta_acumula_sem_push():
    t = fresh_turn()
    r = call(t, "fold", event={"type": "session.text.delta", "data": {"sessionID": "ses_abc", "delta": "olá "}})
    assert r["action"] == "none"
    assert r["turn"]["out_text"] == "olá "


def test_permission_asked_forca_push():
    t = fresh_turn()
    r = call(t, "fold", event={"type": "permission.asked", "data": {
        "sessionID": "ses_abc", "id": "p1", "action": "bash",
        "message": "rodar ls", "resources": ["*"]}})
    assert r["action"] == "push_force"
    assert len(r["turn"]["perm_queue"]) == 1
    rr = call(r["turn"], "render_running", opencode_dir="/tmp")
    assert "Permissão pedida" in rr["text"]
    assert rr["keyboard"][0][0]["data"] == "perm:p1:once"


def test_tool_called_e_success_registram_leitura():
    t = fresh_turn()
    t = call(t, "fold", event={"type": "session.tool.called", "data": {
        "sessionID": "ses_abc", "id": "t1", "name": "read",
        "input": {"filePath": "/tmp/x.txt"}}})["turn"]
    t = call(t, "fold", event={"type": "session.tool.success", "data": {
        "sessionID": "ses_abc", "id": "t1"}})["turn"]
    assert t["reads"] == ["/tmp/x.txt"]


def test_form_created_vira_pergunta_e_submit_monta_answer():
    t = fresh_turn()
    form = {"id": "f1", "sessionID": "ses_abc", "title": "Deploy", "fields": [
        {"name": "env", "title": "Ambiente", "type": "string",
         "options": [{"label": "prod", "value": "prod"}, {"label": "dev", "value": "dev"}]}]}
    r = call(t, "fold", event={"type": "form.created", "data": {"form": form}})
    assert r["action"] == "push_force"
    t = r["turn"]
    assert len(t["questions"]) == 1
    t = call(t, "select_option", request_id="f1", qidx=0, opt=0)["turn"]
    sub = call(t, "submit_form", request_id="f1")
    assert sub["complete"] is True and sub["answer"] == {"env": "prod"}
    assert sub["turn"]["questions"] == []


def test_submit_incompleto_aponta_missing():
    t = fresh_turn()
    form = {"id": "f2", "sessionID": "ses_abc", "fields": [
        {"name": "a", "title": "A"}, {"name": "b", "title": "B"}]}
    t = call(t, "fold", event={"type": "form.created", "data": {"form": form}})["turn"]
    t = call(t, "answer_custom", request_id="f2", qidx=0, text="x")["turn"]
    sub = call(t, "submit_form", request_id="f2")
    assert sub["complete"] is False and sub["missing"] == [1]


def test_idle_termina_e_think_resume():
    t = fresh_turn()
    t = call(t, "fold", event={"type": "session.text.delta", "data": {
        "sessionID": "ses_abc", "delta": "pronto"}})["turn"]
    r = call(t, "fold", event={"type": "session.idle", "data": {"sessionID": "ses_abc"}})
    assert r["action"] == "finish" and r["detail"]["reason"] == "idle"
    th = call(t, "render_think", elapsed=65, opencode_dir="/tmp")
    assert "pensou em 1m05s" in th["text"]
    assert "blockquote expandable" in th["text"]


def test_execution_failed_preenche_erro_e_termina():
    t = fresh_turn()
    r = call(t, "fold", event={"type": "session.execution.failed", "data": {
        "sessionID": "ses_abc", "error": "boom"}})
    assert r["action"] == "finish"
    assert "boom" in r["turn"]["out_text"]


def test_render_result_html_seguro():
    t = fresh_turn()
    t["out_text"] = "**oi** `x`"
    r = call(t, "render_result")
    assert "<b>oi</b>" in r["text"] and "<code>x</code>" in r["text"]


def test_redact_mascara_token():
    token = "ghp_" + "a1b2" * 9
    r = call(None, "redact", text=f"Bearer {token} fim")
    assert token not in r["text"]


def test_split_quebra_longo():
    r = call(None, "split", text="\n".join(["x" * 100] * 50), limit=1000)
    assert len(r["chunks"]) > 1
    assert all(len(c) <= 1000 for c in r["chunks"])


def test_acao_desconhecida_erro():
    r = subprocess.run(WORKER, input='{"action": "x"}', capture_output=True, text=True, timeout=30)
    assert "desconhecida" in r.stdout


def test_safe_filename_limpa_e_trunca():
    r = call(None, "safe_filename", name="nota: fiscal/final?.pdf", default="x.bin")
    assert r["filename"] == "nota_ fiscal_final_.pdf"
    r = call(None, "safe_filename", name="", default="doc.bin")
    assert r["filename"] == "doc.bin"


def test_media_note_formatos():
    over = call(None, "media_note", kind="oversize", filename="v.mp4", size_mb=25)
    assert over["text"] == "[anexo ignorado (25 MiB, limite 20 MiB): v.mp4]"
    assert call(None, "media_note", kind="inaccessible", filename="a.ogg")["text"] == "[anexo não acessível: a.ogg]"
    assert call(None, "media_note", kind="download_failed", filename="b")["text"] == "[falha ao baixar anexo: b]"


def test_media_limits():
    r = call(None, "media_limits")
    assert r["max_bytes"] == 20 * 1024 * 1024
    assert r["fallback_mime"]["voice"] == "audio/ogg"
