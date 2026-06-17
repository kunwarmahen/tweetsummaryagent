from datetime import datetime, timedelta, timezone

from agents.threader import ThreadStitcher
from db.models import ThreadMode
from state import DigestRun, TweetItem


def _ts(base, minutes):
    return (base + timedelta(minutes=minutes)).isoformat()


BASE = datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)


def test_reply_mode_chains_self_replies(make_ctx):
    ctx = make_ctx(thread_mode=ThreadMode.reply, thread_gap_minutes=10)
    st = DigestRun()
    st.filtered_tweets = [
        TweetItem("1", "alice", "root", created_at=_ts(BASE, 0), likes=10),
        TweetItem("2", "alice", "reply 1", created_at=_ts(BASE, 30), likes=5, is_self_reply=True),
        TweetItem("3", "alice", "reply 2", created_at=_ts(BASE, 90), likes=8, is_self_reply=True),
        TweetItem("4", "alice", "standalone", created_at=_ts(BASE, 200)),
    ]
    ThreadStitcher(ctx).run(st)
    by_id = {t.tweet_id: t for t in st.filtered_tweets}
    assert len(st.filtered_tweets) == 2                  # thread(1-3) chained despite big gaps
    assert by_id["1"].member_ids == ["1", "2", "3"]
    assert by_id["1"].likes == 10
    assert "4" in by_id


def test_reply_mode_does_not_merge_unrelated_close_tweets(make_ctx):
    ctx = make_ctx(thread_mode=ThreadMode.reply, thread_gap_minutes=10)
    st = DigestRun()
    st.filtered_tweets = [
        TweetItem("a", "bob", "one", created_at=_ts(BASE, 0)),
        TweetItem("b", "bob", "two", created_at=_ts(BASE, 1)),   # close in time but not a reply
    ]
    ThreadStitcher(ctx).run(st)
    assert len(st.filtered_tweets) == 2


def test_time_mode_merges_within_gap(make_ctx):
    ctx = make_ctx(thread_mode=ThreadMode.time, thread_gap_minutes=10)
    st = DigestRun()
    st.filtered_tweets = [
        TweetItem("1", "alice", "a", created_at=_ts(BASE, 0), likes=10),
        TweetItem("2", "alice", "b", created_at=_ts(BASE, 2), likes=5),
        TweetItem("3", "alice", "later", created_at=_ts(BASE, 300)),
    ]
    ThreadStitcher(ctx).run(st)
    by_id = {t.tweet_id: t for t in st.filtered_tweets}
    assert len(st.filtered_tweets) == 2
    assert by_id["1"].member_ids == ["1", "2"]


def test_retweets_and_undated_pass_through(make_ctx):
    ctx = make_ctx(thread_mode=ThreadMode.reply, thread_gap_minutes=10)
    st = DigestRun()
    st.filtered_tweets = [
        TweetItem("rt", "alice", "repost", created_at=_ts(BASE, 0), is_retweet=True),
        TweetItem("nd", "alice", "no date", created_at=None),
    ]
    ThreadStitcher(ctx).run(st)
    assert {t.tweet_id for t in st.filtered_tweets} == {"rt", "nd"}
