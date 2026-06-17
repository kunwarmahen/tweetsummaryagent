from agents.summarizer import Summarizer, _coerce_indices, _items_list
from state import DigestRun, TweetItem


def test_coerce_indices_flattens_malformed_shapes():
    # nested lists are what crashed live ("unhashable type: 'list'") on a 213-tweet run
    assert _coerce_indices([[1, 2], [3]]) == [1, 2, 3]
    assert _coerce_indices([1, 2, 3]) == [1, 2, 3]
    assert _coerce_indices(["1", "2", "x"]) == [1, 2]
    assert _coerce_indices([{"index": 5}, {"i": 6}]) == [5, 6]
    assert _coerce_indices([1.0, 2.0]) == [1, 2]
    assert _coerce_indices(None) == []
    assert _coerce_indices("nonsense") == []
    assert _coerce_indices([True, 1]) == [1]   # bools aren't indices


def test_items_list_handles_wrapper_keys():
    assert _items_list({"items": [1]}) == [1]
    assert _items_list({"highlights": [2]}) == [2]
    assert _items_list({"themes": [3]}) == [3]
    assert _items_list([4]) == [4]
    assert _items_list({"other": 1}) == []


def test_highlights_maps_indices(make_ctx, monkeypatch):
    ctx = make_ctx(ollama_model="m", max_themes=2)
    sm = Summarizer(ctx)
    monkeypatch.setattr(
        sm, "_chat_json",
        lambda prompt, system=None: {"items": [{"index": 1, "line": "L1"}, {"index": 2, "line": "L2"}]},
    )
    st = DigestRun()
    st.filtered_tweets = [
        TweetItem("a", "high", "t", likes=50),
        TweetItem("b", "low", "t", likes=3),
    ]
    sm._summarize_highlights(st)
    assert [t.title for t in st.themes] == ["@high", "@low"]   # ranked by engagement
    assert st.themes[0].summary == "L1"


def test_per_account_groups_and_caps(make_ctx, monkeypatch):
    ctx = make_ctx(ollama_model="m", max_themes=1)
    sm = Summarizer(ctx)
    monkeypatch.setattr(sm, "_chat_json", lambda prompt, system=None: {"summary": "did stuff"})
    st = DigestRun()
    st.filtered_tweets = [
        TweetItem("1", "busy", "t1"), TweetItem("2", "busy", "t2"),
        TweetItem("3", "quiet", "t3"),
    ]
    sm._summarize_per_account(st)
    assert len(st.themes) == 1                 # capped to max_themes
    assert st.themes[0].title == "@busy"       # most active account first
    assert set(st.themes[0].tweet_ids) == {"1", "2"}
