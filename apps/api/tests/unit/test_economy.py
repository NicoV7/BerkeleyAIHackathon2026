"""WS-1 economy unit tests — coins, items, inventory, shop.

Two layers, mirroring the repo's host-safe test convention (see
``test_gacha_router.py`` / ``test_run_persistence.py``):

  * PURE-LOGIC + SIMULATED-SQL tests (always run on a bare host): the coin
    reward curve, the idempotent catalog seed via a fake session, the migration
    smoke (``init_db`` adds the ``coins`` column), and — crucially — the
    atomicity contract. The atomicity tests drive the *real* router code through
    a ``FakeSession`` that faithfully reproduces conditional
    ``UPDATE ... WHERE col >= :n`` rowcount semantics and ``ON CONFLICT`` upsert
    over in-memory dicts, so "insufficient coins is rejected" and "a double buy
    never overspends" are genuinely exercised without a live Postgres.

  * DB-BACKED round-trip tests gated behind ``require_db`` — they *skip* (never
    error) on a bare host, and run for real against the compose stack.

No Postgres / Redis / network for the always-on tests.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

import pytest

# Skip the whole file if the router cannot import (older schema, etc.).
economy = pytest.importorskip("app.routers.economy")

from app.db.models import (  # noqa: E402
    EncounterResult,
    Item,
    ItemKind,
    PlayerInventory,
    Run,
    ShopStock,
)
from app.economy.catalog import (  # noqa: E402
    DEFAULT_SHOP,
    STARTER_ITEMS,
    upsert_items,
    upsert_shop,
)
from app.schemas import BuyItemRequest, UseItemRequest  # noqa: E402


# =========================================================================== #
# FakeSession — faithful conditional-UPDATE / ON CONFLICT simulation
# =========================================================================== #


class _Result:
    def __init__(self, rows: list[tuple] | None = None, rowcount: int = -1) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def first(self):
        return self._rows[0] if self._rows else None


class _ScalarsAll:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)

    def first(self) -> Any:
        return self._items[0] if self._items else None


class _ScalarsResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> _ScalarsAll:
        return _ScalarsAll(self._items)

    def all(self) -> list[Any]:
        # used for select(A, B) join rows (list of tuples)
        return list(self._items)


class FakeSession:
    """In-memory async session that honors the router's atomicity contract.

    The router NEVER read-then-writes a balance; it issues conditional UPDATEs
    and branches on ``rowcount``. This fake reproduces exactly that: the coin
    debit and stock decrement only "match a row" when the WHERE guard holds, and
    the inventory upsert is INSERT-or-add. A rollback restores a snapshot taken
    at the last commit (so step-3 rollback genuinely restores the step-2 debit).
    """

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.items: dict[str, Item] = {}
        self.inventory: dict[tuple[str, str], int] = {}  # (run_id, item_key) -> qty
        self.shop: dict[tuple[str, str], dict] = {}  # (npc, item) -> {price, qty}
        self._snapshot: Optional[dict] = None
        self.commits = 0
        self._commit_snapshot()

    # ---- snapshot for rollback ----
    def _commit_snapshot(self) -> None:
        self._snapshot = {
            "runs": {k: v.coins for k, v in self.runs.items()},
            "inventory": dict(self.inventory),
            "shop": {k: dict(v) for k, v in self.shop.items()},
        }

    # ---- seed helpers ----
    def seed_run(self, run: Run) -> None:
        self.runs[run.id] = run
        self._commit_snapshot()

    def seed_item(self, item: Item) -> None:
        self.items[item.key] = item

    def seed_inventory(self, run_id: str, item_key: str, qty: int) -> None:
        self.inventory[(run_id, item_key)] = qty
        self._commit_snapshot()

    def seed_shop(self, npc: str, item_key: str, price: int, qty: int) -> None:
        self.shop[(npc, item_key)] = {"price": price, "qty": qty}
        self._commit_snapshot()

    # ---- session API ----
    async def get(self, cls: type, key: Any) -> Any:
        if cls is Run:
            return self.runs.get(key)
        if cls is Item:
            return self.items.get(key)
        return None

    async def commit(self) -> None:
        self.commits += 1
        self._commit_snapshot()

    async def rollback(self) -> None:
        snap = self._snapshot or {}
        for rid, coins in snap.get("runs", {}).items():
            if rid in self.runs:
                self.runs[rid].coins = coins
        self.inventory = dict(snap.get("inventory", {}))
        self.shop = {k: dict(v) for k, v in snap.get("shop", {}).items()}

    def add(self, obj: Any) -> None:
        if isinstance(obj, Item):
            self.items[obj.key] = obj
        elif isinstance(obj, ShopStock):
            self.shop[(obj.npc_id, obj.item_key)] = {
                "price": obj.price,
                "qty": obj.qty,
            }

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        params = params or {}
        sql = str(getattr(stmt, "text", stmt))
        s = " ".join(sql.lower().split())

        # ---- SELECT coins FROM runs ----
        if s.startswith("select coins from runs"):
            run = self.runs.get(params["rid"])
            return _Result(rows=[(run.coins,)] if run else [])

        # ---- SELECT qty FROM player_inventory ----
        if s.startswith("select qty from player_inventory"):
            qty = self.inventory.get((params["rid"], params["ik"]))
            return _Result(rows=[(qty,)] if qty is not None else [])

        # ---- UPDATE runs SET coins = coins - :cost WHERE ... coins >= :cost ----
        if "update runs set coins = coins - :cost" in s:
            run = self.runs.get(params["rid"])
            if run is not None and run.coins >= params["cost"]:
                run.coins -= params["cost"]
                return _Result(rowcount=1)
            return _Result(rowcount=0)

        # ---- UPDATE runs SET coins = coins + :amt (award) ----
        if "update runs set coins = coins + :amt" in s:
            run = self.runs.get(params["rid"])
            if run is not None:
                run.coins += params["amt"]
                return _Result(rowcount=1)
            return _Result(rowcount=0)

        # ---- UPDATE shop_stock SET qty = qty - :n WHERE ... qty >= :n ----
        if "update shop_stock set qty = qty - :n" in s:
            key = (params["npc"], params["ik"])
            row = self.shop.get(key)
            if row is not None and row["qty"] >= params["n"]:
                row["qty"] -= params["n"]
                return _Result(rowcount=1)
            return _Result(rowcount=0)

        # ---- UPDATE player_inventory SET qty = qty - 1 WHERE ... qty >= 1 ----
        if "update player_inventory set qty = qty - 1" in s:
            key = (params["rid"], params["ik"])
            cur = self.inventory.get(key, 0)
            if cur >= 1:
                self.inventory[key] = cur - 1
                return _Result(rowcount=1)
            return _Result(rowcount=0)

        # ---- INSERT INTO player_inventory ... ON CONFLICT DO UPDATE ----
        if "insert into player_inventory" in s:
            key = (params["rid"], params["ik"])
            self.inventory[key] = self.inventory.get(key, 0) + params["n"]
            return _Result(rowcount=1)

        # ---- SELECT ... FROM shop_stock JOIN items (shop listing) ----
        if "shop_stock" in s and "join items" in s and s.startswith("select"):
            rows = []
            for k, v in self.shop.items():
                item = self.items.get(k[1])
                if item is None:
                    continue
                rows.append(
                    (
                        ShopStock(
                            npc_id=k[0],
                            item_key=k[1],
                            price=v["price"],
                            qty=v["qty"],
                        ),
                        item,
                    )
                )
            return _ScalarsResult(rows)

        # ---- SELECT ... FROM shop_stock (resolve a shop row) ----
        if "shop_stock" in s and s.startswith("select"):
            # buy(): single row for (npc, item)
            # SQLAlchemy compiles bound params; resolve from the in-memory shop by
            # scanning instead of relying on param names.
            rows = [
                ShopStock(npc_id=k[0], item_key=k[1], price=v["price"], qty=v["qty"])
                for k, v in self.shop.items()
            ]
            return _ScalarsResult(rows)

        # ---- SELECT ... FROM player_inventory JOIN items (inventory list) ----
        if "player_inventory" in s and "items" in s and s.startswith("select"):
            rows = []
            for (rid, ik), qty in self.inventory.items():
                if qty <= 0:
                    continue
                item = self.items.get(ik)
                if item is None:
                    continue
                rows.append((PlayerInventory(run_id=rid, item_key=ik, qty=qty), item))
            return _ScalarsResult(rows)

        return _ScalarsResult([])


# =========================================================================== #
# Pure reward curve (always-on)
# =========================================================================== #


def test_coin_reward_win_base() -> None:
    from app.routers.debate import _coin_reward

    roster = [{"role": "enemy", "level": 1}, {"role": "party", "level": 3}]
    assert _coin_reward(roster, EncounterResult.win) == 30


def test_coin_reward_capture_is_higher_than_win() -> None:
    from app.routers.debate import _coin_reward

    roster = [{"role": "enemy", "level": 1}]
    win = _coin_reward(roster, EncounterResult.win)
    cap = _coin_reward(roster, EncounterResult.capture)
    assert cap > win


def test_coin_reward_scales_with_enemy_level() -> None:
    from app.routers.debate import _coin_reward

    low = _coin_reward([{"role": "enemy", "level": 1}], EncounterResult.win)
    high = _coin_reward([{"role": "enemy", "level": 5}], EncounterResult.win)
    assert high > low
    # +5 per level above 1 -> level 5 = base + 20.
    assert high == low + 20


def test_coin_reward_handles_bad_levels() -> None:
    from app.routers.debate import _coin_reward

    roster = [{"role": "enemy", "level": "oops"}, {"role": "enemy"}]
    # Falls back to enemy_level=1 -> base win reward, never raises.
    assert _coin_reward(roster, EncounterResult.win) == 30


# =========================================================================== #
# Migration smoke (always-on): init_db adds the coins column
# =========================================================================== #


def test_init_db_adds_coins_column() -> None:
    """The idempotent ALTER list must add runs.coins (create_all can't ALTER)."""
    import inspect

    from app.db import session as session_mod

    src = inspect.getsource(session_mod.init_db)
    assert "ALTER TABLE runs ADD COLUMN IF NOT EXISTS coins" in src
    # Defensive: it must be an integer column with a NOT NULL default 0.
    assert re.search(r"coins\s+INTEGER\s+NOT\s+NULL\s+DEFAULT\s+0", src, re.I)


def test_new_tables_registered_on_metadata() -> None:
    from sqlmodel import SQLModel

    tables = set(SQLModel.metadata.tables.keys())
    for t in ("items", "player_inventory", "shop_stock"):
        assert t in tables, f"{t} not registered -> create_all won't make it"


# =========================================================================== #
# Catalog seed idempotency (always-on, via FakeSession)
# =========================================================================== #


class _SeedSession:
    """Minimal session for the catalog upserts (get / add / execute / commit)."""

    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self.shop: dict[tuple[str, str], ShopStock] = {}
        self.commits = 0

    async def get(self, cls: type, key: Any) -> Any:
        return self.items.get(key) if cls is Item else None

    def add(self, obj: Any) -> None:
        if isinstance(obj, Item):
            self.items[obj.key] = obj
        elif isinstance(obj, ShopStock):
            self.shop[(obj.npc_id, obj.item_key)] = obj

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, stmt: Any) -> Any:
        sql = str(getattr(stmt, "compile", lambda: stmt)()).lower()
        if "shop_stock" in sql:
            return _ScalarsResult(list(self.shop.values()))
        return _ScalarsResult(list(self.items.values()))


def test_upsert_items_is_idempotent() -> None:
    s = _SeedSession()
    n1 = asyncio.run(upsert_items(s))
    assert n1 == len(STARTER_ITEMS)
    assert len(s.items) == len(STARTER_ITEMS)
    # Re-run: same row count, no duplicates (keyed on Item.key).
    n2 = asyncio.run(upsert_items(s))
    assert n2 == len(STARTER_ITEMS)
    assert len(s.items) == len(STARTER_ITEMS)


def test_upsert_shop_does_not_refill_depleted_stock() -> None:
    s = _SeedSession()
    asyncio.run(upsert_shop(s, "merchant"))
    assert len(s.shop) == len(DEFAULT_SHOP)
    # Deplete one row, then re-run the seed: qty must NOT be refilled.
    key = next(iter(s.shop))
    s.shop[key].qty = 0
    asyncio.run(upsert_shop(s, "merchant"))
    assert s.shop[key].qty == 0  # rerun never refills a live (depleted) shop


def _seed_starter_items(s: FakeSession) -> None:
    for row in STARTER_ITEMS:
        s.seed_item(
            Item(
                key=row["key"],
                name=row["name"],
                kind=row["kind"],
                effect=row["effect"],
                price=row["price"],
            )
        )


# =========================================================================== #
# ATOMICITY: buy + double-spend + use-decrement (always-on, real router code)
# =========================================================================== #


def _stock_item(key: str = "potion_hp_small", price: int = 25, qty: int = 5) -> Item:
    return Item(key=key, name="Small HP Potion", kind=ItemKind.potion_hp,
                effect={"hp": 40}, price=price)


def test_buy_succeeds_and_debits_coins_and_stock() -> None:
    s = FakeSession()
    s.seed_run(Run(id="r1", debate_topic="t", coins=100))
    s.seed_item(_stock_item())
    s.seed_shop("merchant", "potion_hp_small", price=25, qty=5)

    res = asyncio.run(
        economy.buy_item("merchant", "r1", BuyItemRequest(item_key="potion_hp_small", qty=2), s)
    )
    assert res.spent == 50
    assert res.coins == 50
    assert res.owned_qty == 2
    assert s.runs["r1"].coins == 50
    assert s.shop[("merchant", "potion_hp_small")]["qty"] == 3
    assert s.inventory[("r1", "potion_hp_small")] == 2


def test_known_merchant_shop_materializes_default_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = FakeSession()
    _seed_starter_items(s)
    monkeypatch.setattr(
        economy,
        "_is_known_merchant",
        lambda npc_id: npc_id == "reedmarket_merchant",
    )

    shop = asyncio.run(economy.get_shop("reedmarket_merchant", s))

    assert shop.npc_id == "reedmarket_merchant"
    assert len(shop.items) == len(DEFAULT_SHOP)
    assert ("reedmarket_merchant", "camp_token") in s.shop


def test_direct_buy_from_known_merchant_materializes_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = FakeSession()
    s.seed_run(Run(id="r1", debate_topic="t", coins=100))
    _seed_starter_items(s)
    monkeypatch.setattr(
        economy,
        "_is_known_merchant",
        lambda npc_id: npc_id == "reedmarket_merchant",
    )

    res = asyncio.run(
        economy.buy_item(
            "reedmarket_merchant",
            "r1",
            BuyItemRequest(item_key="potion_hp_small"),
            s,
        )
    )

    assert res.npc_id == "reedmarket_merchant"
    assert res.coins == 75
    assert s.shop[("reedmarket_merchant", "potion_hp_small")]["qty"] == 98


def test_buy_rejects_insufficient_coins_without_mutation() -> None:
    s = FakeSession()
    s.seed_run(Run(id="r1", debate_topic="t", coins=10))  # too poor for 25
    s.seed_item(_stock_item())
    s.seed_shop("merchant", "potion_hp_small", price=25, qty=5)

    with pytest.raises(Exception) as exc:
        asyncio.run(
            economy.buy_item("merchant", "r1", BuyItemRequest(item_key="potion_hp_small"), s)
        )
    assert "insufficient" in str(exc.value).lower()
    # Nothing mutated: coins intact, stock intact, no inventory created.
    assert s.runs["r1"].coins == 10
    assert s.shop[("merchant", "potion_hp_small")]["qty"] == 5
    assert ("r1", "potion_hp_small") not in s.inventory


def test_two_rapid_buys_cannot_overspend() -> None:
    """Wallet only affords ONE buy; the second conditional debit matches 0 rows."""
    s = FakeSession()
    s.seed_run(Run(id="r1", debate_topic="t", coins=25))  # exactly one potion
    s.seed_item(_stock_item())
    s.seed_shop("merchant", "potion_hp_small", price=25, qty=5)

    # First buy: succeeds, drains the wallet to 0.
    first = asyncio.run(
        economy.buy_item("merchant", "r1", BuyItemRequest(item_key="potion_hp_small"), s)
    )
    assert first.coins == 0

    # Second buy (the "concurrent" retry): must be rejected — no double-spend.
    with pytest.raises(Exception) as exc:
        asyncio.run(
            economy.buy_item("merchant", "r1", BuyItemRequest(item_key="potion_hp_small"), s)
        )
    assert "insufficient" in str(exc.value).lower()
    assert s.runs["r1"].coins == 0  # never went negative
    assert s.inventory[("r1", "potion_hp_small")] == 1  # only one ever owned


def test_buy_out_of_stock_rolls_back_coin_debit() -> None:
    """Stock decrement failing AFTER the coin debit must roll the debit back."""
    s = FakeSession()
    s.seed_run(Run(id="r1", debate_topic="t", coins=100))
    s.seed_item(_stock_item())
    s.seed_shop("merchant", "potion_hp_small", price=25, qty=1)  # only 1 in stock

    with pytest.raises(Exception) as exc:
        asyncio.run(
            economy.buy_item("merchant", "r1", BuyItemRequest(item_key="potion_hp_small", qty=2), s)
        )
    assert "stock" in str(exc.value).lower()
    # The transaction rolled back: coins restored to the pre-buy balance.
    assert s.runs["r1"].coins == 100
    assert s.shop[("merchant", "potion_hp_small")]["qty"] == 1
    assert ("r1", "potion_hp_small") not in s.inventory


def test_use_item_decrements_inventory_atomically() -> None:
    s = FakeSession()
    s.seed_run(Run(id="r1", debate_topic="t", coins=0))
    s.seed_item(_stock_item())
    s.seed_inventory("r1", "potion_hp_small", 2)

    # Use one — qty goes 2 -> 1. (No live battle, so no heal target; consume only.)
    res = asyncio.run(
        economy.use_item("r1", UseItemRequest(item_key="potion_hp_small"), s)
    )
    assert res.remaining_qty == 1
    assert s.inventory[("r1", "potion_hp_small")] == 1


def test_use_item_rejects_when_not_owned() -> None:
    s = FakeSession()
    s.seed_run(Run(id="r1", debate_topic="t", coins=0))
    s.seed_item(_stock_item())
    s.seed_inventory("r1", "potion_hp_small", 0)  # owned-but-empty line

    with pytest.raises(Exception) as exc:
        asyncio.run(
            economy.use_item("r1", UseItemRequest(item_key="potion_hp_small"), s)
        )
    assert "inventory" in str(exc.value).lower()
    assert s.inventory[("r1", "potion_hp_small")] == 0  # never went negative


def test_use_item_repeated_cannot_go_negative() -> None:
    s = FakeSession()
    s.seed_run(Run(id="r1", debate_topic="t", coins=0))
    s.seed_item(_stock_item())
    s.seed_inventory("r1", "potion_hp_small", 1)

    # First use drains to 0.
    asyncio.run(economy.use_item("r1", UseItemRequest(item_key="potion_hp_small"), s))
    assert s.inventory[("r1", "potion_hp_small")] == 0
    # Second use (double-tap) is rejected — never negative.
    with pytest.raises(Exception):
        asyncio.run(economy.use_item("r1", UseItemRequest(item_key="potion_hp_small"), s))
    assert s.inventory[("r1", "potion_hp_small")] == 0


# =========================================================================== #
# Idempotent coin-award on finalize retry (always-on, FakeSession)
# =========================================================================== #


def test_award_coins_credits_once_per_call() -> None:
    """_award_coins issues an atomic in-place credit; called once -> credited once."""
    from types import SimpleNamespace

    from app.routers.debate import _award_coins

    s = FakeSession()
    s.seed_run(Run(id="r1", debate_topic="t", coins=0))
    enc = SimpleNamespace(run_id="r1")
    roster = [{"role": "enemy", "level": 1}]

    awarded = asyncio.run(_award_coins(s, enc, roster, EncounterResult.win))
    assert awarded == 30
    assert s.runs["r1"].coins == 30


def test_finalize_retry_does_not_double_award(monkeypatch: pytest.MonkeyPatch) -> None:
    """The finalize idempotency guard means a *retried* finalize never re-credits.

    _finalize early-returns when ``enc.result != ongoing``, so the coin credit
    (which lives after that guard) fires exactly once. We assert that semantics
    directly: a second invocation of the awarding path is gated by the same guard
    the existing SummonItem/XP awards rely on.
    """
    from app.routers import debate as debate_mod

    # The guard lives in _finalize: `if enc.result != EncounterResult.ongoing: return []`.
    import inspect

    src = inspect.getsource(debate_mod._finalize)
    assert "enc.result != EncounterResult.ongoing" in src
    # And the coin award is placed AFTER that guard, inside the same txn block.
    award_pos = src.index("_award_coins")
    guard_pos = src.index("already finalized")
    assert award_pos > guard_pos, "coin award must sit after the idempotency guard"


# =========================================================================== #
# DB-backed round-trip (gated: skips on a bare host)
# =========================================================================== #


@pytest.mark.usefixtures("require_db")
def test_db_roundtrip_buy_use_and_award(require_db, monkeypatch) -> None:  # noqa: ARG001
    """End-to-end against a real Postgres: seed -> credit -> buy -> use.

    Skips cleanly when no host-reachable Postgres (require_db). Exercises the
    real conditional SQL (rowcount, ``ON CONFLICT`` upsert, ``"def"`` quoting) on
    the actual DB.

    The app's shared engine binds connections to the event loop it was created
    on; running this test under a fresh ``asyncio.run()`` would reuse a
    closed-loop connection. So we stand up a NullPool engine for THIS loop and
    monkeypatch ``app.db.session.{engine,SessionLocal}`` to it (the router's
    internal ``SessionLocal()`` calls then use the same loop-bound engine), then
    dispose it. The DATABASE_URL is rewritten to localhost for host access.
    """
    import uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    import app.db.session as db_session
    from app.config import settings
    from app.economy.catalog import seed_economy

    url = settings.database_url
    for docker_host in ("@postgres:", "@db:"):
        url = url.replace(docker_host, "@localhost:")

    async def _run() -> None:
        eng = create_async_engine(url, poolclass=NullPool)
        maker = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(db_session, "engine", eng)
        monkeypatch.setattr(db_session, "SessionLocal", maker)
        try:
            await db_session.init_db()  # ensures coins column + economy tables exist
            async with maker() as s:
                await seed_economy(s)
            run_id = str(uuid.uuid4())
            async with maker() as s:
                # Insert via the ORM so NOT NULL columns (created_at) are
                # populated by the model defaults; coins is the starting wallet.
                s.add(Run(id=run_id, debate_topic="t", player_name="P", coins=100))
                await s.commit()

            # Buy 2 HP potions (price 25 each = 50). Coins 100 -> 50.
            async with maker() as s:
                res = await economy.buy_item(
                    "merchant", run_id,
                    BuyItemRequest(item_key="potion_hp_small", qty=2), s,
                )
                assert res.spent == 50
                assert res.coins == 50
                assert res.owned_qty == 2

            # Use one -> remaining 1.
            async with maker() as s:
                used = await economy.use_item(
                    run_id, UseItemRequest(item_key="potion_hp_small"), s
                )
                assert used.remaining_qty == 1

            # Wallet reflects the debit.
            async with maker() as s:
                wallet = await economy.get_wallet(run_id, s)
                assert wallet.coins == 50
        finally:
            await eng.dispose()

    asyncio.run(_run())
