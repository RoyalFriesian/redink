"""GitHub App webhook router — mounted on the orchestrator API.

Endpoints:
  POST /webhooks/github   single endpoint, dispatches on `X-GitHub-Event` header.

Events handled:
  - `pull_request_review_comment` (action=created with non-null in_reply_to_id):
    drives the M3 engagement loop via `handle_comment_reply`.
  - `pull_request` (action=synchronize): logs head_sha change; in-flight sessions
    detect and abort engagement on stale comments (see state_machine).
  - `ping`: returns pong.

Hardening:
  - Constant-time HMAC-SHA256 signature check against GITHUB_WEBHOOK_SECRET.
  - In-process dedup on `X-GitHub-Delivery` (GitHub retries on our 5xx / timeout).
  - Acks immediately (FastAPI is async; handler work is in-process for M3,
    enqueued to RQ in M7).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections import OrderedDict

from fastapi import APIRouter, Header, HTTPException, Request

from services.config import settings
from services.orchestrator.state_machine import handle_comment_reply

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/github", tags=["github"])

_SEEN_DELIVERIES: OrderedDict[str, None] = OrderedDict()
_SEEN_CAP = 2000


def _dedup(delivery_id: str | None) -> bool:
    if not delivery_id:
        return False
    if delivery_id in _SEEN_DELIVERIES:
        return True
    _SEEN_DELIVERIES[delivery_id] = None
    if len(_SEEN_DELIVERIES) > _SEEN_CAP:
        _SEEN_DELIVERIES.popitem(last=False)
    return False


def _verify(raw: bytes, signature: str | None) -> None:
    secret = settings().github_webhook_secret
    if not secret:
        # No secret configured -> treat every request as unauthorized rather than
        # silently accepting forged payloads.
        raise HTTPException(500, "GITHUB_WEBHOOK_SECRET not configured")
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(401, "missing signature")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "bad signature")


@router.post("")
async def webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
):
    raw = await request.body()
    _verify(raw, x_hub_signature_256)

    if _dedup(x_github_delivery):
        return {"ok": True, "duplicate": True}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "invalid json") from exc

    if x_github_event == "ping":
        return {"ok": True, "pong": True}

    if x_github_event == "pull_request_review_comment":
        return _on_review_comment(payload)

    if x_github_event == "pull_request":
        return _on_pull_request(payload)

    # Unhandled events (issue_comment, push, ...) are ack'd so GitHub stops retrying.
    log.debug("ignoring github event: %s", x_github_event)
    return {"ok": True, "ignored": x_github_event}


# ---------------------------------------------------------------- dispatchers


def _on_review_comment(payload: dict) -> dict:
    if payload.get("action") != "created":
        return {"ok": True, "ignored": "non-created action"}

    comment = payload.get("comment") or {}
    parent_id = comment.get("in_reply_to_id")
    if not parent_id:
        # Top-level comment, not a reply to one of our findings.
        return {"ok": True, "ignored": "top-level comment"}

    pr = payload.get("pull_request") or {}
    pr_url = pr.get("html_url") or ""
    user = comment.get("user") or {}
    reply_text = comment.get("body") or ""
    author_login = user.get("login") or ""
    author_is_bot = (user.get("type") == "Bot") or author_login.endswith("[bot]")

    if not pr_url or not reply_text.strip():
        return {"ok": True, "ignored": "missing fields"}

    result = handle_comment_reply(
        pr_url=pr_url,
        parent_comment_id=int(parent_id),
        reply_text=reply_text,
        reply_author_login=author_login,
        reply_author_is_bot=author_is_bot,
    )
    return {"ok": True, "action": result.action, "reason": result.reason}


def _on_pull_request(payload: dict) -> dict:
    if payload.get("action") != "synchronize":
        return {"ok": True, "ignored": payload.get("action")}
    # The state machine guards engagement against stale head_sha; force-push cleanup
    # (aborting in-flight reviews, nudging Slack thread) lands in M7.
    pr = payload.get("pull_request") or {}
    log.info(
        "pull_request synchronize: %s head now %s",
        pr.get("html_url"),
        (pr.get("head") or {}).get("sha"),
    )
    return {"ok": True}
