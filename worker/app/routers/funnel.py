"""Router do funnel Tailscale."""
from fastapi import APIRouter

from .. import schemas as S
from ..services import funnel as F

router = APIRouter(prefix="/api/funnel", tags=["funnel"])


@router.get("/status", response_model=S.FunnelStatusResponse)
async def status() -> dict:
    return await F._status()


@router.post("/on", response_model=S.FunnelActionResponse)
async def on() -> dict:
    return await F._on()


@router.post("/off", response_model=S.FunnelActionResponse)
async def off() -> dict:
    return await F._off()
