"""Config via .env (raiz do repo) + validacao no import.

O logging e inicializado antes da validacao do OWNER_ID para que a falta
dele gere a mensagem de erro clara em vez de NameError.
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

VERSION = "1.9.1"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
if not OWNER_ID:
    logger.error("OWNER_ID não configurado — recusando iniciar por segurança.")
    raise SystemExit("OWNER_ID obrigatório. Defina-o no .env.")
CHAT_ID = os.getenv("CHAT_ID", "")
OPENCODE_DIR = os.getenv("OPENCODE_DIR", str(Path.home()))
OC_PORT = int(os.getenv("OPENCODE_SERVER_PORT", "4100"))
OC_URL = os.getenv("OPENCODE_SERVER_URL", f"http://127.0.0.1:{OC_PORT}")
BATTERY_PATH = os.getenv("BATTERY_PATH", "/sys/class/power_supply/battery")
BATTERY_CHECK_INTERVAL = int(os.getenv("BATTERY_CHECK_INTERVAL", "30") or "30")
BATTERY_LOW_PCT = int(os.getenv("BATTERY_LOW_PCT", "20") or "20")
