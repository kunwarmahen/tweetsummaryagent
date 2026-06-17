"""Telegram delivery — send the digest to a chat via the Bot API.

Telegram caps messages at 4096 chars and supports only a small HTML subset (no CSS), so
we format a compact themed message and split it across messages as needed. Uses stdlib
urllib (no extra dependency).
"""
from __future__ import annotations

import html
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/{method}"
_MAX_LEN = 3800   # stay safely under Telegram's 4096 limit


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def format_messages(themes: list[dict], date_str: str, total_tweets: int) -> list[str]:
    """Render themes into one or more Telegram-HTML message strings.

    Each theme dict has: title, summary, tweets (list of TweetItem).
    """
    header = f"📰 <b>Daily X Digest</b> — {_esc(date_str)}\n{total_tweets} tweets · {len(themes)} themes"
    if not themes:
        return [header + "\n\n<i>No new tweets in the selected window.</i>"]

    blocks: list[str] = []
    for th in themes:
        lines = [f"\n<b>{_esc(th['title'])}</b>", _esc(th["summary"])]
        for t in th["tweets"][:5]:
            handle = _esc(t.handle)
            link = f'<a href="{_esc(t.url)}">@{handle}</a>' if t.url else f"@{handle}"
            lines.append(f"• {link}")
        blocks.append("\n".join(lines))

    # Pack blocks into messages under the length limit.
    messages: list[str] = []
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > _MAX_LEN:
            messages.append(current)
            current = block.lstrip("\n")
        else:
            current += "\n" + block
    if current:
        messages.append(current)
    return messages


def send_message(token: str, chat_id: str, text: str) -> bool:
    url = API.format(token=token, method="sendMessage")
    payload = json.dumps({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        logger.error("Telegram send failed: %s %s", e.code, e.read().decode(errors="ignore")[:200])
        return False
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


def send_digest(token: str, chat_id: str, themes: list[dict], date_str: str, total_tweets: int) -> bool:
    messages = format_messages(themes, date_str, total_tweets)
    ok = True
    for msg in messages:
        if not send_message(token, chat_id, msg):
            ok = False
    return ok


def get_updates(token: str) -> list[dict]:
    """Fetch recent updates — used to discover the chat id during setup."""
    url = API.format(token=token, method="getUpdates")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read()).get("result", [])
    except Exception as e:
        logger.error("getUpdates failed: %s", e)
        return []
