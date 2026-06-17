"""CLI entrypoint for the Twitter Summary Agent.

Commands:
  init-db   Create the SQLite database and tables.
  login     One-time browser login; saves the session for scraping.   (Phase 2)
  run       Run the digest pipeline once.                             (Phase 3)
  serve     Start the web UI + in-process scheduler.                  (Phase 4/5)
"""
import argparse
import sys


def cmd_init_db(_args: argparse.Namespace) -> int:
    from db.session import init_db
    from config import settings

    init_db()
    print(f"Initialized database at {settings.db_path}")
    return 0


def cmd_login(_args: argparse.Namespace) -> int:
    from auth.login import run_login
    return run_login()


def cmd_import_profile(args: argparse.Namespace) -> int:
    """Decrypt X cookies from your real Chrome and reuse that logged-in session."""
    from agents.browser import import_chrome_cookies
    from auth.login import capture_handle

    print("Importing X session cookies from Google Chrome…")
    n = import_chrome_cookies(cookie_file=args.cookie_file)
    print(f"Imported {n} cookies. Confirming the session…")
    handle = capture_handle()
    if handle:
        print(f"Done. Logged in as @{handle}. You can now run: python main.py collect")
        return 0
    print("Cookies imported, but couldn't confirm a logged-in session. "
          "Make sure you're logged into X in Chrome, then rerun import-profile.")
    return 1


def cmd_collect(args: argparse.Namespace) -> int:
    """Phase 2 debug tool: scrape and dump tweets without summarizing."""
    import json
    import logging

    from agents.base import AgentContext
    from agents.collector import Collector
    from config import settings
    from db.session import get_session, get_settings
    from state import DigestRun

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with get_session() as session:
        app_settings = get_settings(session)
    ctx = AgentContext(config=settings, app_settings=app_settings, logger=logging.getLogger("collect"))

    state = DigestRun()
    Collector(ctx, max_accounts=args.max_accounts).run(state)
    state.snapshot(settings.data_dir, "collected")

    out = args.out or f"{settings.data_dir}/tweets_{state.started_at[:10]}.json"
    with open(out, "w") as f:
        json.dump([t.__dict__ for t in state.raw_tweets], f, indent=2, default=str)
    print(f"Collected {len(state.raw_tweets)} tweets -> {out}")
    return 0


def cmd_telegram_chatid(_args: argparse.Namespace) -> int:
    from agents import telegram
    from config import settings

    if not settings.telegram_bot_token:
        print("Set TELEGRAM_BOT_TOKEN in .env first (create a bot via @BotFather).")
        return 1
    print("Send any message to your bot in Telegram, then this lists the chat id(s):\n")
    updates = telegram.get_updates(settings.telegram_bot_token)
    seen = {}
    for u in updates:
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat", {})
        if chat.get("id") is not None:
            seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name", "")
    if not seen:
        print("No chats found. Message the bot, then rerun.")
        return 1
    for cid, name in seen.items():
        print(f"  chat_id={cid}  ({name})")
    print("\nPut the chat id in .env as TELEGRAM_CHAT_ID.")
    return 0


def cmd_telegram_test(_args: argparse.Namespace) -> int:
    from agents import telegram
    from config import settings

    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
        return 1
    ok = telegram.send_message(settings.telegram_bot_token, settings.telegram_chat_id,
                               "✅ <b>Twitter Summary Agent</b> Telegram test message.")
    print("Sent!" if ok else "Failed — check token/chat id (see logs).")
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    import logging

    import pipeline

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    state = pipeline.run(max_accounts=args.max_accounts)
    print(f"Done. {len(state.filtered_tweets)} tweets, {len(state.themes)} themes -> {state.digest_path}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    import logging

    import pipeline

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    state = pipeline.resume(run_id=args.run_id)
    if state is None:
        print("Nothing to resume (no failed run with a saved snapshot).")
        return 1
    print(f"Resumed run {state.run_id}. {len(state.filtered_tweets)} tweets, "
          f"{len(state.themes)} themes -> {state.digest_path}")
    return 0


def cmd_delete_run(args: argparse.Namespace) -> int:
    import logging

    import pipeline

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    summary = pipeline.delete_run(args.run_id)
    if summary is None:
        print(f"Run {args.run_id} not found.")
        return 1
    print(f"Deleted run {summary['run_id']}: {summary['tweets']} digested tweets, "
          f"{summary['raw_tweets']} raw tweets, "
          f"digest {'removed' if summary['digest_deleted'] else 'none'}, "
          f"snapshots {'removed' if summary['snapshots_deleted'] else 'none'}.")
    return 0


def cmd_archive_backfill(_args: argparse.Namespace) -> int:
    import logging

    import pipeline
    from db.session import init_db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    init_db()   # ensure the raw_tweets table exists
    n = pipeline.backfill_raw_archive()
    print(f"Backfilled {n} raw tweets into the archive from existing snapshots.")
    return 0


def cmd_trends_rebuild(args: argparse.Namespace) -> int:
    """Rebuild materialized trend tables from the archive + snapshots (safe to re-run)."""
    import logging

    from agents import analytics
    from db.session import init_db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    init_db()   # ensure the trend tables exist
    days = analytics.recompute_daily_stats()
    print(f"Rebuilt daily_stats: {days} day(s) of activity.")
    if not args.no_themes:
        try:
            themes = analytics.rebuild_theme_history()
            print(f"Rebuilt theme_history: {themes} theme(s) (embeddings via Ollama).")
        except Exception as e:
            print(f"Theme history skipped (needs Ollama running): {e}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import logging

    import uvicorn

    # Surface our scheduler/pipeline logs alongside uvicorn's.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    print(f"Starting web UI at http://{args.host}:{args.port}")
    uvicorn.run("web.app:app", host=args.host, port=args.port,
                log_level="info", log_config=None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="twitter-summary-agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the SQLite database and tables").set_defaults(func=cmd_init_db)
    sub.add_parser("login", help="One-time browser login").set_defaults(func=cmd_login)

    imp = sub.add_parser("import-profile", help="Reuse your logged-in Chrome X session (decrypts cookies)")
    imp.add_argument("--cookie-file", default=None,
                     help="Path to Chrome Cookies DB (default: ~/.config/google-chrome/Default/Cookies)")
    imp.set_defaults(func=cmd_import_profile)

    collect = sub.add_parser("collect", help="Scrape and dump tweets (debug, no summary)")
    collect.add_argument("--out", default=None, help="Output JSON path")
    collect.add_argument("--max-accounts", type=int, default=None,
                         help="Limit number of accounts scraped (for quick testing)")
    collect.set_defaults(func=cmd_collect)

    sub.add_parser("telegram-chatid", help="Discover your Telegram chat id").set_defaults(func=cmd_telegram_chatid)
    sub.add_parser("telegram-test", help="Send a Telegram test message").set_defaults(func=cmd_telegram_test)

    run_p = sub.add_parser("run", help="Run the digest pipeline once")
    run_p.add_argument("--max-accounts", type=int, default=None, help="Limit accounts scraped (testing)")
    run_p.set_defaults(func=cmd_run)

    resume_p = sub.add_parser("resume", help="Resume a failed run from its snapshot (no re-scrape)")
    resume_p.add_argument("run_id", type=int, nargs="?", default=None,
                          help="Run id to resume (default: most recent failed run)")
    resume_p.set_defaults(func=cmd_resume)

    sub.add_parser("archive-backfill",
                   help="Import past 1_collected snapshots into the raw tweet archive (one-time)"
                   ).set_defaults(func=cmd_archive_backfill)

    del_p = sub.add_parser("delete-run", help="Delete a run and all its data (tweets, archive, files)")
    del_p.add_argument("run_id", type=int, help="Run id to delete")
    del_p.set_defaults(func=cmd_delete_run)

    trends_p = sub.add_parser("trends-rebuild",
                              help="Rebuild trend tables from the archive + snapshots (daily_stats + themes)")
    trends_p.add_argument("--no-themes", action="store_true",
                          help="Only rebuild daily_stats; skip theme embedding (no Ollama needed)")
    trends_p.set_defaults(func=cmd_trends_rebuild)

    serve = sub.add_parser("serve", help="Start the web UI + scheduler")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
