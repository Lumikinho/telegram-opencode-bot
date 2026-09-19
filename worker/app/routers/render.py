"""Router de render: markdown -> HTML seguro do Telegram."""
from fastapi import APIRouter

from .. import schemas as S
from ..services import render as R

router = APIRouter(prefix="/api/render", tags=["render"])


@router.post("/markdown", response_model=S.RenderMarkdownResponse)
def markdown(body: S.RenderMarkdownRequest) -> dict:
    return {"html": R.markdown_to_html(body.text)}
