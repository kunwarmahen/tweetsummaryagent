"""Embedding-based clusterer — an alternative to the LLM's one-prompt grouping.

Embeds each tweet with a local embedding model (e.g. nomic-embed-text), then groups them by
cosine similarity. The Summarizer later writes a title + narrative per group. Useful when the
single-prompt themes get muddy or the daily volume is large.

Pure-Python cosine (no numpy). For a personal daily volume this is plenty fast.
"""
from __future__ import annotations

import math

import ollama

from agents.base import Agent
from config import settings
from state import DigestRun, ThemeCluster


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class EmbeddingClusterer(Agent):
    name = "clusterer"

    def run(self, state: DigestRun) -> DigestRun:
        tweets = state.filtered_tweets
        if not tweets:
            return state

        model = self.ctx.app_settings.embedding_model
        threshold = self.ctx.app_settings.similarity_threshold
        max_themes = self.ctx.app_settings.max_themes

        self.log.info("Embedding %d tweets with %s", len(tweets), model)
        client = ollama.Client(host=settings.ollama_url)
        embeddings = []
        for t in tweets:
            resp = client.embeddings(model=model, prompt=(t.text or "")[:1000])
            embeddings.append(_normalize(resp["embedding"]))

        # Greedy agglomerative clustering against running (normalized) centroids.
        clusters: list[dict] = []
        for i, emb in enumerate(embeddings):
            best, best_sim = None, threshold
            for c in clusters:
                sim = _dot(emb, c["centroid"])
                if sim >= best_sim:
                    best, best_sim = c, sim
            if best is None:
                clusters.append({"members": [i], "sum": list(emb), "centroid": emb})
            else:
                best["members"].append(i)
                best["sum"] = [a + b for a, b in zip(best["sum"], emb)]
                best["centroid"] = _normalize(best["sum"])

        # Cap to max_themes: keep the largest, lump the rest into one overflow group.
        clusters.sort(key=lambda c: len(c["members"]), reverse=True)
        if len(clusters) > max_themes:
            kept = clusters[:max_themes - 1]
            overflow = [i for c in clusters[max_themes - 1:] for i in c["members"]]
            kept.append({"members": overflow})
            clusters = kept

        state.themes = [
            ThemeCluster(title="", summary="", tweet_ids=[tweets[i].tweet_id for i in c["members"]])
            for c in clusters if c["members"]
        ]
        self.log.info("Clustered into %d groups (threshold %.2f)", len(state.themes), threshold)
        return state
