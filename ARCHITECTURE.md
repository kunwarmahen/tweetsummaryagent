# Architecture

The Twitter Summary Agent borrows the **multi-agent + shared-state** pattern from
[inquiro](../poddebugger/inquiro): a single `DigestRun` state object flows through a
linear pipeline of role-agents. Each agent reads the state, does its job, and writes back.
After every stage the state is snapshotted to `data/` so a run can be inspected or replayed
without re-scraping.

Unlike inquiro (which loops with verify/adjudicate steps for debugging), this is a clean
**linear pipeline** — no investigation loop is needed for summarization.

## Process model

A single FastAPI process does everything:

- Serves the **config UI** (HTML pages).
- Hosts **APScheduler** (started in the FastAPI lifespan) which fires the daily pipeline.
- Exposes a **"Run now"** action that triggers the same pipeline on demand.

The CLI (`main.py`) can also run any piece headlessly (`init-db`, `login`, `run`, `resume`,
`delete-run`, `archive-backfill`, `serve`).

## Pipeline

```
Collector ─► Filter ─► [Threader] ─► [Clusterer] ─► Summarizer ─► Reporter
   │           │            │             │              │            │
 Playwright  window      stitch        embedding       Ollama      email +
 scrape      +exclude     threads       grouping        gemma4      Telegram +
 following   +dedup       (optional)    (optional)      themes      saved HTML + DigestRun
```

- **Threader** runs when `stitch_threads` is on — merges an author's rapid self-replies.
- **Clusterer** runs only for `digest_style = themed` **and** `clustering_method = embedding`.
- **Summarizer** branches on `digest_style`: `themed` (LLM one-prompt or per-embedding-cluster),
  `per_account` (one summary per account), `highlights` (top tweets, one line each).

| Agent | Responsibility |
|-------|----------------|
| **Collector** | Reuse saved browser session; enumerate the following list (minus the blocklist); scrape each account's tweets in the time window (text, timestamp, links, metrics) into the state. |
| **Filter** | Drop exclude-keyword hits and empties; keep the time window; dedup within batch and against the `tweets` table (cross-day). |
| **Threader** *(optional)* | Merge an author's self-reply chain into one item (text joined, max engagement, `member_ids` kept for dedup). `reply` mode (default) chains tweets flagged `is_self_reply` — accurate and gap-independent; `time` mode falls back to merging within `thread_gap_minutes`. Retweets / undated tweets pass through. |

> **Reply metadata & the `with_replies` timeline.** X's default "Posts" tab hides thread
> continuations (they're replies). So in `reply` mode the Collector scrapes each account's
> `with_replies` timeline, detects the "Replying to @handle" context (via `innerText`), and keeps
> only that author's **originals + self-replies** (dropping conversation parents and replies to
> others). The Threader then chains the self-replies onto their root. Other modes use the cleaner
> Posts tab.
| **Clusterer** *(optional)* | Themed + embedding mode only. Embeds each tweet (`nomic-embed-text`), greedily groups by cosine similarity (pure-Python, no numpy) against running centroids, caps to `max_themes`. Produces tweet groups for the Summarizer to title. |
| **Summarizer** | Branches on `digest_style`. `themed`: one `gemma4:e4b` prompt clusters + narrates (index-referenced), or summarizes pre-made embedding clusters. `per_account`: one summary per account (most active first, capped). `highlights`: top tweets by engagement, one line each. |
| **Reporter** | Resolve theme tweet-IDs to full tweets, render the newsletter (`web/templates/digest.html`), save the HTML to `data/digests/`, and deliver: email via SMTP (STARTTLS) and Telegram (`agents/telegram.py`, compact themed message auto-split under 4096 chars) — each gated on its creds being set and themes non-empty. |

## Run lifecycle, snapshots & recovery

Each stage writes a snapshot to `data/runs/<run_id>/` (`1_collected`, `2_filtered`,
`2a_threaded`, `2b_clustered`, `3_summarized`, `4_reported`). These make a run replayable and
power three operations:

- **Raw archive** — immediately after collection, every scraped tweet is appended to `raw_tweets`
  (`pipeline._archive_raw`, idempotent by `tweet_id`). Because it runs before the filter and before
  any later stage can fail, the scrape is never lost even if summarization crashes.
- **Resume** (`pipeline.resume`) — loads the *furthest-along* snapshot for a run and re-runs only
  the remaining stages, reusing the same `digest_runs` row (no re-scrape). The post-collection
  stages are defined once (`_stage_plan`) and shared by `run()` and `resume()`. `_persist_tweets`
  is idempotent so a resume can't double-insert. CLI `resume [id]` / UI "Resume" on failed runs.
- **Delete** (`pipeline.delete_run`) — removes a run and *all* its data: the `digest_runs` row, its
  `tweets` and `raw_tweets`, the saved digest HTML (only if under `data/`), and the snapshot dir.
  Refuses while the run is in progress. CLI `delete-run <id>` / UI "Delete".

`backfill_raw_archive()` (CLI `archive-backfill`) seeds `raw_tweets` from existing `1_collected`
snapshots — a one-time historical import for DBs created before the archive existed.

## Browser collection & auth

On Linux, Chrome encrypts cookies with a key held in the desktop keyring. A scripted
Playwright login or a copied profile can't reproduce that key and gets flagged by X
("temporarily limited"). So:

- **Session import** (`main.py import-profile`): decrypts your X/Twitter cookies straight from
  Chrome's cookie DB using `browser_cookie3`, and writes a Playwright `storage_state`
  (`auth/storage_state.json`). No scripted login, nothing for X to flag. Your handle is saved
  to `auth/session_meta.json`.
- **Runs**: launch a hardened browser (real Google Chrome via `channel="chrome"` when present,
  automation flags dropped, JS fingerprint patched), load the `storage_state`, visit
  `x.com/{handle}`, scroll, and extract `article[data-testid="tweet"]` nodes via in-page JS.
- **Resilience**: all DOM selectors live in one module (`agents/selectors.py`) so X UI changes
  are a one-file fix. Cookies expire periodically → rerun `import-profile`.
- `main.py login` remains as a manual, headed login fallback.

## Data model (SQLite via SQLModel)

| Table | Purpose |
|-------|---------|
| `settings` | Schedule, Ollama model, digest style, time-window, max tweets/themes, include-retweets, exclude-keywords, thread stitching (+ gap), clustering method + embedding model + similarity threshold. |
| `excluded_accounts` | Handles to skip when scraping the following list (the blocklist). |
| `account_settings` | Per-account overrides keyed by handle — currently `max_tweets` (falls back to `settings.max_tweets_per_account`). |
| `topics` | Optional themes to bias/filter toward. |
| `digest_runs` | Run history: timestamp, status, tweet count, error — shown in the UI. |
| `tweets` | Per-run cache of **digested** tweets; enables cross-day dedup and digest archives. |
| `raw_tweets` | Append-only archive of **every** collected tweet (pre-filter), deduped by `tweet_id`; for analysis. Written right after collection, so it survives filtering and failures. |

## Directory layout

```
twitter_summary_agent/
├── pyproject.toml
├── .env.example                 # bootstrap secrets only (SMTP, ollama url)
├── config.py                    # pydantic-settings for bootstrap
├── db/
│   ├── models.py                # SQLModel tables
│   └── session.py               # engine, init_db, get_session
├── state.py                     # DigestRun + TweetItem + ThemeCluster dataclasses
├── agents/
│   ├── base.py                  # Agent base + AgentContext
│   ├── browser.py               # hardened launcher + import_chrome_cookies()
│   ├── selectors.py             # ALL X DOM selectors (one-file fix point)
│   ├── collector.py             # Playwright scrape (session check, retry/backoff, paced,
│   │                            #   per-account tweet limits)
│   ├── filter.py                # keyword / window / dedup
│   ├── threader.py              # stitch rapid self-reply threads (optional)
│   ├── clusterer.py             # embedding-based clustering (optional mode)
│   ├── summarizer.py            # Ollama summary — themed / per-account / highlights
│   ├── reporter.py              # render + save + email (SMTP) + Telegram
│   ├── telegram.py              # Telegram Bot API client + formatter
│   └── util.py                  # extract_json helper
├── auth/login.py                # capture_handle() + manual login fallback
├── pipeline.py                  # orchestrator, run-lock, DigestRun row; resume / delete_run /
│                                 #   raw archive + backfill
├── scheduler.py                 # APScheduler (started in FastAPI lifespan)
├── web/
│   ├── app.py                   # FastAPI app + lifespan starts scheduler
│   ├── routes.py                # dashboard, accounts (+ per-account limits), settings, topics,
│   │                            #   runs (run-now / resume / delete), digest
│   └── templates/               # base, index, accounts, settings, runs, digest
├── db/
│   ├── models.py                # SQLModel tables + enums
│   └── session.py               # engine, init_db, additive column migrations
├── tests/                       # pytest suite (util, telegram, filter, threader, summarizer
│                                #   styles, clusterer, collector, recovery, archive, delete)
│                                #   — no network/LLM; DB tests use an in-memory engine
├── data/                        # state snapshots (runs/) + saved digests (digests/) + agent.db
└── main.py                      # CLI: init-db | import-profile | login | collect | run |
                                 #      resume | delete-run | archive-backfill |
                                 #      telegram-chatid | telegram-test | serve
```

## Configuration split

- **`.env`** — only bootstrap secrets/endpoints needed before the DB exists: Ollama URL/model,
  SMTP creds, Telegram bot token + chat id, paths.
- **SQLite** — everything else (schedule, collection, summarization, clustering, excludes, topics),
  editable live in the UI without restarting.

## Containerization (Podman / Docker)

`Dockerfile` builds an image (Python 3.12 + Chromium via `playwright install --with-deps`) that
runs `main.py serve`. The container intentionally has **no host keyring access**, so it never
imports cookies itself:

- `import-profile` is run on the **host** (decrypts cookies → `auth/storage_state.json`).
- `auth/` is mounted **read-only** into the container; the collector reuses that session. When
  running as root the launcher adds `--no-sandbox`/`--disable-dev-shm-usage` for Chromium.
- `data/` is mounted read-write (DB, snapshots, digests).
- **Host networking** lets the container reach Ollama at `localhost:11434` and serves the UI on
  host `:8000`. See `docker-compose.yml` and `run-podman.sh`.
