"""Gacha pool seed — named real-world personas pullable at run-start and via
dropped summon items (Wave A).

Each row is upserted by ``key`` into the ``personas`` table on app startup
(see ``app.party.personas_seed.upsert_personas``). The catalog is hand-curated
to span every ``MonsterDomain`` so party composition is meaningful: pulling
multiple PHILOSOPHY personas should feel different from a balanced spread.

Tier weights (used by Wave A's gacha roll):
  common      70%
  rare        25%
  legendary   5%

Stats follow the seed convention: total points cluster around 30 (atk + def +
mp/10) with archetype-flavored variance — engineers hit harder (high ATK,
lower DEF/MP), philosophers tank arguments (balanced), CEOs spend MP freely.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.db.models import DebateType, MonsterDomain, Persona

# ---------------------------------------------------------------------------
# Seed catalog
# ---------------------------------------------------------------------------

# Each row mirrors the ``Persona`` table columns. Wikipedia URLs are
# canonical English titles; the Wave A hydrate task fetches and distills these.
PERSONAS_SEED: list[dict[str, Any]] = [
    # ---- PHILOSOPHY ----
    {
        "key": "socrates",
        "name": "Socrates",
        "domain": MonsterDomain.PHILOSOPHY,
        "type": DebateType.socratic,
        "wiki_url": "https://en.wikipedia.org/wiki/Socrates",
        "tagline": "I know that I know nothing.",
        "tier": "rare",
        "default_atk": 11, "default_def": 13, "default_mp": 60, "default_max_hp": 100,
    },
    {
        "key": "nietzsche",
        "name": "Friedrich Nietzsche",
        "domain": MonsterDomain.PHILOSOPHY,
        "type": DebateType.chaos,
        "wiki_url": "https://en.wikipedia.org/wiki/Friedrich_Nietzsche",
        "tagline": "What does not kill me makes me stronger.",
        "tier": "rare",
        "default_atk": 14, "default_def": 9, "default_mp": 55, "default_max_hp": 95,
    },
    {
        "key": "camus",
        "name": "Albert Camus",
        "domain": MonsterDomain.PHILOSOPHY,
        "type": DebateType.pathos,
        "wiki_url": "https://en.wikipedia.org/wiki/Albert_Camus",
        "tagline": "One must imagine Sisyphus happy.",
        "tier": "common",
        "default_atk": 10, "default_def": 12, "default_mp": 55, "default_max_hp": 100,
    },
    {
        "key": "confucius",
        "name": "Confucius",
        "domain": MonsterDomain.PHILOSOPHY,
        "type": DebateType.ethos,
        "wiki_url": "https://en.wikipedia.org/wiki/Confucius",
        "tagline": "It does not matter how slowly you go as long as you do not stop.",
        "tier": "common",
        "default_atk": 9, "default_def": 14, "default_mp": 50, "default_max_hp": 105,
    },
    {
        "key": "simone_de_beauvoir",
        "name": "Simone de Beauvoir",
        "domain": MonsterDomain.PHILOSOPHY,
        "type": DebateType.rhetoric,
        "wiki_url": "https://en.wikipedia.org/wiki/Simone_de_Beauvoir",
        "tagline": "One is not born, but rather becomes, a woman.",
        "tier": "rare",
        "default_atk": 12, "default_def": 11, "default_mp": 55, "default_max_hp": 100,
    },

    # ---- ENGINEERING ----
    {
        "key": "linus_torvalds",
        "name": "Linus Torvalds",
        "domain": MonsterDomain.ENGINEERING,
        "type": DebateType.logos,
        "wiki_url": "https://en.wikipedia.org/wiki/Linus_Torvalds",
        "tagline": "Talk is cheap. Show me the code.",
        "tier": "common",
        "default_atk": 15, "default_def": 8, "default_mp": 45, "default_max_hp": 95,
    },
    {
        "key": "ada_lovelace",
        "name": "Ada Lovelace",
        "domain": MonsterDomain.ENGINEERING,
        "type": DebateType.logos,
        "wiki_url": "https://en.wikipedia.org/wiki/Ada_Lovelace",
        "tagline": "The machine is the imagination of its operator.",
        "tier": "rare",
        "default_atk": 12, "default_def": 10, "default_mp": 60, "default_max_hp": 95,
    },
    {
        "key": "alan_turing",
        "name": "Alan Turing",
        "domain": MonsterDomain.ENGINEERING,
        "type": DebateType.socratic,
        "wiki_url": "https://en.wikipedia.org/wiki/Alan_Turing",
        "tagline": "We can only see a short distance ahead, but we can see plenty there that needs to be done.",
        "tier": "legendary",
        "default_atk": 14, "default_def": 11, "default_mp": 65, "default_max_hp": 100,
    },
    {
        "key": "grace_hopper",
        "name": "Grace Hopper",
        "domain": MonsterDomain.ENGINEERING,
        "type": DebateType.ethos,
        "wiki_url": "https://en.wikipedia.org/wiki/Grace_Hopper",
        "tagline": "The most dangerous phrase is, 'we have always done it this way.'",
        "tier": "common",
        "default_atk": 11, "default_def": 12, "default_mp": 50, "default_max_hp": 100,
    },
    {
        "key": "carmack",
        "name": "John Carmack",
        "domain": MonsterDomain.ENGINEERING,
        "type": DebateType.logos,
        "wiki_url": "https://en.wikipedia.org/wiki/John_Carmack",
        "tagline": "Focused, hard work is the real key to success.",
        "tier": "common",
        "default_atk": 14, "default_def": 9, "default_mp": 45, "default_max_hp": 95,
    },

    # ---- SCIENCE ----
    {
        "key": "marie_curie",
        "name": "Marie Curie",
        "domain": MonsterDomain.SCIENCE,
        "type": DebateType.logos,
        "wiki_url": "https://en.wikipedia.org/wiki/Marie_Curie",
        "tagline": "Nothing in life is to be feared, it is only to be understood.",
        "tier": "rare",
        "default_atk": 12, "default_def": 12, "default_mp": 55, "default_max_hp": 100,
    },
    {
        "key": "einstein",
        "name": "Albert Einstein",
        "domain": MonsterDomain.SCIENCE,
        "type": DebateType.chaos,
        "wiki_url": "https://en.wikipedia.org/wiki/Albert_Einstein",
        "tagline": "Imagination is more important than knowledge.",
        "tier": "legendary",
        "default_atk": 13, "default_def": 11, "default_mp": 70, "default_max_hp": 100,
    },
    {
        "key": "feynman",
        "name": "Richard Feynman",
        "domain": MonsterDomain.SCIENCE,
        "type": DebateType.socratic,
        "wiki_url": "https://en.wikipedia.org/wiki/Richard_Feynman",
        "tagline": "I would rather have questions that can't be answered than answers that can't be questioned.",
        "tier": "rare",
        "default_atk": 13, "default_def": 10, "default_mp": 60, "default_max_hp": 100,
    },
    {
        "key": "darwin",
        "name": "Charles Darwin",
        "domain": MonsterDomain.SCIENCE,
        "type": DebateType.logos,
        "wiki_url": "https://en.wikipedia.org/wiki/Charles_Darwin",
        "tagline": "It is not the strongest of the species that survives, but the most adaptable.",
        "tier": "common",
        "default_atk": 11, "default_def": 13, "default_mp": 50, "default_max_hp": 105,
    },
    {
        "key": "carl_sagan",
        "name": "Carl Sagan",
        "domain": MonsterDomain.SCIENCE,
        "type": DebateType.pathos,
        "wiki_url": "https://en.wikipedia.org/wiki/Carl_Sagan",
        "tagline": "Extraordinary claims require extraordinary evidence.",
        "tier": "common",
        "default_atk": 10, "default_def": 12, "default_mp": 60, "default_max_hp": 100,
    },

    # ---- BUSINESS ----
    {
        "key": "steve_jobs",
        "name": "Steve Jobs",
        "domain": MonsterDomain.BUSINESS,
        "type": DebateType.rhetoric,
        "wiki_url": "https://en.wikipedia.org/wiki/Steve_Jobs",
        "tagline": "Real artists ship.",
        "tier": "rare",
        "default_atk": 14, "default_def": 9, "default_mp": 55, "default_max_hp": 95,
    },
    {
        "key": "satya_nadella",
        "name": "Satya Nadella",
        "domain": MonsterDomain.BUSINESS,
        "type": DebateType.ethos,
        "wiki_url": "https://en.wikipedia.org/wiki/Satya_Nadella",
        "tagline": "Our industry does not respect tradition — it only respects innovation.",
        "tier": "common",
        "default_atk": 11, "default_def": 12, "default_mp": 50, "default_max_hp": 100,
    },
    {
        "key": "warren_buffett",
        "name": "Warren Buffett",
        "domain": MonsterDomain.BUSINESS,
        "type": DebateType.logos,
        "wiki_url": "https://en.wikipedia.org/wiki/Warren_Buffett",
        "tagline": "Price is what you pay; value is what you get.",
        "tier": "common",
        "default_atk": 10, "default_def": 14, "default_mp": 45, "default_max_hp": 105,
    },
    {
        "key": "oprah_winfrey",
        "name": "Oprah Winfrey",
        "domain": MonsterDomain.BUSINESS,
        "type": DebateType.pathos,
        "wiki_url": "https://en.wikipedia.org/wiki/Oprah_Winfrey",
        "tagline": "Turn your wounds into wisdom.",
        "tier": "rare",
        "default_atk": 12, "default_def": 11, "default_mp": 60, "default_max_hp": 100,
    },

    # ---- ETHICS ----
    {
        "key": "mlk",
        "name": "Martin Luther King Jr.",
        "domain": MonsterDomain.ETHICS,
        "type": DebateType.pathos,
        "wiki_url": "https://en.wikipedia.org/wiki/Martin_Luther_King_Jr.",
        "tagline": "The arc of the moral universe is long, but it bends toward justice.",
        "tier": "legendary",
        "default_atk": 14, "default_def": 12, "default_mp": 65, "default_max_hp": 105,
    },
    {
        "key": "gandhi",
        "name": "Mahatma Gandhi",
        "domain": MonsterDomain.ETHICS,
        "type": DebateType.ethos,
        "wiki_url": "https://en.wikipedia.org/wiki/Mahatma_Gandhi",
        "tagline": "Be the change you wish to see in the world.",
        "tier": "rare",
        "default_atk": 10, "default_def": 15, "default_mp": 60, "default_max_hp": 110,
    },
    {
        "key": "hannah_arendt",
        "name": "Hannah Arendt",
        "domain": MonsterDomain.ETHICS,
        "type": DebateType.logos,
        "wiki_url": "https://en.wikipedia.org/wiki/Hannah_Arendt",
        "tagline": "The sad truth is that most evil is done by people who never make up their minds to be good or evil.",
        "tier": "rare",
        "default_atk": 12, "default_def": 11, "default_mp": 55, "default_max_hp": 100,
    },
    {
        "key": "peter_singer",
        "name": "Peter Singer",
        "domain": MonsterDomain.ETHICS,
        "type": DebateType.logos,
        "wiki_url": "https://en.wikipedia.org/wiki/Peter_Singer",
        "tagline": "All animals are equal — equality is a moral idea, not a fact.",
        "tier": "common",
        "default_atk": 12, "default_def": 10, "default_mp": 55, "default_max_hp": 100,
    },

    # ---- ART ----
    {
        "key": "shakespeare",
        "name": "William Shakespeare",
        "domain": MonsterDomain.ART,
        "type": DebateType.rhetoric,
        "wiki_url": "https://en.wikipedia.org/wiki/William_Shakespeare",
        "tagline": "Brevity is the soul of wit.",
        "tier": "legendary",
        "default_atk": 13, "default_def": 11, "default_mp": 65, "default_max_hp": 100,
    },
    {
        "key": "frida_kahlo",
        "name": "Frida Kahlo",
        "domain": MonsterDomain.ART,
        "type": DebateType.pathos,
        "wiki_url": "https://en.wikipedia.org/wiki/Frida_Kahlo",
        "tagline": "I paint my own reality.",
        "tier": "rare",
        "default_atk": 12, "default_def": 10, "default_mp": 60, "default_max_hp": 100,
    },
    {
        "key": "maya_angelou",
        "name": "Maya Angelou",
        "domain": MonsterDomain.ART,
        "type": DebateType.pathos,
        "wiki_url": "https://en.wikipedia.org/wiki/Maya_Angelou",
        "tagline": "There is no greater agony than bearing an untold story inside you.",
        "tier": "rare",
        "default_atk": 11, "default_def": 12, "default_mp": 60, "default_max_hp": 100,
    },
    {
        "key": "orson_welles",
        "name": "Orson Welles",
        "domain": MonsterDomain.ART,
        "type": DebateType.rhetoric,
        "wiki_url": "https://en.wikipedia.org/wiki/Orson_Welles",
        "tagline": "The enemy of art is the absence of limitations.",
        "tier": "common",
        "default_atk": 12, "default_def": 10, "default_mp": 55, "default_max_hp": 100,
    },

    # ---- GENERAL (versatile generalists; no domain match bonus, but resilient) ----
    {
        "key": "benjamin_franklin",
        "name": "Benjamin Franklin",
        "domain": MonsterDomain.GENERAL,
        "type": DebateType.logos,
        "wiki_url": "https://en.wikipedia.org/wiki/Benjamin_Franklin",
        "tagline": "An investment in knowledge pays the best interest.",
        "tier": "common",
        "default_atk": 11, "default_def": 11, "default_mp": 55, "default_max_hp": 105,
    },
    {
        "key": "mark_twain",
        "name": "Mark Twain",
        "domain": MonsterDomain.GENERAL,
        "type": DebateType.chaos,
        "wiki_url": "https://en.wikipedia.org/wiki/Mark_Twain",
        "tagline": "The two most important days in your life are the day you are born and the day you find out why.",
        "tier": "common",
        "default_atk": 12, "default_def": 11, "default_mp": 55, "default_max_hp": 100,
    },
    {
        "key": "winston_churchill",
        "name": "Winston Churchill",
        "domain": MonsterDomain.GENERAL,
        "type": DebateType.rhetoric,
        "wiki_url": "https://en.wikipedia.org/wiki/Winston_Churchill",
        "tagline": "Success is not final, failure is not fatal: it is the courage to continue that counts.",
        "tier": "rare",
        "default_atk": 13, "default_def": 12, "default_mp": 55, "default_max_hp": 105,
    },
]


async def upsert_personas(session: AsyncSession) -> int:
    """Insert/refresh the seed catalog. Returns the number of rows touched.

    Idempotent — re-running the app's startup hook does not duplicate rows
    (the primary key is ``Persona.key``) and updates rewrite the tagline /
    stats if the seed file changed.
    """
    existing = {p.key for p in (await session.execute(select(Persona))).scalars().all()}
    touched = 0
    for row in PERSONAS_SEED:
        if row["key"] in existing:
            # Update in place; cheap because the catalog is small.
            persona = await session.get(Persona, row["key"])
            if persona is None:
                continue  # shouldn't happen, but be defensive
            for field, value in row.items():
                setattr(persona, field, value)
            session.add(persona)
        else:
            session.add(Persona(**row))
        touched += 1
    await session.commit()
    return touched
