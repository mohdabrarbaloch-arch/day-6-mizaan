"""Health and readiness."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from ..config import settings
from ..database import engine
from ..schemas import HealthOut

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """Liveness plus an honest statement of what is and is not wired up.

    `backend` reports "none" when no inference endpoint is configured. It does
    not claim a model server exists when it does not.
    """
    db_status = "ok"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any failure means "not ok"
        db_status = "unavailable"

    return HealthOut(
        status="ok" if db_status == "ok" else "degraded",
        version=settings.version,
        backend="configured" if settings.llm_base_url else "none",
        database=db_status,
    )
