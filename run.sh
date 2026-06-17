#!/usr/bin/env bash
# Convenience runner for the Twitter Summary Agent.
#
# Auto-uses the project venv so you never type ./venv/bin/python.
#
# Usage:
#   ./run.sh                 # interactive menu of common commands
#   ./run.sh serve           # pass a command straight through to main.py
#   ./run.sh run --max-accounts 5
#   ./run.sh <any main.py command...>
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

# --- pass-through mode ----------------------------------------------------
# Any arguments => run them directly (and let main.py handle --help, errors).
if [[ $# -gt 0 ]]; then
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
    5) run                Run the digest pipeline once
    6) run (limited)      Run once, capped to N accounts (quick test)
    7) collect            Scrape & dump tweets only (debug, no summary)

  Runs
    8) resume             Resume the most recent failed run (no re-scrape)
    9) delete-run         Delete a run and all its data
   10) archive-backfill   Import past snapshots into the raw tweet archive

  Telegram
   11) telegram-chatid    Discover your Telegram chat id
   12) telegram-test      Send a Telegram test message

    h) help               Show full main.py help
    q) quit
MENU

read -rp "Choice: " choice
case "$choice" in
  1)  run init-db ;;
  2)  run import-profile ;;
  3)  run login ;;
  4)  run serve ;;
  5)  run run ;;
  6)  read -rp "Max accounts: " n; run run --max-accounts "$n" ;;
  7)  run collect ;;
  8)  read -rp "Run id to resume (blank = latest failed): " rid
      if [[ -n "$rid" ]]; then run resume "$rid"; else run resume; fi ;;
  9)  read -rp "Run id to delete: " rid; run delete-run "$rid" ;;
  10) run archive-backfill ;;
  11) run telegram-chatid ;;
  12) run telegram-test ;;
  h|H) run --help ;;
  q|Q) echo "Bye."; exit 0 ;;
  *)  echo "Unknown choice: $choice"; exit 1 ;;
esac
