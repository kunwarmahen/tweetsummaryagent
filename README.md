# Twitter Summary Agent

A daily **themed-newsletter digest** of tweets from the accounts you follow on X/Twitter.

It reuses your logged-in Chrome session (Playwright), scrapes the last 24 hours of tweets
from your following list (minus any accounts you've excluded), groups them into themes
with a local LLM (Ollama `gemma4:e4b`), and delivers a newsletter-style summary by email
and as a saved HTML file. Everything is configurable through a small web UI.

Architecture is inspired by the [inquiro](../poddebugger/inquiro) multi-agent framework:
a shared **State** object flows through a pipeline of role-agents
(`Collector → Filter → Summarizer → Reporter`), and each stage is snapshotted so a run
can be replayed or debugged without re-scraping.

## Features

- 🌐 **Browser-based collection** — no API key; reuses your Chrome X session (cookies decrypted locally).
- 📋 **Following-list scraping** with a per-account **exclude list** (blocklist).
- 🧠 **Local LLM summarization** via Ollama (`gemma4:e4b`) — private, free.
- 📰 **Three digest styles** — themed newsletter, per-account summaries, or ranked highlights.
- 🧩 **Two clustering modes** — one-prompt LLM grouping, or embedding-based clustering (`nomic-embed-text`).
- 🧵 **Thread stitching** — merges an author's rapid self-replies into one item before summarizing.
- ♻️ **Re-run any past run** — replay captured tweets (no re-scrape) with a different style, clustering, model, or topics; per-run detail page shows what each run did.
- ⭐ **Important accounts** — mark VIP accounts to color-highlight their tweets (with a legend), guarantee they appear, and float them to the top; ⭐-marked in Telegram.
- 🎚️ **Per-account tweet limits** — cap how many tweets to capture for specific accounts (others use the global default).
- 📧 **Email + Telegram + saved HTML** delivery.
- ⚙️ **Web config UI** (FastAPI + HTML) backed by **SQLite**.
- ⏰ **Built-in scheduler** (APScheduler, in-process) — either one daily run, or a decoupled **collect (every few hours) → live "Today" draft → evening delivery** pipeline so the portal shows the day as it builds up while email/Telegram still goes out once a day. The collector early-stops on already-seen tweets so frequent scraping stays light. CLI: `ingest` / `process` / `deliver`.
- 🔁 **Crash-resilient runs** — every stage is snapshotted, so a run that fails after scraping can be **resumed** without re-scraping.
- 🗄️ **Raw tweet archive** — every collected tweet (pre-filter) is stored for later analysis, surviving filtering and failures.
- 🗑️ **Run management** — delete a run and all of its data (tweets, archive rows, digest, snapshots) from the CLI or UI.
- 📈 **Trends dashboard** — a **Trends** page (and dashboard sparkline) charting tweets & engagement over time (Chart.js), an account leaderboard, top tweets, **recurring topics** tracked across runs via title embeddings, and a weekly **"This week in your feed"** LLM retrospective. Backfill with `python main.py trends-rebuild`.

## Quick start

```bash
# 1. Install
pip install -e .
playwright install chromium          # real Chrome is auto-used if installed

# 2. Initialize the database
python main.py init-db

# 3. Import your X session from Chrome (be logged into X in Chrome first)
python main.py import-profile

# 4. Run once on demand (add --max-accounts N to test quickly)
python main.py run

# 5. Or start the web UI + daily scheduler
python main.py serve   # then open http://localhost:8000
```

See **[QUICKSTART.md](QUICKSTART.md)** for the full 5-minute setup.

Bootstrap secrets (SMTP creds, Ollama URL/model) live in `.env` (see `.env.example`).
Everything else — schedule, excluded accounts, topics, digest style — is edited in the UI
and stored in SQLite.

> **Auth note:** On Linux, Chrome encrypts cookies with a desktop-keyring key, so a scripted
> login or a copied profile gets flagged ("temporarily limited"). Instead, `import-profile`
> decrypts your X cookies locally and reuses that session. Rerun it if cookies expire.

## Run in a container (Podman / Docker)

The container runs the web UI + scheduler. It does **not** log into X — you import your
session on the host first (which decrypts your Chrome cookies), then mount `auth/` in.

```bash
# On the host, once:
./venv/bin/python main.py import-profile      # creates auth/storage_state.json
cp .env.example .env                           # set SMTP / Telegram if wanted

# Then build + run with Podman:
./run-podman.sh             # or: ./run.sh deploy   (same thing, via the launcher menu)
# → UI at http://localhost:8765   (set APP_PORT=… to change it)

# Or with compose (podman-compose or docker compose):
podman-compose up -d        # docker compose up -d
```

Uses **host networking** so the container reaches Ollama on the host at `localhost:11434`.
Keep Ollama running on the host with the models pulled. Re-run `import-profile` on the host
when the X session expires; the read-only `auth/` mount picks it up. Follow logs with
`./run.sh logs`; one-off run: `podman exec twitter-summary-agent python main.py run`.

**Data lives separately from the project.** The container keeps its own DB, run snapshots, and
digests in `~/.local/share/twitter-summary-agent` (override with `TSA_DATA_DIR`), so it never
writes the same SQLite file as a host `serve`. It starts empty (schema auto-created). Check auth
with `podman exec twitter-summary-agent python main.py collect --max-accounts 2`. To seed it from
your own data later, stop the container and copy your data in:

```bash
podman stop twitter-summary-agent
cp -a data/. ~/.local/share/twitter-summary-agent/    # carries config + runs + archive
./run-podman.sh
```

> **Launcher:** `./run.sh` is an interactive menu (and pass-through) for every `main.py`
> command plus container ops — `./run.sh deploy` builds + (re)deploys, `./run.sh logs` tails
> the container.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```
Unit tests cover the JSON parser, Telegram formatter/chunking, filter, thread stitcher,
summarizer styles (incl. malformed-index hardening), embedding clusterer, per-account limit
resolution, snapshot round-trip / resume planning, the raw archive, and run deletion — no
network or LLM calls (all mocked; DB-touching tests use an isolated in-memory engine).

## Documentation

- [QUICKSTART.md](QUICKSTART.md) — 5-minute setup, command reference, troubleshooting.
- [ARCHITECTURE.md](ARCHITECTURE.md) — components, data flow, agent pipeline, DB schema.
- [ROADMAP.md](ROADMAP.md) — phased build plan and status.
- [SETUP.md](SETUP.md) — detailed install, session import, and configuration guide.

## Status

✅ Feature-complete (all 5 phases). Import session → scrape → filter → summarize → render →
email/save — either as one daily run or split into a **collect → live draft → evening deliver**
pipeline, all configurable via the web UI. See [ROADMAP.md](ROADMAP.md).

## Disclaimer

Browser scraping of X is unofficial and may break when X changes its UI, and may be subject
to X's Terms of Service. Use a session you're comfortable automating. Selectors are isolated
in one module so breakage is a one-file fix.
