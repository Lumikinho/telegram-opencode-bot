"""Testes do monitor de bateria (sem hardware real)."""
import bot.config as config
from bot.battery import format_bateria, read_battery


def _fake_sysfs(tmp_path):
    d = tmp_path / "battery"
    d.mkdir()
    (d / "capacity").write_text("15\n")
    (d / "status").write_text("Discharging\n")
    (d / "health").write_text("Good\n")
    (d / "technology").write_text("Li-ion\n")
    (d / "voltage_now").write_text("4000000\n")
    (d / "current_now").write_text("500000\n")
    (d / "temp").write_text("300\n")
    return str(d)


def test_read_battery_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BATTERY_PATH", _fake_sysfs(tmp_path))
    info = read_battery()
    assert info["capacity"] == "15"
    assert info["status"] == "Discharging"


def test_read_battery_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BATTERY_PATH", str(tmp_path / "nao-existe"))
    info = read_battery()
    assert info["capacity"] is None


def test_format_bateria_full():
    out = format_bateria({
        "capacity": "15", "status": "Discharging", "health": "Good",
        "technology": "Li-ion", "voltage_now": "4000000",
        "current_now": "500000", "temp": "300",
    })
    assert "*15%*" in out
    assert "descarregando" in out
    assert "30.0" in out


def test_format_bateria_empty():
    out = format_bateria({})
    assert "desconhecida" in out
