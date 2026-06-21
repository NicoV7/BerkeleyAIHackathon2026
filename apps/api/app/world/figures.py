"""figures.py — Historical figure catalog + summon mechanic (Wave 4).

Figures are summonable allies. The player "meets" them as NPCs in towns
(archetype=figure on a town anchor), wins a recruit-trial debate against them,
and then they appear in the SummonOverlay during any battle. Summoning a
figure injects ONE turn in their voice into the orchestrator stream.

Data:
    apps/api/data/figures/<id>.json — static catalog (bio, voice spec, sprite).
Runtime:
    Recruitment status lives in the per-run event log (figure_recruited event).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.world import event_log

CATALOG_DIR = Path(__file__).resolve().parents[2] / "data" / "figures"


@dataclass
class Figure:
    id: str
    name: str
    bio: str
    voice: str
    sprite: str
    signature_topics: list[str]
    famous_quotes: list[str]
    recruit_trial_topic: str
    alignment: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Figure":
        return cls(
            id=d["id"],
            name=d.get("name", d["id"].title()),
            bio=d.get("bio", ""),
            voice=d.get("voice", ""),
            sprite=d.get("sprite", ""),
            signature_topics=list(d.get("signature_topics") or []),
            famous_quotes=list(d.get("famous_quotes") or []),
            recruit_trial_topic=d.get("recruit_trial_topic", ""),
            alignment=d.get("alignment", "neutral"),
        )

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "bio": self.bio,
            "sprite": self.sprite,
            "alignment": self.alignment,
        }


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Figure]:
    """Load every figure JSON in ``CATALOG_DIR`` (memoized for process lifetime)."""
    out: dict[str, Figure] = {}
    if not CATALOG_DIR.exists():
        return out
    for path in sorted(CATALOG_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fig = Figure.from_dict(data)
            out[fig.id] = fig
        except Exception:  # noqa: BLE001 — never crash on a malformed catalog entry
            continue
    return out


def all_figures() -> list[Figure]:
    return list(_catalog().values())


def get_figure(figure_id: str) -> Figure | None:
    return _catalog().get(figure_id)


async def is_recruited(run_id: str, figure_id: str) -> bool:
    return await event_log.has(run_id, "figure_recruited", figure_id=figure_id)


async def recruit(run_id: str, figure_id: str) -> bool:
    """Mark a figure as recruited (idempotent)."""
    if get_figure(figure_id) is None:
        return False
    if await is_recruited(run_id, figure_id):
        return True
    await event_log.append(run_id, "figure_recruited", figure_id=figure_id)
    return True


async def recruited_list(run_id: str) -> list[Figure]:
    events = await event_log.recent(run_id, limit=event_log.MAX_EVENTS)
    seen: set[str] = set()
    out: list[Figure] = []
    for evt in events:
        if evt.kind != "figure_recruited":
            continue
        fid = evt.data.get("figure_id")
        if not fid or fid in seen:
            continue
        fig = get_figure(fid)
        if fig is not None:
            out.append(fig)
            seen.add(fid)
    return out


def reset_catalog_cache() -> None:
    """Test hook: drop the lru_cache so a fresh load picks up new files."""
    _catalog.cache_clear()
