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
│   ├── turns.ts     # ciclo de turnos: status/stream/finish, typing, botões
│   ├── workers.ts   # Bun.spawn -> py/*_cli.py (JSON stdin/stdout)
│   └── telegram.ts  # Bot grammy: auth dono, comandos, texto livre
├── py/
│   ├── battery_cli.py # sysfs -> JSON (sem telegram)
│   ├── funnel_cli.py  # tailscale funnel (status/on/off)
│   ├── render_cli.py  # markdown -> html seguro (sem telegram)
│   └── turn_cli.py    # dobra de eventos v2 + render (port puro de turns.py/render.py)
├── tests-ts/        # bun test (port da norma v2)
├── tests/test_turn_cli.py # pytest do worker de turnos via CLI JSON
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
bun test                 # tests-ts (v2: auth header, pick_session_id, funnel parse, battery, turnos)
venv/bin/pytest -q       # suíte Python (regressão + test_turn_cli via CLI JSON)
```

## Turnos (parte Python)

O estado de protocolo do turno vive no worker `py/turn_cli.py` — port
stateless e sem Telegram de `bot/turns.py` + funções puras de
`bot/render.py`. O gateway (`src/turns.ts`) guarda o JSON do turno, dobra
cada evento SSE via `fold` e executa o I/O: placeholder "pensando…",
edição de status com throttle 1s, balão de resposta por streaming,
ordem invertida resposta-em-cima/think-embaixo e botões pós-turno.

Ações do worker: `new_turn`, `fold` (none|push|push_force|finish),
`select_option`, `set_custom`/`answer_custom`, `submit_form`/`drop_form`,
`render_running`/`render_think`/`render_result`, `telegram_html`,
`plain_text`, `split`, `redact`. Teclados saem como JSON
`[{text, data}]` (callbacks `perm:`/`qo:`/`qt:`/`qs:`/`qr:`/`qc:`,
dentro do limite de 64 bytes do Telegram).

## Bun nativo

- `src/store.ts` usa `bun:sqlite` (`HYBRID_DB`, padrão
  `<OPENCODE_DIR>/.opencode_bot_hybrid.sqlite`): chats (sid/model/agent)
  e kv sobrevivem a reinícios — substitui o PicklePersistence;
- `opencode serve` é spawnado com `Bun.spawn`, stdout/stderr direto no
  log via `Bun.file`, senha com `crypto.getRandomValues`, espera com
  `Bun.sleep`; SSE via `fetch` + reader nativo.

## Mídia e restart

- Anexos (foto, documento, áudio, voz, vídeo, vídeo redondo, GIF):
  `src/media.ts` coleta/download (limite 20 MiB, data URL base64) com
  nomes/MIME/notas vindos do worker (`safe_filename`, `media_note`,
  `media_limits`); legenda vira o prompt, notas de anexo falho anexadas;
- `/restart [bot|servidor|ambos]` em `src/restart.ts`: mata turnos,
  recicla o servidor (relê porta/URL do `.env`, kill por padrão com teto
  de 3 rodadas) ou relança o bot via `run-hybrid.sh` + exit; teclado de
  confirmação `__restart:*` igual ao núcleo Python.

## Norma v2 (resumo)

- `Authorization: Basic base64("opencode:<senha>")` em toda rota;
- rotas sob `/api/*` com envelope `{data: ...}`;
- SSE em `GET /api/event` com envelope `{type, data}`;
- prompt em `POST /api/session/{id}/prompt {text, files}`;
- modelo/agente são estado da sessão (`/model`, `/agent`);
- permissões `.../permission/{id}/reply {reply: once|always|reject}`;
- perguntas são forms (`form.created` + `.../form/{id}/reply|cancel`).
