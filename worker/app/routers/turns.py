"""Router de turnos: dobra de eventos SSE + renders (port do dispatch de turn_cli.main)."""
from fastapi import APIRouter

from .. import schemas as S
from ..services import turns as T

router = APIRouter(prefix="/api/turns", tags=["turns"])


@router.post("/new", response_model=S.NewTurnResponse)
def new_turn(body: S.NewTurnRequest) -> dict:
    return {"turn": T._ser_turn(T.new_turn(body.chat_id))}


@router.post("/fold", response_model=S.FoldResponse)
def fold(body: S.FoldRequest) -> dict:
    turn = T._deser_turn(body.turn)
    res = T.fold_event(turn, body.event)
    res["turn"] = T._ser_turn(turn)
    return res


@router.post("/select-option", response_model=S.SelectOptionResponse)
def select_option(body: S.SelectOptionRequest) -> dict:
    turn = T._deser_turn(body.turn)
    item = next((q for q in turn["questions"]
                 if q["request_id"] == body.request_id and q["qidx"] == body.qidx), None)
    changed = False
    if item:
        if item.get("multiple"):
            sel = set(turn["qsel"].get((body.request_id, body.qidx)) or ())
            if body.opt in sel:
                sel.discard(body.opt)
            else:
                sel.add(body.opt)
            turn["qsel"][(body.request_id, body.qidx)] = sel
        else:
            opts = item.get("options") or []
            item["answer"] = [opts[body.opt]["value"]] if body.opt < len(opts) else []
        changed = True
    return {"turn": T._ser_turn(turn), "changed": changed}


@router.post("/set-custom", response_model=S.TurnPayload)
def set_custom(body: S.SetCustomRequest) -> dict:
    turn = T._deser_turn(body.turn)
    turn["awaiting_custom"] = (body.request_id, body.qidx)
    return {"turn": T._ser_turn(turn)}


@router.post("/answer-custom", response_model=S.FormAnswerResponse)
def answer_custom(body: S.AnswerCustomRequest) -> dict:
    turn = T._deser_turn(body.turn)
    item = next((q for q in turn["questions"]
                 if q["request_id"] == body.request_id and q["qidx"] == body.qidx), None)
    if item:
        item["answer"] = [body.text]
    turn["awaiting_custom"] = None
    res = T.build_form_answer(turn, body.request_id)
    res["turn"] = T._ser_turn(turn)
    return res


@router.post("/submit-form", response_model=S.FormAnswerResponse)
def submit_form(body: S.SubmitFormRequest) -> dict:
    turn = T._deser_turn(body.turn)
    res = T.build_form_answer(turn, body.request_id)
    if res["complete"]:
        T.drop_questions(turn, body.request_id)
    res["turn"] = T._ser_turn(turn)
    return res


@router.post("/drop-form", response_model=S.TurnPayload)
def drop_form(body: S.DropFormRequest) -> dict:
    turn = T._deser_turn(body.turn)
    T.drop_questions(turn, body.request_id)
    return {"turn": T._ser_turn(turn)}


@router.post("/render-running", response_model=S.RenderRunningResponse)
def render_running(body: S.RenderRunningRequest) -> dict:
    turn = T._deser_turn(body.turn)
    return T.render_running(turn, body.opencode_dir)


@router.post("/render-think", response_model=S.TextResponse)
def render_think(body: S.RenderThinkRequest) -> dict:
    turn = T._deser_turn(body.turn)
    return {"text": T.render_think(turn, body.elapsed, body.opencode_dir)}


@router.post("/render-result", response_model=S.TextResponse)
def render_result(body: S.RenderResultRequest) -> dict:
    turn = T._deser_turn(body.turn)
    return {"text": T.result_text(turn)}


@router.post("/render-diff", response_model=S.TextResponse)
def render_diff(body: S.RenderDiffRequest) -> dict:
    turn = T._deser_turn(body.turn)
    return {"text": T.render_file_diff(turn, body.path, body.opencode_dir)}


@router.post("/telegram-html", response_model=S.HtmlResponse)
def telegram_html(body: S.TelegramHtmlRequest) -> dict:
    return {"html": T.telegram_html(body.text, body.max_len)}


@router.post("/plain-text", response_model=S.TextResponse)
def plain_text(body: S.PlainTextRequest) -> dict:
    return {"text": T.plain_text(body.html)}


@router.post("/split", response_model=S.SplitResponse)
def split(body: S.SplitRequest) -> dict:
    return {"chunks": T.split_text(body.text, body.limit)}


@router.post("/redact", response_model=S.TextResponse)
def redact(body: S.RedactRequest) -> dict:
    return {"text": T.redact_secrets(body.text)}


@router.post("/safe-filename", response_model=S.SafeFilenameResponse)
def safe_filename(body: S.SafeFilenameRequest) -> dict:
    return {"filename": T.safe_filename(body.name, body.default)}


@router.post("/media-note", response_model=S.TextResponse)
def media_note(body: S.MediaNoteRequest) -> dict:
    return {"text": T.media_note(body.kind, body.filename, body.size_mb)}


@router.get("/media-limits", response_model=S.MediaLimitsResponse)
def media_limits() -> dict:
    return {"max_bytes": T.MEDIA_MAX_BYTES, "fallback_mime": T._MEDIA_FALLBACK_MIME}
