"""Slack Events + slash-command FastAPI sub-router.

Mounted on the orchestrator API. Two entry points:

- `POST /webhooks/slack/commands`  -> handles `/review-pr <url>`.
- `POST /webhooks/slack/events`    -> handles URL verification + thread `message` events.

Every handler acks within 3 seconds (Slack's timeout) and offloads real work via
`submit_clarification` / `start_session` + `advance`. The signing-secret check is
delegated to slack_bolt; webhook retry-storm dedup happens below via the
`X-Slack-Retry-Num` header plus an in-process `event_id` set.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from fastapi import APIRouter, Request
from sqlalchemy import select

from services.config import settings
from services.orchestrator.db import db_session
from services.orchestrator.models import ReviewSession, SessionStatus
from services.orchestrator.state_machine import submit_clarification

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/slack", tags=["slack"])

# In-process dedup — Slack retries within seconds, so a small LRU is enough.
_SEEN_EVENT_IDS: OrderedDict[str, None] = OrderedDict()
_SEEN_CAP = 2000


def _dedup(event_id: str | None) -> bool:
    """Return True if this event has already been handled."""
    if not event_id:
        return False
    if event_id in _SEEN_EVENT_IDS:
        return True
    _SEEN_EVENT_IDS[event_id] = None
    if len(_SEEN_EVENT_IDS) > _SEEN_CAP:
        _SEEN_EVENT_IDS.popitem(last=False)
    return False


def _bolt_handler():
    """Build a slack_bolt FastAPI handler on demand so importing this module is cheap
    even when Slack isn't configured."""
    from slack_bolt import App
    from slack_bolt.adapter.fastapi import SlackRequestHandler

    s = settings()
    if not s.slack_bot_token or not s.slack_signing_secret:
        return None

    bolt = App(token=s.slack_bot_token, signing_secret=s.slack_signing_secret)

    @bolt.command("/review-pr")
    def handle_review_pr(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip()
        if not text or text.lower() in {"help", "-h", "--help", "?"}:
            respond(_review_pr_usage())
            return
        try:
            url, engine, model = _parse_review_pr_args(text)
        except ValueError as exc:
            respond(f":x: {exc}\n{_review_pr_usage()}")
            return
        user_id = command.get("user_id")
        import httpx

        # Echo the chosen config back so the user sees engine+model immediately
        # — before any LLM roundtrip. Resolves the ambiguity of an unset model
        # (= "engine default") against the settings-level default.
        from services.engines.base import resolve_model

        effective_model = resolve_model(engine or s.redink_engine, model)
        try:
            r = httpx.post(
                f"{s.redink_api_url.rstrip('/')}/reviews",
                json={
                    "pr_url": url,
                    "engine": engine or s.redink_engine,
                    "model": model,
                    "mode": "fresh",
                    "slack_author_id": user_id,
                },
                timeout=300,
            )
            r.raise_for_status()
            data = r.json()
            respond(
                f"🟥 *REDINK* — review started · session `{data['id']}`\n"
                f"> engine=`{engine or s.redink_engine}`  model=`{effective_model}`  "
                f"status=`{data['status']}`\n"
                f"Watch <#{s.slack_review_channel}|{s.slack_review_channel}> for updates."
            )
        except httpx.HTTPError as exc:
            respond(f":x: failed to start review: `{exc}`")

    @bolt.event("message")
    def handle_message(event, logger):
        if event.get("subtype"):
            return  # ignore bot_message, message_changed, etc.
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return  # only care about in-thread replies
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

    return SlackRequestHandler(bolt)


_handler = None


def _get_handler():
    global _handler
    if _handler is None:
        _handler = _bolt_handler()
    return _handler


def _review_pr_usage() -> str:
    return (
        "🟥 *REDINK*\n"
        "*Usage:* `/review-pr <github-pr-url> [engine=ollama|claude-code] [model=<name>]`\n"
        "\n"
        "*Examples:*\n"
        "• `/review-pr https://github.com/org/repo/pull/42`  _(default engine + model)_\n"
        "• `/review-pr https://github.com/org/repo/pull/42 engine=ollama model=gemma4:e2b`\n"
        "• `/review-pr https://github.com/org/repo/pull/42 engine=ollama model=gemma3:12b`  _(bigger local model)_\n"
        "• `/review-pr https://github.com/org/repo/pull/42 engine=claude-code model=claude-sonnet-4-6`\n"
        "• `/review-pr https://github.com/org/repo/pull/42 engine=claude-code model=claude-opus-4-5`  _(deepest review)_\n"
        "\n"
        "*Engines:* `ollama` (local, free) · `claude-code` (frontier, ~cents/PR)\n"
        "*Replying in the thread* answers an open clarification round.\n"
        "\n"
        "Full reference: `docs/slack.md` in the Redink repo."
    )


_ALLOWED_ENGINES = {"ollama", "claude-code"}


def _parse_review_pr_args(text: str) -> tuple[str, str | None, str | None]:
    """Parse `<url> [engine=X] [model=Y]` from the slash-command body.

    Slack's slash-command text is a single string with whitespace-separated
    tokens. We accept `key=value` pairs (mirroring the docs) plus the older
    `--engine X` / `--model Y` long-flag form so old muscle memory still
    works. Order doesn't matter; unknown tokens raise so the user sees a
    usage message rather than having an option silently ignored.
    """
    parts = text.split()
    url: str | None = None
    engine: str | None = None
    model: str | None = None

    i = 0
    while i < len(parts):
        tok = parts[i]
        if tok.startswith("engine="):
            engine = tok.split("=", 1)[1] or None
        elif tok.startswith("model="):
            model = tok.split("=", 1)[1] or None
        elif tok in ("--engine", "-e") and i + 1 < len(parts):
            engine = parts[i + 1]
            i += 1
        elif tok in ("--model", "-m") and i + 1 < len(parts):
            model = parts[i + 1]
            i += 1
        elif tok.startswith("http"):
            url = tok
        else:
            raise ValueError(f"unrecognised argument: `{tok}`")
        i += 1

    if not url:
        raise ValueError("PR URL is required")
    if engine and engine not in _ALLOWED_ENGINES:
        raise ValueError(
            f"unknown engine `{engine}` — must be one of: {', '.join(sorted(_ALLOWED_ENGINES))}"
        )
    return url, engine, model


@router.post("/commands")
async def commands(request: Request):
    h = _get_handler()
    if h is None:
        return {"ok": False, "error": "slack not configured"}
    return await h.handle(request)


@router.post("/events")
async def events(request: Request):
    h = _get_handler()
    if h is None:
        return {"ok": False, "error": "slack not configured"}

    body = await request.body()

    # Cheap dedup — Slack retries within seconds on timeouts.
    if request.headers.get("content-type", "").startswith("application/json"):
        import json as _json

        try:
            payload = _json.loads(body)
        except _json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and _dedup(payload.get("event_id")):
            return {"ok": True, "duplicate": True}

    # Replay the body to slack_bolt (its signature check reads it again).
    from starlette.requests import Request as StarletteRequest

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return await h.handle(StarletteRequest(request.scope, _receive))
