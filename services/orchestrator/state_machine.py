"""Per-PR review session state machine.

`advance(session_id)` is the single entry point. It inspects `session.status`, drives
the session as far as possible in one call, and returns. Awaiting states (Slack
clarification, GitHub comment reply) are PASSIVE — the call returns and the session
row sits in the DB until an external event (Slack reply, GH webhook, API call) fires
`advance()` again.

M2: INGEST, GATHER_CONTEXT, EVALUATE_CONTEXT, AWAIT_SLACK_CLARIFICATION, REVIEW,
POST, DONE all wired. Engagement loop (AWAIT_COMMENT_REPLY, ENGAGE_ON_COMMENT) lands
in M3 and will hang off the same entry point.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select

from services.config import settings
from services.engines.base import (
    ClarificationQuestion,
    ReviewContext,
    ReviewEngine,
    RoundQA,
    get_engine,
)
from services.orchestrator.db import db_session
from services.orchestrator.locks import pr_lock
from services.orchestrator.models import (
    CommentThread,
    Finding,
    ReviewSession,
    SessionStatus,
    SlackRound,
)

log = logging.getLogger(__name__)


@dataclass
class AdvanceResult:
    session_id: str
    status: str
    posted_review_url: str | None = None


_STAGE_LABELS = {
    SessionStatus.INGEST: ":mag: *Ingesting PR* — reading title, body, and diff",
    SessionStatus.GATHER_CONTEXT: ":satellite_antenna: *Gathering context* — repo, linked issues, tickets",
    SessionStatus.EVALUATE_CONTEXT: ":brain: *Evaluating context* — do I understand this PR?",
    SessionStatus.REVIEW: ":pencil2: *Reviewing changed files*",
    SessionStatus.POST: ":outbox_tray: *Posting review comments to GitHub*",
    SessionStatus.DONE: ":checkered_flag: *Done*",
}


def _transition(session: ReviewSession, db, status: SessionStatus) -> None:
    """Move the session to `status` and commit immediately.

    Intermediate commits are what make `GET /reviews/{id}` observe real-time
    progress while `advance()` is running in the background. Without these,
    the whole state machine runs in a single transaction and the CLI spinner
    jumps straight from INGEST to DONE.
    """
    session.status = status
    db.commit()
    label = _STAGE_LABELS.get(status)
    if label:
        _progress(session, db, label)


def _effective_model(session: ReviewSession) -> str:
    """Return the model name to surface in Slack / CLI for this session.

    Session stores the user's explicit pick (or NULL → "use engine default");
    we resolve to the actually-used value via `resolve_model` so Slack never
    shows `model=None`.
    """
    from services.engines.base import resolve_model

    return resolve_model(session.engine, session.model)


def _progress(session: ReviewSession, db, text: str) -> None:
    """Post a progress ping into the session's Slack thread, creating it on first use.

    Best-effort: Slack outages or misconfiguration never block the pipeline.
    Commits any thread_ts we just created so a crash after this point still
    lets the next advance() find the thread.
    """
    from services import slack_poster

    try:
        ts = slack_poster.ensure_thread(
            pr_url=session.pr_url,
            session_id=session.id,
            engine=session.engine,
            model=_effective_model(session),
            thread_ts=session.slack_thread_ts,
        )
        if ts and ts != session.slack_thread_ts:
            session.slack_thread_ts = ts
            db.commit()
        slack_poster.post_progress(ts, text)
    except Exception:
        log.exception("progress post failed (non-fatal)")


def _make_progress_cb(session: ReviewSession, db):
    """Return a callable the providers/engine can use without knowing about DB or slack."""

    def _cb(text: str) -> None:
        _progress(session, db, text)

    return _cb


# ---------------------------------------------------------------- entry


def start_session(
    pr_url: str,
    *,
    engine: str | None = None,
    model: str | None = None,
    mode: str = "fresh",
    slack_author_id: str | None = None,
) -> str:
    with db_session() as db:
        s = ReviewSession(
            pr_url=pr_url,
            engine=engine or settings().redink_engine,
            # Empty string → NULL (= use engine default) so the Slack/CLI layer
            # can show the effective model resolved from settings later.
            model=(model or settings().redink_model or None),
            mode=mode,
            status=SessionStatus.INGEST,
            slack_author_id=slack_author_id,
        )
        db.add(s)
        db.flush()
        return s.id


def advance(session_id: str) -> AdvanceResult:
    with db_session() as db:
        session = db.get(ReviewSession, session_id)
        if session is None:
            raise ValueError(f"unknown session {session_id}")

        with pr_lock(db, session.pr_url) as got:
            if not got:
                log.info("another worker holds the lock for %s; skipping", session.pr_url)
                return AdvanceResult(session_id, session.status)

            try:
                return _drive(session, db)
            except Exception as exc:
                log.exception("session %s failed", session.id)
                session.status = SessionStatus.FAILED
                session.error = str(exc)[:4000]
                return AdvanceResult(session.id, session.status)


# ---------------------------------------------------------------- driver


def _drive(session: ReviewSession, db) -> AdvanceResult:
    engine: ReviewEngine = get_engine(session.engine, session.model)
    status = session.status

    if status == SessionStatus.DONE:
        return AdvanceResult(session.id, status)

    # Steps up to evaluation run whenever the session is in an early or awaiting state.
    if status in {
        SessionStatus.INGEST,
        SessionStatus.GATHER_CONTEXT,
        SessionStatus.EVALUATE_CONTEXT,
        SessionStatus.AWAIT_SLACK_CLARIFICATION,
    }:
        ctx, refs = _ingest_and_gather(session, db)
        evaluation = engine.evaluate_context(ctx)

        if evaluation.reasoning:
            _progress(
                session, db,
                f":brain: *Understanding so far:* {evaluation.reasoning.strip()[:800]}",
            )

        rounds_done = sum(1 for r in session.slack_rounds if r.answered_at is not None)
        pending = [r for r in session.slack_rounds if r.answered_at is None]
        max_rounds = settings().redink_max_clarification_rounds

        # If we have no ticket/doc reference AND haven't already asked for one,
        # prepend a synthetic request. Non-blocking: the author can reply "none"
        # and we'll proceed. Skipping when `evaluation.sufficient` keeps us from
        # nagging authors whose PR description is actually self-explanatory.
        if not _refs_have_ticket_or_doc(refs) and not _already_asked_for_doc(session):
            if evaluation.questions or not evaluation.sufficient:
                evaluation.questions = [_missing_doc_question(), *evaluation.questions]
                evaluation.sufficient = False

        if not evaluation.sufficient and rounds_done < max_rounds and not pending:
            # Guard against the "ask the same question forever" failure mode
            # observed with small models: if a round has already been answered
            # and the new questions materially overlap with any prior round's
            # questions, the model isn't learning from the author — stop asking
            # and review with whatever we have, noting low confidence.
            if rounds_done >= 1 and _questions_repeat(evaluation.questions, session.slack_rounds):
                log.info(
                    "session %s: new questions repeat prior rounds — force-advancing to REVIEW",
                    session.id,
                )
                caveat = (
                    "_Reviewed without full clarity — the model kept asking the same things "
                    "despite your answers. Treat low-confidence findings with skepticism._"
                )
                return _review_and_post(session, engine, ctx, db, caveat=caveat)
            return _open_clarification_round(session, evaluation.questions, db)

        if pending:
            # We're being called on an AWAIT_* session but no one has answered yet.
            session.status = SessionStatus.AWAIT_SLACK_CLARIFICATION
            return AdvanceResult(session.id, session.status)

        # Either sufficient, or we've hit the round cap and are proceeding with caveat.
        caveat = None
        if not evaluation.sufficient:
            caveat = (
                "_Reviewed without full clarity — clarification rounds exhausted. "
                "Treat low-confidence findings with skepticism._"
            )

        return _review_and_post(session, engine, ctx, db, caveat=caveat)

    if status in {SessionStatus.REVIEW, SessionStatus.POST}:
        # Crash-recovery: finish where we left off by re-running review+post.
        ctx, _refs = _ingest_and_gather(session, db)
        return _review_and_post(session, engine, ctx, db, caveat=None)

    return AdvanceResult(session.id, status)


# ---------------------------------------------------------------- steps


def _ingest_and_gather(session: ReviewSession, db) -> tuple[ReviewContext, "PRRefs"]:
    """Fetch PR + providers + memory-cached repo snapshot.

    Returns the review context AND the resolved `PRRefs` so the driver can
    decide, from the refs found (or not found), whether to append a synthetic
    "please share a ticket/design-doc link" clarifying question.

    Commits after each phase transition so clients polling `GET /reviews/{id}`
    see progress in real time (otherwise the whole state machine runs in one
    transaction and status jumps straight from INGEST to DONE).
    """
    from services.context.providers import all_providers
    from services.context.providers.base import gather
    from services.context.providers.github_pr import fetch_pr_bundle
    from services.context.providers.linked_issues import extract_refs

    _transition(session, db, SessionStatus.GATHER_CONTEXT)
    bundle = fetch_pr_bundle(session.pr_url)
    session.head_sha = bundle.head_sha

    # Pull any URLs/ticket keys the author pasted into Slack/CLI replies into
    # the reference set so the next evaluate pass picks up the doc they shared.
    supplemental = _answered_round_text(session)

    refs = extract_refs(
        pr_url=session.pr_url,
        repo_slug=f"{bundle.owner}/{bundle.repo}",
        title=bundle.title,
        body=bundle.body + ("\n\n" + supplemental if supplemental else ""),
        branch_name="",
    )
    external_chunks = gather(all_providers(), refs, on_progress=_make_progress_cb(session, db))

    _transition(session, db, SessionStatus.EVALUATE_CONTEXT)
    ctx = ReviewContext(
        pr_url=session.pr_url,
        head_sha=bundle.head_sha,
        title=bundle.title,
        body=bundle.body,
        diff=bundle.diff,
        files=bundle.files,
        chunks=external_chunks,
        rounds=_build_rounds(session),
    )
    return ctx, refs


def _build_rounds(session: ReviewSession) -> list[RoundQA]:
    """Collect answered Slack/CLI clarification rounds as structured Q&A.

    Unanswered rounds are excluded — we only foreground what the model can
    actually learn from. Preserves chronological order so the prompt shows
    how understanding evolved across rounds.
    """
    rounds: list[RoundQA] = []
    for r in sorted(session.slack_rounds, key=lambda x: x.round_no):
        if r.answered_at is None:
            continue
        qs_raw = (r.questions_json or {}).get("questions", []) if isinstance(r.questions_json, dict) else []
        questions = [
            ClarificationQuestion(
                id=str(q.get("id") or ""),
                text=str(q.get("text") or ""),
                why_needed=str(q.get("why_needed") or ""),
            )
            for q in qs_raw
        ]
        answers = r.answers_json or {}
        if "_free_form" in answers:
            answer_text = str(answers["_free_form"])
        else:
            lines = []
            for q in questions:
                a = answers.get(q.id)
                if a:
                    lines.append(f"A{q.id}: {a}")
            answer_text = "\n".join(lines)
        rounds.append(RoundQA(round_no=r.round_no, questions=questions, answer_text=answer_text))
    return rounds


def _answered_round_text(session: ReviewSession) -> str:
    """Concatenate every answered Slack/CLI round's text for re-scanning."""
    parts: list[str] = []
    for r in session.slack_rounds:
        if r.answered_at is None or not r.answers_json:
            continue
        for v in r.answers_json.values():
            if isinstance(v, str) and v.strip():
                parts.append(v)
    return "\n".join(parts)


_JIRA_URL_HOSTS = ("atlassian.net/browse/", "jira.")
_CONFLUENCE_URL_HINTS = ("atlassian.net/wiki/", "confluence.", "viewpage.action")


def _refs_have_ticket_or_doc(refs) -> bool:
    """True if the PR already points at *some* external intent source.

    Any one of a Jira key, GitHub issue, or a Jira/Confluence URL is enough —
    the aim is to avoid nagging the author when they've already given us
    something to read.
    """
    if refs.jira_keys or refs.github_issues:
        return True
    for url in refs.urls:
        u = url.lower()
        if any(h in u for h in _JIRA_URL_HOSTS + _CONFLUENCE_URL_HINTS):
            return True
    return False


def _already_asked_for_doc(session: ReviewSession) -> bool:
    """True if any prior round already includes the share-ticket-or-doc question."""
    for r in session.slack_rounds:
        qs = (r.questions_json or {}).get("questions") or []
        for q in qs:
            if q.get("id") == "share_ticket_or_doc":
                return True
    return False


def _missing_doc_question() -> ClarificationQuestion:
    return ClarificationQuestion(
        id="share_ticket_or_doc",
        text=(
            "I couldn't find a Jira ticket or a Confluence/design-doc link in the "
            "PR description. Could you share at least one so I can review against "
            "intent? (Not a blocker — reply with 'none' if there isn't one.)"
        ),
        why_needed=(
            "A ticket or design doc tells me the 'why' behind the change. Without "
            "one I have to guess from the diff, which is how reviewers miss intent."
        ),
    )


def _questions_repeat(
    new_questions: list[ClarificationQuestion],
    prior_rounds: list[SlackRound],
) -> bool:
    """True if every new question textually overlaps with one already asked.

    Normalisation: lowercase + strip punctuation + token set. A Jaccard overlap
    ≥0.55 against any prior question counts as "same question". Only fires when
    at least one prior round has been answered (checked by the caller) — we
    never want to suppress the FIRST attempt at clarification.
    """
    if not new_questions:
        return False
    prior: list[set[str]] = []
    for r in prior_rounds:
        qs = (r.questions_json or {}).get("questions", []) if isinstance(r.questions_json, dict) else []
        for q in qs:
            prior.append(_normalise_tokens(q.get("text") or ""))
    if not prior:
        return False
    for nq in new_questions:
        nq_tokens = _normalise_tokens(nq.text)
        if not nq_tokens:
            continue
        if not any(_jaccard(nq_tokens, p) >= 0.55 for p in prior if p):
            return False
    return True


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "to", "for", "in", "on", "and", "or", "is", "are",
        "this", "that", "what", "which", "how", "does", "do", "be", "by", "as",
        "with", "from", "it", "its", "pr", "change",
    }
)


def _normalise_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_SPLIT.split(text.lower()) if t and t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _open_clarification_round(
    session: ReviewSession,
    questions: list[ClarificationQuestion],
    db,
) -> AdvanceResult:
    from services import slack_poster

    round_no = len(session.slack_rounds) + 1
    questions_json = {
        "questions": [
            {"id": q.id, "text": q.text, "why_needed": q.why_needed} for q in questions
        ]
    }

    if not session.slack_thread_ts:
        session.slack_thread_ts = slack_poster.open_pr_thread(
            pr_url=session.pr_url,
            session_id=session.id,
            engine=session.engine,
            model=_effective_model(session),
        )

    sr = SlackRound(
        session_id=session.id,
        round_no=round_no,
        questions_json=questions_json,
        thread_ts=session.slack_thread_ts,
    )
    db.add(sr)

    if session.slack_thread_ts:
        slack_poster.post_clarification_questions(
            session.slack_thread_ts,
            questions,
            round_no=round_no,
            author_slack_id=session.slack_author_id,
        )
    else:
        log.warning(
            "Slack not configured — clarification round %d open but unsent. "
            "Reply via `redink answer %s --text '...'` to resume.",
            round_no,
            session.id,
        )

    session.status = SessionStatus.AWAIT_SLACK_CLARIFICATION
    return AdvanceResult(session.id, session.status)


def _review_and_post(
    session: ReviewSession,
    engine: ReviewEngine,
    ctx: ReviewContext,
    db,
    *,
    caveat: str | None,
) -> AdvanceResult:
    from services import slack_poster
    from services.github_poster import post_review

    _transition(session, db, SessionStatus.REVIEW)
    findings = engine.review(ctx, on_progress=_make_progress_cb(session, db))

    _transition(session, db, SessionStatus.POST)
    rows: list[Finding] = []
    for f in findings:
        row = Finding(
            session_id=session.id,
            path=f.path,
            line=f.line,
            severity=f.severity,
            body=f.body,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    review_url = post_review(ctx, findings, rows, summary_caveat=caveat)

    _transition(session, db, SessionStatus.DONE)
    if session.slack_thread_ts and review_url:
        slack_poster.post_review_complete(
            session.slack_thread_ts,
            review_url=review_url,
            finding_count=len(findings),
        )

    return AdvanceResult(session.id, session.status, posted_review_url=review_url)


# ---------------------------------------------------------------- clarify


def record_clarification_answer(session_id: str, answers: dict[str, str]) -> None:
    """Persist the answer to the most recent open round WITHOUT re-driving.

    Separated from `submit_clarification` so API callers can acknowledge the
    POST quickly and run `advance()` as a background task. Raises `ValueError`
    when the session is unknown or has no pending round.
    """
    with db_session() as db:
        session = db.get(ReviewSession, session_id)
        if session is None:
            raise ValueError(f"unknown session {session_id}")

        pending = [r for r in session.slack_rounds if r.answered_at is None]
        if not pending:
            raise ValueError("no pending clarification round to answer")

        from datetime import UTC, datetime

        round_to_answer = max(pending, key=lambda r: r.round_no)
        round_to_answer.answers_json = answers
        round_to_answer.answered_at = datetime.now(UTC)
        session.status = SessionStatus.EVALUATE_CONTEXT
        db.flush()


def submit_clarification(session_id: str, answers: dict[str, str]) -> AdvanceResult:
    """Record + advance. Used by non-API callers that want synchronous behaviour."""
    record_clarification_answer(session_id, answers)
    return advance(session_id)


# ---------------------------------------------------------------- engagement (M3)


@dataclass
class EngagementResult:
    action: str  # concede | clarify | defend | escalate | skipped
    reason: str | None = None
    bot_reply_id: int | None = None


def handle_comment_reply(
    *,
    pr_url: str,
    parent_comment_id: int,
    reply_text: str,
    reply_author_login: str,
    reply_author_is_bot: bool,
) -> EngagementResult:
    """Engage (concede/clarify/defend/escalate) on a human reply to one of our inline comments.

    Called from the GitHub webhook. Silent no-op for bot replies (including our own)
    and for replies that don't match a finding we posted. Hard caps at
    `REDINK_MAX_COMMENT_ENGAGEMENT_ROUNDS` rounds per thread, then always escalates.
    """
    if reply_author_is_bot:
        log.debug("ignoring bot reply from %s", reply_author_login)
        return EngagementResult(action="skipped", reason="bot reply")

    with db_session() as db:
        finding = db.execute(
            select(Finding).where(Finding.posted_comment_id == parent_comment_id)
        ).scalar_one_or_none()
        if finding is None:
            return EngagementResult(action="skipped", reason="no matching finding")
        if finding.status in {"resolved", "escalated"}:
            return EngagementResult(action="skipped", reason=f"finding {finding.status}")

        session = db.get(ReviewSession, finding.session_id)
        if session is None or session.pr_url != pr_url:
            return EngagementResult(action="skipped", reason="session/pr mismatch")

        with pr_lock(db, session.pr_url) as got:
            if not got:
                return EngagementResult(action="skipped", reason="lock contended")

            reply_hash = _hash(reply_text)
            existing = db.execute(
                select(CommentThread)
                .where(CommentThread.finding_id == finding.id)
                .where(CommentThread.human_reply_hash == reply_hash)
            ).scalar_one_or_none()
            if existing is not None:
                return EngagementResult(action="skipped", reason="duplicate reply")

            rounds_done = db.execute(
                select(CommentThread).where(CommentThread.finding_id == finding.id)
            ).scalars().all()
            round_no = len(rounds_done) + 1
            max_rounds = settings().redink_max_comment_engagement_rounds

            if round_no > max_rounds:
                return _persist_engagement(
                    db,
                    finding=finding,
                    session=session,
                    round_no=round_no,
                    parent_comment_id=parent_comment_id,
                    human_reply=reply_text,
                    human_reply_hash=reply_hash,
                    action="escalate",
                    body=(
                        "This thread has hit the engagement cap. Flagging for a human "
                        "maintainer to weigh in."
                    ),
                )

            # Build fresh context and ask the engine for an action.
            from services.context.providers import all_providers
            from services.context.providers.base import gather
            from services.context.providers.github_pr import fetch_pr_bundle
            from services.context.providers.linked_issues import extract_refs

            bundle = fetch_pr_bundle(session.pr_url)
            if bundle.head_sha != session.head_sha:
                # Diff changed under us — don't keep litigating stale findings.
                return _persist_engagement(
                    db,
                    finding=finding,
                    session=session,
                    round_no=round_no,
                    parent_comment_id=parent_comment_id,
                    human_reply=reply_text,
                    human_reply_hash=reply_hash,
                    action="escalate",
                    body=(
                        "The PR head has moved since I posted this comment — not "
                        "engaging on a stale finding. Please re-run `redink review` "
                        "if you want a fresh pass."
                    ),
                )

            refs = extract_refs(
                pr_url=session.pr_url,
                repo_slug=f"{bundle.owner}/{bundle.repo}",
                title=bundle.title,
                body=bundle.body,
                branch_name="",
            )
            ctx = ReviewContext(
                pr_url=session.pr_url,
                head_sha=bundle.head_sha,
                title=bundle.title,
                body=bundle.body,
                diff=bundle.diff,
                files=bundle.files,
                chunks=gather(all_providers(), refs),
                rounds=_build_rounds(session),
            )
            engine = get_engine(session.engine, session.model)
            engine_finding = _finding_orm_to_dto(finding)
            action, body = engine.engage_on_reply(engine_finding, reply_text, ctx)
            return _persist_engagement(
                db,
                finding=finding,
                session=session,
                round_no=round_no,
                parent_comment_id=parent_comment_id,
                human_reply=reply_text,
                human_reply_hash=reply_hash,
                action=action,
                body=body,
            )


def _persist_engagement(
    db,
    *,
    finding: Finding,
    session: ReviewSession,
    round_no: int,
    parent_comment_id: int,
    human_reply: str,
    human_reply_hash: str,
    action: str,
    body: str,
) -> EngagementResult:
    from services.github_poster import post_comment_reply

    repo_slug, pr_number = _parse_pr_slug_and_number(session.pr_url)
    try:
        bot_reply_id = post_comment_reply(repo_slug, pr_number, parent_comment_id, body)
    except Exception:
        log.exception("failed to post reply on %s#%s", repo_slug, parent_comment_id)
        bot_reply_id = None

    db.add(
        CommentThread(
            finding_id=finding.id,
            round_no=round_no,
            parent_comment_id=parent_comment_id,
            human_reply=human_reply[:8000],
            human_reply_hash=human_reply_hash,
            engine_action=action,
            engine_response=body[:8000],
            bot_reply_id=bot_reply_id,
        )
    )
    if action == "concede":
        finding.status = "resolved"
    elif action == "escalate":
        finding.status = "escalated"
    # "clarify" and "defend" leave the finding open for another round.

    return EngagementResult(action=action, bot_reply_id=bot_reply_id)


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.strip().encode()).hexdigest()


def _finding_orm_to_dto(row: Finding):
    from services.engines.base import Finding as FindingDto

    return FindingDto(path=row.path, line=row.line, body=row.body, severity=row.severity)


def _parse_pr_slug_and_number(pr_url: str) -> tuple[str, int]:
    from services.github_app import parse_pr_url

    ref = parse_pr_url(pr_url)
    return ref.slug, ref.number
