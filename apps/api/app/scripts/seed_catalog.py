"""Seed the Skill catalog + document the type chart (Agent 3: BALANCE).

Why this exists
---------------
Type / skill choices should *visibly* change battle outcomes. The damage engine
already supports per-skill power and a type-effectiveness chart, but the Skill
table ships empty. This script seeds the same Markdown-backed battle catalog the
game uses at runtime — each move has a ``type`` (drives the type chart) and a
``power`` (the ``skill_mult`` the damage formula multiplies in) — and documents
the canonical type chart in one place.

Design
------
  * Idempotent: safe to run repeatedly. Skills are upserted by unique ``name`` —
    a second run updates type/power/fragment in place and never creates dupes.
  * Failure-tolerant: if Postgres is unreachable it prints a clear message and
    exits 0, so it is safe on a bare host / CI.
  * Pure catalog data: the actual numbers (type chart, level curves) live in
    ``app.debate.damage`` and ``app.party.balance`` — this file only seeds rows
    and echoes the chart for humans.

Usage
-----
    # From apps/api, venv active (or `uv run`):
    python -m app.scripts.seed_catalog            # seed + print the type chart
    python -m app.scripts.seed_catalog --print    # just print the chart, no DB

It can also be imported and awaited from app startup / tests:

    from app.scripts.seed_catalog import seed_skill_catalog
    n_created, n_updated = await seed_skill_catalog(session)
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any, Iterable

from app.debate.damage import DEFAULT_TYPE_CHART

# --------------------------------------------------------------------------- #
# Catalog data — derived from app/skills/*.md
# --------------------------------------------------------------------------- #
#
# `type` is a DebateType value (uppercase). `power` is the skill_mult fed to
# compute_damage(): >1.0 hits harder, <1.0 is a softer/utility move. Each move's
# prompt_fragment is what gets injected into the debater's system prompt when the
# move is selected — so picking a skill changes *behavior*, not just the number.


def _seed_row_from_skill(skill: dict[str, Any]) -> dict[str, Any]:
    """Convert parsed Markdown skill metadata into the DB seed row shape."""
    return {
        "name": skill["name"],
        "type": str(skill["type"]).upper(),
        "power": float(skill.get("power", 1.0) or 1.0),
        "description": skill.get("description", ""),
        "prompt_fragment": skill.get("prompt_fragment", ""),
        "cost": int(skill.get("mp_cost", skill.get("cost", 0)) or 0),
    }


def _load_skill_catalog_rows() -> list[dict[str, Any]]:
    from app.debate.skill_engine import skill_catalog

    return [_seed_row_from_skill(skill) for skill in skill_catalog()]


SKILL_CATALOG: list[dict[str, Any]] = _load_skill_catalog_rows()


def catalog() -> list[dict[str, Any]]:
    """Return a copy of the seed catalog (handy for tests / inspection)."""
    return [dict(row) for row in _load_skill_catalog_rows()]


def format_type_chart(chart: dict[str, dict[str, float]] | None = None) -> str:
    """Human-readable dump of the type-effectiveness chart for docs/CLI."""
    chart = chart if chart is not None else DEFAULT_TYPE_CHART
    lines = ["Type chart (attacker -> defender = multiplier):"]
    for attacker in sorted(chart):
        pairs = ", ".join(
            f"{defender} x{mult}" for defender, mult in sorted(chart[attacker].items())
        )
        lines.append(f"  {attacker}: {pairs}")
    return "\n".join(lines)


async def seed_skill_catalog(
    session: Any, rows: Iterable[dict[str, Any]] | None = None
) -> tuple[int, int]:
    """Idempotently upsert the Skill catalog into the DB by unique ``name``.

    Returns ``(n_created, n_updated)``. Caller owns the transaction outside of a
    standalone run; we ``flush`` but only ``commit`` when run as a script (see
    :func:`main`). Re-running with the same catalog updates rows in place — never
    inserts duplicates.
    """
    from sqlalchemy import select

    from app.db.models import DebateType, Skill

    rows = list(rows) if rows is not None else catalog()
    n_created = 0
    n_updated = 0

    for row in rows:
        result = await session.execute(select(Skill).where(Skill.name == row["name"]))
        existing = result.scalar_one_or_none()
        dtype = DebateType(row["type"])
        if existing is None:
            session.add(
                Skill(
                    name=row["name"],
                    type=dtype,
                    description=row.get("description", ""),
                    prompt_fragment=row.get("prompt_fragment", ""),
                    power=float(row.get("power", 1.0)),
                    cost=int(row.get("cost", 0)),
                )
            )
            n_created += 1
        else:
            existing.type = dtype
            existing.description = row.get("description", "")
            existing.prompt_fragment = row.get("prompt_fragment", "")
            existing.power = float(row.get("power", 1.0))
            existing.cost = int(row.get("cost", 0))
            session.add(existing)
            n_updated += 1

    await session.flush()
    return n_created, n_updated


async def main(print_only: bool = False) -> None:
    print(format_type_chart())
    if print_only:
        return

    try:
        from app.db.session import SessionLocal
    except Exception as exc:  # noqa: BLE001
        print(f"\nDB layer unavailable ({exc}); skipping seed.")
        return

    try:
        async with SessionLocal() as session:
            n_created, n_updated = await seed_skill_catalog(session)
            await session.commit()
        print(
            f"\nSeeded Skill catalog: {n_created} created, {n_updated} updated "
            f"({len(catalog())} total)."
        )
    except Exception as exc:  # noqa: BLE001 - bare host / CI without Postgres
        print(f"\nPostgres unreachable ({exc}); seed skipped. Safe to retry later.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the debate Skill catalog.")
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Only print the type chart; do not touch the database.",
    )
    args = parser.parse_args()
    asyncio.run(main(print_only=args.print_only))
