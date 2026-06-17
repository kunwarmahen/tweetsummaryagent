#!/usr/bin/env bash
# Build and run the Twitter Summary Agent with Podman.
#
# Prerequisites (on the HOST, once):
#   1. Be logged into X (x.com) in Google Chrome.
#   2. ./venv/bin/python main.py import-profile   # decrypts your session into auth/
#   3. cp .env.example .env                        # set SMTP / Telegram if you want them
#   4. Ollama running on the host with the models pulled (gemma4:e4b, nomic-embed-text).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "No .env found — copy .env.example to .env first."; exit 1
fi
if [[ ! -f auth/storage_state.json ]]; then
  echo "No auth/storage_state.json — run 'python main.py import-profile' on the host first."; exit 1
fi

IMAGE=twitter-summary-agent
# Resolve the UI port with precedence: shell env > .env > built-in default (8765).
# (Podman's -e wins over --env-file, so we must read .env ourselves to honor it.)
if [[ -z "${APP_PORT:-}" && -f .env ]]; then
  APP_PORT="$(sed -n 's/^[[:space:]]*APP_PORT=//p' .env | tail -n1 | tr -d '[:space:]')"
fi
APP_PORT="${APP_PORT:-8765}"   # default avoids the common 8000 clash; host networking = host port

echo "Building $IMAGE…"
podman build -t "$IMAGE" .

echo "Starting container (host networking, port $APP_PORT)…"
podman run -d --replace --name "$IMAGE" \
  --network=host \
  --env-file .env \
  -e OLLAMA_URL=http://localhost:11434 \
  -e APP_PORT="$APP_PORT" \
  -v "$(pwd)/data:/app/data:Z" \
  -v "$(pwd)/auth:/app/auth:ro,Z" \
  --restart unless-stopped \
  "$IMAGE"

echo "Up. Web UI: http://localhost:$APP_PORT"
echo "Logs:        podman logs -f $IMAGE"
echo "Run once:    podman exec $IMAGE python main.py run"
echo "Stop:        podman stop $IMAGE"
