"""Periodic sweeper — nudges stale clarification rounds and times them out.

Runs as a separate process (`python -m ops.sweeper`) or as a cron job. The sweeper
is the counterpart to the PASSIVE `AWAIT_SLACK_CLARIFICATION` state: without it,
sessions whose authors never reply would sit open forever.

Behaviour:
  - After NUDGE_AFTER_H hours with no reply: post a polite nudge in the thread.
  - After TIMEOUT_AFTER_H hours: treat the round as "no answer" and resume the
    review with a caveat summary.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from services.config import settings
from services.orchestrator.db import db_session
from services.orchestrator.models import ReviewSession, SessionStatus

log = logging.getLogger(__name__)

NUDGE_AFTER_H = float(os.environ.get("REDINK_NUDGE_AFTER_H", "4"))
TIMEOUT_AFTER_H = float(os.environ.get("REDINK_TIMEOUT_AFTER_H", "48"))
SLEEP_SECONDS = int(os.environ.get("REDINK_SWEEPER_SLEEP_S", "300"))


def sweep_once() -> None:
    from services import slack_poster
    from services.orchestrator.state_machine import submit_clarification

    now = datetime.now(UTC)
    with db_session() as db:
        awaiting = db.execute(
            select(ReviewSession).where(
                ReviewSession.status == SessionStatus.AWAIT_SLACK_CLARIFICATION
            )
        ).scalars().all()
        ids_to_timeout: list[str] = []
        nudged_threads: list[str] = []
        for s in awaiting:
            pending = [r for r in s.slack_rounds if r.answered_at is None]
            if not pending:
                continue
            latest = max(pending, key=lambda r: r.round_no)
            age = now - _aware(latest.asked_at)
            if age > timedelta(hours=TIMEOUT_AFTER_H):
                ids_to_timeout.append(s.id)
            elif age > timedelta(hours=NUDGE_AFTER_H) and s.slack_thread_ts:
                nudged_threads.append(s.slack_thread_ts)

    for ts in nudged_threads:
        slack_poster.post_status(
            ts,
            ":bell: Still waiting on clarification — Redink will proceed without "
            f"full context in {int(TIMEOUT_AFTER_H - NUDGE_AFTER_H)}h if no reply.",
        )

    for sid in ids_to_timeout:
        log.info("timing out session %s; proceeding with empty answers", sid)
        try:
            submit_clarification(sid, {"_free_form": "(no reply — timed out)"})
        except Exception:
            log.exception("timeout resume failed for %s", sid)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def run() -> None:
    logging.basicConfig(level=settings().redink_log_level)
    log.info("sweeper up; interval=%ds", SLEEP_SECONDS)
    while True:
        try:
            sweep_once()
        except Exception:
            log.exception("sweeper tick failed")
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    run()
