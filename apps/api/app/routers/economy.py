"""Economy router (WS-1) — coins, inventory, item use, and the NPC shop.

Endpoints (mounted under the shared /api prefix):
    GET  /api/runs/{run_id}/wallet                 -> WalletState
    GET  /api/runs/{run_id}/inventory              -> list[InventoryItem]
    POST /api/runs/{run_id}/inventory/use          UseItemRequest  -> UseItemResult
    GET  /api/shop/{npc_id}                         -> ShopState
    POST /api/shop/{npc_id}/buy?run_id=...          BuyItemRequest  -> BuyItemResult

ATOMICITY CONTRACT (the whole point of this module):
    Every balance/stock mutation is a single conditional SQL statement —
    ``UPDATE ... SET col = col - :n WHERE col >= :n`` — and the decision to
    accept/reject is made from ``result.rowcount``. We NEVER read-then-write a
    balance in Python, so two rapid/concurrent buys can never both succeed past
    the available coins or stock (the second UPDATE matches 0 rows and is
    rejected with a 4xx). Inventory increments use an ``INSERT ... ON CONFLICT
    DO UPDATE`` upsert so a buy is a single round-trip with no lost update.

The buy flow takes ``run_id`` as a query parameter (the path is keyed on the
NPC) and performs: atomic coin debit -> atomic stock decrement -> inventory
upsert, all inside one transaction; if the stock decrement fails after the coin
debit, the transaction is rolled back so coins are never lost.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Item, ItemKind, PlayerInventory, Run, ShopStock
from app.db.session import get_session
from app.economy.catalog import DEFAULT_SHOP_NPC, upsert_shop
from app.schemas import (
    BuyItemRequest,
    BuyItemResult,
    InventoryItem,
    ShopItem,
    ShopState,
    UseItemRequest,
    UseItemResult,
    WalletState,
)

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["economy"])

Session = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_run(session: AsyncSession, run_id: str) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


async def _coins(session: AsyncSession, run_id: str) -> int:
    """Read the current wallet balance via raw SQL (the Run model may be stale)."""
    res = await session.execute(
        text("SELECT coins FROM runs WHERE id = :rid"), {"rid": run_id}
    )
    row = res.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return int(row[0] or 0)


async def _inventory_qty(session: AsyncSession, run_id: str, item_key: str) -> int:
    res = await session.execute(
        text(
            "SELECT qty FROM player_inventory "
            "WHERE run_id = :rid AND item_key = :ik"
        ),
        {"rid": run_id, "ik": item_key},
    )
    row = res.first()
    return int(row[0]) if row is not None else 0


async def _shop_rows(session: AsyncSession, npc_id: str) -> list[tuple[ShopStock, Item]]:
    """Return joined stock rows for a shop, ordered for stable UI display."""
    res = await session.execute(
        select(ShopStock, Item)
        .join(Item, Item.key == ShopStock.item_key)
        .where(ShopStock.npc_id == npc_id)
        .order_by(Item.name)
    )
    return list(res.all())


async def _materialize_default_shop_if_known(
    session: AsyncSession, npc_id: str
) -> bool:
    """Seed default stock for a canonical merchant id on first visit."""
    if not _is_known_merchant(npc_id):
        return False
    await upsert_shop(session, npc_id)
    return True


def _is_known_merchant(npc_id: str) -> bool:
    """True when ``npc_id`` is the seed shop or a canonical merchant anchor."""
    if npc_id == DEFAULT_SHOP_NPC:
        return True
    try:
        from app.world import canonical as canonical_mod

        specs = []
        world = canonical_mod.get_canonical_world()
        if world is not None:
            specs.append(world.spec)
        if canonical_mod.INTERIORS_DIR.exists():
            for path in sorted(canonical_mod.INTERIORS_DIR.glob("*.json")):
                parts = path.stem.split("_")
                if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                    continue
                key = f"{parts[0]}:{parts[1]}:{parts[2]}"
                interior = canonical_mod.get_canonical_interior(key)
                if interior is not None:
                    specs.append(interior.spec)
        for spec in specs:
            for poi in spec.pois:
                for anchor in poi.npc_anchors:
                    if anchor.npc_id == npc_id and anchor.archetype == "merchant":
                        return True
    except Exception:  # noqa: BLE001 - unknown ids simply are not shopkeepers
        return False
    return False


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/wallet", response_model=WalletState)
async def get_wallet(run_id: str, session: Session) -> WalletState:
    """Return the run's coin balance."""
    coins = await _coins(session, run_id)
    return WalletState(run_id=run_id, coins=coins)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/inventory", response_model=list[InventoryItem])
async def get_inventory(run_id: str, session: Session) -> list[InventoryItem]:
    """List a run's owned items (qty > 0) joined with catalog metadata."""
    await _require_run(session, run_id)
    res = await session.execute(
        select(PlayerInventory, Item)
        .join(Item, Item.key == PlayerInventory.item_key)
        .where(PlayerInventory.run_id == run_id, PlayerInventory.qty > 0)
        .order_by(Item.name)
    )
    out: list[InventoryItem] = []
    for inv, item in res.all():
        out.append(
            InventoryItem(
                item_key=inv.item_key,
                name=item.name,
                kind=item.kind.value if hasattr(item.kind, "value") else str(item.kind),
                qty=inv.qty,
                effect=dict(item.effect or {}),
                price=item.price,
            )
        )
    return out


@router.post("/runs/{run_id}/inventory/use", response_model=UseItemResult)
async def use_item(
    run_id: str, body: UseItemRequest, session: Session
) -> UseItemResult:
    """Consume ONE unit of an owned item and apply its effect.

    Atomicity: the quantity is decremented with a single conditional UPDATE
    (``WHERE qty >= 1``); only if that matches a row do we apply the effect, so a
    double-tap can never consume more than is owned. Potions adjust the lead
    party member's HP/MP in the active encounter cache (best-effort — the item is
    still consumed even if no battle is live, matching how potions bank). Camp
    tokens are consumed here and picked up by the camp/rest flow later; training
    items apply a permanent stat gain to the lead party Monster.
    """
    await _require_run(session, run_id)

    item = await session.get(Item, body.item_key)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    # ---- Atomic decrement: reject if not owned ----
    res = await session.execute(
        text(
            "UPDATE player_inventory SET qty = qty - 1 "
            "WHERE run_id = :rid AND item_key = :ik AND qty >= 1"
        ),
        {"rid": run_id, "ik": body.item_key},
    )
    if res.rowcount != 1:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"Item not in inventory: {body.item_key}"
        )

    # ---- Apply effect (best-effort beyond the consume) ----
    applied: dict = {}
    target: str | None = None
    message = ""
    kind = item.kind if isinstance(item.kind, ItemKind) else ItemKind(str(item.kind))
    effect = dict(item.effect or {})

    try:
        if kind in (ItemKind.potion_hp, ItemKind.potion_mp):
            applied, target, message = await _apply_potion(run_id, kind, effect)
        elif kind == ItemKind.camp_token:
            applied, message = {}, "Camp token consumed (banked for camp/rest)."
        elif kind in (
            ItemKind.training_atk,
            ItemKind.training_def,
            ItemKind.training_mp,
        ):
            applied, target, message = await _apply_training(session, run_id, kind, effect)
    except Exception as e:  # noqa: BLE001 — effect failure must not lose the consume
        log.info("economy: use effect skipped (%s)", e)
        message = message or "Item consumed."

    await session.commit()
    remaining = await _inventory_qty(session, run_id, body.item_key)
    return UseItemResult(
        run_id=run_id,
        item_key=body.item_key,
        applied=applied,
        target=target,
        remaining_qty=remaining,
        message=message or "Item consumed.",
    )


async def _lead_party_monster_id(run_id: str):
    """Return the lead (lowest-level-tiebreak) player Monster id for a run, or None."""
    from app.db.models import Monster, MonsterOwner
    from app.db.session import SessionLocal

    async with SessionLocal() as s:
        res = await s.execute(
            select(Monster)
            .where(Monster.run_id == run_id, Monster.owner == MonsterOwner.player)
            .order_by(Monster.created_at.asc())
        )
        m = res.scalars().first()
        return m.id if m else None


async def _apply_potion(
    run_id: str, kind: ItemKind, effect: dict
) -> tuple[dict, str | None, str]:
    """Restore HP/MP on the lead party member's most recent ongoing encounter.

    Reads the active encounter from Redis and bumps the lead party combatant's
    HP/MP (capped at max). Best-effort: with no live battle the potion is still
    consumed (the caller already decremented qty); we report applied={} so the
    UI knows nothing was healed.
    """
    from app.db.models import Encounter, EncounterResult
    from app.db.session import SessionLocal
    from app.redis_state import get_hp_map, get_mp_map, set_hp, set_mp

    async with SessionLocal() as s:
        res = await s.execute(
            select(Encounter)
            .where(
                Encounter.run_id == run_id,
                Encounter.result == EncounterResult.ongoing,
            )
            .order_by(Encounter.created_at.desc())
        )
        enc = res.scalars().first()
    if enc is None:
        return {}, None, "No active battle — potion consumed (no target to heal)."

    # Reuse the encounter router's roster loader to find the lead party member.
    from app.routers.encounter import load_combatants

    try:
        combatants = await load_combatants(enc.id)
    except HTTPException:
        return {}, None, "No active battle — potion consumed (no target to heal)."
    lead = next(
        (c for c in combatants if c.role == "party" and c.hp > 0), None
    ) or next((c for c in combatants if c.role == "party"), None)
    if lead is None:
        return {}, None, "No party member to heal — potion consumed."

    if kind == ItemKind.potion_hp:
        amount = int(effect.get("hp", 0))
        hp_map = await get_hp_map(enc.id)
        cur = int(hp_map.get(lead.monster_id, lead.hp))
        new = min(int(lead.max_hp), cur + amount)
        await set_hp(enc.id, lead.monster_id, new)
        return {"hp": new - cur}, lead.monster_id, f"Healed {new - cur} HP."
    else:  # potion_mp
        amount = int(effect.get("mp", 0))
        mp_map = await get_mp_map(enc.id)
        cur = int(mp_map.get(lead.monster_id, lead.max_mp))
        new = min(int(lead.max_mp), cur + amount)
        await set_mp(enc.id, lead.monster_id, new)
        return {"mp": new - cur}, lead.monster_id, f"Restored {new - cur} MP."


async def _apply_training(
    session: AsyncSession, run_id: str, kind: ItemKind, effect: dict
) -> tuple[dict, str | None, str]:
    """Apply a permanent stat gain to the lead party Monster via atomic SQL.

    ATK/DEF/Max-MP are bumped with a single UPDATE on the chosen monster row so
    the gain is durable and concurrency-safe.
    """
    monster_id = await _lead_party_monster_id(run_id)
    if monster_id is None:
        return {}, None, "No party member to train — item consumed."

    col, amount = {
        ItemKind.training_atk: ("atk", int(effect.get("atk", 0))),
        ItemKind.training_def: ('"def"', int(effect.get("def", 0))),
        ItemKind.training_mp: ("max_mp", int(effect.get("mp", 0))),
    }[kind]

    await session.execute(
        text(f"UPDATE monsters SET {col} = {col} + :amt WHERE id = :mid"),
        {"amt": amount, "mid": monster_id},
    )
    label = col.strip('"')
    return {label: amount}, monster_id, f"{label.upper()} +{amount} (permanent)."


# ---------------------------------------------------------------------------
# Shop
# ---------------------------------------------------------------------------


@router.get("/shop/{npc_id}", response_model=ShopState)
async def get_shop(npc_id: str, session: Session) -> ShopState:
    """Return an NPC's stock, lazily seeding known canonical merchants."""
    rows = await _shop_rows(session, npc_id)
    if not rows and await _materialize_default_shop_if_known(session, npc_id):
        rows = await _shop_rows(session, npc_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Shop not found")
    items = [
        ShopItem(
            item_key=stock.item_key,
            name=item.name,
            kind=item.kind.value if hasattr(item.kind, "value") else str(item.kind),
            price=stock.price,
            qty=stock.qty,
            effect=dict(item.effect or {}),
        )
        for stock, item in rows
    ]
    return ShopState(npc_id=npc_id, items=items)


@router.post("/shop/{npc_id}/buy", response_model=BuyItemResult)
async def buy_item(
    npc_id: str, run_id: str, body: BuyItemRequest, session: Session
) -> BuyItemResult:
    """Buy ``qty`` units of an item from an NPC for the given run.

    Atomic, in this order, inside one transaction:
      1. Resolve the price from the shop row (existence + price source).
      2. Debit coins with a conditional UPDATE (``WHERE coins >= :cost``).
         rowcount 0 -> 402 insufficient funds, nothing mutated.
      3. Decrement stock with a conditional UPDATE (``WHERE qty >= :n``).
         rowcount 0 -> roll back the whole txn (coins restored) -> 409 out of stock.
      4. Upsert the run's inventory (+qty) with INSERT ... ON CONFLICT DO UPDATE.
    No balance/stock value is ever read into Python and written back, so two
    concurrent buys cannot both pass step 2 or step 3 beyond what's available.
    """
    await _require_run(session, run_id)
    n = max(1, int(body.qty))

    # ---- 1. Resolve shop row (price + existence) ----
    res = await session.execute(
        select(ShopStock).where(
            ShopStock.npc_id == npc_id, ShopStock.item_key == body.item_key
        )
    )
    stock = res.scalars().first()
    if stock is None and await _materialize_default_shop_if_known(session, npc_id):
        res = await session.execute(
            select(ShopStock).where(
                ShopStock.npc_id == npc_id, ShopStock.item_key == body.item_key
            )
        )
        stock = res.scalars().first()
    if stock is None:
        raise HTTPException(status_code=404, detail="Item not sold here")
    cost = int(stock.price) * n

    # ---- 2. Atomic coin debit (reject on insufficient funds) ----
    debit = await session.execute(
        text(
            "UPDATE runs SET coins = coins - :cost "
            "WHERE id = :rid AND coins >= :cost"
        ),
        {"cost": cost, "rid": run_id},
    )
    if debit.rowcount != 1:
        await session.rollback()
        have = await _coins(session, run_id)
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient coins: need {cost}, have {have}",
        )

    # ---- 3. Atomic stock decrement (reject -> roll back the debit) ----
    dec = await session.execute(
        text(
            "UPDATE shop_stock SET qty = qty - :n "
            "WHERE npc_id = :npc AND item_key = :ik AND qty >= :n"
        ),
        {"n": n, "npc": npc_id, "ik": body.item_key},
    )
    if dec.rowcount != 1:
        await session.rollback()  # restores the coins debited in step 2
        raise HTTPException(
            status_code=409, detail=f"Out of stock: {body.item_key}"
        )

    # ---- 4. Inventory upsert (single round-trip, no lost update) ----
    await session.execute(
        text(
            "INSERT INTO player_inventory (id, run_id, item_key, qty) "
            "VALUES (:id, :rid, :ik, :n) "
            "ON CONFLICT (run_id, item_key) "
            "DO UPDATE SET qty = player_inventory.qty + :n"
        ),
        {
            "id": _new_inventory_id(),
            "rid": run_id,
            "ik": body.item_key,
            "n": n,
        },
    )

    await session.commit()

    coins = await _coins(session, run_id)
    owned = await _inventory_qty(session, run_id, body.item_key)
    return BuyItemResult(
        run_id=run_id,
        npc_id=npc_id,
        item_key=body.item_key,
        qty=n,
        spent=cost,
        coins=coins,
        owned_qty=owned,
    )


def _new_inventory_id() -> str:
    import uuid

    return str(uuid.uuid4())
