"""Filter agent — narrows raw tweets down to what should be summarized.

- enforces the time window,
- drops tweets containing any excluded keyword,
- removes near-empty tweets,
- dedups within the batch and against tweets already digested on previous days.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from agents.base import Agent
from db.models import Tweet
from db.session import get_session
from state import DigestRun, TweetItem


class Filter(Agent):
    name = "filter"

    def _exclude_keywords(self) -> list[str]:
        raw = self.ctx.app_settings.exclude_keywords or ""
        return [k.strip().lower() for k in raw.split(",") if k.strip()]

    def _already_seen(self) -> set[str]:
        """tweet_ids already stored from previous runs (cross-day dedup)."""
        with get_session() as session:
            rows = session.exec(select(Tweet.tweet_id)).all()
        return set(rows)

    def run(self, state: DigestRun) -> DigestRun:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.ctx.app_settings.time_window_hours)
        keywords = self._exclude_keywords()
        seen = self._already_seen()

        kept: dict[str, TweetItem] = {}
        dropped_kw = dropped_dup = dropped_old = dropped_empty = 0

        for t in state.raw_tweets:
            if t.tweet_id in seen or t.tweet_id in kept:
                dropped_dup += 1
                continue
            if not (t.text or "").strip():
                dropped_empty += 1
                continue
            if t.created_at:
                created = datetime.fromisoformat(t.created_at.replace("Z", "+00:00"))
                if created < cutoff:
                    dropped_old += 1
                    continue
            low = t.text.lower()
            if any(kw in low for kw in keywords):
                dropped_kw += 1
                continue
            kept[t.tweet_id] = t

        state.filtered_tweets = list(kept.values())
        self.log.info(
            "Filtered %d -> %d (dropped: %d dup, %d old, %d keyword, %d empty)",
            len(state.raw_tweets), len(state.filtered_tweets),
            dropped_dup, dropped_old, dropped_kw, dropped_empty,
        )
        return state
