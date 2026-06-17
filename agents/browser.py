"""Browser launching + session import.

Auth strategy on Linux: rather than copy Chrome's profile (whose cookies are encrypted
with a desktop-keyring key the automated browser can't reproduce), we decrypt the X cookies
in Python via `browser_cookie3` and hand Playwright a plaintext `storage_state`. The launched
browser then needs no keyring access at all.

Launches are hardened against X's bot detection (real Chrome if available, automation flags
dropped, fingerprint patched).
"""
import json
import os
from pathlib import Path

from config import settings

REAL_CHROME_DIR = Path.home() / ".config" / "google-chrome"

_REAL_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

def _launch_args() -> list[str]:
    args = ["--disable-blink-features=AutomationControlled", "--no-first-run"]
    # Chromium refuses to run as root (e.g. inside a container) without these.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    return args


def launch_context(p, headless: bool):
    """Return (browser, context). Loads the saved storage_state if present."""
    args = _launch_args()
    try:
        browser = p.chromium.launch(channel="chrome", headless=headless,
                                    args=args, ignore_default_args=["--enable-automation"])
        print("[browser] using real Google Chrome (channel=chrome)")
    except Exception as e:
        print(f"[browser] Chrome unavailable ({e}); falling back to bundled Chromium")
        browser = p.chromium.launch(headless=headless,
                                    args=args, ignore_default_args=["--enable-automation"])

    ctx_kwargs = dict(
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        user_agent=_REAL_UA,
    )
    if session_exists():
        ctx_kwargs["storage_state"] = settings.storage_state_path
    context = browser.new_context(**ctx_kwargs)
    context.add_init_script(_STEALTH_JS)
    return browser, context


def session_exists() -> bool:
    """True if a usable saved session (storage_state) exists."""
    path = Path(settings.storage_state_path)
    return path.exists() and path.stat().st_size > 50


def import_chrome_cookies(cookie_file: str | None = None) -> int:
    """Decrypt X/Twitter cookies from the real Chrome profile into a Playwright
    storage_state. Returns the number of cookies imported."""
    import browser_cookie3 as bc3

    cf = cookie_file or str(REAL_CHROME_DIR / "Default" / "Cookies")
    jar = bc3.chrome(cookie_file=cf)

    cookies = []
    for c in jar:
        if not any(d in (c.domain or "") for d in ("x.com", "twitter.com")):
            continue
        rest = getattr(c, "_rest", {}) or {}
        cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path or "/",
            "expires": float(c.expires) if c.expires else -1,
            "httpOnly": bool(rest.get("HTTPOnly") or rest.get("HttpOnly")),
            "secure": bool(c.secure),
            "sameSite": "Lax",
        })

    if not cookies:
        raise RuntimeError(
            "No x.com/twitter.com cookies found. Log into X in Google Chrome first, "
            "then rerun import-profile."
        )

    path = Path(settings.storage_state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies, "origins": []}, indent=2))
    return len(cookies)
