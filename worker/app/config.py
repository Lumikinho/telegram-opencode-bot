"""Config do worker via env."""
import os
from pathlib import Path

HOST = os.getenv("WORKER_HOST", "127.0.0.1")
PORT = int(os.getenv("WORKER_PORT", "8090") or "8090")
BATTERY_PATH = os.getenv("BATTERY_PATH", "/sys/class/power_supply/battery")
OPENCODE_DIR = os.getenv("OPENCODE_DIR", str(Path.home()))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info")
