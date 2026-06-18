"""Tests for the browser-export cookie importer (agents/cookies.py)."""
import json

import pytest

from agents import cookies


def _netscape(rows: list[str]) -> str:
    header = "# Netscape HTTP Cookie File\n"
    return header + "\n".join(rows) + "\n"


def test_parse_netscape_basic():
    text = _netscape([
        ".x.com\tTRUE\t/\tTRUE\t1893456000\tauth_token\tABC123",
        "#HttpOnly_.x.com\tTRUE\t/\tTRUE\t1893456000\tct0\tCSRF",
    ])
    out = cookies.parse_netscape(text)
    by = {c["name"]: c for c in out}
    assert by["auth_token"]["value"] == "ABC123"
    assert by["auth_token"]["secure"] is True
    assert by["ct0"]["httpOnly"] is True            # #HttpOnly_ prefix honored


def test_parse_json_array_and_object():
    arr = json.dumps([
        {"name": "auth_token", "value": "X", "domain": ".x.com", "path": "/",
         "expirationDate": 1893456000, "httpOnly": True, "secure": True, "sameSite": "no_restriction"},
    ])
    (c,) = cookies.parse_json(arr)
    assert c["name"] == "auth_token" and c["expires"] == 1893456000
    assert c["sameSite"] == "None"                  # no_restriction -> None

    obj = json.dumps({"cookies": json.loads(arr)})
    assert cookies.parse_json(obj)[0]["name"] == "auth_token"


def test_import_writes_storage_state_and_filters_domains(tmp_path):
    out = tmp_path / "storage_state.json"
    text = _netscape([
        ".x.com\tTRUE\t/\tTRUE\t1893456000\tauth_token\tABC",
        ".google.com\tTRUE\t/\tTRUE\t1893456000\tSID\tnope",   # other domain -> dropped
    ])
    n = cookies.import_cookies_text(text, path=str(out))
    assert n == 1
    saved = json.loads(out.read_text())
    assert [c["name"] for c in saved["cookies"]] == ["auth_token"]
    assert saved["origins"] == []


def test_import_requires_auth_token(tmp_path):
    text = _netscape([".x.com\tTRUE\t/\tTRUE\t1893456000\tct0\tonly"])
    with pytest.raises(ValueError, match="auth_token"):
        cookies.import_cookies_text(text, path=str(tmp_path / "s.json"))


def test_import_rejects_no_x_cookies(tmp_path):
    text = _netscape([".google.com\tTRUE\t/\tTRUE\t1893456000\tSID\tx"])
    with pytest.raises(ValueError, match="No x.com"):
        cookies.import_cookies_text(text, path=str(tmp_path / "s.json"))


def test_auto_detect_json_vs_netscape(tmp_path):
    js = json.dumps([{"name": "auth_token", "value": "Z", "domain": "x.com"}])
    assert cookies.import_cookies_text(js, path=str(tmp_path / "a.json")) == 1   # detected JSON
