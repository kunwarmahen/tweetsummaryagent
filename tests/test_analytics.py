from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from agents import analytics
from db.models import DailyStat, RawTweet, ThemeHistory, TopicCluster
from state import ThemeCluster


@pytest.fixture
def session():
    """Isolated in-memory DB so analytics tests never touch data/agent.db."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _raw(s, tid, handle, created, likes=0, retweets=0, **kw):
    s.add(RawTweet(tweet_id=tid, handle=handle, created_at=created,
                   likes=likes, retweets=retweets, **kw))


# ----------------------------------------------------------------- daily_stats
def test_recompute_daily_stats_aggregates_per_day(session):
    d1 = datetime(2026, 6, 15, 10, tzinfo=timezone.utc)
    d2 = datetime(2026, 6, 16, 10, tzinfo=timezone.utc)
    _raw(session, "1", "a", d1, likes=5, retweets=1)
    _raw(session, "2", "b", d1, likes=3, retweets=0, is_self_reply=True)
    _raw(session, "3", "a", d2, likes=10, retweets=2, is_retweet=True)
    _raw(session, "4", "nodate", None)            # no created_at -> skipped
    session.commit()

    assert analytics.recompute_daily_stats(session) == 2
    rows = {r.date: r for r in session.exec(select(DailyStat)).all()}
    assert rows["2026-06-15"].tweet_count == 2
    assert rows["2026-06-15"].account_count == 2
    assert rows["2026-06-15"].engagement == 9          # 5+1 + 3+0
    assert rows["2026-06-15"].self_reply_count == 1
    assert rows["2026-06-16"].retweet_count == 1


def test_recompute_daily_stats_is_idempotent(session):
    _raw(session, "1", "a", datetime(2026, 6, 15, tzinfo=timezone.utc), likes=1)
    session.commit()
    analytics.recompute_daily_stats(session)
    analytics.recompute_daily_stats(session)          # no duplicate rows
    assert len(session.exec(select(DailyStat)).all()) == 1


# ----------------------------------------------------- leaderboard / top tweets (windowed)
def test_account_leaderboard_and_top_tweets_window(session):
    now = datetime.now(timezone.utc)
    _raw(session, "1", "busy", now, likes=10, retweets=0)
    _raw(session, "2", "busy", now, likes=1, retweets=0)
    _raw(session, "3", "viral", now, likes=500, retweets=100)
    _raw(session, "old", "busy", now - timedelta(days=30), likes=999)   # outside 7d window
    session.commit()

    leaders = analytics.account_leaderboard(session, days=7)
    assert leaders[0]["handle"] == "busy" and leaders[0]["tweets"] == 2   # most active
    tops = analytics.top_tweets(session, days=7)
    assert tops[0].tweet_id == "3"                                        # most engaged
    assert "old" not in {t.tweet_id for t in tops}                       # window excludes it


# ----------------------------------------------------------- theme continuity (single-linkage)
@pytest.fixture
def fake_embeddings(monkeypatch):
    """Deterministic title embeddings so continuity is tested without Ollama."""
    vectors = {
        "AI chips": [1.0, 0.0, 0.0],
        "Semiconductors and AI": [0.98, 0.02, 0.0],   # ~same topic as 'AI chips'
        "Market selloff": [0.0, 1.0, 0.0],            # different topic
    }
    monkeypatch.setattr(analytics, "_embed_theme",
                        lambda client, model, title, summary: vectors[title])


def _theme(title, ids):
    return ThemeCluster(title=title, summary="", tweet_ids=ids)


def test_index_run_themes_links_recurring_topic_across_runs(session, fake_embeddings):
    eng = {"t1": 100, "t2": 50}
    analytics.index_run_themes(session, 1, "2026-06-15",
                               [_theme("AI chips", ["t1"]), _theme("Market selloff", ["t2"])],
                               eng, model="x")
    # Next day: a near-identical AI theme should join the SAME cluster; the market one is new again.
    analytics.index_run_themes(session, 2, "2026-06-16",
                               [_theme("Semiconductors and AI", ["t1"])], {"t1": 30}, model="x")

    clusters = session.exec(select(TopicCluster)).all()
    ai = [c for c in clusters if c.appearance_count == 2]
    assert len(ai) == 1                                  # AI cluster recurred
    assert ai[0].label == "Semiconductors and AI"        # latest title names it
    assert len(clusters) == 2                            # AI (x2) + market (x1)
    assert len(session.exec(select(ThemeHistory)).all()) == 3


def test_index_run_themes_keeps_same_run_themes_separate(session, fake_embeddings):
    # Two themes in ONE run never merge into each other even if similar.
    analytics.index_run_themes(session, 1, "2026-06-15",
                               [_theme("AI chips", ["t1"]), _theme("Semiconductors and AI", ["t2"])],
                               {}, model="x")
    assert len(session.exec(select(TopicCluster)).all()) == 2


def test_trending_themes_ranks_recurring_first(session, fake_embeddings):
    analytics.index_run_themes(session, 1, "2026-06-15",
                               [_theme("AI chips", ["t1"]), _theme("Market selloff", ["t2"])],
                               {"t1": 1, "t2": 1}, model="x")
    analytics.index_run_themes(session, 2, "2026-06-16",
                               [_theme("Semiconductors and AI", ["t1"])], {"t1": 1}, model="x")
    # Wide window so both fixture dates are included regardless of today's date.
    trending = analytics.trending_themes(session, days=3650)
    assert trending[0]["label"] == "Semiconductors and AI"   # 2 days seen ranks first
    assert trending[0]["days"] == 2


# ----------------------------------------------------------------- weekly meta-digest
def test_generate_meta_digest_stores_narrative(session, fake_embeddings, monkeypatch):
    analytics.index_run_themes(session, 1, "2026-06-15",
                               [_theme("AI chips", ["t1"])], {"t1": 9}, model="x")
    monkeypatch.setattr(analytics, "_llm_meta_narrative",
                        lambda lines, days, model: "## This week\n\nAI was everywhere.")

    row = analytics.generate_meta_digest(days=3650, session=session)
    assert row is not None and "AI was everywhere" in row.narrative
    assert analytics.latest_meta_digest(session).id == row.id


def test_generate_meta_digest_no_themes_returns_none(session, monkeypatch):
    # Should not even call the LLM when there are no themes in the window.
    monkeypatch.setattr(analytics, "_llm_meta_narrative",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called")))
    assert analytics.generate_meta_digest(days=7, session=session) is None
