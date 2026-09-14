#!/bin/bash
cd "$(dirname "$0")"

if [ ! -f ".env" ]; then
    echo "No .env file found. Copying from .env.example..."
    cp .env.example .env
    echo "Edit .env with your BOT_TOKEN before running again."
    exit 1
fi

# .env é a fonte da verdade (igual ao run.sh Python).
set -a
# shellcheck disable=SC1091
source .env
set +a

export BUN_INSTALL="$HOME/.bun"
export PATH="$BUN_INSTALL/bin:$PATH"

if [ ! -d "node_modules" ]; then
    echo "Installing bun dependencies..."
    bun install
fi

echo "Starting hybrid bot (Bun+TS gateway, Python workers)..."
bun src/index.ts
