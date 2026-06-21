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
    # Wave 2: structured POIs overlaid on the grid (camp/town/den/landmark/goal).
    # Additive/Optional so the existing MapState consumers keep working.
    pois: list["POI"] = []


# ---- World structure (Wave 2 WorldSpec-lite; strict subset of the Wave-C WorldSpec) ----


class POI(BaseModel):
    """A point of interest on the map. `kind` is the structural role."""
    kind: Literal["camp", "town", "den", "landmark", "start", "goal"]
    x: int
    y: int
    name: str = ""


class Region(BaseModel):
    name: str
    biome: str = "plains"
    # Inclusive tile bounds [x0,y0,x1,y1]; optional so a flat map can omit them.
    bounds: Optional[list[int]] = None


class WorldSpecLite(BaseModel):
    """Structured world the FE can render now; the Wave-C generator must emit a
    superset of these fields so the frontend contract survives unchanged."""
    seed: int = 0
    width: int
    height: int
    regions: list[Region] = []
    pois: list[POI] = []
    start: Optional[POI] = None
    goal: Optional[POI] = None


# ---- Campsite (Wave 2: rest hub) ----


class RestResult(BaseModel):
    run_id: str
    healed: list[MonsterSummary] = []
    day: int = 0
    encounters_since_rest: int = 0
    message: str = ""


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
    # Additive, backward-compatible with old persisted JSON (all Optional).
    why: Optional[str] = None
    logic: Optional[float] = None
    persuasion: Optional[float] = None
    actor_id: Optional[str] = None


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


class TurnRequest(BaseModel):
    # The party agent the player chose to argue this round. None = auto-pick.
    actor_id: Optional[str] = None


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


class Scorecard(BaseModel):
    """Wave A: measurable before/after training delta against a fixed benchmark.

    Units are explicit to avoid the legacy `TrainJob.score_delta` ambiguity:
    win_rate_* are 0..1, judge_score_* are 0..100. Computed by the benchmark
    harness (deterministic: temp=0 + fixed seed + N-run averaging).
    """
    win_rate_before: float = 0.0
    win_rate_after: float = 0.0
    win_rate_delta: float = 0.0
    judge_score_before: float = 0.0
    judge_score_after: float = 0.0
    judge_score_delta: float = 0.0
    genome_diff: str = ""
    n_benchmark_runs: int = 0


class TrainingHistoryEntry(BaseModel):
    genome_version: int
    kind: Literal["gepa", "grpo", "evolution", "seed"]
    created_at: str
    judge_score: Optional[float] = None
    win_rate: Optional[float] = None
    note: str = ""


class TrainingHistory(BaseModel):
    monster_id: str
    entries: list[TrainingHistoryEntry] = []


class TrainJob(BaseModel):
    job_id: str
    monster_id: str
    kind: Literal["gepa", "grpo"]
    status: Literal["queued", "running", "awaiting_preference", "done", "failed"]
    score_delta: Optional[float] = None
    # Wave A: full measurable delta (Optional — populated when a benchmark ran).
    scorecard: Optional["Scorecard"] = None


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


# ---- Persistence (Wave A cross-cutting: run save / resume) ----


class RunSaveResult(BaseModel):
    """Result of POST /api/runs/{id}/save — snapshots run + party to durable PG."""
    run_id: str
    saved: bool
    saved_at: str
    party_size: int = 0


class RunResumeState(BaseModel):
    """Full durable run state for GET /api/runs/{id} (survives restart).

    Extends the in-flight RunState with the captured roster + a resume marker so
    the frontend can rehydrate a session after a reload.
    """
    id: str
    debate_topic: str
    player_x: int
    player_y: int
    status: str
    party: list[MonsterSummary] = []
    captured: list[MonsterSummary] = []
    saved_at: Optional[str] = None
    resumable: bool = False
