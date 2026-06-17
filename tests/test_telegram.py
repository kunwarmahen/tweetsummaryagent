from agents import telegram
from state import TweetItem


def test_empty_digest():
    msgs = telegram.format_messages([], "Monday", 0)
    assert len(msgs) == 1
    assert "No new tweets" in msgs[0]


def test_basic_formatting():
    themes = [{"title": "Theme", "summary": "Summary.",
               "tweets": [TweetItem("1", "alice", "hi", url="https://x.com/alice/status/1")]}]
    msgs = telegram.format_messages(themes, "Monday", 5)
    assert len(msgs) == 1
    assert "<b>Theme</b>" in msgs[0]
    assert '<a href="https://x.com/alice/status/1">@alice</a>' in msgs[0]


def test_html_escaping():
    themes = [{"title": "A & B <c>", "summary": "x > y", "tweets": []}]
    msgs = telegram.format_messages(themes, "Monday", 1)
    assert "&amp;" in msgs[0]
    assert "&lt;c&gt;" in msgs[0]


def test_chunking_stays_under_limit():
    themes = [{"title": f"T{i}", "summary": "x" * 500, "tweets": []} for i in range(20)]
    msgs = telegram.format_messages(themes, "Monday", 20)
    assert len(msgs) > 1
    assert all(len(m) <= 4096 for m in msgs)
