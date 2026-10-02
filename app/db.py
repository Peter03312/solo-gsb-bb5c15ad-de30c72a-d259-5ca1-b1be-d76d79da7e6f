"""Database engine, startup readiness wait and schema initialisation."""
from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from .config import settings

log = logging.getLogger("radio.db")

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def wait_for_database(timeout_seconds: float = 60.0) -> None:
    """Block until the database accepts connections (Compose startup race)."""
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            log.info("database is ready after %d attempt(s)", attempt)
            return
        except OperationalError:
            if time.monotonic() >= deadline:
                raise
            log.info("waiting for database (attempt %d) ...", attempt)
            time.sleep(1.0)


def _ensure_guard_band_column() -> None:
    """Add ``revision_meta.guard_band`` to databases created before it existed.

    ``create_all`` never alters existing tables, so the column is added with
    a plain idempotent ``ALTER TABLE``; its value is backfilled in
    ``init_db`` from the ``GUARD_BAND`` environment variable, which was the
    operative setting before online configuration existed.
    """
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("revision_meta")}
    if "guard_band" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE revision_meta ADD COLUMN guard_band FLOAT")
        )


def _ensure_suspended_revision_column() -> None:
    """Add ``applications.suspended_revision`` to older databases.

    Mirrors :func:`_ensure_guard_band_column`: ``create_all`` only creates
    missing tables, so the nullable column marking a temporary outage is
    added idempotently; existing rows simply start as NULL (not suspended).
    """
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("applications")}
    if "suspended_revision" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE applications ADD COLUMN suspended_revision INTEGER")
        )


def _ensure_approval_event_waitlist_columns() -> None:
    """Add waitlist-era columns to ``approval_events`` on older databases.

    ``auto_approved`` marks approvals granted automatically while draining
    the standby queue on a suspend/revoke; ``release_revision`` records which
    release revision triggered that auto approval.  Both are added
    idempotently; historical manual approvals start as ``0`` / NULL.
    """
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("approval_events")}
    with engine.begin() as conn:
        if "auto_approved" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE approval_events "
                    "ADD COLUMN auto_approved BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        if "release_revision" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE approval_events "
                    "ADD COLUMN release_revision INTEGER"
                )
            )


def init_db() -> None:
    """Create tables and seed the singleton revision row (revision 0).

    The guard band is seeded from ``GUARD_BAND`` only on a brand-new
    database (or backfilled once into databases created before online
    guard-band configuration existed).  An already-effective setting is
    never overwritten at startup: restarts keep the live value.
    """
    # Imported here so models register on Base.metadata before create_all().
    from . import models  # noqa: F401
    from .models import Base, GuardBandEvent, RevisionMeta

    Base.metadata.create_all(bind=engine)
    _ensure_guard_band_column()
    _ensure_suspended_revision_column()
    _ensure_approval_event_waitlist_columns()
    with SessionLocal() as session, session.begin():
        meta = session.get(RevisionMeta, 1)
        if meta is None:
            meta = RevisionMeta(
                id=1, revision=0, guard_band=settings.guard_band
            )
            session.add(meta)
            session.flush()
        elif meta.guard_band is None:
            # Database predates online guard-band configuration: the
            # GUARD_BAND env var was the operative setting, adopt it once.
            meta.guard_band = settings.guard_band
        seed = (
            session.query(GuardBandEvent)
            .filter(GuardBandEvent.revision == 0)
            .first()
        )
        if seed is None:
            # Initial-value record so every historical revision snapshot can
            # restore the band in force at that time.
            session.add(
                GuardBandEvent(
                    revision=0,
                    previous_guard_band=None,
                    guard_band=meta.guard_band,
                )
            )
