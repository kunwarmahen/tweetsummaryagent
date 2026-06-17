"""Session helpers: capture the logged-in handle, and an optional manual login fallback.

Primary auth path is `import-profile` (decrypts cookies from your real Chrome). `login`
remains as a manual fallback that opens a hardened browser for you to sign in by hand.
"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from agents import selectors
from agents.browser import launch_context
from config import settings

LOGIN_TIMEOUT_MS = 300_000   # 5 minutes for the human to log in
META_PATH = "auth/session_meta.json"


def _detect_handle(page) -> str | None:
    """Read the logged-in user's handle from the profile nav link (href = /handle)."""
    link = page.query_selector(selectors.PROFILE_LINK)
    if not link:
        return None
    href = link.get_attribute("href") or ""
    return href.strip("/").split("/")[0] or None


def _save_handle(handle: str | None) -> None:
    Path(META_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(META_PATH).write_text(json.dumps({"handle": handle}, indent=2))


def capture_handle() -> str | None:
    """Open the saved session headlessly, read and save the handle."""
    handle = None
    with sync_playwright() as p:
        browser, context = launch_context(p, headless=True)
        page = context.new_page()
        page.goto(f"{selectors.BASE}/home", wait_until="domcontentloaded")
        try:
            page.wait_for_selector(selectors.PROFILE_LINK, timeout=20_000)
            handle = _detect_handle(page)
        except Exception:
            pass
        context.close()
        browser.close()
    if handle:
        _save_handle(handle)
    return handle


def run_login() -> int:
    """Manual fallback: open a browser and let the user sign in by hand."""
    with sync_playwright() as p:
        browser, context = launch_context(p, headless=False)
        page = context.new_page()

        print("Opening X. Please log in (including any 2FA) in the browser window…")
        print("Tip: if you hit 'temporarily limited', close it, wait, and rerun later.")
        page.goto(f"{selectors.BASE}/login")

        try:
            page.wait_for_selector(selectors.PROFILE_LINK, timeout=LOGIN_TIMEOUT_MS)
        except Exception:
            print("Timed out / login not detected. Nothing saved.")
            context.close()
            browser.close()
            return 1

        handle = _detect_handle(page)
        context.storage_state(path=settings.storage_state_path)
        _save_handle(handle)
        context.close()
        browser.close()

    print(f"Session saved (handle: @{handle})" if handle else "Session saved.")
    return 0


def load_handle() -> str | None:
    try:
        return json.loads(Path(META_PATH).read_text()).get("handle")
    except (FileNotFoundError, json.JSONDecodeError):
        return None
