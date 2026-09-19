# meu-bot

Bot Telegram (grammY + Bun) que conversa com o `opencode serve`, com a
lógica pesada num worker Python (FastAPI) via HTTP.

```
meu-bot/
├── bot/            # TypeScript + Bun (camada Telegram)
│   ├── src/
│   │   ├── index.ts        # entrypoint: polling (dev) / webhook (prod)
│   │   ├── bot.ts          # instância + middlewares + registro
│   │   ├── config/env.ts   # validação de env (zod)
│   │   ├── commands/       # um arquivo por comando (+ index.ts)
│   │   ├── handlers/       # panels, messages, callbacks
│   │   ├── middlewares/    # auth (dono), logging, ratelimit
│   │   ├── services/       # opencode, python (HTTP), turns, exec, battery, restart, store
│   │   ├── db/             # bun:sqlite (sessões por chat)
│   │   ├── utils/          # models, version, media
│   │   └── types/openapi.ts# gerado do worker (make gen-types)
│   ├── tests/              # bun test
│   ├── package.json
│   └── tsconfig.json
├── worker/         # Python (lógica pesada)
│   ├── app/
│   │   ├── main.py         # FastAPI (/healthz, /docs, /openapi.json)
│   │   ├── routers/        # turns, battery, funnel, render
│   │   ├── services/       # regras puras (sem Telegram)
│   │   ├── schemas.py      # pydantic (= contrato OpenAPI)
│   │   └── config.py
│   ├── tests/              # pytest (via TestClient)
│   ├── pyproject.toml      # gerenciado com uv
│   └── uv.lock
├── docker-compose.yml      # bot + worker
├── .env.example
├── Makefile                # make dev, make test, ...
└── README.md
```

## Decisões

- **grammY**: TypeScript-first, funciona no Bun, plugins de session/rate-limit/i18n.
- **HTTP (FastAPI)**: bot chama o worker por HTTP com timeout por chamada;
  o streaming de status continua no gateway, então tarefa demorada não trava o bot.
- **Contrato**: o FastAPI gera o OpenAPI; `make gen-types` regenera
  `bot/src/types/openapi.ts` (`openapi-typescript`) — o bot fica tipado de ponta a ponta.
- **Polling em dev, webhook em prod**: `APP_ENV=dev` usa long polling;
  `APP_ENV=prod` + `WEBHOOK_DOMAIN` sobe `Bun.serve` e registra o webhook.

## Setup

```bash
cp .env.example .env   # preencha BOT_TOKEN + OWNER_ID
make install           # bun install + uv sync
make dev-worker        # terminal 1: worker em :8090
make dev               # terminal 2: bot (polling)
```

Produção: `make up` (compose: worker saudável antes do bot; `restart: unless-stopped`).

## Comandos

`/menu /new /cancel /status /bateria /funnel /models /agents /sessions /exec /restart /help`.
`/models` lista só modelos gratuitos (custo 0 no `/api/model`).

## Testes e lint

```bash
make test        # pytest do worker + bun test do bot
make lint        # oxlint + tsc
make lint-worker # ruff
```

## Env

| Var | Padrão | Descrição |
| --- | ------ | --------- |
| `BOT_TOKEN` | — | token do @BotFather |
| `OWNER_ID` | — | seu id de usuário do Telegram |
| `WORKER_URL` | `http://127.0.0.1:8090` | URL do worker (no compose: `http://worker:8090`) |
| `APP_ENV` | `dev` | `dev` polling / `prod` webhook |
| `WEBHOOK_DOMAIN` | — | domínio do webhook em prod |
| `DATA_DIR` | `./data` | sqlite do gateway |
| demais | ver `.env.example` | mesmas do projeto original |
