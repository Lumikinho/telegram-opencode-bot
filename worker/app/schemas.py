"""Schemas Pydantic do contrato bot <-> worker (vira OpenAPI)."""
from typing import Any, Optional
from pydantic import BaseModel, Field


class TurnPayload(BaseModel):
    turn: dict[str, Any]


class NewTurnRequest(BaseModel):
    chat_id: int = 0


class NewTurnResponse(BaseModel):
    turn: dict[str, Any]


class FoldRequest(BaseModel):
    turn: dict[str, Any]
    event: dict[str, Any] = Field(default_factory=dict)


class FoldDetail(BaseModel):
    reason: Optional[str] = None
    error: Optional[str] = None
    tool_phase: Optional[str] = None
    tool_id: Optional[str] = None
    diff_path: Optional[str] = None


class FoldResponse(BaseModel):
    turn: dict[str, Any]
    action: str
    detail: Optional[FoldDetail] = None


class SelectOptionRequest(BaseModel):
    turn: dict[str, Any]
    request_id: str = ""
    qidx: int = 0
    opt: int = 0


class SelectOptionResponse(BaseModel):
    turn: dict[str, Any]
    changed: bool = False


class SetCustomRequest(BaseModel):
    turn: dict[str, Any]
    request_id: str = ""
    qidx: int = 0


class AnswerCustomRequest(BaseModel):
    turn: dict[str, Any]
    request_id: str = ""
    qidx: int = 0
    text: str = ""


class FormAnswerResponse(BaseModel):
    turn: dict[str, Any]
    complete: bool = False
    answer: dict[str, Any] = Field(default_factory=dict)
    missing: list[int] = Field(default_factory=list)


class SubmitFormRequest(BaseModel):
    turn: dict[str, Any]
    request_id: str = ""


class DropFormRequest(BaseModel):
    turn: dict[str, Any]
    request_id: str = ""


class RenderRunningRequest(BaseModel):
    turn: dict[str, Any]
    opencode_dir: str = ""


class TurnButton(BaseModel):
    text: str
    data: str


class RenderRunningResponse(BaseModel):
    text: str = ""
    keyboard: list[list[TurnButton]] = Field(default_factory=list)


class RenderThinkRequest(BaseModel):
    turn: dict[str, Any]
    elapsed: float = 0.0
    opencode_dir: str = ""


class RenderResultRequest(BaseModel):
    turn: dict[str, Any]


class RenderDiffRequest(BaseModel):
    turn: dict[str, Any]
    path: str = ""
    opencode_dir: str = ""


class TextResponse(BaseModel):
    text: str = ""


class HtmlResponse(BaseModel):
    html: str = ""


class TelegramHtmlRequest(BaseModel):
    text: str = ""
    max_len: Optional[int] = None


class PlainTextRequest(BaseModel):
    html: str = ""


class SplitRequest(BaseModel):
    text: str = ""
    limit: int = 4000


class SplitResponse(BaseModel):
    chunks: list[str] = Field(default_factory=list)


class RedactRequest(BaseModel):
    text: str = ""


class SafeFilenameRequest(BaseModel):
    name: str = ""
    default: str = "arquivo.bin"


class SafeFilenameResponse(BaseModel):
    filename: str = ""


class MediaNoteRequest(BaseModel):
    kind: str = "download_failed"
    filename: str = "anexo"
    size_mb: int = 0


class MediaLimitsResponse(BaseModel):
    max_bytes: int = 0
    fallback_mime: dict[str, str] = Field(default_factory=dict)


class BatteryResponse(BaseModel):
    capacity: Optional[str] = None
    status: Optional[str] = None
    health: Optional[str] = None
    technology: Optional[str] = None
    voltage_now: Optional[str] = None
    current_now: Optional[str] = None
    temp: Optional[str] = None
    formatted: str = ""


class FunnelEntry(BaseModel):
    url: str = ""
    on: bool = False
    mappings: list[str] = Field(default_factory=list)


class FunnelStatusResponse(BaseModel):
    raw: str = ""
    funnels: list[FunnelEntry] = Field(default_factory=list)


class FunnelActionResponse(BaseModel):
    ok: bool = False
    message: str = ""


class RenderMarkdownRequest(BaseModel):
    text: str = ""


class RenderMarkdownResponse(BaseModel):
    html: str = ""
