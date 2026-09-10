"""Monitor de bateria (30s), /bateria e estado de carga."""
from pathlib import Path
import asyncio
import logging
from . import config, state
from .auth import _get_owner_chat
from .render import _safe_send_message


logger = logging.getLogger(__name__)


state._battery_task: asyncio.Task | None = None
state._battery = {"alerts": True, "last_status": None, "full_notified": False}

_BAT_STATUS_PT = {
    "Charging": "carregando",
    "Discharging": "descarregando",
    "Full": "cheia",
    "Not charging": "sem carregar",
}

_BAT_STATUS_EMOJI = {
    "Charging": "⚡",
    "Discharging": "🔋",
    "Full": "✅",
    "Not charging": "⏹️",
}


def read_battery() -> dict:
    info = {}
    p = Path(config.BATTERY_PATH)
    for key in ("capacity", "status", "health", "technology",
                "voltage_now", "current_now", "temp"):
        try:
            info[key] = (p / key).read_text().strip()
        except (FileNotFoundError, PermissionError, NotADirectoryError, OSError):
            info[key] = None
    return info


def format_bateria(info: dict) -> str:
    status = info.get("status") or "Unknown"
    lines = ["🔋 *Bateria*", ""]
    cap = info.get("capacity")
    if cap is not None:
        try:
            pct = int(cap)
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(f"*{pct}%*  `{bar}`")
        except ValueError:
            lines.append(f"Carga: `{cap}`")
    else:
        lines.append("Carga: desconhecida")
    lines.append(f"Estado: {_BAT_STATUS_EMOJI.get(status, '')} "
                 f"{_BAT_STATUS_PT.get(status, status)}")
    temp_raw = info.get("temp")
    if temp_raw:
        try:
            lines.append(f"Temp: {int(temp_raw) / 10:.1f}°C")
        except ValueError:
            pass
    volt_raw = info.get("voltage_now")
    if volt_raw:
        try:
            lines.append(f"Tensão: {int(volt_raw) / 1_000_000:.2f}V")
        except ValueError:
            pass
    curr_raw = info.get("current_now")
    if curr_raw:
        try:
            curr_ma = int(curr_raw) / 1000
            if curr_ma:
                lines.append(f"Corrente: {abs(curr_ma):.0f}mA")
        except ValueError:
            pass
    if info.get("health"):
        lines.append(f"Saúde: {info['health']}")
    if info.get("technology"):
        lines.append(f"Tecnologia: {info['technology']}")
    return "\n".join(lines)


async def _battery_say(chat_id: int, text: str):
    app = state._app_ref
    if app is None:
        return
    await _safe_send_message(app.bot, chat_id, text, parse_mode="Markdown")


async def _battery_tick():
    app = state._app_ref
    if app is None:
        return
    info = read_battery()
    cap_raw = info.get("capacity")
    if cap_raw is None:
        return
    try:
        pct = int(cap_raw)
    except ValueError:
        return
    status = info.get("status") or "Unknown"
    chat_id = _get_owner_chat()
    prev = state._battery["last_status"]

    if prev is not None and prev != status and chat_id:
        if status == "Charging":
            await _battery_say(chat_id, f"🔌 *Carregador conectado* ({pct}%).")
        elif status == "Discharging":
            await _battery_say(chat_id, f"🔋 *Na bateria* ({pct}%).")

    if status == "Full" and not state._battery["full_notified"]:
        state._battery["full_notified"] = True
        if chat_id:
            await _battery_say(chat_id, f"🔋 *Carga completa!* {pct}%.")
    elif status != "Full":
        state._battery["full_notified"] = False

    if (state._battery["alerts"] and chat_id and pct <= config.BATTERY_LOW_PCT
            and status not in ("Charging", "Full")):
        crit = "‼️ *crítica* " if pct <= 10 else ""
        await _battery_say(
            chat_id,
            f"⚠️ *Bateria baixa!* {crit}{pct}% restante. Conecte o carregador.",
        )
        logger.warning("Bateria baixa: %d%% (%s)", pct, status)

    state._battery["last_status"] = status


async def _battery_watch():
    await asyncio.sleep(10)
    while True:
        try:
            await _battery_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Falha no monitor da bateria")
        await asyncio.sleep(config.BATTERY_CHECK_INTERVAL)
