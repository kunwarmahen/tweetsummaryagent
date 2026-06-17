"""HTTP routes for the config UI."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, Request
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


# ---------------------------------------------------------------- Dashboard
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with get_session() as s:
        cfg = get_settings(s)
        last_run = s.exec(select(DigestRun).order_by(DigestRun.id.desc())).first()
        run_count = s.exec(select(func.count(DigestRun.id))).one()
        tweet_count = s.exec(select(func.count(Tweet.id))).one()
        raw_count = s.exec(select(func.count(RawTweet.id))).one()
        excluded_count = s.exec(select(func.count(ExcludedAccount.id))).one()
    return templates.TemplateResponse(request, "index.html", {
        "cfg": cfg, "last_run": last_run,
        "run_count": run_count, "tweet_count": tweet_count, "raw_count": raw_count,
        "excluded_count": excluded_count, "running": is_running(),
    })


@router.post("/run-now")
def run_now(background_tasks: BackgroundTasks):
    if not is_running():
        background_tasks.add_task(pipeline.run_guarded)
    return RedirectResponse("/runs", status_code=303)


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: int, background_tasks: BackgroundTasks):
    if not is_running():
        background_tasks.add_task(pipeline.resume_guarded, run_id)
    return RedirectResponse("/runs", status_code=303)


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
    return templates.TemplateResponse(request, "accounts.html", {
        "excluded": excluded, "seen": seen,
        "limits": limits, "default_max": default_max,
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
def settings_page(request: Request):
    with get_session() as s:
        cfg = get_settings(s)
        topics = s.exec(select(Topic).order_by(Topic.name)).all()
    return templates.TemplateResponse(request, "settings.html", {
        "cfg": cfg, "topics": topics,
        "styles": [st.value for st in DigestStyle],
        "methods": [m.value for m in ClusteringMethod],
        "thread_modes": [m.value for m in ThreadMode],
    })


@router.post("/settings")
def save_settings(
    schedule_hour: int = Form(...),
    schedule_minute: int = Form(...),
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
    schedule_enabled: str = Form(None),
    include_retweets: str = Form(None),
    stitch_threads: str = Form(None),
):
    with get_session() as s:
        cfg = get_settings(s)
        cfg.schedule_hour = max(0, min(23, schedule_hour))
        cfg.schedule_minute = max(0, min(59, schedule_minute))
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
