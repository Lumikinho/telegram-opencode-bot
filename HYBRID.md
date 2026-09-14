# Híbrido Bun+TS+Python (branch bun-ts-python)

Gateway Telegram em **TypeScript/Bun** (`src/`), workers em **Python**
(`py/`) sem dependências de Telegram, falando com o **opencode server v2**
(mesma norma de `bot/opencode.py` na branch `opencode-v2`).

```
.
├── src/
│   ├── index.ts     # bootstrap: server, SSE, battery watch, bot.start
│   ├── config.ts    # .env + validação (espelha bot/config.py)
│   ├── opencode.ts  # cliente HTTP v2 + SSE + sessões (puro, testável)
│   ├── workers.ts   # Bun.spawn -> py/*_cli.py (JSON stdin/stdout)
│   └── telegram.ts  # Bot grammy: auth dono, comandos, texto livre
├── py/
│   ├── battery_cli.py # sysfs -> JSON (sem telegram)
│   ├── funnel_cli.py  # tailscale funnel (status/on/off)
│   └── render_cli.py  # markdown -> html seguro (sem telegram)
├── tests-ts/        # bun test (port da norma v2)
├── run-hybrid.sh    # .env + bun src/index.ts
└── bot/             # núcleo Python original (referência/fallback)
```

## Rodar

```bash
bun install
cp .env.example .env   # preencha BOT_TOKEN + OWNER_ID
bash run-hybrid.sh
```

Vars honradas (mesmas do `.env.example`): `BOT_TOKEN`, `OWNER_ID`,
`CHAT_ID`, `OPENCODE_DIR`, `OPENCODE_SERVER_PORT`, `OPENCODE_SERVER_URL`,
`OPENCODE_SERVER_PASSWORD`, `BATTERY_PATH`, `BATTERY_CHECK_INTERVAL`,
`BATTERY_LOW_PCT`.

## Testes

```bash
bun test                 # tests-ts (v2: auth header, pick_session_id, funnel parse, battery)
venv/bin/pytest -q       # suíte Python original (regressão)
```

## Norma v2 (resumo)

- `Authorization: Basic base64("opencode:<senha>")` em toda rota;
- rotas sob `/api/*` com envelope `{data: ...}`;
- SSE em `GET /api/event` com envelope `{type, data}`;
- prompt em `POST /api/session/{id}/prompt {text, files}`;
- modelo/agente são estado da sessão (`/model`, `/agent`);
- permissões `.../permission/{id}/reply {reply: once|always|reject}`;
- perguntas são forms (`form.created` + `.../form/{id}/reply|cancel`).
