import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import pipeline
from db.models import DigestRun as DigestRunRow, RawTweet, RunStatus, Tweet


@pytest.fixture
def env(monkeypatch, tmp_path):
    """In-memory DB + temp data dir so delete_run never touches real data."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(pipeline, "get_session", lambda: Session(engine))
    monkeypatch.setattr(pipeline.settings, "data_dir", str(tmp_path))
    return engine, tmp_path


def _seed(engine, tmp_path, run_id=5, status=RunStatus.success):
    digest = tmp_path / "digests" / f"d{run_id}.html"
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_text("<html>digest</html>")
    snap = tmp_path / "runs" / str(run_id)
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "1_collected.json").write_text("{}")
    with Session(engine) as s:
        s.add(DigestRunRow(id=run_id, status=status, digest_path=str(digest)))
        s.add(Tweet(tweet_id="t1", handle="a", run_id=run_id))
        s.add(Tweet(tweet_id="t2", handle="a", run_id=run_id))
        s.add(RawTweet(tweet_id="t1", handle="a", run_id=run_id))
        s.add(RawTweet(tweet_id="z9", handle="b", run_id=999))   # different run, must survive
        s.commit()
    return digest, snap


def test_delete_run_removes_everything(env):
    engine, tmp_path = env
    digest, snap = _seed(engine, tmp_path, run_id=5)

    summary = pipeline.delete_run(5)
    assert summary == {"run_id": 5, "tweets": 2, "raw_tweets": 1,
                       "digest_deleted": True, "snapshots_deleted": True}
    assert not digest.exists()
    assert not snap.exists()
    with Session(engine) as s:
        assert s.get(DigestRunRow, 5) is None
        assert s.exec(select(Tweet).where(Tweet.run_id == 5)).all() == []
        assert s.exec(select(RawTweet).where(RawTweet.run_id == 5)).all() == []
        # other run's data untouched
        assert s.exec(select(RawTweet).where(RawTweet.run_id == 999)).one().tweet_id == "z9"


def test_delete_run_missing_returns_none(env):
    assert pipeline.delete_run(123) is None


def test_delete_run_refuses_running(env):
    engine, tmp_path = env
    _seed(engine, tmp_path, run_id=7, status=RunStatus.running)
    with pytest.raises(RuntimeError):
        pipeline.delete_run(7)
    with Session(engine) as s:
        assert s.get(DigestRunRow, 7) is not None   # nothing deleted
