from datetime import datetime, timezone

import agents.priority
import agents.reporter as rep
from agents.filter import Filter
from agents.priority import PALETTE, pick_color
from agents import telegram
from state import DigestRun, ThemeCluster, TweetItem


def test_pick_color_distinct_then_cycles():
    assert pick_color([]) == PALETTE[0]
    assert pick_color([PALETTE[0]]) == PALETTE[1]
    assert pick_color(PALETTE) == PALETTE[0]   # palette exhausted -> wraps


def test_apply_priority_floats_and_guarantees(make_ctx):
    r = rep.Reporter(make_ctx())
    st = DigestRun()
    st.filtered_tweets = [
        TweetItem("1", "norm", "plain"),
        TweetItem("2", "vip", "vip in theme"),
        TweetItem("3", "vip", "orphan vip"),
    ]
    st.themes = [ThemeCluster(title="Tech", summary="s", tweet_ids=["1", "2"])]
    by_id = {t.tweet_id: t for t in st.filtered_tweets}

    r._apply_priority(st, by_id, {"vip": "#e0245e"})

    assert st.themes[0].title.startswith("⭐")        # synthetic important section first
    assert st.themes[0].tweet_ids == ["3"]            # orphan guaranteed
    tech = next(t for t in st.themes if t.title == "Tech")
    assert tech.tweet_ids == ["2", "1"]               # vip floated above normal


def test_apply_priority_noop_without_important(make_ctx):
    r = rep.Reporter(make_ctx())
    st = DigestRun()
    st.filtered_tweets = [TweetItem("1", "a", "x")]
    st.themes = [ThemeCluster(title="T", summary="s", tweet_ids=["1"])]
    r._apply_priority(st, {"1": st.filtered_tweets[0]}, {})
    assert [t.title for t in st.themes] == ["T"]


def test_telegram_marks_important():
    themes = [{"title": "T", "summary": "s", "tweets": [
        TweetItem("1", "norm", "a", url="u1"),
        TweetItem("2", "vip", "b", url="u2"),
    ]}]
    msg = telegram.format_messages(themes, "Mon", 2, {"vip": "#fff"})[0]
    assert "⭐ Important: @vip" in msg          # legend line
    assert "• ⭐ <a href=\"u2\">@vip</a>" in msg  # vip starred
    assert "• <a href=\"u1\">@norm</a>" in msg    # normal not starred


def test_filter_keeps_vip_past_keyword(make_ctx, monkeypatch):
    monkeypatch.setattr(agents.priority, "load_important", lambda: {"vip": "#fff"})
    ctx = make_ctx(exclude_keywords="giveaway", time_window_hours=24)
    f = Filter(ctx)
    f._already_seen = lambda: set()
    now = datetime.now(timezone.utc).isoformat()
    st = DigestRun()
    st.raw_tweets = [
        TweetItem("1", "norm", "free giveaway", created_at=now),       # dropped: keyword
        TweetItem("2", "vip", "free giveaway too", created_at=now),    # kept: VIP bypass
    ]
    f.run(st)
    assert {t.tweet_id for t in st.filtered_tweets} == {"2"}
