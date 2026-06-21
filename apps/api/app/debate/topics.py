"""Debate topic catalog — a random topic is chosen when each battle starts.

Topics are deliberately spicy, two-sided, and one-line so a small local model can
argue either side. `pick_random_topic` is seedable for reproducible tests/training.

THEMES: the player picks a THEME at run start; each battle draws a RANDOM topic
WITHIN that theme. Every topic is tagged with exactly one theme below. Picking a
theme filters the catalog to that theme's topics; an unknown/empty theme falls
back to the FULL catalog (logged warning, never raises) so a battle never 500s.
"""
from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)

# Each catalog entry is tagged with a theme. The flat TOPIC_CATALOG (used by the
# unseeded/full-catalog path and the no-theme fallback) is derived from this map
# so the two never drift apart.
TOPICS_BY_THEME: dict[str, list[str]] = {
    "Ethics": [
        "Money can buy happiness.",
        "Humans are inherently selfish.",
        "Free will is an illusion.",
        "The death penalty should be abolished.",
        "Censorship is sometimes justified.",
        "Animals deserve the same rights as humans.",
        "Zoos are unethical and should be closed.",
        "Reading fiction is more valuable than reading non-fiction.",
    ],
    "Technology": [
        "Artificial intelligence should be open-sourced.",
        "Self-driving cars will make roads safer.",
        "Cryptocurrency is the future of money.",
        "Privacy is dead and that's okay.",
        "Robots will take more jobs than they create.",
        "The internet has made us less intelligent.",
        "Social media platforms should verify every user's identity.",
        "Video games are a legitimate art form.",
    ],
    "Society": [
        "Social media does more harm than good.",
        "Remote work is better than working in an office.",
        "A four-day work week should be the standard.",
        "Tipping culture should be abolished.",
        "Universal basic income should be adopted everywhere.",
        "Voting should be mandatory.",
        "Professional athletes are paid too much.",
        "Working from a beach is a productivity myth.",
    ],
    "Science": [
        "Humans will colonize Mars within 50 years.",
        "Nuclear energy is the key to fighting climate change.",
        "Space exploration is a waste of money.",
        "Genetically modified food is safe and necessary.",
        "Time travel, if possible, would do more harm than good.",
        "Aliens have already visited Earth.",
        "Climate change is the defining issue of our time.",
        "Vaccination should be mandatory.",
    ],
    "Culture": [
        "Pineapple belongs on pizza.",
        "Cats make better pets than dogs.",
        "College is no longer worth the cost.",
        "Homework should be banned in schools.",
        "Books are better than their movie adaptations.",
        "Standardized testing should be eliminated.",
        "Fast fashion should be banned.",
        "A hot dog is a sandwich.",
    ],
}

# Ordered list of theme names for the frontend picker.
THEMES: list[str] = list(TOPICS_BY_THEME.keys())

# Flat catalog (full set) — derived so it can never drift from the themed map.
TOPIC_CATALOG: list[str] = [t for topics in TOPICS_BY_THEME.values() for t in topics]


def topics_for_theme(theme: str | None) -> list[str]:
    """Return the topics for a theme (case-insensitive), or the FULL catalog.

    Used by the frontend to show example topics under each theme. An unknown or
    empty theme returns the full catalog (no warning — this is a read helper).
    """
    if not theme:
        return list(TOPIC_CATALOG)
    for name, topics in TOPICS_BY_THEME.items():
        if name.lower() == theme.lower():
            return list(topics)
    return list(TOPIC_CATALOG)


def pick_random_topic(seed: int | None = None, theme: str | None = None) -> str:
    """Return a random debate topic.

    - ``seed``: pass for reproducibility. Deterministic for a given (seed, theme).
    - ``theme``: if given, restrict the draw to that theme's topics.

    Fallback contract: an unknown or empty theme (no matching topics) falls back
    to the FULL catalog with a logged warning. This NEVER raises, so encounter
    creation can always resolve a topic without 500ing.
    """
    pool: list[str]
    if theme:
        pool = []
        for name, topics in TOPICS_BY_THEME.items():
            if name.lower() == theme.lower():
                pool = list(topics)
                break
        if not pool:
            logger.warning(
                "pick_random_topic: unknown/empty theme %r; falling back to full catalog",
                theme,
            )
            pool = TOPIC_CATALOG
    else:
        pool = TOPIC_CATALOG

    rng = random.Random(seed) if seed is not None else random
    return rng.choice(pool)
