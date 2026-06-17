import json
from types import SimpleNamespace

import pipeline
from db.models import ClusteringMethod, DigestStyle
from state import DigestRun, ThemeCluster, TweetItem, load_latest_snapshot


def _state():
    st = DigestRun(run_id=7)
    st.raw_tweets = [TweetItem("1", "a", "hello"), TweetItem("2", "b", "world")]
    st.filtered_tweets = [TweetItem("1", "a", "hello", member_ids=["1", "1b"])]
    st.themes = [ThemeCluster(title="T", summary="S", tweet_ids=["1"])]
    return st


def test_snapshot_round_trip():
    st = _state()
    restored = DigestRun.from_dict(st.to_dict())
    assert restored.run_id == 7
    assert [t.tweet_id for t in restored.filtered_tweets] == ["1"]
    assert restored.filtered_tweets[0].member_ids == ["1", "1b"]
    assert restored.themes[0].title == "T"


def test_load_latest_snapshot_picks_furthest(tmp_path):
    run_dir = tmp_path / "runs" / "7"
    run_dir.mkdir(parents=True)
    st = _state()
    (run_dir / "2_filtered.json").write_text(json.dumps(st.to_dict(), default=str))
    (run_dir / "2a_threaded.json").write_text(json.dumps(st.to_dict(), default=str))
    loaded = load_latest_snapshot(str(tmp_path), 7)
    assert loaded is not None
    _, stage = loaded
    assert stage == "2a_threaded"   # furthest along, not 2_filtered


def test_load_latest_snapshot_missing(tmp_path):
    assert load_latest_snapshot(str(tmp_path), 99) is None


def _ctx(**aps):
    return SimpleNamespace(app_settings=SimpleNamespace(**aps))


def test_stage_plan_skips_up_to_start_after():
    """resume from 2a_threaded should run only clusterer(if on)/summarizer/reporter."""
    ctx = _ctx(stitch_threads=True, digest_style=DigestStyle.themed,
               clustering_method=ClusteringMethod.llm)
    plan = pipeline._stage_plan(ctx)
    labels = [l for l, _, _ in plan]
    idx = labels.index("2a_threaded") + 1
    remaining = [(l, cond()) for l, cond, _ in plan[idx:]]
    assert remaining == [("2b_clustered", False), ("3_summarized", True), ("4_reported", True)]


def test_stage_plan_full_run_from_collect():
    ctx = _ctx(stitch_threads=True, digest_style=DigestStyle.themed,
               clustering_method=ClusteringMethod.embedding)
    plan = pipeline._stage_plan(ctx)
    # "1_collected" isn't a post-collection label -> run everything
    labels = [l for l, _, _ in plan]
    assert "1_collected" not in labels
    assert [l for l, cond, _ in plan if cond()] == [
        "2_filtered", "2a_threaded", "2b_clustered", "3_summarized", "4_reported"]
