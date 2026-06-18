# Quickstart

Get a daily X digest running in ~5 minutes. Assumes Ubuntu/Linux with Google Chrome.

## 0. Prerequisites
- **Python 3.11+**
- **Google Chrome**, and you are **logged into X (x.com)** in it (that session is reused).
- **Ollama** running locally with the model pulled:
  ```bash
  ollama pull gemma4:e4b
  ollama pull nomic-embed-text   # only needed for embedding clustering mode
  ```

## 1. Install
```bash
cd ~/Documents/ai/twitter_summary_agent
python -m venv venv
./venv/bin/pip install -e .
./venv/bin/python -m playwright install chromium   # (real Chrome is auto-used if present)
```

## 2. Configure secrets (optional until email)
```bash
cp .env.example .env       # edit SMTP creds later for email; Ollama defaults are fine
```

## 3. Initialize the database
```bash
./venv/bin/python main.py init-db
```

## 4. Import your X session (no password typed — reuses Chrome's login)
```bash
./venv/bin/python main.py import-profile
# -> "Logged in as @yourhandle"
```
> This decrypts your X cookies from Chrome. If it can't confirm a session, make sure you're
> logged into X in Chrome, then rerun. Cookies expire occasionally — just rerun this when
> scraping starts failing.

## 5. Run a quick test (a few accounts)
```bash
./venv/bin/python main.py run --max-accounts 5
# -> Done. N tweets, M themes -> data/digests/digest_*.html
```
Open the printed HTML file in a browser to see your themed digest.

## 6. Full run
```bash
./venv/bin/python main.py run
```

## Command reference
| Command | What it does |
|---------|--------------|
| `init-db` | Create the SQLite database. |
| `import-profile` | Reuse your logged-in Chrome X session (decrypts cookies via the host keyring). |
| `import-cookies <file>` | Import an X session from a browser cookie export (Netscape `cookies.txt` or JSON) — no keyring, works in a container. Also available in the UI **Session** page. |
| `collect [--max-accounts N] [--out FILE]` | Scrape + dump tweets only (debug). |
| `run [--max-accounts N]` | Full pipeline in one go: scrape → filter → summarize → render → deliver. |
| `ingest` | Phase 1: scrape new tweets into the archive (no digest). |
| `process` | Phase 2: refresh the live "Today" draft digest (render only, no send). |
| `deliver` | Phase 3: finalize + send the day's digest from the archive. |
| `resume [run_id]` | Resume a failed run from its last snapshot — no re-scrape (default: most recent failed run). |
| `delete-run <run_id>` | Delete a run and all its data (tweets, raw archive, digest, snapshots). |
| `reset-runs [--yes] [--no-backup]` | Delete **all** run data (runs, tweets, archive, trends) and start fresh — keeps settings, accounts, limits/VIPs, topics. Backs up `agent.db` first. |
| `archive-backfill` | One-time: import past `1_collected` snapshots into the raw tweet archive. |
| `login` | Manual browser-login fallback (rarely needed). |
| `telegram-chatid` | Discover your Telegram chat id (after messaging your bot). |
| `telegram-test` | Send a Telegram test message to verify setup. |
| `serve [--host --port]` | Start the web config UI at http://127.0.0.1:8000. |

## Web UI
```bash
./venv/bin/python main.py serve   # then open http://127.0.0.1:8000
```
- **Dashboard** — stats (archived vs digested tweet counts), current config, "Run now", and a
  **Today's digest (live)** card with **Collect now / Refresh digest / Deliver now** when the
  decoupled schedules are in use.
- **Accounts** — exclude accounts from scraping, and set a **per-account tweet limit** (any handle,
  or one of the recently-seen accounts); accounts without an override use the global default.
  Mark accounts **★ Important** to color-highlight their tweets (legend included), guarantee they
  appear, and float them to the top of the digest; each gets its own auto-assigned (editable) color.
- **Settings** — the three **schedules** (Delivery / Collection / Processing — see below) with a
  **Timezone** for the daily delivery, time
  window, retweets, **thread stitching**, exclude-keywords, model, max themes, topics,
  **digest style** (themed / per-account / highlights), and **clustering** (LLM one-prompt vs.
  embedding-based with `nomic-embed-text` + similarity threshold). Plus **Telegram** test/chat-id
  helpers, **archive backfill**, and a **Danger zone** to reset all run data.
- **Session** — import your X session by uploading a browser cookie export (no host/keyring),
  see its status, and **Test session** to confirm you're logged in.
- **Runs** — history with status; *View* a past digest, **Resume** a failed run, or **Delete** a
  run and all its data. Click a run's **#id** for its detail page (what it did — style, model,
  clustering, accounts, theme titles) and a **Re-run** form: regenerate it with no re-scrape, with
  a different digest style / clustering / model / topics. Re-runs are new entries linked to the
  source; delivery stays off unless you tick "Also email / Telegram".
- **Activity** — history of both background schedules — **collection** (scrape) and **processing**
  (draft refresh): type, status, trigger (schedule vs manual), start time, and counts. Shows the
  **next run time** for each schedule, and logs every cycle including ones that found nothing new,
  so you can see the actual cadence.

## Schedules + email
The scheduler runs inside `serve` — keep that process alive. There are two ways to run it:

- **Simple (default):** one daily run at the time set in **Settings → Delivery schedule**
  (default 08:00) that scrapes, summarizes, and delivers in one go. The hour/minute are
  interpreted in the **Timezone** you pick there (default `America/New_York`), not the
  container's UTC clock. That timezone also controls how times are **displayed** across the UI
  (dashboard, runs, trends) and on the digest's "Generated by" line.
- **Decoupled:** enable **Collection schedule** (scrape every N hours) and **Processing schedule**
  (rebuild the live "Today" draft every M hours). The portal then shows the day's digest as it
  builds up, while **Delivery** still emails/Telegrams once a day in the evening. Tweets are only
  marked "delivered" at send time, so each refresh shows the whole day so far — not slices. These
  intervals are anchored to local midnight (so app restarts don't reset the clock) and jittered
  ±20 min so the scrape times look human; watch the actual fires (and next run time) on the
  **Activity** page.

To receive the digest by email, fill the `SMTP_*`/`EMAIL_*` values in `.env` (e.g. Gmail + App
Password); otherwise it's just saved to `data/digests/` and viewable under **Runs → View** (or the
live draft via the dashboard).

```bash
./venv/bin/python main.py serve   # leave running; schedules fire automatically
```

## Telegram delivery (optional)
1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN`, then **send any message to your bot**.
3. Discover your chat id and save it:
   ```bash
   ./venv/bin/python main.py telegram-chatid    # prints chat_id=...
   # add it to .env as TELEGRAM_CHAT_ID
   ./venv/bin/python main.py telegram-test       # confirm it works
   ```
Each daily digest is then also sent to Telegram (compact themed message, auto-split if long).
Delivery shows as ✉️/📨 icons in the **Runs** table.

## Run in a container (Podman)
```bash
./venv/bin/python main.py import-profile   # on the host — creates auth/storage_state.json
cp .env.example .env                         # optional: SMTP / Telegram
./run-podman.sh                              # build + run; UI at http://localhost:8765
#  …or via the launcher menu:  ./run.sh deploy   (and ./run.sh logs to tail it)
#  change the port:  APP_PORT=9000 ./run-podman.sh
```
The container reuses the mounted `auth/` session (no login inside the container) and reaches
Ollama on the host via host networking. Re-run `import-profile` on the host when the session
expires. One-off run: `podman exec twitter-summary-agent python main.py run`.

It keeps its **own data** in `~/.local/share/twitter-summary-agent` (override with `TSA_DATA_DIR`),
separate from the project's `./data` — starts empty, schema auto-created. Verify auth with
`podman exec twitter-summary-agent python main.py collect --max-accounts 2`. To seed it from your
own data later: `podman stop twitter-summary-agent && cp -a data/. ~/.local/share/twitter-summary-agent/ && ./run-podman.sh`.

> **Launcher:** `./run.sh` (no args) opens an interactive menu covering every command above —
> setup, runs, the `ingest`/`process`/`deliver` phases, `reset-runs`, and container `deploy`/`logs`.

## Tests
```bash
./venv/bin/pip install -e ".[dev]"
./venv/bin/pytest -q
```

## Troubleshooting
| Problem | Fix |
|---------|-----|
| `couldn't confirm a logged-in session` | Log into X in Chrome, rerun `import-profile`. |
| `X session is not active` during a run | Session expired — log into X in Chrome, rerun `import-profile`. |
| Scrape returns 0 / 401 errors | Session expired — rerun `import-profile`. |
| Summaries fail | Ensure `ollama` is running and `gemma4:e4b` is pulled. |
| Selectors broken after an X update | Fix `agents/selectors.py` (all selectors live there). |
