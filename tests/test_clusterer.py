import math

import agents.clusterer as cl
from agents.clusterer import EmbeddingClusterer, _dot, _normalize
from state import DigestRun, TweetItem


def test_normalize_unit_length():
    v = _normalize([3.0, 4.0])
    assert abs(math.hypot(*v) - 1.0) < 1e-9


def test_dot_orthogonal_and_parallel():
    assert abs(_dot([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(_dot([1, 0], [0, 1])) < 1e-9


def test_clusters_similar_vectors(make_ctx, monkeypatch):
    vecs = {"a1": [1.0, 0.0], "a2": [0.99, 0.01], "b1": [0.0, 1.0], "b2": [0.01, 0.99]}

    class FakeClient:
        def __init__(self, host=None):
            pass

        def embeddings(self, model, prompt):
            return {"embedding": vecs[prompt]}

    monkeypatch.setattr(cl.ollama, "Client", FakeClient)
    ctx = make_ctx(embedding_model="e", similarity_threshold=0.8, max_themes=8)
    st = DigestRun()
    st.filtered_tweets = [TweetItem(k, "x", k) for k in vecs]   # text == key

    EmbeddingClusterer(ctx).run(st)
    assert len(st.themes) == 2
    assert sorted(len(t.tweet_ids) for t in st.themes) == [2, 2]
