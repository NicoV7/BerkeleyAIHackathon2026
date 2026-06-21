"""Unit tests for the Wave A gacha router (app/routers/gacha.py).

Pure-logic coverage of the pull endpoint with NO live DB / Redis / Wikipedia:

  * tier roll weights bias toward the requested floor (no item -> common-heavy;
    rare item -> rare/legendary only; legendary item -> guaranteed legendary).
  * `pull(...)` inserts a Monster row with persona defaults, marks any consumed
    summon item, schedules background hydration, and returns a GachaPullResult.
  * `list_summons(...)` walks the run's items via the fake session.

The router is exercised end-to-end through a `FakeSession` that records `.add()`
calls and resolves `.get()` / `.execute()` from in-memory dicts. The background
hydration scheduler is stubbed so the test never touches the event loop's task
queue beyond verifying that scheduling was attempted.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Optional

import pytest

# Skip the whole file if the router cannot import (older Wave 0 schema, etc.).
gacha = pytest.importorskip("app.routers.gacha")

from app.db.models import (  # noqa: E402
    DebateType,
    Monster,
    MonsterDomain,
    MonsterOwner,
    Persona,
    Run,
    SummonItem,
)
from app.schemas import GachaPullRequest  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _ScalarsAll:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)


class _ScalarsResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> _ScalarsAll:
        return _ScalarsAll(self._items)


class FakeSession:
    """A lightweight async-session stand-in for the gacha router.

    Indexed by class name + primary key. Stores added rows in `added`. Supports:
      - `await session.get(Cls, id)` -> the stored row or None
      - `await session.execute(<select-on-any-table>)` -> returns every stored
        row of the table referenced in the FROM clause.
      - `add` / `commit` / `refresh` no-ops appropriate for an in-memory test.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        # Pre-seeded rows by class -> {id: instance}
        self._store: dict[type, dict[Any, Any]] = {Run: {}, Persona: {}, SummonItem: {}, Monster: {}}
        self.committed = 0

    # ---- seed helpers ----

    def seed_run(self, run: Run) -> None:
        self._store[Run][run.id] = run

    def seed_personas(self, personas: list[Persona]) -> None:
        for p in personas:
            self._store[Persona][p.key] = p

    def seed_summon_items(self, items: list[SummonItem]) -> None:
        for it in items:
            self._store[SummonItem][it.id] = it

    # ---- session API ----

    async def get(self, cls: type, key: Any) -> Any:
        return self._store.get(cls, {}).get(key)

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        for cls, store in self._store.items():
            if isinstance(obj, cls):
                pk = getattr(obj, "id", None) or getattr(obj, "key", None)
                if pk is not None:
                    store[pk] = obj
                break

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, obj: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        # Inspect the SELECT's compiled SQL to find the table being queried,
        # then return every stored row of that mapped class.
        sql = str(getattr(stmt, "compile", lambda: stmt)()).lower() if hasattr(stmt, "compile") else str(stmt).lower()
        table_map = {
            "personas": Persona,
            "summon_items": SummonItem,
            "monsters": Monster,
            "runs": Run,
        }
        target_cls: Optional[type] = None
        for tname, cls in table_map.items():
            if tname in sql:
                target_cls = cls
                break
        if target_cls is None:
            return _ScalarsResult([])
        return _ScalarsResult(list(self._store.get(target_cls, {}).values()))


def _persona(key: str, tier: str, domain: str = MonsterDomain.PHILOSOPHY) -> Persona:
    return Persona(
        key=key,
        name=f"{key.title()}",
        domain=domain,
        type=DebateType.logos,
        wiki_url=f"https://en.wikipedia.org/wiki/{key.title()}",
        tagline=f"Tagline for {key}.",
        tier=tier,
        default_atk=12,
        default_def=11,
        default_mp=55,
        default_max_hp=105,
    )


def _run(run_id: str = "run-1") -> Run:
    return Run(id=run_id, debate_topic="Pineapple belongs on pizza.")


def _stub_schedule_hydration(monkeypatch, recorder: list[tuple[str, Optional[str], str]]) -> None:
    def fake(monster_id, wiki_url, fallback_tagline):  # noqa: ANN001
        recorder.append((monster_id, wiki_url, fallback_tagline))

    monkeypatch.setattr(gacha, "_schedule_hydration", fake)


# --------------------------------------------------------------------------- #
# Tier roll
# --------------------------------------------------------------------------- #


def test_weights_for_item_default_is_common_heavy() -> None:
    w = gacha._weights_for_item(None)
    assert w["common"] == 70
    assert w["rare"] == 25
    assert w["legendary"] == 5


def test_weights_for_item_rare_only_rolls_rare_or_legendary() -> None:
    w = gacha._weights_for_item("rare")
    assert set(w.keys()) == {"rare", "legendary"}
    assert "common" not in w


def test_weights_for_item_legendary_is_guaranteed() -> None:
    w = gacha._weights_for_item("legendary")
    assert w == {"legendary": 100}


def test_roll_tier_deterministic_with_seeded_rng() -> None:
    rng = random.Random(42)
    weights = {"common": 100}  # forced
    assert gacha._roll_tier(weights, rng) == "common"


def test_pick_persona_falls_back_to_any_when_tier_is_empty() -> None:
    personas = [_persona("a", "common"), _persona("b", "common")]
    rng = random.Random(0)
    # No rare in the pool -> pick from all available rather than crash.
    pick = gacha._pick_persona(personas, "rare", rng)
    assert pick in personas


# --------------------------------------------------------------------------- #
# pull(...) endpoint
# --------------------------------------------------------------------------- #


def test_pull_creates_monster_with_persona_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    session.seed_run(_run("run-1"))
    persona = _persona("socrates", "common", domain=MonsterDomain.PHILOSOPHY)
    session.seed_personas([persona])

    sched: list[tuple[str, Optional[str], str]] = []
    _stub_schedule_hydration(monkeypatch, sched)

    result = asyncio.run(
        gacha.pull("run-1", GachaPullRequest(summon_item_id=None), session)
    )

    # A Monster row was added with the persona's defaults.
    monsters = [o for o in session.added if isinstance(o, Monster)]
    assert len(monsters) == 1
    m = monsters[0]
    assert m.name == "Socrates"
    assert m.owner == MonsterOwner.player
    assert m.atk == persona.default_atk
    assert m.def_ == persona.default_def
    assert m.mp == persona.default_mp
    assert m.max_mp == persona.default_mp
    assert m.max_hp == persona.default_max_hp
    assert len(m.skills) == 2
    assert all(skill["type"] == DebateType.logos.value for skill in m.skills)
    assert all("effect_kind" in skill for skill in m.skills)
    assert m.domain == MonsterDomain.PHILOSOPHY
    assert m.wiki_url == persona.wiki_url
    assert m.wiki_hydrated is False
    assert m.persona.get("key") == "socrates"
    assert m.persona.get("tagline") == persona.tagline

    # Pull payload echoes the persona key (only candidate in the pool); the
    # rolled tier comes from `_roll_tier` and may be any of the three buckets
    # depending on RNG — we just assert it's one of the known tiers.
    assert result.persona_key == "socrates"
    assert result.persona_tier in {"common", "rare", "legendary"}
    # The returned MonsterSummary carries the un-hydrated flag for FE polling.
    assert result.monster.wiki_hydrated is False
    assert result.monster.persona["tagline"] == persona.tagline
    # And hydration was scheduled with the seed tagline as the fallback.
    assert len(sched) == 1
    assert sched[0][2] == persona.tagline


def test_pull_seed_makes_roll_reproducible(monkeypatch: pytest.MonkeyPatch) -> None:
    def make_session() -> FakeSession:
        s = FakeSession()
        s.seed_run(_run("run-1"))
        s.seed_personas([
            _persona("alpha", "common"),
            _persona("beta", "common"),
            _persona("gamma", "rare"),
        ])
        return s

    _stub_schedule_hydration(monkeypatch, [])

    first = asyncio.run(
        gacha.pull("run-1", GachaPullRequest(seed=2026), make_session())
    )
    second = asyncio.run(
        gacha.pull("run-1", GachaPullRequest(seed=2026), make_session())
    )

    assert first.persona_key == second.persona_key
    assert first.persona_tier == second.persona_tier


def test_pull_consumes_summon_item_and_promotes_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    session.seed_run(_run("run-1"))
    # Pool has only legendary candidates so the rare item's promotion lands on
    # legendary deterministically (no need to seed RNG).
    session.seed_personas([_persona("turing", "legendary")])
    item = SummonItem(id="item-1", run_id="run-1", tier="rare", consumed=False)
    session.seed_summon_items([item])

    _stub_schedule_hydration(monkeypatch, [])

    result = asyncio.run(
        gacha.pull("run-1", GachaPullRequest(summon_item_id="item-1"), session)
    )

    assert result.persona_key == "turing"
    # A rare item's tier roll is restricted to rare or legendary — common is
    # never possible from this distribution.
    assert result.persona_tier in {"rare", "legendary"}
    # The summon item was marked consumed in the same transaction.
    assert item.consumed is True
    assert item in session.added  # re-added in the same txn


def test_pull_rejects_missing_run() -> None:
    session = FakeSession()
    # No run seeded.
    with pytest.raises(Exception) as exc:
        asyncio.run(gacha.pull("run-missing", GachaPullRequest(), session))
    # FastAPI HTTPException is raised; check the detail is friendly.
    assert "run" in str(exc.value).lower()


def test_pull_rejects_consumed_item(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    session.seed_run(_run("run-1"))
    session.seed_personas([_persona("a", "common")])
    item = SummonItem(id="item-1", run_id="run-1", tier="rare", consumed=True)
    session.seed_summon_items([item])

    _stub_schedule_hydration(monkeypatch, [])

    with pytest.raises(Exception) as exc:
        asyncio.run(
            gacha.pull("run-1", GachaPullRequest(summon_item_id="item-1"), session)
        )
    assert "consumed" in str(exc.value).lower()


def test_pull_503s_when_persona_catalog_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    session.seed_run(_run("run-1"))
    # No personas seeded.

    _stub_schedule_hydration(monkeypatch, [])

    with pytest.raises(Exception) as exc:
        asyncio.run(gacha.pull("run-1", GachaPullRequest(), session))
    assert "persona" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# list_summons(...) endpoint
# --------------------------------------------------------------------------- #


def test_list_summons_returns_items_for_run() -> None:
    session = FakeSession()
    session.seed_run(_run("run-1"))
    items = [
        SummonItem(id="i1", run_id="run-1", tier="common", consumed=False),
        SummonItem(id="i2", run_id="run-1", tier="rare", consumed=True),
    ]
    session.seed_summon_items(items)

    result = asyncio.run(gacha.list_summons("run-1", session))

    ids = sorted([s.id for s in result])
    assert ids == ["i1", "i2"]
    # Consumed flag preserved through the projection.
    consumed_flags = {s.id: s.consumed for s in result}
    assert consumed_flags == {"i1": False, "i2": True}


def test_list_summons_404s_when_run_missing() -> None:
    session = FakeSession()
    with pytest.raises(Exception) as exc:
        asyncio.run(gacha.list_summons("no-such-run", session))
    assert "run" in str(exc.value).lower()
