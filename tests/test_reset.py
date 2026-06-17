"""Tests for pipeline.reset_runs — wipes run data, keeps config, refuses while running."""
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import pipeline
from db.models import (AppSettings, DailyStat, DigestRun as DigestRunRow, ExcludedAccount,
                       MetaDigest, RawTweet, RunStatus, Topic, Tweet)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """In-memory DB + temp data dir so reset_runs never touches real data; no DB-file backup."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(pipeline, "get_session", lambda: Session(engine))
    monkeypatch.setattr(pipeline.settings, "data_dir", str(tmp_path))
    return engine, tmp_path


def _seed(engine, tmp_path):
    (tmp_path / "digests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "digests" / "d.html").write_text("<html></html>")
    (tmp_path / "runs" / "5").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runs" / "5" / "1_collected.json").write_text("{}")
    with Session(engine) as s:
        # run data
        s.add(DigestRunRow(id=5, status=RunStatus.success))
        s.add(Tweet(tweet_id="t1", handle="a", run_id=5))
        s.add(RawTweet(tweet_id="t1", handle="a", run_id=5))
        s.add(DailyStat(date="2026-06-17", tweet_count=1))
        s.add(MetaDigest(narrative="x"))
        # config that must survive
        s.add(AppSettings(id=1, max_themes=99))
        s.add(ExcludedAccount(handle="spam"))
        s.add(Topic(name="AI"))
        s.commit()


def test_reset_wipes_run_data_keeps_config(env):
    engine, tmp_path = env
    _seed(engine, tmp_path)

    summary = pipeline.reset_runs(backup=False)

    assert summary["tables"]["digest_runs"] == 1
    assert summary["tables"]["tweets"] == 1
    assert summary["snapshot_dirs"] == 1
    assert summary["files"] >= 1
    with Session(engine) as s:
        assert s.exec(select(DigestRunRow)).all() == []
        assert s.exec(select(Tweet)).all() == []
        assert s.exec(select(RawTweet)).all() == []
        assert s.exec(select(DailyStat)).all() == []
        assert s.exec(select(MetaDigest)).all() == []
        # config preserved
        assert s.get(AppSettings, 1).max_themes == 99
        assert s.exec(select(ExcludedAccount)).one().handle == "spam"
        assert s.exec(select(Topic)).one().name == "AI"
    # on-disk run artifacts gone, folders remain
    assert list((tmp_path / "runs").iterdir()) == []
    assert list((tmp_path / "digests").iterdir()) == []


def test_reset_refuses_while_running(env, monkeypatch):
    monkeypatch.setattr(pipeline, "is_running", lambda: True)
    with pytest.raises(RuntimeError):
        pipeline.reset_runs(backup=False)
