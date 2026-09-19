.PHONY: dev dev-bot dev-worker test test-bot test-worker lint lint-worker gen-types up down logs clean install

install:
	cd bot && bun install
	cd worker && uv sync --group dev

dev-worker:
	cd worker && uv run uvicorn app.main:app --host 127.0.0.1 --port 8090

dev-bot:
	cd bot && bun src/index.ts

dev:
	@echo "Suba o worker em outro terminal: make dev-worker"
	cd bot && bun src/index.ts

test-bot:
	cd bot && BOT_TOKEN=test OWNER_ID=1 bun test

test-worker:
	cd worker && uv run --group dev pytest -q

test: test-worker test-bot

lint:
	cd bot && bunx oxlint src tests && bunx tsc --noEmit

lint-worker:
	cd worker && uvx ruff check app/ tests/

gen-types:
	cd worker && (uv run uvicorn app.main:app --host 127.0.0.1 --port 8090 & echo $$! > /tmp/meu-bot-worker.pid; sleep 3; curl -sf http://127.0.0.1:8090/openapi.json -o ../worker-openapi.json; kill $$(cat /tmp/meu-bot-worker.pid))
	cd bot && bunx openapi-typescript ../worker-openapi.json -o src/types/openapi.ts

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f

clean:
	rm -rf bot/node_modules bot/data data worker/.venv
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
