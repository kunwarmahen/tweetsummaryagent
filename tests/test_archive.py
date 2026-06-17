import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import pipeline
from db.models import RawTweet
from state import DigestRun, TweetItem


@pytest.fixture
def mem_db(monkeypatch):
    """Isolated in-memory DB so archive tests never touch data/agent.db."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(pipeline, "get_session", lambda: Session(engine))
    return engine


def test_archive_raw_stores_all_and_dedups(mem_db):
    st = DigestRun(run_id=1)
    st.raw_tweets = [
        TweetItem("1", "a", "hello"),
        TweetItem("2", "b", "world"),
        TweetItem("2", "b", "world"),   # duplicate id within the batch
    ]
    assert pipeline._archive_raw(1, st) == 2          # dup collapsed
    assert pipeline._archive_raw(1, st) == 0          # idempotent on re-run
    with Session(mem_db) as s:
        rows = s.exec(select(RawTweet)).all()
    assert {r.tweet_id for r in rows} == {"1", "2"}


def test_archive_raw_keeps_tweets_the_filter_would_drop(mem_db):
    """The archive is pre-filter, so excluded/old tweets are still kept for analysis."""
    st = DigestRun(run_id=2)
    st.raw_tweets = [TweetItem("9", "spammer", "buy crypto now")]  # would be keyword-dropped
    assert pipeline._archive_raw(2, st) == 1
    with Session(mem_db) as s:
        assert s.exec(select(RawTweet)).one().handle == "spammer"


def test_archive_raw_empty_is_noop(mem_db):
    assert pipeline._archive_raw(1, DigestRun(run_id=1)) == 0
