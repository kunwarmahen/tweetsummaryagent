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
from config import settings
from state import DigestRun

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"


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
        themes = [
            {
                "title": th.title,
                "summary": th.summary,
                "tweets": [by_id[i] for i in th.tweet_ids if i in by_id],
            }
            for th in state.themes
        ]

        now = datetime.now()
        html = self._env.get_template("digest.html").render(
            date=now.strftime("%A, %B %d, %Y"),
            generated_at=now.strftime("%Y-%m-%d %H:%M"),
            total_tweets=len(state.filtered_tweets),
            themes=themes,
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
                                                       len(state.filtered_tweets))
        elif state.themes and not deliver:
            self.log.info("Delivery disabled for this run; digest saved only")
        return state

    def _send_telegram(self, themes: list[dict], date_str: str, total_tweets: int) -> bool:
        from agents import telegram

        cfg = self.ctx.config
        if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
            self.log.info("Telegram not configured; skipping")
            return False
        ok = telegram.send_digest(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                  themes, date_str, total_tweets)
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
