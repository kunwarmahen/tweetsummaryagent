"""HTTP routes for the config UI."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import func, select

import pipeline
from db.models import (AccountSetting, AppSettings, ClusteringMethod, DigestRun,
                       DigestStyle, ExcludedAccount, RawTweet, ThreadMode, Topic, Tweet)
from db.session import get_session, get_settings

_TEMPLATES = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES))
router = APIRouter()

is_running = pipeline.is_running


def _valid_tz(name: str) -> str:
    """Keep only a real IANA timezone; otherwise leave the schedule on UTC."""
    name = (name or "").strip()
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


# ---------------------------------------------------------------- Dashboard
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    from agents import analytics

    from db.models import RunStatus
    with get_session() as s:
        cfg = get_settings(s)
        draft = s.exec(
            select(DigestRun).where(DigestRun.status == RunStatus.draft)
            .order_by(DigestRun.id.desc())
        ).first()
        last_run = s.exec(
            select(DigestRun).where(DigestRun.status != RunStatus.draft)
            .order_by(DigestRun.id.desc())
        ).first()
        run_count = s.exec(select(func.count(DigestRun.id))).one()
        tweet_count = s.exec(select(func.count(Tweet.id))).one()
        raw_count = s.exec(select(func.count(RawTweet.id))).one()
        excluded_count = s.exec(select(func.count(ExcludedAccount.id))).one()
        spark = analytics.daily_series(s, days=14)
        trending = analytics.trending_themes(s, days=7, limit=3)
    sparkline = {"labels": [d.date for d in spark], "tweets": [d.tweet_count for d in spark]}
    return templates.TemplateResponse(request, "index.html", {
        "cfg": cfg, "last_run": last_run, "draft": draft,
        "run_count": run_count, "tweet_count": tweet_count, "raw_count": raw_count,
        "excluded_count": excluded_count, "running": is_running(),
        "sparkline": sparkline, "trending": trending,
    })


@router.post("/run-now")
def run_now(background_tasks: BackgroundTasks):
    if not is_running():
        background_tasks.add_task(pipeline.run_guarded)
    return RedirectResponse("/runs", status_code=303)


@router.post("/collect-now")
def collect_now(background_tasks: BackgroundTasks):
    """Phase 1: scrape new tweets into the archive."""
    if not is_running():
        background_tasks.add_task(pipeline.collect_guarded)
    return RedirectResponse("/", status_code=303)


@router.post("/refresh-now")
def refresh_now(background_tasks: BackgroundTasks):
    """Phase 2: rebuild the live draft digest (no delivery)."""
    if not is_running():
        background_tasks.add_task(pipeline.refresh_draft_guarded)
    return RedirectResponse("/", status_code=303)


@router.post("/deliver-now")
def deliver_now(background_tasks: BackgroundTasks):
    """Phase 3: finalize + send the day's digest."""
    if not is_running():
        background_tasks.add_task(pipeline.deliver_guarded)
    return RedirectResponse("/runs", status_code=303)


# ---------------------------------------------------------------- Session / auth
@router.get("/session", response_class=HTMLResponse)
def session_page(request: Request, imported: int | None = None, error: str | None = None):
    from agents import session as sess

    return templates.TemplateResponse(request, "session.html", {
        "summary": sess.summary(), "status": sess.last_status(),
        "checking": sess.is_checking(), "running": is_running(),
        "imported": imported, "error": error,
    })


@router.post("/session/import-cookies")
async def import_cookies(file: UploadFile = File(...), fmt: str = Form("auto")):
    """Import an X session from an uploaded browser-cookie export (no keyring needed)."""
    from agents import cookies

    raw = await file.read()
    try:
        text = raw.decode("utf-8", "replace")
        n = cookies.import_cookies_text(text, fmt=fmt)
    except (ValueError, OSError) as e:
        return RedirectResponse(f"/session?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/session?imported={n}", status_code=303)


@router.post("/session/test")
def session_test(background_tasks: BackgroundTasks):
    from agents import session as sess

    if not sess.is_checking() and not is_running():
        background_tasks.add_task(sess.check_guarded)
    return RedirectResponse("/session", status_code=303)


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: int, background_tasks: BackgroundTasks):
    if not is_running():
        background_tasks.add_task(pipeline.resume_guarded, run_id)
    return RedirectResponse("/runs", status_code=303)


@router.post("/runs/{run_id}/delete")
def delete_run(run_id: int):
    try:
        pipeline.delete_run(run_id)
    except RuntimeError:
        pass  # run in progress — ignore and refresh
    return RedirectResponse("/runs", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request):
    from state import load_latest_snapshot

    with get_session() as s:
        row = s.get(DigestRun, run_id)
        topics = [t.name for t in s.exec(select(Topic).order_by(Topic.name)).all()]
        accounts = s.exec(
            select(RawTweet.handle, func.count(RawTweet.id))
            .where(RawTweet.run_id == run_id)
            .group_by(RawTweet.handle).order_by(func.count(RawTweet.id).desc())
        ).all()
    if row is None:
        return HTMLResponse("<p>Run not found.</p>", status_code=404)

    # Theme titles/summaries from the furthest snapshot (if still on disk).
    snap = load_latest_snapshot(pipeline.settings.data_dir, run_id)
    themes = snap[0].themes if snap else []

    return templates.TemplateResponse(request, "run_detail.html", {
        "run": row, "themes": themes, "accounts": accounts,
        "replayable": pipeline.is_replayable(run_id), "running": is_running(),
        "styles": [s.value for s in DigestStyle],
        "methods": [m.value for m in ClusteringMethod],
        "current_topics": ", ".join(topics),
    })


@router.post("/runs/{run_id}/rerun")
def rerun_run(run_id: int, background_tasks: BackgroundTasks,
              digest_style: str = Form(...), clustering_method: str = Form(...),
              ollama_model: str = Form(...), similarity_threshold: float = Form(0.55),
              topics: str = Form(""), deliver: str = Form(None)):
    overrides = {
        "digest_style": DigestStyle(digest_style),
        "clustering_method": ClusteringMethod(clustering_method),
        "ollama_model": ollama_model.strip(),
        "similarity_threshold": max(0.0, min(1.0, similarity_threshold)),
        "topics_override": [t.strip() for t in topics.split(",") if t.strip()],
    }
    if not is_running():
        background_tasks.add_task(pipeline.replay_guarded, run_id, overrides, deliver is not None)
    return RedirectResponse("/runs", status_code=303)


# ---------------------------------------------------------------- Trends
@router.get("/trends", response_class=HTMLResponse)
def trends(request: Request):
    from agents import analytics

    with get_session() as s:
        series = analytics.daily_series(s, days=30)
        leaders = analytics.account_leaderboard(s, days=7)
        tops = analytics.top_tweets(s, days=7)
        trending = analytics.trending_themes(s, days=7)
        meta = analytics.latest_meta_digest(s)
    chart = {
        "labels": [d.date for d in series],
        "tweets": [d.tweet_count for d in series],
        "engagement": [d.engagement for d in series],
        "accounts": [d.account_count for d in series],
    }
    return templates.TemplateResponse(request, "trends.html", {
        "chart": chart, "leaders": leaders, "tops": tops, "trending": trending,
        "meta": meta, "meta_running": analytics.is_meta_running(),
        "has_data": bool(series), "running": is_running(),
    })


@router.post("/trends/meta-digest")
def regenerate_meta_digest(background_tasks: BackgroundTasks):
    from agents import analytics
    if not analytics.is_meta_running():
        background_tasks.add_task(analytics.generate_meta_digest_guarded, 7)
    return RedirectResponse("/trends", status_code=303)


@router.post("/trends/rebuild")
def rebuild_trends(background_tasks: BackgroundTasks, themes: str = Form(None)):
    """Rebuild materialized trend tables from the archive (daily_stats, and themes unless skipped)."""
    from agents import analytics

    def _rebuild(with_themes: bool):
        try:
            analytics.recompute_daily_stats()
            if with_themes:
                analytics.rebuild_theme_history()
        except Exception:
            pass  # best-effort; needs Ollama for theme embeddings

    if not is_running():
        background_tasks.add_task(_rebuild, themes is not None)
    return RedirectResponse("/trends", status_code=303)


# ---------------------------------------------------------------- Maintenance
@router.post("/maintenance/archive-backfill")
def archive_backfill(background_tasks: BackgroundTasks):
    background_tasks.add_task(pipeline.backfill_raw_archive)
    return RedirectResponse("/settings", status_code=303)


@router.post("/maintenance/reset-runs")
def reset_runs_route():
    """Wipe ALL run data (keeps settings/accounts/topics). Backs up the DB first."""
    try:
        pipeline.reset_runs(backup=True)
    except RuntimeError:
        pass  # a run is in progress — ignore and refresh
    return RedirectResponse("/runs", status_code=303)


@router.post("/maintenance/telegram/chatid")
def telegram_chatid():
    from agents import telegram
    from config import settings as cfg

    if not cfg.telegram_bot_token:
        return RedirectResponse(f"/settings?tg={quote('Set TELEGRAM_BOT_TOKEN in .env first.')}",
                                status_code=303)
    seen = {}
    try:
        for u in telegram.get_updates(cfg.telegram_bot_token):
            chat = (u.get("message") or u.get("channel_post") or {}).get("chat", {})
            if chat.get("id") is not None:
                seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name", "")
    except Exception as e:
        return RedirectResponse(f"/settings?tg={quote(f'Lookup failed: {e}')}", status_code=303)
    if not seen:
        msg = "No chats found — message your bot in Telegram, then retry."
    else:
        msg = "Chat id(s): " + "; ".join(f"{cid} ({name})" for cid, name in seen.items())
    return RedirectResponse(f"/settings?tg={quote(msg)}", status_code=303)


@router.post("/maintenance/telegram/test")
def telegram_test():
    from agents import telegram
    from config import settings as cfg

    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        msg = "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first."
    else:
        ok = telegram.send_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                   "✅ <b>Twitter Summary Agent</b> Telegram test message.")
        msg = "Test message sent ✓" if ok else "Send failed — check token/chat id (see logs)."
    return RedirectResponse(f"/settings?tg={quote(msg)}", status_code=303)


# ---------------------------------------------------------------- Accounts
@router.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request):
    with get_session() as s:
        cfg = get_settings(s)
        default_max = cfg.max_tweets_per_account
        excluded = s.exec(select(ExcludedAccount).order_by(ExcludedAccount.handle)).all()
        excluded_handles = {e.handle.lower() for e in excluded}
        limits = s.exec(select(AccountSetting).order_by(AccountSetting.handle)).all()
        seen = s.exec(
            select(Tweet.handle, func.count(Tweet.id))
            .group_by(Tweet.handle).order_by(func.count(Tweet.id).desc())
        ).all()
    seen = [(h, n) for (h, n) in seen if h.lower() not in excluded_handles]
    important = [l for l in limits if l.important]
    important_handles = {l.handle.lower() for l in important}
    from agents.priority import PALETTE
    return templates.TemplateResponse(request, "accounts.html", {
        "excluded": excluded, "seen": seen,
        "limits": limits, "default_max": default_max,
        "important": important, "important_handles": important_handles, "palette": PALETTE,
    })


@router.post("/accounts/limit")
def set_account_limit(handle: str = Form(...), max_tweets: int = Form(...)):
    handle = handle.strip().lstrip("@")
    max_tweets = max(1, max_tweets)
    if handle:
        with get_session() as s:
            row = s.exec(select(AccountSetting).where(AccountSetting.handle == handle)).first()
            if row:
                row.max_tweets = max_tweets
            else:
                row = AccountSetting(handle=handle, max_tweets=max_tweets)
            s.add(row)
            s.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/limit/{limit_id}/delete")
def remove_account_limit(limit_id: int):
    with get_session() as s:
        row = s.get(AccountSetting, limit_id)
        if row:
            s.delete(row)
            s.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/important")
def mark_important(handle: str = Form(...)):
    from agents.priority import pick_color
    handle = handle.strip().lstrip("@")
    if handle:
        with get_session() as s:
            rows = s.exec(select(AccountSetting)).all()
            used = [r.color for r in rows if r.important and r.color]
            row = next((r for r in rows if r.handle.lower() == handle.lower()), None)
            if row is None:
                row = AccountSetting(handle=handle, max_tweets=get_settings(s).max_tweets_per_account)
            if not row.important:
                row.important = True
                row.color = row.color or pick_color(used)
            s.add(row)
            s.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/important/{acc_id}/color")
def set_important_color(acc_id: int, color: str = Form(...)):
    with get_session() as s:
        row = s.get(AccountSetting, acc_id)
        if row and color.strip():
            row.color = color.strip()
            s.add(row)
            s.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/important/{acc_id}/unset")
def unset_important(acc_id: int):
    with get_session() as s:
        row = s.get(AccountSetting, acc_id)
        if row:
            row.important = False
            s.add(row)
            s.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/exclude")
def add_exclude(handle: str = Form(...), note: str = Form("")):
    handle = handle.strip().lstrip("@")
    if handle:
        with get_session() as s:
            exists = s.exec(select(ExcludedAccount).where(ExcludedAccount.handle == handle)).first()
            if not exists:
                s.add(ExcludedAccount(handle=handle, note=note.strip() or None))
                s.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/exclude/{exc_id}/delete")
def remove_exclude(exc_id: int):
    with get_session() as s:
        row = s.get(ExcludedAccount, exc_id)
        if row:
            s.delete(row)
            s.commit()
    return RedirectResponse("/accounts", status_code=303)


# ---------------------------------------------------------------- Topics
@router.post("/topics/add")
def add_topic(name: str = Form(...)):
    name = name.strip()
    if name:
        with get_session() as s:
            exists = s.exec(select(Topic).where(Topic.name == name)).first()
            if not exists:
                s.add(Topic(name=name))
                s.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/topics/{topic_id}/delete")
def remove_topic(topic_id: int):
    with get_session() as s:
        row = s.get(Topic, topic_id)
        if row:
            s.delete(row)
            s.commit()
    return RedirectResponse("/settings", status_code=303)


# ---------------------------------------------------------------- Settings
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tg: str | None = None):
    from config import settings as boot

    with get_session() as s:
        cfg = get_settings(s)
        topics = s.exec(select(Topic).order_by(Topic.name)).all()
    return templates.TemplateResponse(request, "settings.html", {
        "cfg": cfg, "topics": topics,
        "styles": [st.value for st in DigestStyle],
        "methods": [m.value for m in ClusteringMethod],
        "thread_modes": [m.value for m in ThreadMode],
        "tg_msg": tg,
        "tg_token_set": bool(boot.telegram_bot_token),
        "tg_chat_set": bool(boot.telegram_chat_id),
        "running": is_running(),
        "timezones": sorted(available_timezones()),
    })


@router.post("/settings")
def save_settings(
    schedule_hour: int = Form(...),
    schedule_minute: int = Form(...),
    timezone: str = Form("America/New_York"),
    time_window_hours: int = Form(...),
    max_tweets_per_account: int = Form(...),
    max_themes: int = Form(...),
    ollama_model: str = Form(...),
    digest_style: str = Form(...),
    clustering_method: str = Form("llm"),
    embedding_model: str = Form("nomic-embed-text"),
    similarity_threshold: float = Form(0.55),
    exclude_keywords: str = Form(""),
    thread_mode: str = Form("reply"),
    thread_gap_minutes: int = Form(10),
    collection_interval_hours: int = Form(3),
    process_interval_hours: int = Form(4),
    schedule_enabled: str = Form(None),
    collection_enabled: str = Form(None),
    process_enabled: str = Form(None),
    include_retweets: str = Form(None),
    stitch_threads: str = Form(None),
):
    with get_session() as s:
        cfg = get_settings(s)
        cfg.schedule_hour = max(0, min(23, schedule_hour))
        cfg.schedule_minute = max(0, min(59, schedule_minute))
        cfg.timezone = _valid_tz(timezone)
        cfg.collection_interval_hours = max(1, collection_interval_hours)
        cfg.process_interval_hours = max(1, process_interval_hours)
        cfg.collection_enabled = collection_enabled is not None
        cfg.process_enabled = process_enabled is not None
        cfg.time_window_hours = max(1, time_window_hours)
        cfg.max_tweets_per_account = max(1, max_tweets_per_account)
        cfg.max_themes = max(1, max_themes)
        cfg.ollama_model = ollama_model.strip()
        cfg.digest_style = DigestStyle(digest_style)
        cfg.clustering_method = ClusteringMethod(clustering_method)
        cfg.embedding_model = embedding_model.strip() or "nomic-embed-text"
        cfg.similarity_threshold = max(0.0, min(1.0, similarity_threshold))
        cfg.exclude_keywords = exclude_keywords.strip()
        cfg.schedule_enabled = schedule_enabled is not None
        cfg.include_retweets = include_retweets is not None
        cfg.stitch_threads = stitch_threads is not None
        cfg.thread_mode = ThreadMode(thread_mode)
        cfg.thread_gap_minutes = max(1, thread_gap_minutes)
        cfg.updated_at = datetime.utcnow()
        s.add(cfg)
        s.commit()
    import scheduler
    scheduler.reschedule()   # apply schedule changes live
    return RedirectResponse("/settings", status_code=303)


# ---------------------------------------------------------------- Runs
@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    with get_session() as s:
        rows = s.exec(select(DigestRun).order_by(DigestRun.id.desc())).all()
    return templates.TemplateResponse(request, "runs.html", {
        "runs": rows, "running": is_running(),
    })


@router.get("/digest/{run_id}", response_class=HTMLResponse)
def view_digest(run_id: int):
    with get_session() as s:
        row = s.get(DigestRun, run_id)
    if not row or not row.digest_path or not Path(row.digest_path).exists():
        return HTMLResponse("<p>Digest not found.</p>", status_code=404)
    return HTMLResponse(Path(row.digest_path).read_text())
