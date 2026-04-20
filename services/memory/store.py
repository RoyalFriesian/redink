"""Memory store protocol + SQLite-backed default implementation.

Contract:
  - `get(key, expected_etag)` — returns the stored value ONLY if its etag
    matches `expected_etag`. Callers pass the current upstream etag (usually
    a git SHA). Mismatched entries return `None`, which forces a refetch and
    a subsequent `put()` — so stale data never silently makes it into a
    review.
  - `put(key, value, etag)` — upserts by `key`. Rewriting updates `etag` and
    `captured_at`.

`key` is a free-form string namespaced by the caller (e.g.
`repo_snapshot:hotstar/mono-argus`). `value` is any JSON-serialisable dict.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import delete

from services.orchestrator.db import db_session
from services.orchestrator.models import MemoryEntry

log = logging.getLogger(__name__)


@dataclass
class MemoryHit:
    value: dict[str, Any]
    etag: str
    captured_at: datetime


class MemoryStore(Protocol):
    def get(self, key: str, *, expected_etag: str) -> MemoryHit | None: ...

    def put(self, key: str, value: dict[str, Any], *, etag: str) -> None: ...

    def invalidate(self, key: str) -> None: ...


class SqliteMemoryStore:
    """Default memory backend — rows in the `memory_entries` table.

    Works with any SQLAlchemy-supported DB (SQLite on POC, Postgres in prod).
    The table name lies: it's also used under Postgres.
    """

    def get(self, key: str, *, expected_etag: str) -> MemoryHit | None:
        with db_session() as db:
            row = db.get(MemoryEntry, key)
            if row is None:
                return None
            if row.etag != expected_etag:
                log.debug(
                    "memory miss (stale): key=%s cached=%s wanted=%s",
                    key,
                    row.etag,
                    expected_etag,
                )
                return None
            row.last_used_at = datetime.now(UTC)
            return MemoryHit(value=row.value_json, etag=row.etag, captured_at=row.captured_at)

    def put(self, key: str, value: dict[str, Any], *, etag: str) -> None:
        size = len(json.dumps(value, default=str))
        with db_session() as db:
            row = db.get(MemoryEntry, key)
            if row is None:
                row = MemoryEntry(
                    key=key,
                    etag=etag,
                    value_json=value,
                    size_bytes=size,
                )
                db.add(row)
            else:
                row.etag = etag
                row.value_json = value
                row.size_bytes = size
                row.captured_at = datetime.now(UTC)
                row.last_used_at = row.captured_at

    def invalidate(self, key: str) -> None:
        with db_session() as db:
            db.execute(delete(MemoryEntry).where(MemoryEntry.key == key))
