"""Tests for the decoupled Collect / Process(draft) / Deliver phases and collector early-stop."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import pipeline
from agents.collector import Collector
from db.models import DigestRun as DigestRunRow, RawTweet, RunStatus
from state import DigestRun, TweetItem


@pytest.fixture
def mem_db(monkeypatch):
    """Isolated in-memory DB so phase tests never touch data/agent.db."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(pipeline, "get_session", lambda: Session(engine))
    return engine


def _archive(engine, tweet_id, handle, created_at, text="hi"):
    with Session(engine) as s:
        s.add(RawTweet(tweet_id=tweet_id, handle=handle, text=text, created_at=created_at))
        s.commit()


# ---------------------------------------------------------------- archive window
def test_load_archive_window_respects_time_and_emits_aware_iso(mem_db):
    now = datetime.utcnow()
    _archive(mem_db, "fresh", "alice", now - timedelta(hours=2))
    _archive(mem_db, "stale", "bob", now - timedelta(hours=50))

    items = pipeline._load_archive_window(24)
    assert {i.tweet_id for i in items} == {"fresh"}        # stale dropped by window
    # created_at must be parseable as tz-aware so the Filter compares correctly
    parsed = datetime.fromisoformat(items[0].created_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_load_archive_window_maps_fields(mem_db):
    _archive(mem_db, "1", "alice", datetime.utcnow(), text="hello world")
    (item,) = pipeline._load_archive_window(24)
    assert item.handle == "alice" and item.text == "hello world"


# ---------------------------------------------------------------- draft lifecycle
def test_draft_row_helpers(mem_db):
    assert pipeline._current_draft_id() is None
    rid = pipeline._create_draft_row()
    assert pipeline._current_draft_id() == rid

    state = DigestRun(run_id=rid)
    state.filtered_tweets = [TweetItem("1", "alice", "x"), TweetItem("2", "bob", "y")]
    pipeline._update_draft_row(rid, state)
    with Session(mem_db) as s:
        row = s.get(DigestRunRow, rid)
    assert row.status == RunStatus.draft        # still a draft after refresh
    assert row.tweet_count == 2 and row.account_count == 2


def test_delivery_finalizes_open_draft(mem_db):
    """A finalized (success) run is no longer the 'current draft'."""
    rid = pipeline._create_draft_row()
    assert pipeline._current_draft_id() == rid
    with Session(mem_db) as s:                  # simulate delivery finishing the run
        row = s.get(DigestRunRow, rid)
        row.status = RunStatus.success
        s.add(row)
        s.commit()
    assert pipeline._current_draft_id() is None


# ---------------------------------------------------------------- delivery window
def _finished_delivery(engine, finished_at, emailed=True):
    with Session(engine) as s:
        s.add(DigestRunRow(status=RunStatus.success, finished_at=finished_at, emailed=emailed))
        s.commit()


def test_delivery_window_floor_when_recent_delivery(mem_db):
    """A recent delivery keeps the configured floor (24h)."""
    _finished_delivery(mem_db, datetime.utcnow() - timedelta(hours=6))
    assert pipeline._delivery_window_hours(24) == 24


def test_delivery_window_spans_gap_since_last_delivery(mem_db):
    """A delivery 40h ago widens the window past the floor so nothing in between is dropped."""
    _finished_delivery(mem_db, datetime.utcnow() - timedelta(hours=40))
    assert pipeline._delivery_window_hours(24) >= 41        # gap + margin, beats the 24h floor


def test_delivery_window_ignores_undelivered_runs(mem_db):
    """Draft/non-emailed runs don't count as deliveries; with none, fall back to the floor."""
    _finished_delivery(mem_db, datetime.utcnow() - timedelta(hours=40), emailed=False)
    assert pipeline._delivery_window_hours(24) == 24


def test_delivery_window_min_hours_forces_catch_up(mem_db):
    """A one-off catch-up can force a wider reach than both floor and gap."""
    _finished_delivery(mem_db, datetime.utcnow() - timedelta(hours=2))
    assert pipeline._delivery_window_hours(24, min_hours=72) == 72


# ---------------------------------------------------------------- run-lock collision
def test_acquire_run_lock_skips_when_busy_and_not_waiting():
    pipeline._run_lock.acquire()
    try:
        assert pipeline._acquire_run_lock("X", 0) is False        # non-blocking + busy -> skip
    finally:
        pipeline._run_lock.release()


def test_acquire_run_lock_times_out_when_held():
    pipeline._run_lock.acquire()
    try:
        assert pipeline._acquire_run_lock("X", 0.05) is False      # bounded wait expires -> skip
    finally:
        pipeline._run_lock.release()


def test_acquire_run_lock_succeeds_when_free():
    assert pipeline._acquire_run_lock("X", 0) is True
    pipeline._run_lock.release()


def test_acquire_run_lock_waits_then_acquires_after_release():
    """A colliding phase that waits gets the lock once the holder releases (not dropped)."""
    import threading

    pipeline._run_lock.acquire()
    threading.Timer(0.05, pipeline._run_lock.release).start()
    try:
        assert pipeline._acquire_run_lock("X", 5) is True          # waited out the holder
    finally:
        pipeline._run_lock.release()


# ---------------------------------------------------------------- collector early-stop
class _FakePage:
    """Returns the same batch of tweets on every evaluate(), counting scrolls."""
    def __init__(self, batch):
        self.batch = batch
        self.scrolls = 0

    def goto(self, *a, **k): pass
    def wait_for_selector(self, *a, **k): pass
    def wait_for_timeout(self, *a, **k): pass
    def evaluate(self, *a, **k): return self.batch

    class _Mouse:
        def __init__(self, page): self.page = page
        def wheel(self, *a, **k): self.page.scrolls += 1
    @property
    def mouse(self): return _FakePage._Mouse(self)


def _raw(tweet_id, dt):
    return {"url": f"https://x.com/alice/status/{tweet_id}", "userName": "Alice\n@alice",
            "datetime": dt, "text": "t", "like": "0", "rt": "0", "replyTo": None}


def test_scrape_account_stops_at_known(make_ctx):
    """Once a known (already-archived) tweet appears, scrolling stops and it isn't re-collected."""
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    c = Collector(make_ctx(max_tweets_per_account=50, include_retweets=True))
    c._known = {"222"}
    page = _FakePage([_raw("111", recent), _raw("222", recent)])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    out = c._scrape_account(page, "alice", cutoff)
    assert {t.tweet_id for t in out} == {"111"}    # known one skipped
    assert page.scrolls == 0                         # stopped scrolling after reaching known


def test_scrape_account_keeps_scrolling_without_known(make_ctx):
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    c = Collector(make_ctx(max_tweets_per_account=50, include_retweets=True))
    c._known = set()
    page = _FakePage([_raw("111", recent)])           # only 1 tweet, never hits max_per
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    c._scrape_account(page, "alice", cutoff)
    assert page.scrolls > 0                            # kept scrolling looking for more
