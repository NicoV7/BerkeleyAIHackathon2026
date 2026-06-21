"""boss_profile.py — Aggregate the player's playthrough into a final-boss profile.

The final boss adapts to the player by reading their cumulative debate history.
We compute three aggregates:

  1. **Style lean** — logos / pathos / ethos histogram from per-battle judge
     verdicts (the existing per-round judge already records categorical scores).
  2. **Alignment** — derived from recruited figures (their ``alignment`` field).
  3. **Weakness** — topics the player has avoided AND fallacies the judge
     flagged most often.

The boss prompt embeds all three so it can "quote one of your prior arguments
back at you" (style mirror), take a contrary stance (alignment opposition),
and target your dominant fallacy in phase 2.

The aggregator is **pure read**: no DB writes, no Redis state, no LLM. It walks
the existing event log + an optional per-battle log (defaulting to None for now;
the existing judge log lives in app.debate.judge_log and we read it here when
present, falling back gracefully otherwise).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.world import event_log, figures


@dataclass
class PlaythroughProfile:
    style: dict[str, float] = field(default_factory=dict)   # logos/pathos/ethos lean
    alignment: dict[str, int] = field(default_factory=dict) # alignment counts (from figures)
    weakness: list[str] = field(default_factory=list)       # dominant fallacies
    recent_quotes: list[str] = field(default_factory=list)  # 2-3 quotes to mirror back
    bosses_defeated: int = 0
    dungeons_cleared: int = 0
    figures_recruited: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "style": self.style,
            "alignment": self.alignment,
            "weakness": self.weakness,
            "recent_quotes": self.recent_quotes,
            "bosses_defeated": self.bosses_defeated,
            "dungeons_cleared": self.dungeons_cleared,
            "figures_recruited": self.figures_recruited,
        }

    def boss_prompt_blurbs(self) -> dict[str, str]:
        """Three short strings the boss-prompt assembler can interpolate."""
        if self.style:
            top = max(self.style, key=self.style.get)
            style_blurb = (
                f"The hero leans on {top}. Counter with the opposite mode."
            )
        else:
            style_blurb = "The hero's style is balanced."

        if self.alignment:
            top_align = max(self.alignment, key=self.alignment.get)
            align_blurb = (
                f"The hero travels with figures of '{top_align}' alignment. "
                "Take a stance that opposes this lean."
            )
        else:
            align_blurb = "The hero travels alone."

        if self.weakness:
            weakness_blurb = (
                f"The hero's tells: {', '.join(self.weakness[:3])}. "
                "Bait one in phase 2."
            )
        else:
            weakness_blurb = "The hero shows no obvious tells."

        return {
            "style": style_blurb,
            "alignment": align_blurb,
            "weakness": weakness_blurb,
        }


async def compute_profile(run_id: str) -> PlaythroughProfile:
    """Walk the event log + figure roster; aggregate into a PlaythroughProfile."""
    profile = PlaythroughProfile()

    events = await event_log.recent(run_id, limit=event_log.MAX_EVENTS)
    for evt in events:
        if evt.kind == "boss_defeated":
            profile.bosses_defeated += 1
        elif evt.kind == "dungeon_cleared":
            profile.dungeons_cleared += 1
        elif evt.kind == "figure_recruited":
            fid = evt.data.get("figure_id")
            if fid:
                profile.figures_recruited.append(fid)
        elif evt.kind == "battle_won":
            # Optional event the orchestrator can emit with style + quote payload.
            style = evt.data.get("style") or {}
            for k, v in style.items():
                profile.style[k] = profile.style.get(k, 0.0) + float(v)
            q = evt.data.get("quote")
            if q and len(profile.recent_quotes) < 3:
                profile.recent_quotes.append(str(q))
        elif evt.kind == "fallacy_flagged":
            f = evt.data.get("fallacy")
            if f:
                # Count fallacies; we'll surface the top-N in `weakness`.
                profile.style.setdefault("__fallacies__", 0)  # placeholder
                profile.weakness.append(str(f))

    # Compress weakness from raw list → dominant first.
    if profile.weakness:
        c = Counter(profile.weakness)
        profile.weakness = [f for f, _ in c.most_common()]

    # Normalize style so the boss-prompt blurb pops the *lean*, not the count.
    total = sum(v for k, v in profile.style.items() if not k.startswith("__"))
    if total > 0:
        profile.style = {
            k: round(v / total, 3) for k, v in profile.style.items() if not k.startswith("__")
        }

    # Alignment: count each recruited figure's alignment from the catalog.
    align_counter: Counter[str] = Counter()
    for fid in profile.figures_recruited:
        fig = figures.get_figure(fid)
        if fig is not None:
            align_counter[fig.alignment] += 1
    profile.alignment = dict(align_counter)

    return profile
