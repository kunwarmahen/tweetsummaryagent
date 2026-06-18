"""Import an X session from a browser-exported cookie file — no keyring needed.

This is the pure-container alternative to `import-profile` (which decrypts Chrome's cookies via
the host keyring and can't run inside a container). The user exports their x.com cookies with a
browser extension and we convert that file into a Playwright `storage_state.json`.

Supported export formats:
  - Netscape `cookies.txt`  (e.g. "Get cookies.txt LOCALLY")
  - JSON array              (e.g. "Cookie-Editor", "EditThisCookie")

The exported cookies are already plaintext, so no decryption (and no keyring) is involved.
"""
from __future__ import annotations

import json
from pathlib import Path

from config import settings

_X_DOMAINS = ("x.com", "twitter.com")
# The session is useless without these — auth_token authenticates, ct0 is the CSRF token.
_REQUIRED = "auth_token"


def _is_x_domain(domain: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    return any(d == base or d.endswith("." + base) for base in _X_DOMAINS)


def _norm_same_site(value) -> str:
    s = str(value or "").lower()
    if s == "strict":
        return "Strict"
    if s in ("none", "no_restriction", "unspecified"):
        return "None"
    return "Lax"


def parse_netscape(text: str) -> list[dict]:
    """Parse a Netscape `cookies.txt` file (tab-separated, one cookie per line)."""
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        http_only = False
        if line.startswith("#HttpOnly_"):       # some exporters flag httpOnly via this prefix
            http_only = True
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expiry, name, value = parts[:7]
        out.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "expires": float(expiry) if expiry and expiry.lstrip("-").isdigit() else -1,
            "httpOnly": http_only,
            "secure": secure.strip().upper() == "TRUE",
            "sameSite": "Lax",
        })
    return out


def parse_json(text: str) -> list[dict]:
    """Parse a JSON cookie export (array, or an object with a `cookies` array)."""
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("cookies", [])
    if not isinstance(data, list):
        raise ValueError("Unrecognized JSON cookie format (expected an array of cookies).")
    out: list[dict] = []
    for c in data:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        exp = c.get("expirationDate", c.get("expires", c.get("expiry")))
        try:
            expires = float(exp) if exp not in (None, "", -1, "-1") else -1
        except (TypeError, ValueError):
            expires = -1
        out.append({
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path") or "/",
            "expires": expires,
            "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure")),
            "sameSite": _norm_same_site(c.get("sameSite")),
        })
    return out


def _detect(text: str) -> str:
    return "json" if text.lstrip()[:1] in "[{" else "netscape"


def import_cookies_text(text: str, fmt: str = "auto", path: str | None = None) -> int:
    """Convert exported cookie text into a Playwright storage_state. Returns cookie count.

    Keeps only x.com / twitter.com cookies. Raises ValueError if the file has none, or if the
    essential `auth_token` cookie is missing (i.e. the export wasn't from a logged-in session).
    """
    if not text.strip():
        raise ValueError("The cookie file is empty.")
    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        fmt = _detect(text)
    cookies = parse_json(text) if fmt == "json" else parse_netscape(text)

    x_cookies = [c for c in cookies if _is_x_domain(c["domain"]) and c.get("name")]
    if not x_cookies:
        raise ValueError("No x.com / twitter.com cookies found in the file.")
    names = {c["name"] for c in x_cookies}
    if _REQUIRED not in names:
        raise ValueError(
            f"No '{_REQUIRED}' cookie found — export your cookies while logged into x.com."
        )

    out = Path(path or settings.storage_state_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"cookies": x_cookies, "origins": []}, indent=2))
    return len(x_cookies)


def import_cookies_file(file_path: str, fmt: str = "auto", path: str | None = None) -> int:
    return import_cookies_text(Path(file_path).read_text(), fmt=fmt, path=path)
