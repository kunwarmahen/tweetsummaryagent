import agents.reporter as rep
from state import DigestRun, ThemeCluster, TweetItem


def _state():
    st = DigestRun()
    st.filtered_tweets = [TweetItem("1", "alice", "hello", url="https://x.com/alice/status/1")]
    st.themes = [ThemeCluster(title="T", summary="S", tweet_ids=["1"])]
    return st


def test_reporter_skips_delivery_when_disabled(tmp_path, monkeypatch, make_ctx):
    monkeypatch.setattr(rep.settings, "data_dir", str(tmp_path))
    r = rep.Reporter(make_ctx(deliver=False))
    calls = {"email": 0, "tg": 0}
    monkeypatch.setattr(r, "_send_email", lambda *a, **k: calls.update(email=calls["email"] + 1) or True)
    monkeypatch.setattr(r, "_send_telegram", lambda *a, **k: calls.update(tg=calls["tg"] + 1) or True)

    st = _state()
    r.run(st)
    assert calls == {"email": 0, "tg": 0}            # nothing sent
    assert st.digest_path and (tmp_path / "digests").is_dir()   # but digest still saved


def test_reporter_delivers_when_enabled(tmp_path, monkeypatch, make_ctx):
    monkeypatch.setattr(rep.settings, "data_dir", str(tmp_path))
    r = rep.Reporter(make_ctx(deliver=True))
    calls = {"email": 0, "tg": 0}
    monkeypatch.setattr(r, "_send_email", lambda *a, **k: calls.update(email=calls["email"] + 1) or True)
    monkeypatch.setattr(r, "_send_telegram", lambda *a, **k: calls.update(tg=calls["tg"] + 1) or True)

    r.run(_state())
    assert calls == {"email": 1, "tg": 1}
