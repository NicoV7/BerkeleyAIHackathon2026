"""bake_canonical_world.py — Stitch Watabou JSON + curation YAML into THE world.

Reads:
    apps/api/data/world/curation.yaml          (hand-curated names/lore/NPCs)
    apps/api/data/world/watabou_source/*.json  (raw Watabou exports)

Writes:
    apps/api/data/world/canonical.json         (overworld bundle: spec + tiles)
    apps/api/data/world/interiors/<safe_poi>.json  (per-interior bundles)

Run:
    python -m apps.api.scripts.bake_canonical_world

The script is idempotent — re-running it overwrites the artifacts. The whole
pipeline is pure + deterministic, so two bakes from the same inputs produce
byte-identical JSON.

Failure modes:
    - Missing source file → loud error, skip the affected POI.
    - Missing curation.yaml → write a "stub" world with the existing procgen so
      the runtime never crashes on a half-baked check-in.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

# Bake is meant to be run from the repo root or via `python -m`; either way,
# we resolve paths relative to THIS file so the bake works from any cwd.
ROOT = Path(__file__).resolve().parents[1]  # apps/api
DATA = ROOT / "data" / "world"
SOURCES = DATA / "watabou_source"
INTERIORS_OUT = DATA / "interiors"
CURATION = DATA / "curation.yaml"
OUT_CANONICAL = DATA / "canonical.json"

# Match the overworld dimensions in routers/map.py so the FE renders the
# canonical map at the exact same grid as the existing procgen path.
OVERWORLD_W = 20
OVERWORLD_H = 15
# Interior dimensions match build_interior in routers/world.py.
INTERIOR_W = 16
INTERIOR_H = 12


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write artifacts; just report what WOULD be written.",
    )
    args = parser.parse_args(argv)

    if not CURATION.exists():
        print(
            f"!! No curation.yaml at {CURATION} — nothing to bake. "
            "Drop your Watabou exports in watabou_source/ and edit curation.yaml.",
            file=sys.stderr,
        )
        return 2

    curation = yaml.safe_load(CURATION.read_text(encoding="utf-8")) or {}
    seed = int((curation.get("world") or {}).get("seed", 0))
    world_name = (curation.get("world") or {}).get("name", "Canonical")

    overworld_bundle = _bake_overworld(curation, seed)
    if overworld_bundle is None:
        print("!! Overworld bake failed — aborting.", file=sys.stderr)
        return 3

    interiors = _bake_interiors(curation, seed)

    artifact = {
        "version": 1,
        "name": world_name,
        "world": overworld_bundle["spec"],
        "tiles": overworld_bundle["tiles"],
    }

    if args.dry_run:
        print(f"[dry-run] would write {OUT_CANONICAL} ({len(json.dumps(artifact))} bytes)")
        for poi_key, bundle in interiors.items():
            safe = _safe_filename(poi_key)
            print(f"[dry-run] would write interiors/{safe}.json")
        return 0

    INTERIORS_OUT.mkdir(parents=True, exist_ok=True)
    OUT_CANONICAL.write_text(_pretty_json(artifact), encoding="utf-8")
    print(f"✓ wrote {OUT_CANONICAL}")
    for poi_key, bundle in interiors.items():
        safe = _safe_filename(poi_key)
        path = INTERIORS_OUT / f"{safe}.json"
        path.write_text(_pretty_json(bundle), encoding="utf-8")
        print(f"✓ wrote interiors/{safe}.json")
    return 0


def _bake_overworld(curation: dict[str, Any], seed: int) -> dict[str, Any] | None:
    """Build the overworld spec + tiles from the realm source + curation."""
    over = curation.get("overworld") or {}
    src = over.get("source")
    region_curation = over.get("regions") or []

    # Lazy import so the bake script doesn't pull the whole app graph at import
    from app.world.watabou_import import import_watabou
    from app.routers.world import build_world, _attach_interior, poi_id

    if not src:
        print("!! curation.yaml is missing overworld.source", file=sys.stderr)
        return None
    src_path = SOURCES / src
    if not src_path.exists():
        print(
            f"!! overworld source not found: {src_path}\n"
            "   place your Watabou realm JSON export there and re-run.",
            file=sys.stderr,
        )
        return None

    imp = import_watabou(src_path, OVERWORLD_W, OVERWORLD_H, seed=seed)

    # Apply curation: override region names + lore by substring match.
    enriched_regions = []
    for r in imp.spec.regions:
        out = r.model_copy()
        for rule in region_curation:
            match = (rule.get("match") or "").lower()
            if match and match in r.name.lower():
                if rule.get("name"):
                    out = out.model_copy(update={"name": rule["name"]})
                if rule.get("lore"):
                    out = out.model_copy(update={"lore": rule["lore"]})
                break
        enriched_regions.append(out)

    # Apply curation: rename POIs that match the overworld_poi rules, mark
    # them as scripted, and decorate enterable POIs with interior_seed/kind.
    scripted_keys: dict[str, str] = {}  # poi_key -> friendly name override
    for kind in ("towns", "dungeons"):
        for entry in (curation.get(kind) or []):
            key = entry.get("overworld_poi")
            if key:
                scripted_keys[key] = entry.get("name", "")

    enriched_pois = []
    for p in imp.spec.pois:
        key = poi_id(p)
        out = _attach_interior(seed, p)
        if key in scripted_keys:
            override = scripted_keys[key] or out.name
            out = out.model_copy(update={"name": override, "scripted": True})
        enriched_pois.append(out)

    spec = imp.spec.model_copy(
        update={
            "regions": enriched_regions,
            "pois": enriched_pois,
        }
    )
    return {
        "spec": json.loads(spec.model_dump_json()),
        "tiles": imp.tiles,
    }


def _bake_interiors(curation: dict[str, Any], seed: int) -> dict[str, Any]:
    """Build each interior bundle from its source file + town/dungeon curation."""
    from app.schemas import NPCAnchor
    from app.world.watabou_import import import_watabou

    out: dict[str, dict[str, Any]] = {}
    for kind, biome_default in (("towns", "town"), ("dungeons", "dungeon")):
        for entry in (curation.get(kind) or []):
            poi_key = entry.get("overworld_poi")
            src = entry.get("source")
            if not (poi_key and src):
                print(
                    f"!! {kind} entry missing overworld_poi or source: {entry}",
                    file=sys.stderr,
                )
                continue
            src_path = SOURCES / src
            if not src_path.exists():
                print(f"!! interior source not found: {src_path}", file=sys.stderr)
                continue

            imp = import_watabou(
                src_path,
                INTERIOR_W,
                INTERIOR_H,
                name=entry.get("name") or src.replace(".json", ""),
                seed=seed,
            )
            spec = imp.spec

            # Apply curation: region lore + npc anchors (towns only).
            if entry.get("lore") and spec.regions:
                r0 = spec.regions[0].model_copy(update={"lore": entry["lore"]})
                spec = spec.model_copy(update={"regions": [r0, *spec.regions[1:]]})

            anchors = _build_anchors(entry, imp.tiles, INTERIOR_W, INTERIOR_H)
            if anchors:
                # Pin anchors onto the start POI so the FE knows where they live.
                pois = []
                attached = False
                for p in spec.pois:
                    if not attached and p.kind == "start":
                        pois.append(
                            p.model_copy(
                                update={"npc_anchors": anchors, "scripted": True}
                            )
                        )
                        attached = True
                    else:
                        pois.append(p)
                if not attached and pois:
                    pois[0] = pois[0].model_copy(
                        update={"npc_anchors": anchors, "scripted": True}
                    )
                spec = spec.model_copy(update={"pois": pois})

            out[poi_key] = {
                "version": 1,
                "name": entry.get("name") or src,
                "world": json.loads(spec.model_dump_json()),
                "tiles": imp.tiles,
            }
    return out


def _build_anchors(
    entry: dict[str, Any], tiles: list[list[int]], width: int, height: int
) -> list["NPCAnchor"]:
    """Place NPC anchors on FEATURE tiles (deterministic scan order).

    Returns NPCAnchor MODEL instances (not dicts) so the caller can pass them
    straight into POI.model_copy(update={"npc_anchors": [...]}) without tripping
    Pydantic's serializer mismatch.
    """
    from app.schemas import NPCAnchor
    from app.world.algorithms.base import FEATURE

    feature_tiles: list[tuple[int, int]] = []
    for y in range(height):
        for x in range(width):
            if tiles[y][x] == FEATURE:
                feature_tiles.append((x, y))

    out: list[NPCAnchor] = []
    for i, npc in enumerate(entry.get("npcs") or []):
        if i >= len(feature_tiles):
            break
        x, y = feature_tiles[i]
        npc_id = f"{entry.get('overworld_poi','poi').replace(':','_')}__{i}"
        out.append(
            NPCAnchor(
                npc_id=npc_id,
                archetype=npc.get("archetype", "villager"),
                x=x,
                y=y,
                name=npc.get("name", ""),
                figure_id=npc.get("figure_id"),
            )
        )
    return out


def _safe_filename(poi_key: str) -> str:
    return poi_key.replace(":", "_").replace("/", "_")


def _pretty_json(data: Any) -> str:
    # Sorted keys + 2-space indent so re-baking the same inputs produces a
    # byte-identical diff; tiles are compacted to keep the file readable.
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
