"""Per-PR lock to keep two entry points from reviewing the same PR concurrently.

Dialect-aware:
  - **Postgres**: `pg_try_advisory_lock` — true cross-process lock, survives a
    restart of one worker, released on connection close. Required in any
    multi-process deployment.
  - **SQLite (POC)**: process-local `threading.Lock` keyed on the PR URL.
    Good enough when there's only one api process running (the bootstrap's
    no-docker path). If a second process ever speaks to the same SQLite file,
    this degrades to "two concurrent reviews" — acceptable for POC, not prod.

Callers never see the difference — both yield a boolean indicating whether
they got the lock.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.orchestrator.db import is_sqlite

# In-process lock table for the SQLite path. Keyed on PR URL; the outer lock
# protects the dict itself, inner values are per-PR re-entrant locks.
_local_lock_table_guard = threading.Lock()
_local_locks: dict[str, threading.Lock] = {}


def _pg_key(pr_url: str) -> int:
    """Map a PR URL to a stable signed 64-bit int for pg_try_advisory_lock."""
    digest = hashlib.sha256(pr_url.encode()).digest()[:8]
    val = int.from_bytes(digest, "big", signed=False)
    return val - (1 << 63) if val >= (1 << 63) else val


def _get_local_lock(pr_url: str) -> threading.Lock:
    with _local_lock_table_guard:
        lock = _local_locks.get(pr_url)
        if lock is None:
            lock = threading.Lock()
            _local_locks[pr_url] = lock
        return lock


@contextmanager
def pr_lock(db: Session, pr_url: str) -> Iterator[bool]:
    """Try to acquire the per-PR lock. Yields True if acquired, False if not.

    Released automatically on context exit.
    """
    if is_sqlite():
        lock = _get_local_lock(pr_url)
        got = lock.acquire(blocking=False)
        try:
            yield got
        finally:
            if got:
                lock.release()
        return

    key = _pg_key(pr_url)
    got = db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
    try:
        yield bool(got)
    finally:
        if got:
            db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
