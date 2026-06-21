"""Wave 2 — World structure router (POIs + procedural WorldSpecLite).

Endpoints (mounted under the shared /api prefix, matching map.py):
    GET /api/runs/{run_id}/world  (-> WorldSpecLite)

This module owns the SINGLE source of truth for procedural POI placement
(``place_pois``) and region layout (``build_regions``). The map router imports
``place_pois`` so that ``GET /api/runs/{id}/map`` and ``GET /api/runs/{id}/world``
return *identical* POIs for the same run seed — there is no second placement
algorithm to drift out of sync.

Determinism contract:
    Everything derives from ``random.Random(seed ^ MASK)`` with fixed masks, so a
    given seed always yields byte-for-byte identical POIs / regions / world. No
    wall-clock, no unseeded RNG.

Tile legend (the map grid):
    0 = walkable, 1 = blocked, 2 = campsite (a ``camp`` POI tile; FE renders it).
"""
from __future__ import annotations

import random
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Run
from app.db.session import get_session
from app.schemas import NPCAnchor, POI, Region, WorldSpecLite
from app.world import boss_profile, event_log, figures, npcs, quests
from app.world.algorithms import INTERIOR_KINDS, get_generator
from app.world.algorithms.base import BLOCKED_TILES
from app.world.algorithms.biomes import BiomeGenerator

router = APIRouter(prefix="/api", tags=["world"])

# Shared overworld biome generator (stateless; all state is seed-derived).
_BIOME_GEN = BiomeGenerator()

# ---------------------------------------------------------------------------
# Procedural placement — the ONE shared source of truth for map + world.
# ---------------------------------------------------------------------------

# RNG masks: keep POI placement independent of tile/wild-enemy RNG streams so a
# change to one does not perturb the other, while staying fully seed-derived.
# (Region/biome RNG now lives in BiomeGenerator; see build_regions.)
_POI_MASK = 0x5EED  # POI placement stream

# How many of each "extra" POI kind to scatter (besides start/goal).
_CAMP_COUNT = 2
_TOWN_COUNT = 1
_DEN_COUNT = 1
_LANDMARK_COUNT = 2

# Tile value for a campsite (camp POI). 0=walkable, 1=blocked, 2=campsite.
CAMP_TILE = 2

_KIND_NAMES = {
    "start": "Trailhead",
    "goal": "The Summit",
    "camp": "Campsite",
    "town": "Town",
    "den": "Monster Den",
    "landmark": "Landmark",
}


def place_pois(
    seed: int,
    tiles: list[list[int]],
    width: int,
    height: int,
) -> list[POI]:
    """Deterministically place POIs on walkable tiles for ``seed``.

    Pure + deterministic: identical (seed, tiles, width, height) -> identical
    POI list (same order, coords, names, interior fields). Both the map router
    and the world router call this so their POIs never diverge — including the
    additive ``interior_seed``/``interior_kind`` decoration on enterable POIs.

    Placement rules:
      * ``start`` is pinned to the spawn zone (1, 1) — matches create_run's
        ``player_x/y`` so the player begins on the start POI.
      * ``goal`` is pinned to the far walkable corner so the exit is reachable
        and visually "across the map".
      * camp/town/den/landmark are scattered on distinct walkable, non-pinned
        tiles via the seeded RNG.
    """
    rng = random.Random(seed ^ _POI_MASK)

    def _walkable(x: int, y: int) -> bool:
        if not (0 <= x < width and 0 <= y < height):
            return False
        # Treat campsite (2) as walkable too, though we place camps last.
        return tiles[y][x] not in BLOCKED_TILES

    pois: list[POI] = []
    used: set[tuple[int, int]] = set()

    # start: pinned to spawn (matches create_run player_x/y = 1,1).
    start_xy = (1, 1)
    if not _walkable(*start_xy):
        start_xy = _nearest_walkable(start_xy, width, height, tiles)
    pois.append(POI(kind="start", x=start_xy[0], y=start_xy[1], name=_KIND_NAMES["start"]))
    used.add(start_xy)

    # goal: pinned to far corner (walkable nearest to bottom-right interior).
    goal_xy = _nearest_walkable((width - 2, height - 2), width, height, tiles)
    if goal_xy in used:
        goal_xy = _scatter_one(rng, width, height, tiles, used)
    pois.append(POI(kind="goal", x=goal_xy[0], y=goal_xy[1], name=_KIND_NAMES["goal"]))
    used.add(goal_xy)

    # Scatter the rest deterministically, in a fixed kind order.
    plan: list[str] = (
        ["camp"] * _CAMP_COUNT
        + ["town"] * _TOWN_COUNT
        + ["den"] * _DEN_COUNT
        + ["landmark"] * _LANDMARK_COUNT
    )
    counters: dict[str, int] = {}
    for kind in plan:
        xy = _scatter_one(rng, width, height, tiles, used)
        if xy is None:
            break
        used.add(xy)
        counters[kind] = counters.get(kind, 0) + 1
        suffix = f" {counters[kind]}" if plan.count(kind) > 1 else ""
        pois.append(POI(kind=kind, x=xy[0], y=xy[1], name=f"{_KIND_NAMES[kind]}{suffix}"))

    # Decorate enterable POIs (town/den) with their deterministic interior fields
    # HERE, in the single shared placement helper, so /map and /world return
    # byte-for-byte identical POIs (interior fields included). Additive only:
    # non-enterable POIs are returned unchanged (interior_* stay None). Forward
    # refs to helpers defined later in the module are safe — place_pois is only
    # ever called at runtime, never at import.
    return [_attach_interior(seed, p) for p in pois]


def _scatter_one(
    rng: random.Random,
    width: int,
    height: int,
    tiles: list[list[int]],
    used: set[tuple[int, int]],
) -> tuple[int, int] | None:
    """Pick a fresh walkable interior tile not already used; None if exhausted."""
    for _ in range(500):
        x = rng.randint(1, width - 2)
        y = rng.randint(1, height - 2)
        if (x, y) in used:
            continue
        if tiles[y][x] not in BLOCKED_TILES:
            return (x, y)
    # Deterministic fallback: first free walkable tile by scan order.
    for y in range(height):
        for x in range(width):
            if (x, y) not in used and tiles[y][x] not in BLOCKED_TILES:
                return (x, y)
    return None


def _nearest_walkable(
    target: tuple[int, int],
    width: int,
    height: int,
    tiles: list[list[int]],
) -> tuple[int, int]:
    """Spiral outward from ``target`` to the closest walkable tile (deterministic)."""
    tx, ty = target
    max_r = max(width, height)
    for r in range(max_r):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                x, y = tx + dx, ty + dy
                if (
                    0 <= x < width
                    and 0 <= y < height
                    and tiles[y][x] not in BLOCKED_TILES
                ):
                    return (x, y)
    return (1, 1)


def apply_camp_tiles(tiles: list[list[int]], pois: list[POI]) -> list[list[int]]:
    """Overlay CAMP_TILE (2) onto the grid at every ``camp`` POI coordinate.

    Returns a NEW grid (does not mutate the input) so callers that also need the
    raw walkable/blocked grid are unaffected. Camp tiles only ever replace
    walkable (0) tiles — placement guarantees camps sit on walkable positions.
    """
    out = [row[:] for row in tiles]
    for poi in pois:
        if poi.kind == "camp" and 0 <= poi.y < len(out) and 0 <= poi.x < len(out[0]):
            if out[poi.y][poi.x] not in BLOCKED_TILES:
                out[poi.y][poi.x] = CAMP_TILE
    return out


def build_regions(seed: int, width: int, height: int) -> list[Region]:
    """Deterministically derive biome regions from the noise field (Wave 2).

    Replaces the old 4-quadrant random split with biome-aware regions summarized
    from ``BiomeGenerator``'s value-noise biome map, so regions reflect smooth,
    organic biome bands instead of hard quadrant borders. Still pure +
    deterministic in (seed, width, height) — the determinism contract holds.
    """
    return _BIOME_GEN.regions(seed, width, height)


# ---------------------------------------------------------------------------
# Interiors (Wave 2 additive) — enterable POIs + on-demand interior worlds.
# ---------------------------------------------------------------------------

# Map an overworld POI.kind to the interior generator it opens into. Only these
# kinds are enterable; others (start/goal/camp/landmark) have no interior.
_INTERIOR_KIND_FOR_POI = {
    "town": "town",
    "den": "cave",
}

# Interior grids are small, fixed-size scenes (not the overworld dims).
INTERIOR_WIDTH = 16
INTERIOR_HEIGHT = 12

_INTERIOR_SEED_MASK = 0x1A7E  # "INTERIOR" stream, derived from run seed + coords


def poi_id(poi: POI) -> str:
    """Stable, positional identity for a POI: ``kind:x:y``.

    POIs have no id field in the frozen schema, but the FE needs a handle to
    request a specific interior. Coords are unique per placement (place_pois
    never reuses a tile), so ``kind:x:y`` is a stable key for a given run seed.
    """
    return f"{poi.kind}:{poi.x}:{poi.y}"


def interior_seed_for(run_seed: int, poi: POI) -> int:
    """Deterministic interior seed from the run seed + POI position.

    Stable across reloads (no wall-clock), and distinct per POI so two towns in
    one run get different interiors. Masked to keep it independent of the
    overworld POI/region RNG streams.
    """
    return (run_seed * 1000003 + poi.x * 31 + poi.y) ^ _INTERIOR_SEED_MASK


def _attach_interior(run_seed: int, poi: POI) -> POI:
    """Return a copy of ``poi`` with interior_seed/interior_kind set if enterable.

    Additive only: non-enterable POIs are returned unchanged (fields stay None).
    Uses model_copy so we never mutate the placed POI in-place.
    """
    kind = _INTERIOR_KIND_FOR_POI.get(poi.kind)
    if kind is None:
        return poi
    return poi.model_copy(
        update={
            "interior_seed": interior_seed_for(run_seed, poi),
            "interior_kind": kind,
        }
    )


def build_world(
    seed: int,
    tiles: list[list[int]],
    width: int,
    height: int,
) -> WorldSpecLite:
    """Assemble the full WorldSpecLite for a seed — pure + deterministic.

    Enterable POIs (town/den) are decorated with their deterministic
    ``interior_seed`` + ``interior_kind`` so the FE knows which POIs open into an
    interior (via GET /api/runs/{id}/interior/{poi_id}). Purely additive.

    Interior decoration happens inside ``place_pois`` (the single shared source
    of truth), so the POIs here are identical to what the map router returns.
    """
    pois = place_pois(seed, tiles, width, height)
    regions = build_regions(seed, width, height)
    start = next((p for p in pois if p.kind == "start"), None)
    goal = next((p for p in pois if p.kind == "goal"), None)
    return WorldSpecLite(
        seed=seed,
        width=width,
        height=height,
        regions=regions,
        pois=pois,
        start=start,
        goal=goal,
    )


# In-process interior cache keyed by (interior_seed, kind) per the design doc —
# same POI re-entered returns the identical interior without regenerating.
_INTERIOR_CACHE: dict[tuple[int, str], WorldSpecLite] = {}


def _clamp_interior_kind(kind: str | None, fallback: str = "cave") -> str:
    """Clamp an interior_kind HINT to a known generator (never trust it blindly)."""
    return kind if kind in INTERIOR_KINDS else fallback


def build_interior(interior_seed: int, kind: str) -> WorldSpecLite:
    """Generate (or retrieve from cache) an interior WorldSpecLite.

    Pure + deterministic in (interior_seed, kind): the chosen generator derives
    all randomness from the seed, so re-entry yields the identical interior.
    Cached by (interior_seed, kind) per the design doc.
    """
    kind = _clamp_interior_kind(kind)
    cache_key = (interior_seed, kind)
    cached = _INTERIOR_CACHE.get(cache_key)
    if cached is not None:
        return cached

    result = get_generator(kind).generate(
        interior_seed, INTERIOR_WIDTH, INTERIOR_HEIGHT
    )
    start = next((p for p in result.pois if p.kind == "start"), None)
    goal = next((p for p in result.pois if p.kind == "goal"), None)
    spec = WorldSpecLite(
        seed=interior_seed,
        width=result.width,
        height=result.height,
        regions=result.regions,
        pois=result.pois,
        start=start,
        goal=goal,
    )
    _INTERIOR_CACHE[cache_key] = spec
    return spec


def _clear_interior_cache() -> None:
    """Test/maintenance hook: drop all cached interiors."""
    _INTERIOR_CACHE.clear()


# ---------------------------------------------------------------------------
# Living layer helpers (Wave 2) — NPCs, quests, figures, final-boss profile.
# ---------------------------------------------------------------------------


class SummonRequest(BaseModel):
    """Request to summon a recruited historical figure for one battle turn."""

    figure_id: str
    battle_state: dict[str, Any] = Field(default_factory=dict)


class QuestAcceptRequest(BaseModel):
    """Accept or re-fetch the quest offered by an NPC."""

    npc_id: str


WorldEventKind = Literal[
    "dungeon_cleared",
    "boss_defeated",
    "figure_recruited",
    "region_entered",
    "battle_won",
    "fallacy_flagged",
]


class WorldEventRequest(BaseModel):
    """Validated world event payload used by the living-layer event log."""

    kind: WorldEventKind
    data: dict[str, Any] = Field(default_factory=dict)


async def _get_run_or_404(
    run_id: str, session: AsyncSession
) -> Run:
    """Fetch a run or raise the router's standard 404."""
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def _region_for_anchor(spec: WorldSpecLite, anchor: NPCAnchor) -> Region | None:
    """Find the region that contains an NPC anchor, falling back to the first."""
    for region in spec.regions:
        bounds = region.bounds or []
        if len(bounds) != 4:
            continue
        x0, y0, x1, y1 = bounds
        if x0 <= anchor.x <= x1 and y0 <= anchor.y <= y1:
            return region
    return spec.regions[0] if spec.regions else None


def _poi_key_from_interior_path(path: Path) -> str | None:
    """Convert safe interior filenames like ``den_13_9.json`` to ``den:13:9``."""
    parts = path.stem.split("_")
    if len(parts) != 3:
        return None
    kind, x, y = parts
    if not x.isdigit() or not y.isdigit():
        return None
    return f"{kind}:{x}:{y}"


def _iter_canonical_specs() -> list[tuple[str, WorldSpecLite]]:
    """Return the canonical overworld plus every loadable canonical interior."""
    from app.world import canonical as canonical_mod

    specs: list[tuple[str, WorldSpecLite]] = []
    world = canonical_mod.get_canonical_world()
    if world is not None:
        specs.append(("world", world.spec))

    if not canonical_mod.INTERIORS_DIR.exists():
        return specs
    for path in sorted(canonical_mod.INTERIORS_DIR.glob("*.json")):
        key = _poi_key_from_interior_path(path)
        if key is None:
            continue
        bundle = canonical_mod.get_canonical_interior(key)
        if bundle is not None:
            specs.append((key, bundle.spec))
    return specs


def _find_npc_anchor(npc_id: str) -> tuple[NPCAnchor, Region | None] | None:
    """Find an NPC anchor by id across the canonical world and interiors."""
    for _key, spec in _iter_canonical_specs():
        for poi in spec.pois:
            for anchor in poi.npc_anchors:
                if anchor.npc_id == npc_id:
                    return anchor, _region_for_anchor(spec, anchor)
    return None


def _poi_xy_from_key(key: str) -> tuple[int, int] | None:
    """Parse a positional POI key like ``den:166:532`` into coordinates."""
    parts = key.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return None
    return int(parts[1]), int(parts[2])


def _quest_origin_for_anchor(anchor: NPCAnchor) -> tuple[int, int]:
    """Return overworld coordinates for an NPC, including town-interior anchors."""
    prefix = anchor.npc_id.split("__", 1)[0]
    parts = prefix.split("_")
    if len(parts) == 3 and parts[0] in {"town", "den"}:
        if parts[1].isdigit() and parts[2].isdigit():
            return int(parts[1]), int(parts[2])
    return anchor.x, anchor.y


def _candidate_dungeons(anchor: NPCAnchor | None = None) -> list[tuple[str, str]]:
    """Dungeon candidates for dynamic quests, optionally nearest to an NPC."""
    seen: dict[str, str] = {}
    for key, spec in _iter_canonical_specs():
        if key.startswith("den:"):
            name = spec.regions[0].name if spec.regions else key
            seen[key] = name
        for poi in spec.pois:
            if poi.kind == "den" or poi.interior_kind in {"cave", "dungeon"}:
                seen.setdefault(poi_id(poi), poi.name or poi_id(poi))
    candidates = list(seen.items())
    if anchor is None:
        return candidates
    origin_x, origin_y = _quest_origin_for_anchor(anchor)

    def distance_to_anchor(item: tuple[str, str]) -> tuple[float, str]:
        xy = _poi_xy_from_key(item[0])
        if xy is None:
            return (float("inf"), item[0])
        dx = xy[0] - origin_x
        dy = xy[1] - origin_y
        return (dx * dx + dy * dy, item[0])

    return sorted(candidates, key=distance_to_anchor)


def _figure_summary(fig: figures.Figure, recruited: bool = False) -> dict[str, Any]:
    """Frontend-safe figure payload."""
    return {
        **fig.to_summary(),
        "recruited": recruited,
        "signature_topics": fig.signature_topics,
        "recruit_trial_topic": fig.recruit_trial_topic,
    }


def _summon_prompt(fig: figures.Figure, battle_state: dict[str, Any]) -> str:
    """Build the one-turn voice prompt for a summoned figure."""
    topic = battle_state.get("topic") or battle_state.get("debate_topic") or "this debate"
    quotes = " ".join(fig.famous_quotes[:3])
    return (
        f"You are {fig.name}, summoned for one ally turn in a debate about {topic}. "
        f"Voice: {fig.voice} Signature quotes to echo without copying wholesale: {quotes} "
        "Answer in one decisive turn that helps the player."
    )


def _event_to_dict(evt: event_log.Event) -> dict[str, Any]:
    """Serialize an event-log entry for route responses."""
    return {"kind": evt.kind, "data": evt.data, "ts": evt.ts}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/world", response_model=WorldSpecLite)
async def get_world(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorldSpecLite:
    """Return the WorldSpecLite for a run.

    Priority order:
      1. **Canonical world** — if ``apps/api/data/world/canonical.json`` exists,
         return its hand-curated spec. Identity layer (named regions, scripted
         POIs, npc_anchors) lives here.
      2. **Agent-generated world** — gated by ``settings.world_gen_enabled``;
         returns None on any failure so we never 500.
      3. **Seed-procedural world** — the deterministic fallback. ``/world`` and
         ``/map`` agree on POIs via the shared ``place_pois`` helper.
    """
    # Import here to avoid a circular import at module load (map imports world).
    from app.routers.map import MAP_HEIGHT, MAP_WIDTH, _generate_tiles
    from app.world.canonical import get_canonical_world

    run = await _get_run_or_404(run_id, session)

    canonical = get_canonical_world()
    if canonical is not None:
        return canonical.spec

    tiles = _generate_tiles(run.seed)

    # Gated Wave-3 agent generator (default OFF).
    if settings.world_gen_enabled:
        try:
            from app.world.generator import generate_world

            generated = await generate_world(run.seed, MAP_WIDTH, MAP_HEIGHT)
            if generated is not None:
                return generated
        except Exception:  # noqa: BLE001 — generator must never break the route
            pass

    # Default / fallback path: the Wave-2 seed-deterministic procedural world.
    return build_world(run.seed, tiles, MAP_WIDTH, MAP_HEIGHT)


def _stable_str_hash(s: str) -> int:
    """Deterministic 16-bit hash of a string (builtin hash() is salted per run).

    Used only to seed the FALLBACK interior for an unknown poi key, so it stays
    stable across processes/restarts.
    """
    h = 0
    for ch in s:
        h = (h * 131 + ord(ch)) & 0xFFFF
    return h


@router.get("/runs/{run_id}/interior/{poi_key}", response_model=WorldSpecLite)
async def get_interior(
    run_id: str,
    poi_key: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorldSpecLite:
    """Return an interior WorldSpecLite for an enterable POI, generated on demand.

    ``poi_key`` is the positional ``kind:x:y`` identity from ``poi_id(poi)``. The
    interior is generated by the algorithm for the POI's ``interior_kind`` and
    cached by ``(interior_seed, kind)`` — re-entry returns the identical interior.

    Falls back GRACEFULLY like the gated overworld generator: it never raises for
    a bad/unknown poi_key. The only error is 404 for an unknown RUN (same contract
    as the other run-scoped routes). If the POI cannot be resolved or is not
    enterable, we still return a valid (deterministic) cave interior derived from
    a coord-stable seed, so the FE always gets a renderable scene.
    """
    from app.routers.map import MAP_HEIGHT, MAP_WIDTH, _generate_tiles
    from app.world.canonical import get_canonical_interior

    run = await _get_run_or_404(run_id, session)

    # 1) Canonical interior wins if present (hand-curated NPCs/lore).
    canonical_interior = get_canonical_interior(poi_key)
    if canonical_interior is not None:
        return canonical_interior.spec

    # 2) Resolve the POI from the deterministic world so we use the SAME
    # interior_seed/kind the FE saw on /world (no drift).
    tiles = _generate_tiles(run.seed)
    world = build_world(run.seed, tiles, MAP_WIDTH, MAP_HEIGHT)
    match = next((p for p in world.pois if poi_id(p) == poi_key), None)

    if match is not None and match.interior_seed is not None:
        return build_interior(
            match.interior_seed, _clamp_interior_kind(match.interior_kind)
        )

    # 3) Graceful fallback: unknown/non-enterable POI. Derive a stable seed from
    # the run + the requested key so the fallback interior is still deterministic.
    fallback_seed = (
        run.seed * 1000003 + _stable_str_hash(poi_key)
    ) ^ _INTERIOR_SEED_MASK
    return build_interior(fallback_seed, "cave")


@router.post("/runs/{run_id}/npc/{npc_id}/talk")
async def talk_to_npc(
    run_id: str,
    npc_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Generate/cache dialogue for a canonical NPC anchor."""
    await _get_run_or_404(run_id, session)
    match = _find_npc_anchor(npc_id)
    if match is None:
        raise HTTPException(status_code=404, detail="NPC not found")

    anchor, region = match
    dialogue = await npcs.generate_dialogue(run_id, anchor, region)
    return {
        "npc_id": anchor.npc_id,
        "name": anchor.name,
        "archetype": anchor.archetype,
        "text": dialogue.text,
        "cached": dialogue.cached,
        "cache_key": dialogue.cache_key,
    }


@router.get("/runs/{run_id}/figures")
async def list_figures(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Return the historical figure roster with per-run recruitment state."""
    await _get_run_or_404(run_id, session)
    recruited = {fig.id for fig in await figures.recruited_list(run_id)}
    return {
        "figures": [
            _figure_summary(fig, recruited=fig.id in recruited)
            for fig in figures.all_figures()
        ]
    }


@router.post("/runs/{run_id}/summon")
async def summon_figure(
    run_id: str,
    body: SummonRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Summon a recruited figure for one battle turn."""
    await _get_run_or_404(run_id, session)
    fig = figures.get_figure(body.figure_id)
    if fig is None:
        raise HTTPException(status_code=404, detail="Figure not found")
    if not await figures.is_recruited(run_id, body.figure_id):
        raise HTTPException(status_code=409, detail="Figure is not recruited")

    evt = await event_log.append(run_id, "figure_summoned", figure_id=body.figure_id)
    return {
        "summoned": True,
        "figure": _figure_summary(fig, recruited=True),
        "voice": fig.voice,
        "turn_prompt": _summon_prompt(fig, body.battle_state),
        "event": _event_to_dict(evt),
    }


@router.get("/runs/{run_id}/profile")
async def get_profile(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Aggregate the playthrough profile consumed by the final boss."""
    await _get_run_or_404(run_id, session)
    profile = await boss_profile.compute_profile(run_id)
    return {
        "profile": profile.to_dict(),
        "boss_prompt_blurbs": profile.boss_prompt_blurbs(),
    }


@router.post("/runs/{run_id}/quest/accept")
async def accept_quest(
    run_id: str,
    body: QuestAcceptRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Accept or retrieve the active quest offered by an NPC."""
    await _get_run_or_404(run_id, session)
    match = _find_npc_anchor(body.npc_id)
    if match is None:
        raise HTTPException(status_code=404, detail="NPC not found")

    anchor, _region = match
    if anchor.archetype not in {"merchant", "quest_giver"}:
        raise HTTPException(status_code=409, detail="NPC does not offer quests")

    quest = await quests.offer_quest(
        run_id, body.npc_id, candidate_dungeons=_candidate_dungeons(anchor)
    )
    return {"quest": quest.to_dict() if quest is not None else None}


@router.get("/runs/{run_id}/quests")
async def list_run_quests(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Return accepted/completed quests for a run."""
    await _get_run_or_404(run_id, session)
    return {"quests": await quests.list_quests(run_id)}


@router.post("/runs/{run_id}/events")
async def append_world_event(
    run_id: str,
    body: WorldEventRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Append a world event and complete matching event-log quests."""
    await _get_run_or_404(run_id, session)

    data = dict(body.data)
    if body.kind == "figure_recruited":
        figure_id = str(data.get("figure_id") or "")
        if not figure_id:
            raise HTTPException(status_code=400, detail="figure_id is required")
        if not await figures.recruit(run_id, figure_id):
            raise HTTPException(status_code=404, detail="Figure not found")
        events = await event_log.recent(run_id, limit=event_log.MAX_EVENTS)
        evt = next(
            e for e in reversed(events)
            if e.kind == "figure_recruited" and e.data.get("figure_id") == figure_id
        )
    else:
        evt = await event_log.append(run_id, body.kind, **data)

    completed = await quests.maybe_complete_quests(run_id, body.kind, **data)
    return {"event": _event_to_dict(evt), "completed_quests": completed}
