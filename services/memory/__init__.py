"""Pluggable memory layer.

Two implementations, selected at runtime:

  - **SQLite** (default) — stores entries in the existing Redink DB
    (`memory_entries` table). Zero external dependencies; works on the
    no-Docker POC path.
  - **Mempalace** (opt-in) — delegates to https://github.com/mempalace/mempalace
    when `REDINK_MEMPALACE_ENABLED=true` AND the `mempalace` package is
    importable. Uses its semantic search + local-first knowledge graph so
    repeat reviews on the same repo reuse previously-gathered context.

Callers never see the difference — both implement the `MemoryStore`
protocol and both enforce the same **always-check-staleness** contract:
`get(key, expected_etag)` returns `None` unless the stored etag matches.
`etag` is typically a commit SHA, so a stale cache entry is silently
bypassed rather than serving outdated repo knowledge.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from services.memory.store import MemoryStore, SqliteMemoryStore

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_memory() -> MemoryStore:
    """Return the active memory backend. Cached for process lifetime."""
    from services.config import settings

    s = settings()
    if s.redink_mempalace_enabled:
        try:
            from services.memory.mempalace_store import MempalaceMemoryStore

            store = MempalaceMemoryStore()
            log.info("memory backend: mempalace")
            return store
        except Exception:
            log.warning(
                "REDINK_MEMPALACE_ENABLED=true but mempalace is not importable "
                "or failed to initialise; falling back to SQLite memory.",
                exc_info=True,
            )

    log.info("memory backend: sqlite")
    return SqliteMemoryStore()


__all__ = ["MemoryStore", "get_memory"]
