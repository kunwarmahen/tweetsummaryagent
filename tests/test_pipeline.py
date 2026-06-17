import json

import pipeline
from db.models import AppSettings, ClusteringMethod, DigestStyle


def test_to_namespace_applies_overrides_and_preserves_enums():
    ns = pipeline._to_namespace(AppSettings(), {
        "digest_style": DigestStyle.highlights,
        "topics_override": ["ai", "macro"],
        "deliver": False,
    })
    assert ns.digest_style is DigestStyle.highlights
    assert ns.digest_style.value == "highlights"        # still an enum, not a str
    assert ns.clustering_method is ClusteringMethod.llm  # untouched field preserved
    assert ns.topics_override == ["ai", "macro"]         # transient attr accepted
    assert ns.deliver is False


def test_effective_topics_prefers_override():
    from types import SimpleNamespace
    ns = SimpleNamespace(topics_override=["x", "y"])
    assert pipeline._effective_topics(ns) == ["x", "y"]


def test_load_replay_state_and_replayable(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "data_dir", str(tmp_path))
    run_dir = tmp_path / "runs" / "7"
    run_dir.mkdir(parents=True)
    (run_dir / "2_filtered.json").write_text(json.dumps({
        "run_id": 7,
        "filtered_tweets": [{"tweet_id": "1", "handle": "alice", "text": "hi"}],
    }))
    assert pipeline.is_replayable(7) is True
    state = pipeline._load_replay_state(7)
    assert [t.handle for t in state.filtered_tweets] == ["alice"]
    assert pipeline.is_replayable(999) is False
