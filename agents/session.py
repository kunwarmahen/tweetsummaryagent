"""X session status: a static summary of the saved storage_state plus a live login check.

The live check launches the browser (sync Playwright), so callers on the asyncio loop must run
`check_guarded` in a worker thread. The latest result is cached so the UI can display it.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import settings

logger = logging.getLogger("session")

_lock = threading.Lock()
_last: dict = {"checked_at": None, "ok": None, "message": "Not checked yet."}


def summary() -> dict:
    """Static info about the saved session file (no browser launch)."""
    path = Path(settings.storage_state_path)
    if not (path.exists() and path.stat().st_size > 50):
        return {"present": False, "cookie_count": 0, "has_auth_token": False, "expires_at": None}
    try:
        data = json.loads(path.read_text())
        cookies = data.get("cookies", [])
    except Exception:
        return {"present": False, "cookie_count": 0, "has_auth_token": False, "expires_at": None}

    names = {c.get("name") for c in cookies}
    future = [c.get("expires") for c in cookies
              if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0]
    expires_at = None
    if future:
        soonest = min(future)
        expires_at = datetime.fromtimestamp(soonest, tz=timezone.utc).strftime("%Y-%m-%d")
    return {
        "present": True,
        "cookie_count": len(cookies),
        "has_auth_token": "auth_token" in names,
        "expires_at": expires_at,
    }


def last_status() -> dict:
    return dict(_last)


def is_checking() -> bool:
    return _lock.locked()


def _set(ok: bool, message: str) -> None:
    _last.update(ok=ok, message=message,
                 checked_at=datetime.now(timezone.utc).isoformat())


def check_session() -> tuple[bool, str]:
    """Launch the browser and verify the saved session is still logged in."""
    from playwright.sync_api import sync_playwright

    from agents import selectors
    from agents.browser import launch_context, session_exists

    if not session_exists():
        return False, "No session yet — import your X cookies."

    with sync_playwright() as p:
        browser, context = launch_context(p, headless=True)
        page = context.new_page()
        try:
            page.goto(f"{selectors.BASE}/home", wait_until="domcontentloaded")
            page.wait_for_selector(selectors.PROFILE_LINK, timeout=15_000)
            return True, "Session is active — you're logged in. ✓"
        except Exception:
            return False, "Session is not active (logged out or expired). Re-import your cookies."
        finally:
            context.close()
            browser.close()


def check_guarded() -> None:
    """Run a session check unless one (or a pipeline run) is already using the browser."""
    import pipeline

    if pipeline.is_running():
        _set(False, "A run is in progress; try the check again once it finishes.")
        return
    if not _lock.acquire(blocking=False):
        return
    try:
        ok, msg = check_session()
        _set(ok, msg)
        logger.info("Session check: %s", msg)
    except Exception as e:
        _set(False, f"Check failed: {e}")
        logger.exception("Session check failed")
    finally:
        _lock.release()
