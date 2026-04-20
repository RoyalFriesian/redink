"""Slack Bolt app — socket-mode runner for local testing.

Runs `/review-pr` and thread-reply handling over a Slack socket-mode
connection (no public URL needed). This is the recommended path for running
the stack locally; the FastAPI sub-router in `adapters/slack/events.py` is
the equivalent for webhook-mode deployments.

Required environment:
    SLACK_BOT_TOKEN        xoxb-... from OAuth & Permissions → Install
    SLACK_SIGNING_SECRET   from Basic Information → App Credentials
    SLACK_APP_TOKEN        xapp-... from Basic Information → App-Level Tokens
                           (scope: `connections:write`)
    SLACK_REVIEW_CHANNEL   channel name (without '#') the bot is a member of

Start with:
    redink-slack
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select

from services.config import settings
from services.orchestrator.db import db_session
from services.orchestrator.models import ReviewSession, SessionStatus
from services.orchestrator.state_machine import submit_clarification

log = logging.getLogger(__name__)


def run() -> None:
    logging.basicConfig(level=settings().redink_log_level)

    s = settings()
    if not s.slack_bot_token or not s.slack_signing_secret:
        log.warning(
            "SLACK_BOT_TOKEN / SLACK_SIGNING_SECRET not set — "
            "Slack adapter idle. Run `redink init` and configure Slack to enable."
        )
        _idle_forever()

    app_token = s.slack_app_token
    if not app_token:
        log.warning(
            "SLACK_APP_TOKEN not set — socket-mode needs an xapp- token "
            "(Basic Info → App-Level Tokens, scope `connections:write`). "
            "Idling."
        )
        _idle_forever()

    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = App(token=s.slack_bot_token, signing_secret=s.slack_signing_secret)

    @app.command("/review-pr")
    def handle_review_pr(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip()
        url = text.split()[0] if text else ""
        if not url:
            respond("Usage: `/review-pr <github-pr-url>`")
            return
        import httpx

        try:
            r = httpx.post(
                f"{s.redink_api_url.rstrip('/')}/reviews",
                json={
                    "pr_url": url,
                    "engine": s.redink_engine,
                    "mode": "fresh",
                    "slack_author_id": command.get("user_id"),
                },
                timeout=300,
            )
            r.raise_for_status()
            data = r.json()
            respond(
                f":rocket: review started — session `{data['id']}` "
                f"(status: `{data['status']}`). Watch "
                f"<#{s.slack_review_channel}|{s.slack_review_channel}> for updates."
            )
        except httpx.HTTPError as exc:
            respond(f":x: failed to start review: `{exc}`")

    @app.event("message")
    def handle_message(event, logger):
        # Ignore edits, bot messages, channel-join notices, etc.
        if event.get("subtype"):
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return  # only in-thread replies resume a session
        text = (event.get("text") or "").strip()
        if not text:
            return

        with db_session() as db:
            session = db.execute(
                select(ReviewSession).where(ReviewSession.slack_thread_ts == thread_ts)
            ).scalar_one_or_none()

        if session is None:
            return
        if session.status != SessionStatus.AWAIT_SLACK_CLARIFICATION:
            return
        try:
            submit_clarification(session.id, {"_free_form": text})
        except ValueError as exc:
            logger.warning("submit_clarification failed: %s", exc)

    log.info("redink-slack starting (socket mode)")
    SocketModeHandler(app, app_token).start()


def _idle_forever() -> None:
    """Keep the process alive so compose/`redink up` doesn't restart-loop."""
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    run()
