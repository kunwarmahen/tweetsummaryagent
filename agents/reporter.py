"""Reporter agent — render the themed newsletter and save it.

Resolves each theme's tweet IDs back to full tweets, renders the HTML template, and writes
it to data/digests/. Email delivery is added in Phase 5.
"""
from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agents.base import Agent
from agents.priority import load_important
from config import settings
from state import DigestRun, ThemeCluster

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"

_IMPORTANT_TITLE = "⭐ From your important accounts"


class Reporter(Agent):
    name = "reporter"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
        )

    def run(self, state: DigestRun) -> DigestRun:
        by_id = {t.tweet_id: t for t in state.filtered_tweets}
        important = load_important()                 # {handle_lower: color}
        self._apply_priority(state, by_id, important)  # float + guarantee important tweets

        themes = [
            {
                "title": th.title,
                "summary": th.summary,
                "tweets": [by_id[i] for i in th.tweet_ids if i in by_id],
            }
            for th in state.themes
        ]
        # Legend = important accounts actually appearing, in display-case.
        legend: dict[str, str] = {}
        for th in themes:
            for t in th["tweets"]:
                if t.handle.lower() in important:
                    legend[t.handle] = important[t.handle.lower()]

        now = datetime.now()
        html = self._env.get_template("digest.html").render(
            date=now.strftime("%A, %B %d, %Y"),
            generated_at=now.strftime("%Y-%m-%d %H:%M"),
            total_tweets=len(state.filtered_tweets),
            themes=themes,
            vip=important,        # {handle_lower: color} for per-tweet lookup
            legend=legend,        # {Handle: color} for the legend
        )

        out_dir = Path(settings.data_dir) / "digests"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"digest_{now.strftime('%Y%m%d_%H%M')}.html"
        out_path.write_text(html)

        state.digest_html = html
        state.digest_path = str(out_path)
        self.log.info("Digest written to %s", out_path)

        # Deliver (only when there's something to say, a channel is configured, and delivery
        # isn't disabled — e.g. a re-run/replay defaults to not sending).
        deliver = getattr(self.ctx.app_settings, "deliver", True)
        if state.themes and deliver:
            subject = f"Daily X Digest — {now.strftime('%b %d')} ({len(state.themes)} themes)"
            state.emailed = self._send_email(subject, html)
            state.telegram_sent = self._send_telegram(themes, now.strftime("%A, %B %d"),
                                                       len(state.filtered_tweets), important)
        elif state.themes and not deliver:
            self.log.info("Delivery disabled for this run; digest saved only")
        return state

    def _apply_priority(self, state: DigestRun, by_id: dict, important: dict[str, str]) -> None:
        """Float important-account tweets to the top, and guarantee they appear at all.

        - sorts tweets within each theme so important ones lead,
        - floats themes containing important tweets above the rest,
        - if any important tweet didn't make it into a theme, prepends a dedicated section.
        """
        if not important:
            return

        def is_imp(tweet_id: str) -> bool:
            t = by_id.get(tweet_id)
            return bool(t and t.handle.lower() in important)

        for th in state.themes:
            th.tweet_ids.sort(key=lambda i: 0 if is_imp(i) else 1)   # stable: important first

        covered = {i for th in state.themes for i in th.tweet_ids}
        orphans = [t.tweet_id for t in state.filtered_tweets
                   if t.handle.lower() in important and t.tweet_id not in covered]
        if orphans:
            state.themes.insert(0, ThemeCluster(
                title=_IMPORTANT_TITLE,
                summary="Tweets from accounts you marked important.",
                tweet_ids=orphans,
            ))

        # Float themes that contain an important tweet above those that don't (stable).
        state.themes.sort(key=lambda th: 0 if any(is_imp(i) for i in th.tweet_ids) else 1)

    def _send_telegram(self, themes: list[dict], date_str: str, total_tweets: int,
                       important: dict[str, str] | None = None) -> bool:
        from agents import telegram

        cfg = self.ctx.config
        if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
            self.log.info("Telegram not configured; skipping")
            return False
        ok = telegram.send_digest(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                  themes, date_str, total_tweets, important or {})
        if ok:
            self.log.info("Sent digest to Telegram chat %s", cfg.telegram_chat_id)
        return ok

    def _send_email(self, subject: str, html: str) -> bool:
        cfg = self.ctx.config
        if not (cfg.smtp_user and cfg.smtp_password and cfg.email_to):
            self.log.info("SMTP not configured; skipping email (digest saved to file)")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = cfg.email_from or cfg.smtp_user
        msg["To"] = cfg.email_to
        msg.attach(MIMEText(html, "html"))

        try:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.sendmail(msg["From"], [cfg.email_to], msg.as_string())
            self.log.info("Emailed digest to %s", cfg.email_to)
            return True
        except Exception as e:
            self.log.error("Email failed: %s", e)
            return False
