"""Database engine, session factory and schema creation."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base, Region

_url = settings.database_url
_connect_args: dict[str, Any] = {"check_same_thread": False} if _url.startswith("sqlite") else {}

engine: Engine = create_engine(_url, connect_args=_connect_args, future=True)


if _url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        """Turn on foreign keys so cascades actually fire, and WAL for concurrency."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)


def seed_regions() -> None:
    """Insert any seeded region that is not already present. Idempotent."""
    seeds = settings.parsed_regions()
    if not seeds:
        return
    with SessionLocal() as db:
        existing = {r.name for r in db.scalars(select(Region)).all()}
        added = False
        for seed in seeds:
            name = str(seed["name"])
            if name in existing:
                continue
            db.add(
                Region(
                    name=name,
                    display_name=name.replace("-", " ").title(),
                    gpu_quota=int(seed["gpu_quota"]),  # type: ignore[arg-type]
                    gpu_used=int(seed["gpu_used"]),  # type: ignore[arg-type]
                    usd_per_gpu_hour=str(seed["usd_per_gpu_hour"]),
                )
            )
            added = True
        if added:
            db.commit()


def init_db() -> None:
    create_schema()
    seed_regions()
