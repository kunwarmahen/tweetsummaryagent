"""Summarizer agent — local LLM (Ollama) groups tweets into themes and writes a digest.

For a personal daily volume (tens to low-hundreds of tweets), we do clustering + summary in a
single prompt: the model reads the day's tweets and returns themed sections. Tweets are
referenced by short index so the model doesn't have to echo long IDs; we map them back.
"""
from __future__ import annotations

import math

import ollama

from agents.base import Agent
from agents.util import extract_json
from config import settings
from db.models import DigestStyle
from state import DigestRun, ThemeCluster, TweetItem

_MAX_TEXT = 400   # truncate each tweet to keep the prompt tight

# Ollama defaults to a small context window (~4k tokens) and *silently truncates* anything
# longer, so a whole-day prompt (100+ tweets) gets clipped and the model summarizes only a
# fragment — or returns nothing (the "0 themes" failure). We size num_ctx to the prompt instead.
_REPLY_TOKENS = 1500          # headroom for the model's JSON reply
_CHUNK_SIZE = 60              # tweets per call in the chunked fallback


def _ctx_window(prompt: str) -> int:
    """Pick an Ollama num_ctx big enough for prompt+reply (~4 chars/token), snapped + capped."""
    est = len(prompt) // 4 + _REPLY_TOKENS
    for window in (4096, 8192, 16384, 32768):
        if est <= window:
            return window
    return 32768

_SYSTEM = (
    "You are an editor producing a concise daily briefing from tweets. "
    "Group related tweets into a few coherent themes. For each theme write a short, "
    "neutral narrative (2-4 sentences) summarizing what was said. Do not invent facts."
)

_NEUTRAL = "You summarize tweets neutrally and concisely. Respond only with the requested JSON. Do not invent facts."


def _coerce_indices(raw) -> list[int]:
    """Flatten an LLM 'indices' value into a clean list of ints.

    Models sometimes return nested lists ([[1,2],[3]]), strings ("1"), or dicts
    ({"index": 1}). We walk the structure and keep anything int-coercible, so a
    malformed shape never crashes the membership test (unhashable list keys etc.).
    """
    out: list[int] = []

    def walk(v):
        if isinstance(v, bool):
            return
        if isinstance(v, int):
            out.append(v)
        elif isinstance(v, float):
            out.append(int(v))
        elif isinstance(v, str):
            s = v.strip()
            if s.lstrip("-").isdigit():
                out.append(int(s))
        elif isinstance(v, dict):
            for key in ("index", "idx", "i"):
                if key in v:
                    walk(v[key])
                    break
        elif isinstance(v, (list, tuple)):
            for item in v:
                walk(item)

    walk(raw)
    return out


def _items_list(data) -> list:
    """Pull a list of items from an LLM JSON response regardless of the wrapper key."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "highlights", "themes", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _build_prompt(tweets: list[TweetItem], topics: list[str], max_themes: int) -> str:
    lines = []
    for i, t in enumerate(tweets, 1):
        text = (t.text or "").replace("\n", " ").strip()[:_MAX_TEXT]
        lines.append(f"[{i}] @{t.handle}: {text}")
    listing = "\n".join(lines)

    topic_hint = ""
    if topics:
        topic_hint = (
            f"\nThe reader is especially interested in: {', '.join(topics)}. "
            "Prioritize and, if helpful, lead with those themes.\n"
        )

    return (
        f"Here are today's tweets, each prefixed with an index:\n\n{listing}\n"
        f"{topic_hint}\n"
        f"Group them into at most {max_themes} themes. "
        "Respond with JSON of the form:\n"
        '{"themes": [{"title": "...", "summary": "...", "indices": [1, 5, 9]}]}\n'
        "Use the indices shown above. Every theme must reference at least one index. "
        "Order themes from most to least significant."
    )


class Summarizer(Agent):
    name = "summarizer"

    def run(self, state: DigestRun) -> DigestRun:
        tweets = state.filtered_tweets
        if not tweets:
            self.log.warning("No tweets to summarize")
            state.themes = []
            return state

        style = self.ctx.app_settings.digest_style
        if style == DigestStyle.per_account:
            return self._summarize_per_account(state)
        if style == DigestStyle.highlights:
            return self._summarize_highlights(state)
        # themed: if a clusterer already grouped tweets (embedding mode), summarize each group.
        if state.themes:
            return self._summarize_clusters(state)
        return self._summarize_single_prompt(state)

    def _chat_json(self, prompt: str, system: str = _SYSTEM) -> dict | list | None:
        client = ollama.Client(host=settings.ollama_url)
        resp = client.chat(
            model=self.ctx.app_settings.ollama_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            format="json", options={"temperature": 0.3, "num_ctx": _ctx_window(prompt)},
        )
        try:
            return extract_json(resp["message"]["content"])
        except ValueError:
            return None

    def _summarize_per_account(self, state: DigestRun) -> DigestRun:
        """One summary per account (most active first, capped to max_themes accounts)."""
        by_account: dict[str, list[TweetItem]] = {}
        for t in state.filtered_tweets:
            by_account.setdefault(t.handle, []).append(t)
        ranked = sorted(by_account.items(), key=lambda kv: len(kv[1]), reverse=True)
        ranked = ranked[:self.ctx.app_settings.max_themes]

        self.log.info("Per-account digest for %d accounts", len(ranked))
        out: list[ThemeCluster] = []
        for handle, tws in ranked:
            listing = "\n".join(f"- {(t.text or '').replace(chr(10), ' ')[:_MAX_TEXT]}" for t in tws)
            prompt = (
                f"These are today's tweets from @{handle}:\n\n{listing}\n\n"
                'Respond with JSON {"summary": "..."} — a 2-4 sentence neutral summary of what '
                f"@{handle} posted about. Do not invent facts."
            )
            data = self._chat_json(prompt, system=_NEUTRAL)
            summary = (data.get("summary") if isinstance(data, dict) else "") or ""
            summary = summary.strip()
            if not summary:
                continue
            out.append(ThemeCluster(title=f"@{handle}", summary=summary,
                                    tweet_ids=[t.tweet_id for t in tws]))
        state.themes = out
        self.log.info("Produced %d per-account sections", len(out))
        return state

    def _summarize_highlights(self, state: DigestRun) -> DigestRun:
        """Top tweets by engagement, each with a one-line summary."""
        top = sorted(state.filtered_tweets, key=lambda t: t.likes + t.retweets, reverse=True)
        top = top[:self.ctx.app_settings.max_themes]
        listing = "\n".join(
            f"[{i}] @{t.handle}: {(t.text or '').replace(chr(10), ' ')[:_MAX_TEXT]}"
            for i, t in enumerate(top, 1)
        )
        system = ("You write one-sentence highlights for individual tweets. "
                  "Respond only with the requested JSON. Do not invent facts.")
        prompt = (
            f"Here are today's top tweets:\n\n{listing}\n\n"
            "For each, write a single-sentence highlight. Respond with JSON of the form "
            '{"items": [{"index": 1, "line": "..."}]}. Use the indices shown.'
        )
        data = self._chat_json(prompt, system=system)
        items = _items_list(data)
        index_to_tweet = {i: t for i, t in enumerate(top, 1)}

        out: list[ThemeCluster] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                idx = int(it.get("index"))
            except (TypeError, ValueError):
                continue
            t = index_to_tweet.get(idx)
            line = (it.get("line") or it.get("summary") or it.get("title") or "").strip()
            if not t or not line:
                continue
            out.append(ThemeCluster(title=f"@{t.handle}", summary=line, tweet_ids=[t.tweet_id]))
        state.themes = out
        self.log.info("Produced %d highlights", len(out))
        return state

    def _summarize_clusters(self, state: DigestRun) -> DigestRun:
        by_id = {t.tweet_id: t for t in state.filtered_tweets}
        model = self.ctx.app_settings.ollama_model
        client = ollama.Client(host=settings.ollama_url)
        self.log.info("Summarizing %d pre-clustered groups with %s", len(state.themes), model)

        out: list[ThemeCluster] = []
        for cl in state.themes:
            cl_tweets = [by_id[i] for i in cl.tweet_ids if i in by_id]
            if not cl_tweets:
                continue
            listing = "\n".join(f"@{t.handle}: {(t.text or '').replace(chr(10), ' ')[:_MAX_TEXT]}"
                                for t in cl_tweets)
            prompt = (
                f"These tweets belong to one theme:\n\n{listing}\n\n"
                'Respond with JSON {"title": "...", "summary": "..."} — a short title and a '
                "2-4 sentence neutral summary of what was said. Do not invent facts."
            )
            resp = client.chat(
                model=model,
                messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
                format="json", options={"temperature": 0.3, "num_ctx": _ctx_window(prompt)},
            )
            try:
                data = extract_json(resp["message"]["content"])
            except ValueError:
                self.log.warning("Could not parse cluster summary; skipping group")
                continue
            title = (data.get("title") or "Untitled").strip()
            summary = (data.get("summary") or "").strip()
            if not summary:
                continue
            out.append(ThemeCluster(title=title, summary=summary,
                                    tweet_ids=[t.tweet_id for t in cl_tweets]))

        state.themes = out
        self.log.info("Produced %d themes", len(out))
        return state

    def _themes_from_response(self, data, tweets: list[TweetItem]) -> list[ThemeCluster]:
        """Map an LLM themes response (indices into `tweets`) back to ThemeClusters."""
        themes_raw = data.get("themes", data) if isinstance(data, dict) else data
        index_to_id = {i: t.tweet_id for i, t in enumerate(tweets, 1)}

        out: list[ThemeCluster] = []
        for th in themes_raw or []:
            if not isinstance(th, dict):
                continue
            ids = [index_to_id[i] for i in _coerce_indices(th.get("indices")) if i in index_to_id]
            title = (th.get("title") or "Untitled").strip()
            summary = (th.get("summary") or "").strip()
            if not ids or not summary:
                continue
            out.append(ThemeCluster(title=title, summary=summary, tweet_ids=ids))
        return out

    def _summarize_single_prompt(self, state: DigestRun) -> DigestRun:
        tweets = state.filtered_tweets
        max_themes = self.ctx.app_settings.max_themes
        prompt = _build_prompt(tweets, self._topics(), max_themes)

        self.log.info("Summarizing %d tweets with %s", len(tweets), self.ctx.app_settings.ollama_model)
        data = self._chat_json(prompt)
        themes = self._themes_from_response(data, tweets) if data is not None else []

        # A small local model can collapse a whole-day prompt to 0/1 themes. Rather than ship a
        # gutted digest, retry in smaller batches that stay coherent.
        if not themes:
            self.log.warning("Single-prompt summary produced 0 themes from %d tweets; "
                             "falling back to chunked summarization", len(tweets))
            return self._summarize_chunked(state)

        state.themes = themes[:max_themes]
        self.log.info("Produced %d themes", len(state.themes))
        return state

    def _summarize_chunked(self, state: DigestRun) -> DigestRun:
        """Fallback: summarize the day in batches, then keep the strongest themes overall.

        Each batch is small enough to stay coherent for a local model; we split the theme budget
        across batches, collect all themes, and rank them by member engagement to keep the top
        `max_themes`. Guarantees non-empty output whenever the model returns anything usable.
        """
        tweets = state.filtered_tweets
        max_themes = self.ctx.app_settings.max_themes
        topics = self._topics()
        chunks = [tweets[i:i + _CHUNK_SIZE] for i in range(0, len(tweets), _CHUNK_SIZE)]
        per_chunk = max(2, math.ceil(max_themes / len(chunks)) + 1)
        self.log.info("Chunked summary: %d tweets in %d chunk(s), up to %d themes each",
                      len(tweets), len(chunks), per_chunk)

        collected: list[ThemeCluster] = []
        for chunk in chunks:
            data = self._chat_json(_build_prompt(chunk, topics, per_chunk))
            if data is not None:
                collected.extend(self._themes_from_response(data, chunk))

        by_id = {t.tweet_id: t for t in tweets}
        collected.sort(
            key=lambda th: sum(by_id[i].likes + by_id[i].retweets for i in th.tweet_ids if i in by_id),
            reverse=True,
        )
        state.themes = collected[:max_themes]
        self.log.info("Produced %d themes (chunked)", len(state.themes))
        return state

    def _topics(self) -> list[str]:
        # A per-run override (set by a replay) takes precedence over the global Topic table.
        override = getattr(self.ctx.app_settings, "topics_override", None)
        if override is not None:
            return override

        from sqlmodel import select

        from db.models import Topic
        from db.session import get_session

        with get_session() as session:
            return [t.name for t in session.exec(select(Topic)).all()]
