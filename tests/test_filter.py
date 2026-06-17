from datetime import datetime, timedelta, timezone

from agents.filter import Filter
from state import DigestRun, TweetItem


def test_filter_drops_dup_old_keyword_empty(make_ctx):
    ctx = make_ctx(exclude_keywords="spam, giveaway", time_window_hours=24)
    f = Filter(ctx)
    f._already_seen = lambda: {"seen1"}   # bypass DB

    now = datetime.now(timezone.utc)
    recent = now.isoformat()
    old = (now - timedelta(hours=48)).isoformat()

    st = DigestRun()
    st.raw_tweets = [
        TweetItem("keep", "x", "hello world", created_at=recent),
        TweetItem("seen1", "x", "already digested", created_at=recent),   # cross-day dup
        TweetItem("kw", "x", "free GIVEAWAY now", created_at=recent),     # excluded keyword
        TweetItem("old", "x", "yesterday news", created_at=old),          # outside window
        TweetItem("empty", "x", "   ", created_at=recent),                # empty text
        TweetItem("keep", "x", "same id again", created_at=recent),       # within-batch dup
    ]
    f.run(st)
    assert {t.tweet_id for t in st.filtered_tweets} == {"keep"}


def test_filter_keeps_tweets_without_timestamp(make_ctx):
    ctx = make_ctx(exclude_keywords="", time_window_hours=24)
    f = Filter(ctx)
    f._already_seen = lambda: set()
    st = DigestRun()
    st.raw_tweets = [TweetItem("a", "x", "no timestamp", created_at=None)]
    f.run(st)
    assert len(st.filtered_tweets) == 1
