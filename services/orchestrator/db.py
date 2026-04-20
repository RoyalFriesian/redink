from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from services.config import settings
from services.orchestrator.models import Base


def _build_engine():
    url = settings().database_url
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        # SQLAlchemy drops to a single-thread connection by default under SQLite.
        # FastAPI uses a thread pool for sync endpoints, so we disable the check.
        # This is safe here because we always use scoped sessions (`db_session`
        # context manager) that never leak a connection across threads.
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


_engine = _build_engine()
_SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


def is_sqlite() -> bool:
    return settings().database_url.startswith("sqlite")


def init_db() -> None:
    """Create tables + apply idempotent in-place schema patches.

    We're on `create_all` during M1. When a column is added to an existing
    model the fresh-install path is fine but existing SQLite files won't gain
    the column — so we also run a tiny set of `ADD COLUMN IF NOT EXISTS`-style
    patches here. Swap to Alembic once the schema genuinely stabilises.
    """
    Base.metadata.create_all(_engine)
    _patch_missing_columns()


def _patch_missing_columns() -> None:
    """Add columns introduced after a row may already exist. SQLite only."""
    if not is_sqlite():
        return
    from sqlalchemy import text

    # (table, column, DDL type spec) — additive, nullable, no default needed.
    patches = [
        ("review_sessions", "model", "VARCHAR(80)"),
    ]
    with _engine.begin() as conn:
        for table, column, spec in patches:
            cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


@contextmanager
def db_session() -> Iterator[Session]:
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
