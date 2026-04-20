from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SessionStatus(StrEnum):
    INGEST = "INGEST"
    GATHER_CONTEXT = "GATHER_CONTEXT"
    EVALUATE_CONTEXT = "EVALUATE_CONTEXT"
    AWAIT_SLACK_CLARIFICATION = "AWAIT_SLACK_CLARIFICATION"
    REVIEW = "REVIEW"
    POST = "POST"
    MONITORING = "MONITORING"
    AWAIT_COMMENT_REPLY = "AWAIT_COMMENT_REPLY"
    ENGAGE_ON_COMMENT = "ENGAGE_ON_COMMENT"
    DONE = "DONE"
    FAILED = "FAILED"


class ReviewSession(Base):
    __tablename__ = "review_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    pr_url: Mapped[str] = mapped_column(String(1024), index=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default=SessionStatus.INGEST, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="fresh")
    engine: Mapped[str] = mapped_column(String(40), default="ollama")
    # Specific model the engine was told to use (e.g. "gemma4:e2b",
    # "claude-sonnet-4-6"). Nullable for back-compat with rows created before
    # this column existed — reads fall back to the engine's built-in default.
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    slack_thread_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    slack_author_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    findings: Mapped[list[Finding]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    slack_rounds: Mapped[list[SlackRound]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    line: Mapped[int] = mapped_column(Integer)
    severity: Mapped[str] = mapped_column(String(20), default="info")
    body: Mapped[str] = mapped_column(Text)
    posted_comment_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped[ReviewSession] = relationship(back_populates="findings")


class SlackRound(Base):
    __tablename__ = "slack_rounds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"), index=True)
    round_no: Mapped[int] = mapped_column(Integer)
    questions_json: Mapped[dict] = mapped_column(JSON)
    answers_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    thread_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    asked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[ReviewSession] = relationship(back_populates="slack_rounds")


class CommentThread(Base):
    """One row per Redink ↔ human exchange on an inline comment.

    Used for:
      - counting rounds against `REDINK_MAX_COMMENT_ENGAGEMENT_ROUNDS`
      - auditing what the engine said and why
      - deduping webhook retries via `parent_comment_id` + human reply hash
    """

    __tablename__ = "comment_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id"), index=True)
    round_no: Mapped[int] = mapped_column(Integer)
    parent_comment_id: Mapped[int] = mapped_column(Integer, index=True)
    human_reply: Mapped[str] = mapped_column(Text)
    human_reply_hash: Mapped[str] = mapped_column(String(64), index=True)
    engine_action: Mapped[str] = mapped_column(String(20))
    engine_response: Mapped[str] = mapped_column(Text)
    bot_reply_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MemoryEntry(Base):
    """Content-addressable cache for expensive context-gathering work.

    Used by `services.memory.store` to cache repo snapshots, tree walks, and
    other slow-to-compute context that's stable across PRs. Each row is keyed
    by a semantic cache key (e.g. `repo_snapshot:<slug>`). Staleness is
    checked by comparing `etag` (typically a commit SHA) against the current
    upstream value before every read.
    """

    __tablename__ = "memory_entries"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    etag: Mapped[str] = mapped_column(String(128), index=True)
    value_json: Mapped[dict] = mapped_column(JSON)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EngineCall(Base):
    __tablename__ = "engine_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(40))
    engine: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str] = mapped_column(String(64))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
