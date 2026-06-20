"""FastAPI entrypoint.

Mounts the health router (Wave 0) and tolerantly auto-mounts Wave 1 routers as
they land. Each workstream drops a module in app/routers/ exposing `router`;
list its name in OPTIONAL_ROUTERS and it mounts when present — no merge conflict
on a shared include list.
"""
from __future__ import annotations

import importlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.session import init_db
from app.gateway.gateway import gateway
from app.routers import health

log = logging.getLogger("uvicorn.error")

# Wave 1 workstreams add their router module name here (or it's tried anyway).
OPTIONAL_ROUTERS = ["map", "encounter", "debate", "party", "memory", "capture", "training"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log.info("DB initialized")
    yield
    await gateway.aclose()


app = FastAPI(title="Debate RPG API", version="0.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)

for name in OPTIONAL_ROUTERS:
    try:
        mod = importlib.import_module(f"app.routers.{name}")
        app.include_router(mod.router)
        log.info("Mounted router: %s", name)
    except ModuleNotFoundError:
        log.info("Router not present yet (skipping): %s", name)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to mount router %s: %s", name, e)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "debate-rpg-api", "docs": "/docs", "health": "/api/health"}
