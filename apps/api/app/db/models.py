"""SQLModel tables — the FROZEN durable schema (Wave 0 contract).

All six Wave 1 workstreams build against these tables. Do not rename columns or
tables without coordinating; ADD new optional columns/tables in your own module
if you need workstream-local state.

Tables:
  Run               one playthrough (topic, player position, status)
  Monster           party members AND wild/enemy agents (the "Pokémon")
  Skill             debate-move catalog (seeded) + learned moves
  GambitRule        FF12 condition->action behavior rules, ordered per monster
  Memory            per-monster event memory for hybrid RAG (vector + keyword)
  Encounter         durable record of a battle
  TrainingArtifact  GEPA/GRPO genome-optimization outputs
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

EMBED_DIM = 768  # nomic-embed-text dimension


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    # Naive UTC: the timestamp columns are TIMESTAMP WITHOUT TIME ZONE, and
    # asyncpg rejects tz-aware values against them. Keep everything naive-UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- Enums (mirror packages/shared/enums.ts) ----


class MonsterOwner(str, Enum):
    player = "player"
    wild = "wild"
    enemy = "enemy"


class DebateType(str, Enum):
    """The 'element' of a debater — drives the type-effectiveness chart."""

    logos = "LOGOS"        # logic / data
    pathos = "PATHOS"      # emotion / story
    ethos = "ETHOS"        # credibility / authority
    chaos = "CHAOS"        # disruption / reframing
    socratic = "SOCRATIC"  # questioning
    rhetoric = "RHETORIC"  # style / framing


class EventType(str, Enum):
    battle = "BATTLE"
    player = "PLAYER"
    character = "CHARACTER"


class EncounterResult(str, Enum):
    win = "win"
    loss = "loss"
    capture = "capture"
    flee = "flee"
    ongoing = "ongoing"


class RunStatus(str, Enum):
    active = "active"
    ended = "ended"


# ---- Tables ----


class Run(SQLModel, table=True):
    __tablename__ = "runs"

    id: str = Field(default_factory=_uuid, primary_key=True)
    debate_topic: str
    seed: int = 0
    player_x: int = 0
    player_y: int = 0
    status: RunStatus = Field(default=RunStatus.active)
    created_at: datetime = Field(default_factory=_now)


class Monster(SQLModel, table=True):
    __tablename__ = "monsters"

    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    owner: MonsterOwner = Field(default=MonsterOwner.wild)
    name: str
    type: DebateType = Field(default=DebateType.logos)

    # Genome pieces (the only things training mutates) ----
    persona: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    harness: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    skills: list[Any] = Field(default_factory=list, sa_column=Column(JSONB))
    genome_version: int = 1

    # Progression ----
    level: int = 1
    xp: int = 0
    max_hp: int = 100
    evolution_stage: int = 0

    # Which gateway model this agent runs on (bottom-up: defaults to local).
    model: Optional[str] = None

    created_at: datetime = Field(default_factory=_now)


class Skill(SQLModel, table=True):
    __tablename__ = "skills"

    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    type: DebateType = Field(default=DebateType.logos)
    description: str = ""
    prompt_fragment: str = ""  # how the move is injected into the agent prompt
    power: float = 1.0
    cost: int = 0


class GambitRule(SQLModel, table=True):
    __tablename__ = "gambit_rules"

    id: str = Field(default_factory=_uuid, primary_key=True)
    monster_id: str = Field(foreign_key="monsters.id", index=True)
    priority: int = 0
    condition: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    action: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    enabled: bool = True


class Memory(SQLModel, table=True):
    __tablename__ = "memories"
    __table_args__ = (
        Index(
            "ix_memories_embedding",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": "100"},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_memories_keywords",
            "keywords",
            postgresql_using="gin",
            postgresql_ops={"keywords": "gin_trgm_ops"},
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    monster_id: str = Field(foreign_key="monsters.id", index=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    event_type: EventType = Field(default=EventType.battle)
    content: str = Field(sa_column=Column(Text))
    summary: str = Field(default="", sa_column=Column(Text))  # short form injected into context
    embedding: Optional[list[float]] = Field(default=None, sa_column=Column(Vector(EMBED_DIM)))
    # Keyword search vector — populated by the memory store on write.
    keywords: Optional[str] = Field(default=None, sa_column=Column(Text))
    salience: float = 0.5
    encounter_id: Optional[str] = Field(default=None, foreign_key="encounters.id")
    created_at: datetime = Field(default_factory=_now)


class Encounter(SQLModel, table=True):
    __tablename__ = "encounters"

    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    topic: str
    enemy_ids: list[Any] = Field(default_factory=list, sa_column=Column(JSONB))
    party_ids: list[Any] = Field(default_factory=list, sa_column=Column(JSONB))
    result: EncounterResult = Field(default=EncounterResult.ongoing)
    transcript_ref: Optional[str] = None  # legacy redis key pointer (TTL'd)
    # Durable snapshot written on finalize, so the conversation survives the
    # Redis TTL and the cache can be evicted to avoid context pollution.
    transcript: list[Any] = Field(default_factory=list, sa_column=Column(JSONB))
    verdicts: list[Any] = Field(default_factory=list, sa_column=Column(JSONB))
    final_hp: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    created_at: datetime = Field(default_factory=_now)


class TrainingArtifact(SQLModel, table=True):
    __tablename__ = "training_artifacts"

    id: str = Field(default_factory=_uuid, primary_key=True)
    monster_id: str = Field(foreign_key="monsters.id", index=True)
    kind: str = "gepa"  # 'gepa' | 'grpo'
    genome_before: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    genome_after: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    score_delta: float = 0.0
    accepted: bool = False
    created_at: datetime = Field(default_factory=_now)
