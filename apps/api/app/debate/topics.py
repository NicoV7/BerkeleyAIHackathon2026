"""Debate topic catalog — a random topic is chosen when each battle starts.

Topics are deliberately spicy, two-sided, and one-line so a small local model can
argue either side. `pick_random_topic` is seedable for reproducible tests/training.
"""
from __future__ import annotations

import random

TOPIC_CATALOG: list[str] = [
    "Pineapple belongs on pizza.",
    "Social media does more harm than good.",
    "Artificial intelligence should be open-sourced.",
    "Remote work is better than working in an office.",
    "Money can buy happiness.",
    "Video games are a legitimate art form.",
    "Humans will colonize Mars within 50 years.",
    "Cats make better pets than dogs.",
    "College is no longer worth the cost.",
    "Self-driving cars will make roads safer.",
    "A four-day work week should be the standard.",
    "Tipping culture should be abolished.",
    "Cryptocurrency is the future of money.",
    "Homework should be banned in schools.",
    "Nuclear energy is the key to fighting climate change.",
    "Books are better than their movie adaptations.",
    "Space exploration is a waste of money.",
    "Privacy is dead and that's okay.",
    "Standardized testing should be eliminated.",
    "Zoos are unethical and should be closed.",
    "Voting should be mandatory.",
    "Genetically modified food is safe and necessary.",
    "Social media platforms should verify every user's identity.",
    "The death penalty should be abolished.",
    "Humans are inherently selfish.",
    "Free will is an illusion.",
    "Time travel, if possible, would do more harm than good.",
    "Aliens have already visited Earth.",
    "Universal basic income should be adopted everywhere.",
    "Professional athletes are paid too much.",
    "Fast fashion should be banned.",
    "Reading fiction is more valuable than reading non-fiction.",
    "Working from a beach is a productivity myth.",
    "Robots will take more jobs than they create.",
    "Climate change is the defining issue of our time.",
    "Censorship is sometimes justified.",
    "The internet has made us less intelligent.",
    "Vaccination should be mandatory.",
    "Animals deserve the same rights as humans.",
    "A hot dog is a sandwich.",
]


def pick_random_topic(seed: int | None = None) -> str:
    """Return a random debate topic. Pass a seed for reproducibility."""
    rng = random.Random(seed) if seed is not None else random
    return rng.choice(TOPIC_CATALOG)
