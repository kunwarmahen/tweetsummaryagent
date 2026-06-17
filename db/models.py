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


class AppSettings(SQLModel, table=True):
    """Single-row settings (id == 1)."""
    __tablename__ = "settings"

    id: int = Field(default=1, primary_key=True)

    # Schedule (local time, 24h)
    schedule_hour: int = 8
    schedule_minute: int = 0
    schedule_enabled: bool = True

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
    """Per-account overrides keyed by handle. Currently just the tweet capture limit;
    accounts without a row fall back to AppSettings.max_tweets_per_account."""
    __tablename__ = "account_settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    handle: str = Field(index=True, unique=True)   # without leading '@'
    max_tweets: int = 50
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
