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

# Start the web UI + in-process daily scheduler
python main.py serve
# open http://localhost:8000
```

## In the UI

- **Dashboard** — stats, current config, "Run now".
- **Accounts** — exclude accounts from scraping (add a handle, or exclude any recently-seen one).
- **Settings** — schedule, time window, retweets, thread stitching, exclude-keywords, model,
  max themes, topics, digest style (themed / per-account / highlights), and clustering
  (LLM one-prompt vs. embedding-based + similarity threshold).
- **Runs** — history with status and ✉️/📨 delivery icons; *View* opens a past digest.

## Run in a container (Podman / Docker)

The container runs the UI + scheduler and reuses the session you import on the host.

```bash
python main.py import-profile     # on the host — creates auth/storage_state.json
cp .env.example .env              # optional: SMTP / Telegram
./run-podman.sh                   # build + run; UI at http://localhost:8000
# or: podman-compose up -d   (docker compose up -d)
```

Host networking lets the container reach Ollama at `localhost:11434`. The `auth/` directory
is mounted read-only — re-run `import-profile` on the host when the session expires. One-off
run inside the container: `podman exec twitter-summary-agent python main.py run`.

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
