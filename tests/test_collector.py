from agents.collector import Collector


def _raw(**over):
    raw = {
        "url": "https://x.com/alice/status/123",
        "userName": "Alice\n@alice",
        "datetime": "2026-06-16T10:00:00.000Z",
        "text": "hello",
        "like": "3 Likes",
        "rt": "1 repost",
        "replyTo": None,
    }
    raw.update(over)
    return raw


def test_to_item_flags_self_reply(make_ctx):
    c = Collector(make_ctx())
    item = c._to_item(_raw(replyTo="alice"), fallback_handle="alice")
    assert item.reply_to == "alice"
    assert item.is_self_reply is True


def test_to_item_reply_to_other_is_not_self(make_ctx):
    c = Collector(make_ctx())
    item = c._to_item(_raw(replyTo="bob"), fallback_handle="alice")
    assert item.reply_to == "bob"
    assert item.is_self_reply is False


def test_to_item_original_has_no_reply(make_ctx):
    c = Collector(make_ctx())
    item = c._to_item(_raw(replyTo=None), fallback_handle="alice")
    assert item.reply_to is None
    assert item.is_self_reply is False


def test_to_item_returns_none_without_status_url(make_ctx):
    c = Collector(make_ctx())
    assert c._to_item(_raw(url="https://x.com/alice"), fallback_handle="alice") is None


def test_max_for_uses_override_then_default(make_ctx):
    c = Collector(make_ctx(max_tweets_per_account=50))
    c._limits = {"alice": 10}            # as loaded from AccountSetting rows
    assert c._max_for("alice") == 10     # per-account override
    assert c._max_for("Alice") == 10     # case-insensitive
    assert c._max_for("bob") == 50       # falls back to global default
