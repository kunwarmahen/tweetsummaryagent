# Setup Guide

## Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) running locally with the models pulled:
  ```bash
  ollama pull gemma4:e4b
  ollama pull nomic-embed-text   # only for embedding clustering mode
  ```
- **Google Chrome**, logged into X (x.com) — the app reuses that session.
- An SMTP account for delivery (e.g. Gmail with an App Password) — only needed for email.

## Install

```bash
cd twitter_summary_agent
python -m venv venv && source venv/bin/activate
pip install -e .
playwright install chromium
```

## Configure bootstrap secrets

Copy the example env and fill in the few secrets needed before the DB exists:

```bash
cp .env.example .env
```

`.env` holds only:

| Variable | Purpose |
|----------|---------|
| `OLLAMA_URL` | Ollama endpoint (default `http://localhost:11434`) |
| `OLLAMA_MODEL` | Model name (default `gemma4:e4b`) |
| `SMTP_HOST` / `SMTP_PORT` | Mail server |
| `SMTP_USER` / `SMTP_PASSWORD` | Mail auth (use an App Password, not your real password) |
| `EMAIL_FROM` / `EMAIL_TO` | Digest sender/recipient |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram delivery (optional) |

Everything else (schedule, excluded accounts, topics, digest style, clustering, thread
stitching) is set in the web UI and stored in SQLite.

## Initialize the database

```bash
python main.py init-db
```

## Import your X session

Make sure you're logged into X in **Google Chrome**, then:

```bash
python main.py import-profile
```

This decrypts your X/Twitter cookies from Chrome and saves a reusable session
(`auth/storage_state.json`) plus your handle (`auth/session_meta.json`). You should see
`Logged in as @yourhandle`. **Re-run `import-profile` if scraping starts failing with auth
errors** (session cookies expire periodically).

> Why not a scripted login? On Linux, Chrome encrypts cookies with a desktop-keyring key, so
> an automated login or copied profile gets flagged by X ("temporarily limited"). Importing the
> decrypted cookies avoids logging in at all. `python main.py login` exists as a manual fallback.

## Telegram delivery (optional)

1. Create a bot with [@BotFather](https://t.me/BotFather); put its token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send any message to your bot, then discover your chat id:
   ```bash
   python main.py telegram-chatid     # prints chat_id=...  → set TELEGRAM_CHAT_ID in .env
   python main.py telegram-test       # confirm it works
   ```

## Run

```bash
# One-off run; saves the digest to data/digests/ (and emails / Telegrams it if configured)
python main.py run
python main.py run --max-accounts 5   # quick test on a few accounts

# Or drive the decoupled phases by hand:
python main.py ingest                 # scrape new tweets into the archive
python main.py process                # rebuild the live "Today" draft (no send)
python main.py deliver                # finalize + send the day's digest

# Start the web UI + in-process scheduler (fires whichever schedules you enable)
python main.py serve
# open http://localhost:8000
```

## Recovery & data management

Every stage of a run is snapshotted to `data/runs/<id>/`, so failures are recoverable and data
is durable:

```bash
python main.py resume                # resume the most recent failed run (no re-scrape)
python main.py resume 7              # resume a specific run
python main.py delete-run 7          # delete a run and ALL its data (tweets, archive, files)
python main.py reset-runs            # wipe ALL run data and start fresh (keeps settings/accounts/topics)
python main.py archive-backfill      # one-time: seed the raw archive from existing snapshots
```

Every collected tweet (pre-filter) is also stored in the `raw_tweets` archive for later analysis;
it survives filtering and pipeline failures. SQLite handles this comfortably at typical volume
(~60 MB/year).

## In the UI

- **Dashboard** — stats (archived vs digested tweets), current config, "Run now", and a **Today's
  digest (live)** card with Collect / Refresh / Deliver buttons when the decoupled schedules are on.
- **Accounts** — exclude accounts from scraping, and set a **per-account tweet limit** (any handle
  or a recently-seen one); accounts without an override use the global default.
- **Settings** — three **schedules** (Delivery evening send / Collection every N h / Processing
  live-draft every M h), time window, retweets, thread stitching, exclude-keywords, model,
  max themes, topics, digest style (themed / per-account / highlights), and clustering
  (LLM one-prompt vs. embedding-based + similarity threshold).
- **Runs** — history with status and ✉️/📨 delivery icons; *View* a past digest, **Resume** a
  failed run (re-runs the remaining stages from the saved scrape), or **Delete** a run + its data.

## Run in a container (Podman / Docker)

The container runs the UI + scheduler and reuses the session you import on the host.

```bash
python main.py import-profile     # on the host — creates auth/storage_state.json
cp .env.example .env              # optional: SMTP / Telegram
./run-podman.sh                   # build + run; UI at http://localhost:8765
# change the port: APP_PORT=9000 ./run-podman.sh
# or: ./run.sh deploy        (launcher shortcut for the same script)
# or: podman-compose up -d   (docker compose up -d)
```

Host networking lets the container reach Ollama at `localhost:11434`. The `auth/` directory
is mounted read-only — re-run `import-profile` on the host when the session expires. Tail logs
with `./run.sh logs`; one-off run inside the container:
`podman exec twitter-summary-agent python main.py run`.

The `./run.sh` launcher (interactive menu, or pass-through like `./run.sh deploy`) wraps every
`main.py` command plus container `deploy`/`logs`.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `couldn't confirm a logged-in session` | Log into X in Chrome, then rerun `import-profile`. |
| Scrape returns 0 tweets / 401 auth error | Session expired — rerun `python main.py import-profile`. |
| Selectors broken after an X update | Update `agents/selectors.py` (all selectors live there). |
| Summaries empty/garbled | Confirm Ollama is running and `gemma4:e4b` is pulled. |
| No email | Check SMTP creds in `.env`; the saved HTML in `data/digests/` still works regardless. |
