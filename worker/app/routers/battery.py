"""Router de bateria: sysfs -> JSON."""
from fastapi import APIRouter, Query

from .. import config, schemas as S
from ..services import battery as B

router = APIRouter(prefix="/api/battery", tags=["battery"])


@router.get("", response_model=S.BatteryResponse)
def battery(battery_path: str = Query(default="")) -> dict:
    info = B.read_battery(battery_path or config.BATTERY_PATH)
    info["formatted"] = B.format_bateria(info)
    return info
