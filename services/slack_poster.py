"""Slack posting helpers — per-PR thread creation, status, clarification questions.

Thread reuse: every review session is tied to exactly one Slack thread. The root
message is posted to `SLACK_REVIEW_CHANNEL`; subsequent updates and clarification
questions post into the same thread via `thread_ts`. Archived/locked threads fall
back to opening a new one.
"""

from __future__ import annotations

import logging

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from services.config import settings
from services.engines.base import ClarificationQuestion
from services.review.secret_scrubber import scrub_outbound

log = logging.getLogger(__name__)


def _client() -> WebClient:
    token = settings().slack_bot_token
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is not set — run `redink init` and configure Slack.")
    return WebClient(token=token)


def open_pr_thread(
    *, pr_url: str, session_id: str, engine: str, model: str | None = None,
) -> str | None:
    """Post the root message for a PR and return its thread_ts.

    Returns None if Slack is not configured — callers should treat that as
    "Slack is disabled, keep working without clarification".
    """
    s = settings()
    if not s.slack_bot_token:
        return None
    try:
        resp = _client().chat_postMessage(
            channel=s.slack_review_channel,
            text=_root_text(
                pr_url=pr_url, session_id=session_id, engine=engine, model=model,
            ),
            unfurl_links=False,
        )
        return resp["ts"]
    except SlackApiError as exc:
        log.warning("slack post failed: %s", exc.response.get("error"))
        return None


def post_status(thread_ts: str, text: str) -> None:
    _safe_post_in_thread(thread_ts, text)


def post_progress(thread_ts: str | None, text: str) -> None:
    """Best-effort progress ping into the session's thread.

    Silently no-ops when Slack isn't configured or `thread_ts` is missing so
    progress reporting never blocks the review pipeline.
    """
    if not thread_ts:
        return
    _safe_post_in_thread(thread_ts, text)


def ensure_thread(
    *,
    pr_url: str,
    session_id: str,
    engine: str,
    thread_ts: str | None,
    model: str | None = None,
) -> str | None:
    """Return an existing thread_ts or create the root Slack post and return its ts.

    Called from the state machine at the very first progress tick so we don't
    have to wait for the evaluator to decide on clarification before the user
    sees anything in Slack.
    """
    if thread_ts:
        return thread_ts
    if not settings().slack_bot_token:
        return None
    return open_pr_thread(
        pr_url=pr_url, session_id=session_id, engine=engine, model=model,
    )


def post_clarification_questions(
    thread_ts: str,
    questions: list[ClarificationQuestion],
    *,
    round_no: int,
    author_slack_id: str | None = None,
) -> None:
    tag = f"<@{author_slack_id}> " if author_slack_id else ""
    lines = [
        f"{tag}*Round {round_no}* — I need some context before I can review this thoroughly:",
    ]
    for q in questions:
        lines.append(f"\n*{q.id}.* {q.text}")
        lines.append(f"> _why: {q.why_needed}_")
    lines.append(
        "\nReply in this thread with your answers, or run "
        "`redink answer <session-id> --text \"...\"` from the CLI."
    )
    _safe_post_in_thread(thread_ts, "\n".join(lines))


def post_review_complete(thread_ts: str, *, review_url: str, finding_count: int) -> None:
    plural = "finding" if finding_count == 1 else "findings"
    _safe_post_in_thread(
        thread_ts,
        f"🟥 *REDINK* — review posted: {review_url} ({finding_count} {plural}). :white_check_mark:",
    )


# ---------------------------------------------------------------- helpers


def _root_text(
    *, pr_url: str, session_id: str, engine: str, model: str | None = None,
) -> str:
    model_part = f"  model=`{model}`" if model else ""
    # 🟥 *REDINK* is our text-mode wordmark — the red square + bold label gives
    # us a consistent brand stamp in every Slack client without needing a
    # custom workspace emoji.
    return (
        f"🟥 *REDINK* is reviewing {pr_url}\n"
        f"> engine=`{engine}`{model_part}  session=`{session_id}`\n"
        "I'll post updates in this thread."
    )


def _safe_post_in_thread(thread_ts: str, text: str) -> None:
    """Post to `thread_ts` in the configured channel; tolerate archived/locked threads."""
    s = settings()
    if not s.slack_bot_token:
        return
    try:
        _client().chat_postMessage(
            channel=s.slack_review_channel,
            thread_ts=thread_ts,
            text=scrub_outbound(text),
            unfurl_links=False,
        )
    except SlackApiError as exc:
        code = exc.response.get("error")
        log.warning("slack thread post failed (%s); dropping message", code)
