#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

if [ ! -f ".env" ]; then
    echo "No .env file found. Copying from .env.example..."
    cp .env.example .env
    echo "Edit .env with your BOT_TOKEN before running again."
    exit 1
fi

# O .env é a fonte da verdade: exporta por cima de qualquer valor herdado
# (tmux/session), senão o python herdaria ex. OPENCODE_SERVER_PORT antigo.
set -a
# shellcheck disable=SC1091
source .env
set +a

echo "Starting opencode bot..."
python3 -m bot