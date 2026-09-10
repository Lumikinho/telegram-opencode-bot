# opencodebot

Bot do Telegram que controla o [opencode](https://opencode.ai) CLI, com
monitor de bateria do aparelho fundido no mesmo processo.

## Estrutura

```
.
├── run.sh              # sobe venv + `python3 -m bot`
├── requirements.txt    # deps de runtime
├── requirements-dev.txt# pytest + ruff
├── pyproject.toml      # metadados + config pytest/ruff
├── .env                # segredos (nunca commitar; ver .env.example)
├── bot/
│   ├── __main__.py     # entrypoint (`python3 -m bot`)
│   ├── app.py          # post_init/post_shutdown/main
│   ├── config.py       # .env + validacao no import
│   ├── state.py        # singletons do processo (via atributo!)
│   ├── auth.py         # dono + chat de destino
│   ├── opencode.py     # cliente HTTP + ciclo do `opencode serve`
│   ├── mcp_cfg.py      # bloco MCP no JSONC do opencode
│   ├── turns.py        # turnos: stream SSE, permissoes, perguntas, midia
│   ├── render.py       # Markdown->HTML, teclados, resumos
│   ├── restart.py      # /restart: bot, servidor ou ambos
│   ├── battery.py      # monitor 30s + /bateria
│   └── handlers/
│       ├── commands.py # /new /status /restart /bateria ...
│       ├── callbacks.py# botoes inline
│       └── chat.py     # mensagens livres e anexos
└── tests/              # pytest (funcoes puras + registro de handlers)
```

Regra de estado: mutáveis vivem em `state.py`/`config.py` e o acesso é
sempre por atributo (`state.TURNS`, `config.OC_URL`) — nunca `from state
import TURNS`, senão mutações não se propagam.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # e preencha BOT_TOKEN + OWNER_ID
bash run.sh
```

## Variáveis de ambiente

| Var | Padrão | Descrição |
| --- | ------ | --------- |
| `BOT_TOKEN` | — | token do @BotFather |
| `OWNER_ID` | — | seu id de usuário do Telegram |
| `CHAT_ID` | vazio (usa OWNER_ID) | chat das notificações |
| `OPENCODE_DIR` | `$HOME` | diretório das sessões |
| `OPENCODE_SERVER_PORT` | `4100` | porta do `opencode serve` |
| `OPENCODE_SERVER_URL` | `http://127.0.0.1:<porta>` | URL do servidor |
| `BATTERY_PATH` | `/sys/class/power_supply/battery` | sysfs da bateria |
| `BATTERY_CHECK_INTERVAL` | `30` | segundos entre checagens |
| `BATTERY_LOW_PCT` | `20` | % que dispara o alerta |

## Comandos

/new, /status (com bateria), /bateria [on|off], /restart [bot|servidor|ambos],
/cancel, /models, /agents, /sessions, /summarize, /stats, /mcp, /version, /help.

## Testes e lint

```bash
pip install -r requirements-dev.txt
pytest
ruff check bot/ tests/
```
