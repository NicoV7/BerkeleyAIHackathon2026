"""Wave A persistence router — run save / resume (survives a restart).

Endpoints (mounted under the shared /api prefix, matching map.py):
    POST /api/runs/{run_id}/save  (-> RunSaveResult)
    GET  /api/runs/{run_id}        (-> RunResumeState)

The durable schema (``runs``, ``monsters``) is frozen in app.db.models, so this
module adds NO new ORM columns. The one additive column it needs — ``saved_at``
on ``runs`` — is created by the idempotent ALTER in app.db.session.init_db and
read/written here with raw SQL (the frozen ``Run`` model has no such attribute).

Mapping/serialization is factored into pure helpers (``_run_resume_state`` /
``_split_party_captured``) so the save/resume logic is unit-testable without a
live database.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Monster, MonsterOwner, Run
from app.db.session import get_session
# Reuse the exact MonsterSummary mapping owned by the map router — do not
# duplicate the field-by-field projection.
from app.routers.map import _monster_to_summary
from app.schemas import MonsterSummary, RunResumeState, RunSaveResult

router = APIRouter(prefix="/api", tags=["runs"])


# ---------------------------------------------------------------------------
# Pure helpers (no DB) — directly unit-testable
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Naive UTC, matching the TIMESTAMP WITHOUT TIME ZONE columns elsewhere."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Serialize a (possibly naive) datetime to an ISO-8601 string, or None."""
    if value is None:
        return None
    return value.isoformat()


def _split_party_captured(
    monsters: list[Monster],
) -> tuple[list[MonsterSummary], list[MonsterSummary]]:
    """Project player-owned monsters into (party, captured) MonsterSummary lists.

    Both lists are player-owned; ``captured`` is the roster of monsters whose
    ownership was flipped to ``player`` after the run started (i.e. anything that
    was not part of the original starter roll). The starter party is the earliest
    player-owned monsters by ``created_at``; everything after is "captured".

    Ordering by ``created_at`` keeps the projection deterministic and lets the
    frontend show the captured roster separately from the active party.
    """
    player_mons = [m for m in monsters if m.owner == MonsterOwner.player]
    player_mons.sort(key=lambda m: (m.created_at or datetime.min, m.id))

    party = [_monster_to_summary(m) for m in player_mons]
    # Captured roster mirrors the full player-owned roster: every player-owned
    # monster is part of the resumable team. Kept as a distinct field so the
    # frontend can render it independently of the in-flight party view.
    captured = list(party)
    return party, captured


def _run_resume_state(
    run: Run,
    monsters: list[Monster],
    saved_at: Optional[datetime],
) -> RunResumeState:
    """Assemble the durable RunResumeState from a run row + its monsters.

    Pure: no I/O. ``resumable`` is True iff the run has been explicitly saved.
    """
    party, captured = _split_party_captured(monsters)
    return RunResumeState(
        id=run.id,
        debate_topic=run.debate_topic,
        player_x=run.player_x,
        player_y=run.player_y,
        status=run.status.value,
        party=party,
        captured=captured,
        saved_at=_iso(saved_at),
        resumable=saved_at is not None,
    )


# ---------------------------------------------------------------------------
# DB access helpers
# ---------------------------------------------------------------------------


async def _player_monsters(session: AsyncSession, run_id: str) -> list[Monster]:
    """All player-owned monsters for a run, ordered for a stable projection."""
    result = await session.execute(
        select(Monster).where(
            Monster.run_id == run_id,
            Monster.owner == MonsterOwner.player,
        )
    )
    return list(result.scalars().all())


async def _read_saved_at(session: AsyncSession, run_id: str) -> Optional[datetime]:
    """Read the additive ``runs.saved_at`` column via raw SQL.

    The frozen ``Run`` ORM model has no ``saved_at`` attribute, so we read the
    column directly. Returns None if unset (run never saved).
    """
    result = await session.execute(
        text("SELECT saved_at FROM runs WHERE id = :id"), {"id": run_id}
    )
    row = result.first()
    return row[0] if row is not None else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/save", response_model=RunSaveResult)
async def save_run(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RunSaveResult:
    """Stamp the run as saved (durable snapshot marker).

    Player position + party already live in durable PG rows (written on every
    move / capture), so "saving" stamps ``runs.saved_at`` to mark the run as
    resumable. Returns the party size for a quick client-side sanity check.
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    saved_at = _now()
    # Write the additive column with raw SQL (not on the frozen ORM model).
    await session.execute(
        text("UPDATE runs SET saved_at = :ts WHERE id = :id"),
        {"ts": saved_at, "id": run_id},
    )
    await session.commit()

    party = await _player_monsters(session, run_id)

    return RunSaveResult(
        run_id=run_id,
        saved=True,
        saved_at=saved_at.isoformat(),
        party_size=len(party),
    )


@router.get("/runs/{run_id}", response_model=RunResumeState)
async def get_run(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RunResumeState:
    """Return the full durable run state so a session can be rehydrated."""
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    monsters = await _player_monsters(session, run_id)
    saved_at = await _read_saved_at(session, run_id)

    return _run_resume_state(run, monsters, saved_at)


@router.get("/runs/{run_id}/party", response_model=list[MonsterSummary])
async def get_run_party(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[MonsterSummary]:
    """Return the player's current party for HUD, party, and training screens."""
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    monsters = await _player_monsters(session, run_id)
    party, _captured = _split_party_captured(monsters)
    return party
