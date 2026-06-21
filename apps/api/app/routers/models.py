"""Model routing inspection endpoints.

Only redacted provider status is exposed here: clients can see whether a
provider is configured and what fallback order is active, never the API keys.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.gateway import pareto

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("/pareto")
async def pareto_status() -> dict[str, Any]:
    """Return current Pareto-selected model fallback state with secrets redacted."""
    return pareto.redacted_status()
