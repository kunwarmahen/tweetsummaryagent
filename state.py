"""Shared pipeline state — the object that flows through the agents.

Inspired by inquiro's AgentContext/InvestigationState: each agent reads and mutates this,
and the orchestrator snapshots it to disk after each stage for replay/debugging.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class TweetItem:
    tweet_id: str
    handle: str
    text: str
    author_name: Optional[str] = None
    url: Optional[str] = None
    created_at: Optional[str] = None   # ISO string for JSON-friendliness
    likes: int = 0
    retweets: int = 0
    is_retweet: bool = False
    reply_to: Optional[str] = None       # handle this tweet replies to (if any)
    is_self_reply: bool = False          # replies to its own author (thread continuation)
    member_ids: list[str] = field(default_factory=list)   # all source IDs when stitched


@dataclass
class ThemeCluster:
    title: str
    summary: str
    tweet_ids: list[str] = field(default_factory=list)


@dataclass
class DigestRun:
    """The state object passed Collector -> Filter -> Summarizer -> Reporter."""
    run_id: Optional[int] = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    raw_tweets: list[TweetItem] = field(default_factory=list)
    filtered_tweets: list[TweetItem] = field(default_factory=list)
    themes: list[ThemeCluster] = field(default_factory=list)

    digest_html: Optional[str] = None
    digest_path: Optional[str] = None
    emailed: bool = False
    telegram_sent: bool = False
    error: Optional[str] = None

    def snapshot(self, data_dir: str, stage: str) -> Path:
        """Persist the current state to data_dir for replay/debugging."""
        out_dir = Path(data_dir) / "runs" / (str(self.run_id) if self.run_id else self.started_at)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{stage}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return path

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "raw_tweets": [asdict(t) for t in self.raw_tweets],
            "filtered_tweets": [asdict(t) for t in self.filtered_tweets],
            "themes": [asdict(c) for c in self.themes],
            "digest_path": self.digest_path,
            "emailed": self.emailed,
            "telegram_sent": self.telegram_sent,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DigestRun":
        """Rebuild a state from a snapshot dict (the inverse of to_dict)."""
        def items(rows, klass):
            fields = klass.__dataclass_fields__
            return [klass(**{k: v for k, v in row.items() if k in fields}) for row in (rows or [])]

        st = cls(run_id=d.get("run_id"))
        if d.get("started_at"):
            st.started_at = d["started_at"]
        st.raw_tweets = items(d.get("raw_tweets"), TweetItem)
        st.filtered_tweets = items(d.get("filtered_tweets"), TweetItem)
        st.themes = items(d.get("themes"), ThemeCluster)
        st.digest_path = d.get("digest_path")
        st.emailed = d.get("emailed", False)
        st.telegram_sent = d.get("telegram_sent", False)
        st.error = d.get("error")
        return st


# Snapshot stages in pipeline order — used to find the furthest-along snapshot to resume from.
SNAPSHOT_ORDER = ["1_collected", "2_filtered", "2a_threaded", "2b_clustered", "3_summarized", "4_reported"]


def load_latest_snapshot(data_dir: str, run_id: int) -> Optional[tuple["DigestRun", str]]:
    """Load the most advanced snapshot for a run. Returns (state, stage_label) or None."""
    run_dir = Path(data_dir) / "runs" / str(run_id)
    if not run_dir.is_dir():
        return None
    present = {p.stem: p for p in run_dir.glob("*.json")}
    for label in reversed(SNAPSHOT_ORDER):
        if label in present:
            data = json.loads(present[label].read_text())
            return DigestRun.from_dict(data), label
    return None
