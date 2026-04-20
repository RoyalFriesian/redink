from __future__ import annotations

import logging

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from services.config import settings
from services.orchestrator.db import db_session, init_db
from services.orchestrator.models import ReviewSession, SessionStatus
from services.orchestrator.state_machine import (
    advance,
    record_clarification_answer,
    start_session,
)

log = logging.getLogger(__name__)
app = FastAPI(title="Redink Orchestrator", version="0.1.0")


class ReviewRequest(BaseModel):
    pr_url: str
    engine: str | None = None
    # Optional explicit model override (e.g. "gemma4:e2b",
    # "claude-sonnet-4-6"). Null means "use the engine's default from
    # settings." Surfaced in Slack + CLI so reviewers always know what
    # produced the comments.
    model: str | None = None
    mode: str = "fresh"
    slack_author_id: str | None = None


class ReviewResponse(BaseModel):
    id: str
    status: str
    posted_review_url: str | None = None


class PendingQuestion(BaseModel):
    id: str
    text: str
    why_needed: str


class StatusResponse(BaseModel):
    id: str
    pr_url: str
    status: str
    engine: str
    # Effective model in use — resolved from `session.model` with a fallback
    # to the engine's settings-level default, so the CLI/Slack never show
    # `None` when the user didn't pass `--model` explicitly.
    model: str
    head_sha: str | None = None
    error: str | None = None
    finding_count: int = 0
    slack_thread_ts: str | None = None
    pending_questions: list[PendingQuestion] = []


class ClarifyRequest(BaseModel):
    answers: dict[str, str]


@app.on_event("startup")
def _startup() -> None:
    logging.basicConfig(level=settings().redink_log_level)
    init_db()
    log.info("redink-api ready")


@app.post("/reviews", response_model=ReviewResponse)
def create_review(req: ReviewRequest, background: BackgroundTasks) -> ReviewResponse:
    """Kick off a review. Returns immediately; work runs in the background.

    Clients poll `GET /reviews/{id}` to track progress. Each phase inside
    `advance()` commits on entry so `status` updates are visible in real time.
    """
    session_id = start_session(
        req.pr_url,
        engine=req.engine,
        model=req.model,
        mode=req.mode,
        slack_author_id=req.slack_author_id,
    )
    background.add_task(advance, session_id)
    return ReviewResponse(id=session_id, status=SessionStatus.INGEST)


@app.get("/reviews/{session_id}", response_model=StatusResponse)
def get_review(session_id: str) -> StatusResponse:
    with db_session() as db:
        s = db.get(ReviewSession, session_id)
        if s is None:
            raise HTTPException(404, "unknown session")

        pending: list[PendingQuestion] = []
        if s.status == SessionStatus.AWAIT_SLACK_CLARIFICATION:
            open_rounds = [r for r in s.slack_rounds if r.answered_at is None]
            if open_rounds:
                latest = max(open_rounds, key=lambda r: r.round_no)
                for q in latest.questions_json.get("questions", []):
                    pending.append(PendingQuestion(**q))

        from services.engines.base import resolve_model

        return StatusResponse(
            id=s.id,
            pr_url=s.pr_url,
            status=s.status,
            engine=s.engine,
            model=resolve_model(s.engine, s.model),
            head_sha=s.head_sha,
            error=s.error,
            finding_count=len(s.findings),
            slack_thread_ts=s.slack_thread_ts,
            pending_questions=pending,
        )


@app.post("/reviews/{session_id}/clarify", response_model=ReviewResponse)
def clarify_review(
    session_id: str, req: ClarifyRequest, background: BackgroundTasks
) -> ReviewResponse:
    """Store the author's answer and resume the review asynchronously."""
    try:
        record_clarification_answer(session_id, req.answers)
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "unknown" in msg else 409
        raise HTTPException(code, msg) from exc
    background.add_task(advance, session_id)
    return ReviewResponse(id=session_id, status=SessionStatus.EVALUATE_CONTEXT)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# Slack events are handled by a sub-router so we only import the Slack SDK when needed.
try:
    from adapters.slack.events import router as slack_router

    app.include_router(slack_router)
except ImportError as _exc:  # slack_bolt not installed (shouldn't happen in prod)
    log.warning("Slack events router unavailable: %s", _exc)

# GitHub webhook drives the comment-reply engagement loop (M3) and force-push handling (M7).
try:
    from adapters.github_webhook.handler import router as github_router

    app.include_router(github_router)
except ImportError as _exc:
    log.warning("GitHub webhook router unavailable: %s", _exc)


def run() -> None:
    uvicorn.run(
        "services.orchestrator.api:app",
        host=settings().api_host,
        port=settings().api_port,
        log_level=settings().redink_log_level.lower(),
    )


if __name__ == "__main__":
    run()
