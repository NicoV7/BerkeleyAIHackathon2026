"""Economy award helpers (WS-2 reuse seam) — credit coins + grant items.

These mirror the ATOMIC SQL patterns already used by the economy router's buy
path (``UPDATE runs SET coins = coins + :n``; inventory ``INSERT ... ON CONFLICT
DO UPDATE``) so quest rewards (and any future grant flows) credit through the
SAME concurrency-safe path instead of duplicating ad-hoc read-then-write logic.

Public surface:
    await credit_coins(session, run_id, amount) -> int          # coins added
    await grant_item(session, run_id, item_key, qty=1) -> int   # owned qty after
    await award(session, run_id, spec) -> dict                  # pay a reward_spec

``spec`` is the quest ``reward_spec``: ``{"coins": int, "items": [item_key,...]}``.
Callers own the transaction boundary (we never commit) so the award composes
inside the same txn that recorded the completion event when desired; the world
router commits once after paying out.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def credit_coins(session: AsyncSession, run_id: str, amount: int) -> int:
    """Atomically credit ``amount`` coins to a run's wallet. Returns coins added.

    No read-then-write — ``coins = coins + :amt`` composes safely with the
    wallet/shop debit path. A non-positive amount is a no-op.
    """
    amount = int(amount or 0)
    if amount <= 0:
        return 0
    await session.execute(
        text("UPDATE runs SET coins = coins + :amt WHERE id = :rid"),
        {"amt": amount, "rid": run_id},
    )
    return amount


async def grant_item(
    session: AsyncSession, run_id: str, item_key: str, qty: int = 1
) -> int:
    """Atomically add ``qty`` of ``item_key`` to a run's inventory (upsert).

    Single round-trip ``INSERT ... ON CONFLICT DO UPDATE`` so concurrent grants
    never lose an update. Returns the resulting owned quantity (best-effort read).
    """
    qty = max(1, int(qty))
    await session.execute(
        text(
            "INSERT INTO player_inventory (id, run_id, item_key, qty) "
            "VALUES (:id, :rid, :ik, :n) "
            "ON CONFLICT (run_id, item_key) "
            "DO UPDATE SET qty = player_inventory.qty + :n"
        ),
        {"id": str(uuid.uuid4()), "rid": run_id, "ik": item_key, "n": qty},
    )
    res = await session.execute(
        text(
            "SELECT qty FROM player_inventory "
            "WHERE run_id = :rid AND item_key = :ik"
        ),
        {"rid": run_id, "ik": item_key},
    )
    row = res.first()
    return int(row[0]) if row is not None else qty


async def award(
    session: AsyncSession, run_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
    """Pay out a quest reward_spec: credit coins + grant each listed item.

    ``spec`` -> ``{"coins": int, "items": [item_key, ...]}``. Returns a summary
    ``{"coins": int, "items": {item_key: owned_qty}}``. Does NOT commit — the
    caller owns the transaction. Item grants are best-effort: a missing catalog
    key (FK violation) is skipped without aborting the whole award.
    """
    coins = await credit_coins(session, run_id, int(spec.get("coins") or 0))
    granted: dict[str, int] = {}
    for item_key in spec.get("items") or []:
        try:
            granted[item_key] = await grant_item(session, run_id, str(item_key), 1)
        except Exception:  # noqa: BLE001 — a bad item key must not lose the coins
            continue
    return {"coins": coins, "items": granted}
