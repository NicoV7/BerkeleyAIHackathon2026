"""Seed the Skill catalog + document the type chart (Agent 3: BALANCE).

Why this exists
---------------
Type / skill choices should *visibly* change battle outcomes. The damage engine
already supports per-skill power and a type-effectiveness chart, but the Skill
table ships empty. This script seeds a small, opinionated catalog of debate
"moves" — each with a ``type`` (drives the type chart) and a ``power`` (the
``skill_mult`` the damage formula multiplies in) — and documents the canonical
type chart in one place.

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
# Catalog data — (name, type, power, description, prompt_fragment, cost)
# --------------------------------------------------------------------------- #
#
# `type` is a DebateType value (uppercase). `power` is the skill_mult fed to
# compute_damage(): >1.0 hits harder, <1.0 is a softer/utility move. Each move's
# prompt_fragment is what gets injected into the debater's system prompt when the
# move is selected — so picking a skill changes *behavior*, not just the number.

SKILL_CATALOG: list[dict[str, Any]] = [
    {
        "name": "Syllogism Strike",
        "type": "LOGOS",
        "power": 1.2,
        "description": "A tight deductive chain that forces the conclusion.",
        "prompt_fragment": "Lay out a clean premise-premise-conclusion chain and dare them to deny a step.",
        "cost": 0,
    },
    {
        "name": "Data Barrage",
        "type": "LOGOS",
        "power": 1.0,
        "description": "Stack concrete figures the audience can picture.",
        "prompt_fragment": "Anchor every claim to a concrete number or measured example.",
        "cost": 0,
    },
    {
        "name": "Heartstring Pull",
        "type": "PATHOS",
        "power": 1.2,
        "description": "A vivid story that makes the stakes feel personal.",
        "prompt_fragment": "Tell a short, vivid story that makes the cost land emotionally.",
        "cost": 0,
    },
    {
        "name": "Moral Appeal",
        "type": "PATHOS",
        "power": 1.0,
        "description": "Frame the issue as a question of right and wrong.",
        "prompt_fragment": "Reframe the dispute as a clear moral choice the audience already feels.",
        "cost": 0,
    },
    {
        "name": "Authority Citation",
        "type": "ETHOS",
        "power": 1.2,
        "description": "Borrow the weight of a credible source or precedent.",
        "prompt_fragment": "Cite a credible authority or precedent and lean on its standing.",
        "cost": 0,
    },
    {
        "name": "Credibility Wall",
        "type": "ETHOS",
        "power": 0.9,
        "description": "Establish your own standing to deflect attacks.",
        "prompt_fragment": "Briefly establish your relevant standing, then turn it into a shield.",
        "cost": 0,
    },
    {
        "name": "Reframe Gambit",
        "type": "CHAOS",
        "power": 1.3,
        "description": "Yank the debate onto unexpected, favorable ground.",
        "prompt_fragment": "Reject the framing entirely and redefine what the debate is really about.",
        "cost": 1,
    },
    {
        "name": "Pattern Break",
        "type": "CHAOS",
        "power": 1.1,
        "description": "Disrupt the opponent's rhythm with a surprising pivot.",
        "prompt_fragment": "Break their rhythm with an unexpected pivot that resets the exchange.",
        "cost": 0,
    },
    {
        "name": "Probing Question",
        "type": "SOCRATIC",
        "power": 1.1,
        "description": "A sharp question that exposes a hidden assumption.",
        "prompt_fragment": "End your turn with one sharp question that exposes their hidden assumption.",
        "cost": 0,
    },
    {
        "name": "Steelman Trap",
        "type": "SOCRATIC",
        "power": 1.2,
        "description": "Build their best case, then collapse it from inside.",
        "prompt_fragment": "State the strongest version of their view, then dismantle exactly that.",
        "cost": 1,
    },
    {
        "name": "Rhetorical Flourish",
        "type": "RHETORIC",
        "power": 1.1,
        "description": "Win the framing with style and a memorable line.",
        "prompt_fragment": "Win the framing first with a crisp, memorable line that defines the terms.",
        "cost": 0,
    },
    {
        "name": "Analogy Volley",
        "type": "RHETORIC",
        "power": 1.0,
        "description": "A vivid analogy that makes the abstract concrete.",
        "prompt_fragment": "Use a vivid analogy to make an abstract point land instantly.",
        "cost": 0,
    },
]


def catalog() -> list[dict[str, Any]]:
    """Return a copy of the seed catalog (handy for tests / inspection)."""
    return [dict(row) for row in SKILL_CATALOG]


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

    rows = list(rows) if rows is not None else SKILL_CATALOG
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
            f"({len(SKILL_CATALOG)} total)."
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
