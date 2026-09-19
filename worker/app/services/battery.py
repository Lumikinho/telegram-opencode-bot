#!/usr/bin/env python3
"""Worker de bateria: sysfs -> JSON. Sem dependências de Telegram.

stdin:  {"battery_path": "/sys/class/power_supply/battery"}
stdout: {"capacity": ..., "status": ..., ..., "formatted": "..."}
"""
import json
import sys
from pathlib import Path

STATUS_PT = {
    "Charging": "carregando",
    "Discharging": "descarregando",
    "Full": "cheia",
    "Not charging": "sem carregar",
}
STATUS_EMOJI = {
    "Charging": "»",
    "Discharging": "[BAT]",
    "Full": "[OK]",
    "Not charging": "[STOP]",
}


def read_battery(base: str) -> dict:
    info: dict = {}
    p = Path(base)
    for key in ("capacity", "status", "health", "technology",
                "voltage_now", "current_now", "temp"):
        try:
            info[key] = (p / key).read_text().strip()
        except (FileNotFoundError, PermissionError, NotADirectoryError, OSError):
            info[key] = None
    return info


def format_bateria(info: dict) -> str:
    status = info.get("status") or "Unknown"
    lines = ["[BAT] *Bateria*", ""]
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
    lines.append(f"Estado: {STATUS_EMOJI.get(status, '')} "
                 f"{STATUS_PT.get(status, status)}")
    if info.get("temp"):
        try:
            lines.append(f"Temp: {int(info['temp']) / 10:.1f}°C")
        except ValueError:
            pass
    if info.get("voltage_now"):
        try:
            lines.append(f"Tensão: {int(info['voltage_now']) / 1_000_000:.2f}V")
        except ValueError:
            pass
    if info.get("current_now"):
        try:
            curr_ma = int(info["current_now"]) / 1000
            if curr_ma:
                lines.append(f"Corrente: {abs(curr_ma):.0f}mA")
        except ValueError:
            pass
    if info.get("health"):
        lines.append(f"Saúde: {info['health']}")
    if info.get("technology"):
        lines.append(f"Tecnologia: {info['technology']}")
    return "\n".join(lines)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    base = payload.get("battery_path") or "/sys/class/power_supply/battery"
    info = read_battery(base)
    info["formatted"] = format_bateria(info)
    json.dump(info, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
