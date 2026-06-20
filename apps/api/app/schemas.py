"""FROZEN API contract (Wave 0). Pydantic models -> OpenAPI -> TS types.

These are the request/response shapes the frontend and all routers code against.
Add fields as Optional to avoid breaking the generated TS client mid-wave.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---- Common ----


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: bool
    redis: bool
    gateway: dict[str, Any]


# ---- Run / map ----


class CreateRunRequest(BaseModel):
    topic: str = Field(..., description="The debate topic for this entire run")
    seed: int = 0


class MonsterSummary(BaseModel):
    id: str
    name: str
    type: str
    owner: str
    level: int
    xp: int
    max_hp: int
    evolution_stage: int
    skills: list[Any] = []


class RunState(BaseModel):
    id: str
    debate_topic: str
    player_x: int
    player_y: int
    status: str
    party: list[MonsterSummary] = []


class TileEnemy(BaseModel):
    id: str
    x: int
    y: int
    sprite: str = "enemy"


class MapState(BaseModel):
    width: int
    height: int
    tiles: list[list[int]]  # 0 = walkable, 1 = blocked
    player_x: int
    player_y: int
    enemies: list[TileEnemy] = []


class MoveRequest(BaseModel):
    dx: int = 0
    dy: int = 0


class MoveResult(BaseModel):
    player_x: int
    player_y: int
    encounter_id: Optional[str] = None  # set when a collision triggers a battle


# ---- Encounter / debate ----


class CombatantState(BaseModel):
    monster_id: str
    name: str
    type: str
    role: Literal["party", "enemy"]
    hp: int
    max_hp: int


class Utterance(BaseModel):
    turn: int
    actor_id: str
    actor_role: Literal["party", "enemy", "judge"]
    skill_used: Optional[str] = None
    text: str
    ts: float


class JudgeVerdict(BaseModel):
    turn: int
    target: str
    score: float
    rationale: str
    damage: int


class EncounterState(BaseModel):
    id: str
    run_id: str
    topic: str
    phase: Literal["intro", "debating", "capturable", "won", "lost"] = "intro"
    turn_no: int = 0
    combatants: list[CombatantState] = []
    transcript: list[Utterance] = []
    verdicts: list[JudgeVerdict] = []


class CreateEncounterRequest(BaseModel):
    run_id: str
    wild_id: Optional[str] = None
    enemy_group_id: Optional[str] = None


class AutoRequest(BaseModel):
    rounds: int = 1


class PlayerArgueRequest(BaseModel):
    """WS-G: a human-typed argument for the player's lead party monster.

    `skill_id` is the chosen skill's NAME (skills have no separate id); the engine
    resolves it against the lead combatant's skills for the rhetorical type +
    damage power multiplier.
    """
    text: str
    skill_id: Optional[str] = None


class TurnResult(BaseModel):
    encounter: EncounterState
    new_utterances: list[Utterance] = []
    new_verdicts: list[JudgeVerdict] = []
    capturable_ids: list[str] = []


class CaptureRequest(BaseModel):
    wild_id: str


class CaptureResult(BaseModel):
    success: bool
    monster: Optional[MonsterSummary] = None
    message: str = ""


# ---- Gambits ----


class GambitRuleModel(BaseModel):
    id: Optional[str] = None
    priority: int = 0
    condition: dict[str, Any] = {}
    action: dict[str, Any] = {}
    enabled: bool = True


class GambitList(BaseModel):
    monster_id: str
    rules: list[GambitRuleModel] = []


# ---- Memory ----


class MemoryItem(BaseModel):
    id: str
    event_type: str
    summary: str
    content: str
    salience: float
    created_at: str


class MemoryQueryResult(BaseModel):
    monster_id: str
    items: list[MemoryItem] = []


# ---- Training ----


class TrainRequest(BaseModel):
    rounds: int = 4


class TrainJob(BaseModel):
    job_id: str
    monster_id: str
    kind: Literal["gepa", "grpo"]
    status: Literal["queued", "running", "awaiting_preference", "done", "failed"]
    score_delta: Optional[float] = None


class PreferenceVariant(BaseModel):
    variant_id: str
    transcript: list[Utterance] = []
    judge_score: float = 0.0


class PreferenceBatch(BaseModel):
    job_id: str
    monster_id: str
    variants: list[PreferenceVariant] = []


class PreferenceSubmit(BaseModel):
    ranking: list[str]  # variant_ids best -> worst
