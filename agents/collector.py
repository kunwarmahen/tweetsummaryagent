"""Collector agent — Playwright browser scraping of the following list.

Reuses the saved session, enumerates the accounts you follow (minus the blocklist),
and scrapes each account's recent tweets into the shared state.

NOTE: uses Playwright's *sync* API, so this must run in a worker thread, never directly
inside an asyncio event loop (the scheduler/Run-now path runs it off-thread).
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright
from sqlmodel import select

from agents import selectors
from agents.base import Agent
from agents.browser import launch_context, session_exists
from auth.login import load_handle
from db.models import AccountSetting, ExcludedAccount, RawTweet, ThreadMode
from db.session import get_session
from state import DigestRun, TweetItem

_STATUS_RE = re.compile(r"/status/(\d+)")
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]+)")
_COUNT_RE = re.compile(r"([\d,.]+)")

# JS run in-page to extract every visible tweet at once (robust + fast).
_EXTRACT_JS = """
() => Array.from(document.querySelectorAll('article[data-testid="tweet"]')).map(a => {
  const textEl = a.querySelector('[data-testid="tweetText"]');
  const timeEl = a.querySelector('time');
  const linkEl = timeEl ? timeEl.closest('a') : null;
  const social = a.querySelector('[data-testid="socialContext"]');
  const userName = a.querySelector('[data-testid="User-Name"]');
  const likeEl = a.querySelector('[data-testid="like"]');
  const rtEl = a.querySelector('[data-testid="retweet"]');
  // "Replying to @handle" context → capture the first replied-to handle (innerText is robust).
  let replyTo = null;
  const rm = (a.innerText || '').match(/Replying to\\s+@(\\w+)/);
  if (rm) replyTo = rm[1];
  return {
    text: textEl ? textEl.innerText : '',
    datetime: timeEl ? timeEl.getAttribute('datetime') : null,
    url: linkEl ? linkEl.href : null,
    social: social ? social.innerText : null,
    userName: userName ? userName.innerText : null,
    like: likeEl ? likeEl.getAttribute('aria-label') : null,
    rt: rtEl ? rtEl.getAttribute('aria-label') : null,
    replyTo: replyTo,
  };
})
"""

_FOLLOWING_JS = """
() => Array.from(document.querySelectorAll('[data-testid="UserCell"]')).map(c => {
  const link = c.querySelector('a[href^="/"]');
  return link ? link.getAttribute('href').replace(/^\\//, '') : null;
}).filter(h => h && !h.includes('/'))
"""


def _parse_count(label: str | None) -> int:
    if not label:
        return 0
    m = _COUNT_RE.search(label.replace(",", ""))
    if not m:
        return 0
    val = m.group(1)
    try:
        return int(float(val))
    except ValueError:
        return 0


class Collector(Agent):
    name = "collector"

    def __init__(self, ctx, max_accounts: int | None = None, skip_known: bool = True):
        super().__init__(ctx)
        self.max_accounts = max_accounts
        self.skip_known = skip_known        # don't re-scrape tweets already in the archive
        self._limits: dict[str, int] = {}   # per-handle tweet caps, populated in run()
        self._known: set[str] = set()       # tweet_ids already archived (early-stop scrolling)

    def _excluded_handles(self) -> set[str]:
        with get_session() as session:
            rows = session.exec(select(ExcludedAccount)).all()
        return {r.handle.lstrip("@").lower() for r in rows}

    def _known_ids(self) -> set[str]:
        """tweet_ids already in the raw archive — so frequent runs skip what we've seen."""
        with get_session() as session:
            return set(session.exec(select(RawTweet.tweet_id)).all())

    def _account_limits(self) -> dict[str, int]:
        """Per-handle max-tweets overrides (lowercased handle -> cap)."""
        with get_session() as session:
            rows = session.exec(select(AccountSetting)).all()
        return {r.handle.lstrip("@").lower(): r.max_tweets for r in rows}

    def _max_for(self, handle: str) -> int:
        return self._limits.get(handle.lower(), self.ctx.app_settings.max_tweets_per_account)

    def _scroll(self, page, rounds: int, pause_ms: int = 1200) -> None:
        for _ in range(rounds):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(pause_ms + random.randint(0, 600))

    def _enumerate_following(self, page, my_handle: str) -> list[str]:
        page.goto(selectors.following_url(my_handle), wait_until="domcontentloaded")
        try:
            page.wait_for_selector(selectors.USER_CELL, timeout=15_000)
        except Exception:
            self.log.warning("No following cells found for @%s", my_handle)
            return []
        seen: dict[str, None] = {}
        for _ in range(30):  # scroll the following list
            for h in page.evaluate(_FOLLOWING_JS):
                seen.setdefault(h, None)
            self._scroll(page, rounds=1)
        return list(seen.keys())

    def _check_session(self, page) -> None:
        """Fail fast with a clear message if the saved session is no longer logged in."""
        page.goto(f"{selectors.BASE}/home", wait_until="domcontentloaded")
        try:
            page.wait_for_selector(selectors.PROFILE_LINK, timeout=15_000)
        except Exception:
            raise RuntimeError(
                "X session is not active (you appear logged out). Make sure you're logged into "
                "X in Google Chrome, then run `python main.py import-profile`."
            )

    def _scrape_account(self, page, handle: str, cutoff: datetime,
                        with_replies: bool = False, attempts: int = 2) -> list[TweetItem]:
        url = selectors.with_replies_url(handle) if with_replies else selectors.profile_url(handle)
        loaded = False
        for attempt in range(attempts):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_selector(selectors.TWEET, timeout=15_000)
                loaded = True
                break
            except Exception:
                if attempt < attempts - 1:
                    backoff = 3000 * (attempt + 1) + random.randint(0, 1500)
                    page.wait_for_timeout(backoff)  # transient slowness/throttle — retry
        if not loaded:
            self.log.warning("No tweets visible for @%s (after %d attempts)", handle, attempts)
            return []

        items: dict[str, TweetItem] = {}
        max_per = self._max_for(handle)
        reached_known = False
        for _ in range(15):  # scroll the profile timeline
            for raw in page.evaluate(_EXTRACT_JS):
                item = self._to_item(raw, fallback_handle=handle)
                if item is None or item.created_at is None:
                    continue
                if with_replies:
                    # with_replies interleaves others' tweets and conversational replies;
                    # keep only this author's originals + self-reply thread parts.
                    if item.handle.lower() != handle.lower():
                        continue
                    if item.reply_to and not item.is_self_reply:
                        continue
                # Already archived → everything below is older and also archived, so we can
                # stop scrolling once this page is processed (timeline is newest-first).
                if self.skip_known and item.tweet_id in self._known:
                    reached_known = True
                    continue
                created = datetime.fromisoformat(item.created_at.replace("Z", "+00:00"))
                if created < cutoff:
                    continue
                items[item.tweet_id] = item
            if len(items) >= max_per or reached_known:
                break
            self._scroll(page, rounds=1)
        return list(items.values())[:max_per]

    def _to_item(self, raw: dict, fallback_handle: str) -> TweetItem | None:
        url = raw.get("url") or ""
        m = _STATUS_RE.search(url)
        if not m:
            return None
        author_handle, author_name = fallback_handle, None
        user_name = raw.get("userName") or ""
        hm = _HANDLE_RE.search(user_name)
        if hm:
            author_handle = hm.group(1)
            author_name = user_name.split("\n")[0].strip() or None
        social = (raw.get("social") or "").lower()
        is_rt = "repost" in social or "retweet" in social
        reply_to = raw.get("replyTo")
        is_self_reply = bool(reply_to) and reply_to.lower() == author_handle.lower()
        return TweetItem(
            tweet_id=m.group(1),
            handle=author_handle,
            author_name=author_name,
            reply_to=reply_to,
            is_self_reply=is_self_reply,
            text=raw.get("text") or "",
            url=url,
            created_at=raw.get("datetime"),
            likes=_parse_count(raw.get("like")),
            retweets=_parse_count(raw.get("rt")),
            is_retweet=is_rt,
        )

    def run(self, state: DigestRun) -> DigestRun:
        my_handle = load_handle()
        if not my_handle:
            raise RuntimeError("No saved handle. Run `python main.py login` first.")
        if not session_exists():
            raise RuntimeError("No saved session. Run `python main.py login` first.")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.ctx.app_settings.time_window_hours)
        excluded = self._excluded_handles()
        self._limits = self._account_limits()
        if self._limits:
            self.log.info("Per-account tweet limits set for %d account(s)", len(self._limits))
        if self.skip_known:
            self._known = self._known_ids()
            self.log.info("Skipping %d already-archived tweets (early-stop on known)", len(self._known))
        include_rt = self.ctx.app_settings.include_retweets
        # Reply-based thread detection needs the "with replies" timeline (the Posts tab
        # hides thread continuations). Other modes use the cleaner Posts tab.
        with_replies = (
            getattr(self.ctx.app_settings, "stitch_threads", False)
            and getattr(self.ctx.app_settings, "thread_mode", ThreadMode.reply) == ThreadMode.reply
        )

        with sync_playwright() as p:
            browser, context = launch_context(p, headless=True)
            page = context.new_page()

            self._check_session(page)
            following = self._enumerate_following(page, my_handle)
            self.log.info("Found %d followed accounts", len(following))
            targets = [h for h in following if h.lower() not in excluded]
            self.log.info("Scraping %d after excluding %d blocked", len(targets), len(following) - len(targets))
            if self.max_accounts:
                targets = targets[:self.max_accounts]
                self.log.info("Limiting to first %d accounts (test mode)", len(targets))

            if with_replies:
                self.log.info("Reply-mode thread detection: scraping 'with replies' timelines")
            empty_streak = 0
            for i, handle in enumerate(targets):
                try:
                    tweets = self._scrape_account(page, handle, cutoff, with_replies=with_replies)
                except Exception as e:  # one bad account shouldn't kill the run
                    self.log.warning("Failed scraping @%s: %s", handle, e)
                    continue
                if not include_rt:
                    tweets = [t for t in tweets if not t.is_retweet]
                self.log.info("@%s -> %d tweets in window", handle, len(tweets))
                state.raw_tweets.extend(tweets)

                # Detect likely rate-limiting: many consecutive empty timelines.
                empty_streak = empty_streak + 1 if not tweets else 0
                if empty_streak >= 8:
                    self.log.warning(
                        "8 consecutive accounts returned no tweets — X is likely rate-limiting. "
                        "Stopping early; try again later (X throttles rapid scraping)."
                    )
                    break

                # Gentle pause between accounts; back off harder as empties accumulate.
                if i < len(targets) - 1:
                    base = random.randint(2500, 5000)
                    page.wait_for_timeout(base + empty_streak * 1500)

            context.close()
            browser.close()

        self.log.info("Collected %d raw tweets total", len(state.raw_tweets))
        return state
