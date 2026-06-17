#!/usr/bin/env bash
# Convenience runner for the Twitter Summary Agent.
#
# Auto-uses the project venv so you never type ./venv/bin/python.
#
# Usage:
#   ./run.sh                 # interactive menu (auto-starts `serve` after 5s)
#   ./run.sh serve           # pass a command straight through to main.py
#   ./run.sh run --max-accounts 5
#   ./run.sh <any main.py command...>
#   ./run.sh deploy          # build + (re)deploy the Podman container (run-podman.sh)
#   ./run.sh logs            # follow the running container's logs
set -euo pipefail
cd "$(dirname "$0")"

# --- pick the interpreter -------------------------------------------------
if [[ -x venv/bin/python ]]; then
  PY="venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  PY="python"
fi

run() {
  echo "+ $PY main.py $*"
  exec "$PY" main.py "$@"
}

deploy() {           # build + (re)deploy the container via the dedicated script
  echo "+ ./run-podman.sh"
  exec ./run-podman.sh
}

container_logs() {
  echo "+ podman logs -f twitter-summary-agent"
  exec podman logs -f twitter-summary-agent
}

# --- pass-through mode ----------------------------------------------------
# Any arguments => run them directly (and let main.py handle --help, errors).
# A few keywords are intercepted for container ops (not main.py commands).
if [[ $# -gt 0 ]]; then
  case "$1" in
    deploy|podman) deploy ;;
    logs)          container_logs ;;
  esac
  run "$@"
fi

# --- interactive menu (no args) -------------------------------------------
cat <<'MENU'
Twitter Summary Agent — what would you like to do?

  Setup
    1) init-db            Create the SQLite database and tables
    2) import-profile     Reuse your logged-in Chrome X session (decrypts cookies)
    3) login              One-time browser login (fallback)

  Daily use
    4) serve              Start the web UI + scheduler (http://127.0.0.1:8000)
    5) run                Run the digest pipeline once (scrape -> deliver)
    6) run (limited)      Run once, capped to N accounts (quick test)
    7) collect            Scrape & dump tweets only (debug, no summary)

  Pipeline phases (decoupled scheduling)
    8) ingest             Scrape new tweets into the archive
    9) process            Refresh the live "Today" draft (render, no send)
   10) deliver            Finalize + send today's digest

  Runs
   11) resume             Resume the most recent failed run (no re-scrape)
   12) delete-run         Delete a run and all its data
   13) reset-runs         Wipe ALL run data (keeps settings/accounts/topics)
   14) archive-backfill   Import past snapshots into the raw tweet archive

  Container (Podman)
   15) deploy             Build image + (re)deploy the container
   16) logs               Follow the running container's logs

  Telegram
   17) telegram-chatid    Discover your Telegram chat id
   18) telegram-test      Send a Telegram test message

    h) help               Show full main.py help
    q) quit

  (no choice within 5s -> starts the server)
MENU

# Default to `serve` if the user doesn't choose within 5 seconds
# (or just presses Enter). `read` times out with a non-zero status,
# which `set -e` would treat as fatal, so guard it.
if ! read -t 5 -rp "Choice [default: serve]: " choice || [[ -z "$choice" ]]; then
  echo "+ defaulting to serve"
  choice=4
fi

case "$choice" in
  1)  run init-db ;;
  2)  run import-profile ;;
  3)  run login ;;
  4)  run serve ;;
  5)  run run ;;
  6)  read -rp "Max accounts: " n; run run --max-accounts "$n" ;;
  7)  run collect ;;
  8)  run ingest ;;
  9)  run process ;;
  10) run deliver ;;
  11) read -rp "Run id to resume (blank = latest failed): " rid
      if [[ -n "$rid" ]]; then run resume "$rid"; else run resume; fi ;;
  12) read -rp "Run id to delete: " rid; run delete-run "$rid" ;;
  13) run reset-runs ;;
  14) run archive-backfill ;;
  15) deploy ;;
  16) container_logs ;;
  17) run telegram-chatid ;;
  18) run telegram-test ;;
  h|H) run --help ;;
  q|Q) echo "Bye."; exit 0 ;;
  *)  echo "Unknown choice: $choice"; exit 1 ;;
esac
