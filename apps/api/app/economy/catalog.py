"""Starter item catalog + idempotent seed (WS-1).

``STARTER_ITEMS`` is the hand-curated item catalog (HP/MP potions, a camp token,
and three training stat-ups). ``DEFAULT_SHOP`` stocks the demo merchant NPC.

``seed_economy(session)`` upserts both idempotently — safe to rerun on every
startup (matches the ``upsert_personas`` pattern in ``app.party.personas_seed``):
INSERT-or-update keyed on the natural PK / unique constraint, so re-running never
duplicates rows. It is wired into the app lifespan in ``app.main``.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Item, ItemKind, ShopStock

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

#: Each row mirrors the ``Item`` table columns. ``effect`` is interpreted by the
#: economy router's ``use`` path: potions read ``hp``/``mp`` (restore amount),
#: training items read ``atk``/``def``/``mp`` (permanent stat gain), camp_token
#: carries no in-line effect (it's banked + consumed by the camp/rest flow).
STARTER_ITEMS: list[dict[str, Any]] = [
    {
        "key": "potion_hp_small",
        "name": "Small HP Potion",
        "kind": ItemKind.potion_hp,
        "effect": {"hp": 40},
        "price": 25,
    },
    {
        "key": "potion_mp_small",
        "name": "Small MP Potion",
        "kind": ItemKind.potion_mp,
        "effect": {"mp": 30},
        "price": 25,
    },
    {
        "key": "camp_token",
        "name": "Camp Token",
        "kind": ItemKind.camp_token,
        "effect": {},
        "price": 40,
    },
    {
        "key": "training_atk",
        "name": "Whetstone (ATK +3)",
        "kind": ItemKind.training_atk,
        "effect": {"atk": 3},
        "price": 60,
    },
    {
        "key": "training_def",
        "name": "Aegis Tome (DEF +3)",
        "kind": ItemKind.training_def,
        "effect": {"def": 3},
        "price": 60,
    },
    {
        "key": "training_mp",
        "name": "Focus Charm (Max MP +10)",
        "kind": ItemKind.training_mp,
        "effect": {"mp": 10},
        "price": 60,
    },
]

#: Demo merchant stock. Each row: (item_key, price-override, starting qty).
#: ``price`` here overrides the catalog price for this vendor (kept equal in the
#: seed, but the column exists so NPCs can discount/markup independently).
DEFAULT_SHOP_NPC = "merchant"
DEFAULT_SHOP: list[dict[str, Any]] = [
    {"item_key": "potion_hp_small", "price": 25, "qty": 99},
    {"item_key": "potion_mp_small", "price": 25, "qty": 99},
    {"item_key": "camp_token", "price": 40, "qty": 20},
    {"item_key": "training_atk", "price": 60, "qty": 5},
    {"item_key": "training_def", "price": 60, "qty": 5},
    {"item_key": "training_mp", "price": 60, "qty": 5},
]


# ---------------------------------------------------------------------------
# Idempotent seed
# ---------------------------------------------------------------------------


async def upsert_items(session: AsyncSession) -> int:
    """Insert/refresh the item catalog. Returns the number of rows touched.

    Idempotent — keyed on ``Item.key``; re-running updates name/kind/effect/price
    in place rather than duplicating.
    """
    existing = {
        i.key for i in (await session.execute(select(Item))).scalars().all()
    }
    touched = 0
    for row in STARTER_ITEMS:
        if row["key"] in existing:
            item = await session.get(Item, row["key"])
            if item is None:
                continue
            for field, value in row.items():
                setattr(item, field, value)
            session.add(item)
        else:
            session.add(Item(**row))
        touched += 1
    await session.commit()
    return touched


async def upsert_shop(session: AsyncSession, npc_id: str = DEFAULT_SHOP_NPC) -> int:
    """Insert the default shop stock for ``npc_id``. Returns rows touched.

    Idempotent on (npc_id, item_key): an existing row's *price* is refreshed but
    its *qty* is left untouched (so a rerun never refills a depleted shop). Only
    brand-new (npc, item) pairs get the seed quantity.
    """
    res = await session.execute(
        select(ShopStock).where(ShopStock.npc_id == npc_id)
    )
    existing = {row.item_key: row for row in res.scalars().all()}
    touched = 0
    for row in DEFAULT_SHOP:
        cur = existing.get(row["item_key"])
        if cur is not None:
            # Refresh price only — never overwrite the live (possibly depleted) qty.
            cur.price = row["price"]
            session.add(cur)
        else:
            session.add(
                ShopStock(
                    npc_id=npc_id,
                    item_key=row["item_key"],
                    price=row["price"],
                    qty=row["qty"],
                )
            )
        touched += 1
    await session.commit()
    return touched


async def seed_economy(session: AsyncSession) -> int:
    """Seed the item catalog + default shop. Returns total rows touched."""
    n = await upsert_items(session)
    n += await upsert_shop(session)
    return n
