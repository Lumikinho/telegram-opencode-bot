"""Estado mutavel compartilhado (singletons do processo).

Acesso sempre via atributo (`state.TURNS`, `config.OC_URL`) para que
mutacoes feitas em um modulo sejam visiveis nos demais.
"""
import asyncio

import httpx
from telegram.ext import Application

_app_ref: Application | None = None
_client: httpx.AsyncClient | None = None
_server_proc: asyncio.subprocess.Process | None = None
_we_started_server = False
_stream_task: asyncio.Task | None = None
_battery_task: asyncio.Task | None = None
_battery: dict = {"alerts": True, "last_status": None, "full_notified": False}
TURNS: dict[int, dict] = {}
