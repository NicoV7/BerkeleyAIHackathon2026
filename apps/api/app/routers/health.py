"""Health router — reports db, redis, and gateway status (Wave 0 exit criterion)."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.db.session import engine
from app.gateway.gateway import gateway
from app.redis_state import ping as redis_ping
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    db_ok = False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False

    try:
        redis_ok = await redis_ping()
    except Exception:  # noqa: BLE001
        redis_ok = False

    gw = await gateway.health()
    status = "ok" if (db_ok and redis_ok and gw.get("ok")) else "degraded"
    return HealthResponse(status=status, db=db_ok, redis=redis_ok, gateway=gw)
