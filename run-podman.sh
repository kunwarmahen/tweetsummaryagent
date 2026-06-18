#!/usr/bin/env bash
# Build and run the Twitter Summary Agent with Podman.
#
# Prerequisites (once):
#   1. cp .env.example .env                        # set SMTP / Telegram if you want them
#   2. Ollama running on the host with the models pulled (gemma4:e4b, nomic-embed-text).
#   3. An X session — either:
#        - on the host: ./venv/bin/python main.py import-profile   (decrypts Chrome cookies), OR
#        - after start, via the web UI Session page: upload a browser cookie export
#          (no host Chrome/keyring needed — the container-friendly path).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "No .env found — copy .env.example to .env first."; exit 1
fi

IMAGE=twitter-summary-agent
# Resolve the UI port with precedence: shell env > .env > built-in default (8765).
# (Podman's -e wins over --env-file, so we must read .env ourselves to honor it.)
if [[ -z "${APP_PORT:-}" && -f .env ]]; then
  APP_PORT="$(sed -n 's/^[[:space:]]*APP_PORT=//p' .env | tail -n1 | tr -d '[:space:]')"
fi
APP_PORT="${APP_PORT:-8765}"   # default avoids the common 8000 clash; host networking = host port

# The container keeps its OWN data (DB, run snapshots, digests) here — separate from the
# project's ./data so the host and container never write the same SQLite file. Starts empty
# (init_db creates the schema). To seed it later, stop the container and overwrite this dir
# with your own ./data, e.g.:
#   podman stop twitter-summary-agent && cp -a data/. "$TSA_DATA_DIR"/ && ./run-podman.sh
TSA_DATA_DIR="${TSA_DATA_DIR:-$HOME/.local/share/twitter-summary-agent}"
# Auth (the X session) is mounted READ-WRITE so the Session page / `import-cookies` can write it
# from inside the container. Defaults to the project's ./auth (where host import-profile writes).
TSA_AUTH_DIR="${TSA_AUTH_DIR:-$(pwd)/auth}"
mkdir -p "$TSA_DATA_DIR" "$TSA_AUTH_DIR"

if [[ ! -f "$TSA_AUTH_DIR/storage_state.json" ]]; then
  echo "Note: no X session yet — after start, import one at http://localhost:$APP_PORT/session"
  echo "      (or run 'python main.py import-profile' on the host)."
fi

echo "Building $IMAGE…"
podman build -t "$IMAGE" .

echo "Starting container (host networking, port $APP_PORT)…"
echo "  data dir: $TSA_DATA_DIR"
echo "  auth dir: $TSA_AUTH_DIR"
podman run -d --replace --name "$IMAGE" \
  --network=host \
  --env-file .env \
  -e OLLAMA_URL=http://localhost:11434 \
  -e APP_PORT="$APP_PORT" \
  -v "$TSA_DATA_DIR:/app/data:Z" \
  -v "$TSA_AUTH_DIR:/app/auth:Z" \
  --restart unless-stopped \
  "$IMAGE"

echo "Up. Web UI:   http://localhost:$APP_PORT"
echo "Import auth:  http://localhost:$APP_PORT/session   (upload a cookie export)"
echo "Test auth:    podman exec $IMAGE python main.py collect --max-accounts 2"
echo "Logs:         podman logs -f $IMAGE"
echo "Stop:         podman stop $IMAGE"
