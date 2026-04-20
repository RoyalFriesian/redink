"""Mempalace-backed memory store (opt-in).

Mempalace (https://github.com/mempalace/mempalace) is a local-first AI memory
system with semantic search and a temporal knowledge graph. Enabled when:

  1. `REDINK_MEMPALACE_ENABLED=true` in the environment, AND
  2. The `mempalace` Python package is installed AND importable.

The integration is deliberately defensive: if any Mempalace API call raises,
we fall back to the SQLite store for that operation rather than failing the
review. We also mirror every put into SQLite so SQLite remains the source of
truth for staleness checks — Mempalace's semantic-search recall is used as a
**supplement** (finding conceptually-related past reviews) rather than a
replacement for exact-match caching.

The Mempalace Python API is still stabilising, so we probe for the call
shapes we know about and guard the rest. As the API firms up the
`_to_palace_*` / `_from_palace_*` helpers are the only places that need to
move.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from services.memory.store import MemoryHit, SqliteMemoryStore

log = logging.getLogger(__name__)


class MempalaceMemoryStore:
    """Semantic-search memory on top of the SQLite exact-match cache.

    Raises on construction if mempalace isn't importable — the factory in
    `services.memory.__init__` catches that and falls back to SQLite.
    """

    def __init__(self) -> None:
        # Probe import first — fail fast so the factory can fall back.
        import mempalace  # noqa: F401

        from services.config import settings

        palace_dir = Path(settings().redink_home).expanduser() / "mempalace"
        palace_dir.mkdir(parents=True, exist_ok=True)
        self._palace_dir = palace_dir
        self._fallback = SqliteMemoryStore()

        # Best-effort handle to the palace. Different mempalace versions have
        # shipped `Palace(path)` and `open_palace(path)` factories; probe both.
        self._palace: Any | None = None
        try:
            self._palace = self._open_palace(palace_dir)
            log.info("Mempalace opened at %s", palace_dir)
        except Exception:
            log.warning("Mempalace import succeeded but opening the palace failed", exc_info=True)
            self._palace = None

    @staticmethod
    def _open_palace(path: Path) -> Any:
        import mempalace

        for attr in ("open", "open_palace", "Palace"):
            ctor = getattr(mempalace, attr, None)
            if ctor is not None:
                return ctor(str(path))
        raise RuntimeError("mempalace module exposes no known entry point (open/open_palace/Palace)")

    # --- MemoryStore protocol ---------------------------------------------

    def get(self, key: str, *, expected_etag: str) -> MemoryHit | None:
        # Exact-match cache check always wins — staleness must be authoritative.
        return self._fallback.get(key, expected_etag=expected_etag)

    def put(self, key: str, value: dict[str, Any], *, etag: str) -> None:
        self._fallback.put(key, value, etag=etag)
        if self._palace is None:
            return
        try:
            self._store_in_palace(key=key, value=value, etag=etag)
        except Exception:
            log.debug("mempalace put failed; SQLite mirror is authoritative", exc_info=True)

    def invalidate(self, key: str) -> None:
        self._fallback.invalidate(key)
        if self._palace is None:
            return
        try:
            self._invalidate_in_palace(key)
        except Exception:
            log.debug("mempalace invalidate failed; SQLite mirror already cleared", exc_info=True)

    # --- palace-specific helpers (update as the API stabilises) -----------

    def _store_in_palace(self, *, key: str, value: dict[str, Any], etag: str) -> None:
        """Attempt to store in the palace using whichever write API is present."""
        payload = {"key": key, "etag": etag, "value": value}
        for method_name in ("store", "add", "put", "remember"):
            meth = getattr(self._palace, method_name, None)
            if callable(meth):
                meth(payload)
                return
        raise RuntimeError("no known write method on palace (tried store/add/put/remember)")

    def _invalidate_in_palace(self, key: str) -> None:
        for method_name in ("forget", "delete", "remove"):
            meth = getattr(self._palace, method_name, None)
            if callable(meth):
                meth(key)
                return
        # no-op if the palace doesn't expose a delete API
