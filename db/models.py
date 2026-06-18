"""SQLModel tables — the runtime source of truth, edited via the web UI."""
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class DigestStyle(str, Enum):
    themed = "themed"          # cluster related tweets into themes (default)
    per_account = "per_account"
    highlights = "highlights"


class ClusteringMethod(str, Enum):
    llm = "llm"                # one prompt groups + summarizes (default)
    embedding = "embedding"    # embed + cluster, then summarize each cluster


class ThreadMode(str, Enum):
    reply = "reply"            # chain self-replies using captured reply metadata (default)
    time = "time"              # merge an author's tweets within thread_gap_minutes


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    draft = "draft"        # intraday "live" digest, refreshed in place until delivery finalizes it


class AppSettings(SQLModel, table=True):
    """Single-row settings (id == 1)."""
    __tablename__ = "settings"

    id: int = Field(default=1, primary_key=True)

    # Delivery schedule — the once-a-day evening send (local time, 24h)
    schedule_hour: int = 8
    schedule_minute: int = 0
    schedule_enabled: bool = True
    # IANA timezone the schedule hour/minute are interpreted in. The container clock is
    # usually UTC, so without this the evening send fires at the wrong wall-clock time.
    timezone: str = "America/New_York"

    # Collection schedule — scrape new tweets into the archive every N hours.
    # When enabled, the delivery job reads from the archive instead of scraping inline.
    collection_enabled: bool = False
    collection_interval_hours: int = 3

    # Processing schedule — refresh the "live" draft digest every N hours so the portal
    # shows the day so far (filter+thread+cluster+summarize, rendered but NOT delivered).
    process_enabled: bool = False
    process_interval_hours: int = 4

    # Collection
    time_window_hours: int = 24
    max_tweets_per_account: int = 50
    include_retweets: bool = False
    exclude_keywords: str = ""   # comma-separated; tweets containing any are dropped
    stitch_threads: bool = True       # merge an author's self-replies into one item
    thread_mode: ThreadMode = ThreadMode.reply   # reply-metadata chaining vs time-gap
    thread_gap_minutes: int = 10      # max gap (time mode only)

    # Summarization
    digest_style: DigestStyle = DigestStyle.themed
    ollama_model: str = "gemma4:e4b"
    max_themes: int = 8

    # Clustering
    clustering_method: ClusteringMethod = ClusteringMethod.llm
    embedding_model: str = "nomic-embed-text"
    similarity_threshold: float = 0.55   # cosine; higher = tighter themes

    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExcludedAccount(SQLModel, table=True):
    """Handles to skip when scraping the following list (blocklist)."""
    __tablename__ = "excluded_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    handle: str = Field(index=True, unique=True)   # without leading '@'
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AccountSetting(SQLModel, table=True):
    """Per-account overrides keyed by handle: tweet capture limit, and "important" status
    with a highlight color. Accounts without a row fall back to global defaults."""
    __tablename__ = "account_settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    handle: str = Field(index=True, unique=True)   # without leading '@'
    max_tweets: int = 50
    important: bool = False           # VIP — highlighted + guaranteed in the digest
    color: Optional[str] = None       # hex highlight color (auto-assigned when marked important)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Topic(SQLModel, table=True):
    """Optional topics to bias/filter the digest toward."""
    __tablename__ = "topics"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DigestRun(SQLModel, table=True):
    """One pipeline execution — shown in the UI run history."""
    __tablename__ = "digest_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    status: RunStatus = RunStatus.pending
    tweet_count: int = 0
    theme_count: int = 0
    digest_path: Optional[str] = None     # saved HTML
    emailed: bool = False
    telegram_sent: bool = False
    error: Optional[str] = None

    # What this run did (recorded going forward; older runs are NULL).
    source_run_id: Optional[int] = None   # set when this run is a replay of another
    digest_style: Optional[str] = None
    clustering_method: Optional[str] = None
    ollama_model: Optional[str] = None
    time_window_hours: Optional[int] = None
    max_themes: Optional[int] = None
    topics: Optional[str] = None          # comma-joined topics used
    account_count: Optional[int] = None   # distinct accounts captured


class JobRun(SQLModel, table=True):
    """One background-schedule cycle — collection (scrape) or processing (draft refresh).

    Cheap and append-only: logged per fire (even on no-op or error) so each schedule's actual
    cadence is visible on the Activity page, independent of the digest_runs/draft row a process
    cycle reuses or the raw_tweets a collect cycle appends to (both of which hide idle fires).
    The two count columns are interpreted per job:
      - collect:  primary = tweets scraped,        secondary = newly archived
      - process:  primary = tweets in the window,  secondary = themes rendered
    """
    __tablename__ = "job_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    job: str = Field(index=True)         # 'collect' | 'process'
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    status: str = "running"              # 'running' -> 'ok' | 'skipped' | 'error'
    trigger: str = "schedule"            # 'schedule' | 'manual'
    primary_count: int = 0
    secondary_count: int = 0
    digest_path: Optional[str] = None    # process cycles: the interim digest HTML they rendered
    error: Optional[str] = None


class RawTweet(SQLModel, table=True):
    """Append-only archive of EVERY tweet the collector captured (pre-filter), for analysis.

    Written right after collection, so it survives filtering and pipeline failures. Deduped by
    tweet_id (first capture wins). Separate from `tweets` (the digested cross-day-dedup cache).
    """
    __tablename__ = "raw_tweets"

    id: Optional[int] = Field(default=None, primary_key=True)
    tweet_id: str = Field(index=True, unique=True)
    handle: str = Field(index=True)
    author_name: Optional[str] = None
    text: str = ""
    url: Optional[str] = None
    created_at: Optional[datetime] = Field(default=None, index=True)
    likes: int = 0
    retweets: int = 0
    is_retweet: bool = False
    reply_to: Optional[str] = None
    is_self_reply: bool = False
    run_id: Optional[int] = Field(default=None, foreign_key="digest_runs.id")
    scraped_at: datetime = Field(default_factory=datetime.utcnow)


class DailyStat(SQLModel, table=True):
    """Per-UTC-date aggregate over `raw_tweets`, materialized for the trends charts.

    Rebuilt wholesale from `raw_tweets` (idempotent) after each real run — see
    agents/analytics.recompute_daily_stats. Tweets without a parseable created_at are skipped
    (they can't be placed on a day).
    """
    __tablename__ = "daily_stats"

    date: str = Field(primary_key=True)   # 'YYYY-MM-DD' (UTC), from date(created_at)
    tweet_count: int = 0
    account_count: int = 0                 # distinct handles that day
    total_likes: int = 0
    total_retweets: int = 0
    engagement: int = 0                    # likes + retweets
    retweet_count: int = 0
    self_reply_count: int = 0


class TopicCluster(SQLModel, table=True):
    """Persistent cross-run topic identity for theme continuity.

    A digest theme joins the cluster of its nearest prior theme (single-linkage on title
    embeddings, cosine > threshold) or starts a new cluster. Single-linkage — rather than an
    averaging centroid — is deliberate: nomic title embeddings share a high baseline cosine, so
    a running centroid regresses to a generic direction and swallows everything. Powers the
    "trending themes" view — see agents/analytics.
    """
    __tablename__ = "theme_clusters"

    id: Optional[int] = Field(default=None, primary_key=True)
    label: str = ""                        # latest theme title in this cluster
    first_seen: Optional[str] = None       # 'YYYY-MM-DD'
    last_seen: Optional[str] = None        # 'YYYY-MM-DD'
    appearance_count: int = 0              # number of themes folded in


class ThemeHistory(SQLModel, table=True):
    """One row per theme of every original (non-replay) run — the raw material for trends.

    Stores the theme's unit title-embedding so later runs can match against it (single-linkage).
    """
    __tablename__ = "theme_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: Optional[int] = Field(default=None, foreign_key="digest_runs.id")
    run_date: str = Field(index=True)      # 'YYYY-MM-DD' (UTC)
    title: str = ""
    summary: str = ""
    member_count: int = 0
    engagement: int = 0                    # sum of member tweets' likes+retweets
    embedding_json: str = "[]"             # unit title embedding (JSON list of floats)
    cluster_id: Optional[int] = Field(default=None, foreign_key="theme_clusters.id", index=True)


class MetaDigest(SQLModel, table=True):
    """An LLM 'this week in your feed' narrative over recent theme history.

    Generated weekly by the scheduler and on demand from the Trends page (Regenerate). The
    latest row is shown on /trends. Stored as markdown.
    """
    __tablename__ = "meta_digests"

    id: Optional[int] = Field(default=None, primary_key=True)
    period_start: str = ""                 # 'YYYY-MM-DD'
    period_end: str = ""                   # 'YYYY-MM-DD'
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    narrative: str = ""                    # markdown
    model: Optional[str] = None


class Tweet(SQLModel, table=True):
    """Per-run tweet cache — enables cross-day dedup and digest archives."""
    __tablename__ = "tweets"

    id: Optional[int] = Field(default=None, primary_key=True)
    tweet_id: str = Field(index=True, unique=True)
    handle: str = Field(index=True)
    author_name: Optional[str] = None
    text: str = ""
    url: Optional[str] = None
    created_at: Optional[datetime] = None
    likes: int = 0
    retweets: int = 0
    is_retweet: bool = False
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    run_id: Optional[int] = Field(default=None, foreign_key="digest_runs.id")
