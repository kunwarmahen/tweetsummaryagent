"""Thread stitcher — merge an author's self-reply chain into one item.

Two modes (Settings → thread_mode):
- "reply" (default): uses captured reply metadata. A tweet that replies to its own author
  (`is_self_reply`) continues the preceding tweet's thread — accurate, gap-independent.
- "time": heuristic fallback — an author's consecutive tweets within `thread_gap_minutes`
  are merged. Useful when X didn't render the "Replying to" context.

Either way the summarizer sees one coherent thread instead of fragments. Retweets and
undated tweets pass through untouched.
"""
from __future__ import annotations

from datetime import datetime

from agents.base import Agent
from db.models import ThreadMode
from state import DigestRun, TweetItem


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class ThreadStitcher(Agent):
    name = "threader"

    def run(self, state: DigestRun) -> DigestRun:
        mode = getattr(self.ctx.app_settings, "thread_mode", ThreadMode.reply)
        tweets = state.filtered_tweets

        # Tweets without a timestamp can't be ordered; retweets aren't threads.
        by_author: dict[str, list[TweetItem]] = {}
        passthrough: list[TweetItem] = []
        for t in tweets:
            if _parse(t.created_at) is None or t.is_retweet:
                passthrough.append(t)
            else:
                by_author.setdefault(t.handle, []).append(t)

        joins = self._joins_time if mode == ThreadMode.time else self._joins_reply
        gap_seconds = self.ctx.app_settings.thread_gap_minutes * 60

        merged: list[TweetItem] = []
        threads_found = 0
        for group in by_author.values():
            group.sort(key=lambda t: _parse(t.created_at))
            run: list[TweetItem] = [group[0]]
            for t in group[1:]:
                if joins(t, run[-1], gap_seconds):
                    run.append(t)
                else:
                    merged.append(self._merge(run))
                    threads_found += len(run) > 1
                    run = [t]
            merged.append(self._merge(run))
            threads_found += len(run) > 1

        state.filtered_tweets = merged + passthrough
        self.log.info("Stitched %d threads (%s mode); %d -> %d items",
                      threads_found, mode.value, len(tweets), len(state.filtered_tweets))
        return state

    @staticmethod
    def _joins_reply(t: TweetItem, prev: TweetItem, gap_seconds: int) -> bool:
        return t.is_self_reply

    @staticmethod
    def _joins_time(t: TweetItem, prev: TweetItem, gap_seconds: int) -> bool:
        return (_parse(t.created_at) - _parse(prev.created_at)).total_seconds() <= gap_seconds

    def _merge(self, run: list[TweetItem]) -> TweetItem:
        if len(run) == 1:
            return run[0]
        first = run[0]   # earliest tweet anchors the thread (its id/url/time)
        return TweetItem(
            tweet_id=first.tweet_id,
            handle=first.handle,
            author_name=first.author_name,
            text="\n\n".join(t.text for t in run if t.text),
            url=first.url,
            created_at=first.created_at,
            likes=max(t.likes for t in run),
            retweets=max(t.retweets for t in run),
            is_retweet=False,
            member_ids=[t.tweet_id for t in run],
        )
