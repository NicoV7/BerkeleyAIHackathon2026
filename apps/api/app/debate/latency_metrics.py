"""latency_metrics.py — per-round / per-utterance battle latency instrumentation.

WS-0-LAT, instrument-FIRST: make battle latency MEASURABLE so the team has a
data-driven local-vs-hosted go/no-go. This module is the small, reusable seam the
orchestrator (and WS-4 later) emits through. It is deliberately dependency-free
(stdlib only), has negligible overhead, and never raises into the battle loop.

What it captures
----------------
  * Round duration (wall-clock ms for one pass over the turn queue).
  * Per-utterance generation time (ms) keyed by actor + role + side.
  * Whether a templated FALLBACK was used for an utterance, and WHY:
      - "timeout"      — the stream's first-token guard fired,
      - "empty"        — the model returned nothing / errored,
      - "judge_deadline" — the judge deadline fired (human round).
  * A process-wide fallback-rate counter (utterances total vs fallbacks).

Output format
-------------
Each finished round emits ONE structured log line on the ``battle.latency``
logger at INFO, JSON-encoded so it greps/parses cleanly::

    battle.latency {"event": "round", "eid": "...", "round_ms": 8421,
      "utterances": 3, "fallbacks": 1, "fallback_rate": 0.333,
      "gen_ms": {"p50": 2100, "p95": 5200, "max": 5200},
      "actors": [{"actor_id": "...", "role": "enemy", "side": "against",
                  "gen_ms": 5200, "fallback": true, "fallback_reason": "timeout"}]}

The same numbers are returned as a plain dict from ``RoundTimer.finish()`` so the
spike script / tests can consume them WITHOUT parsing logs.

Usage (orchestrator)
--------------------
    rt = RoundTimer.start(eid)
    ...
    with rt.utterance(actor.monster_id, actor.role, actor.side) as u:
        text = ... generate ...
        if used_fallback:
            u.mark_fallback("timeout")
    ...
    metrics = rt.finish()   # logs the round line + returns the dict

Everything is best-effort: a metrics failure must never break a battle round, so
the public entry points swallow their own errors.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

log = logging.getLogger("battle.latency")

# Process-wide fallback counters. Cheap atomic-ish ints (GIL-protected for the
# +=). Exposed via ``fallback_rate()`` so a /metrics endpoint or the spike script
# can read the lifetime rate. Reset with ``reset_counters()`` (tests / spikes).
_TOTAL_UTTERANCES = 0
_TOTAL_FALLBACKS = 0
# Per-reason tally so the go/no-go can tell "model too slow" (timeout) from
# "model returned junk" (empty) — they imply different fixes.
_FALLBACK_BY_REASON: dict[str, int] = {}


def reset_counters() -> None:
    """Zero the process-wide counters (used by the spike script + tests)."""
    global _TOTAL_UTTERANCES, _TOTAL_FALLBACKS
    _TOTAL_UTTERANCES = 0
    _TOTAL_FALLBACKS = 0
    _FALLBACK_BY_REASON.clear()


def fallback_rate() -> float:
    """Lifetime fallback rate (fallbacks / utterances) — 0.0 when none seen."""
    return (_TOTAL_FALLBACKS / _TOTAL_UTTERANCES) if _TOTAL_UTTERANCES else 0.0


def counters_snapshot() -> dict[str, object]:
    """Read-only view of the lifetime counters for /metrics or the spike."""
    return {
        "utterances": _TOTAL_UTTERANCES,
        "fallbacks": _TOTAL_FALLBACKS,
        "fallback_rate": round(fallback_rate(), 4),
        "fallback_by_reason": dict(_FALLBACK_BY_REASON),
    }


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (no numpy dependency). Empty -> 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # nearest-rank: rank = ceil(pct/100 * N), 1-indexed
    rank = max(1, int(round((pct / 100.0) * len(ordered) + 0.4999)))
    rank = min(rank, len(ordered))
    return ordered[rank - 1]


@dataclass
class UtteranceMetric:
    """One actor turn's timing + fallback outcome."""

    actor_id: str
    role: str
    side: Optional[str]
    gen_ms: int = 0
    fallback: bool = False
    fallback_reason: Optional[str] = None
    _t0: float = field(default=0.0, repr=False)

    def mark_fallback(self, reason: str) -> None:
        """Record that this utterance fell back to a template, and why.

        ``reason`` is a short stable token: "timeout" | "empty" | "judge_deadline".
        """
        self.fallback = True
        self.fallback_reason = reason


@dataclass
class RoundTimer:
    """Times one round and accumulates per-utterance metrics.

    Build with ``RoundTimer.start(eid)``, wrap each utterance generation in
    ``with rt.utterance(...) as u:`` (call ``u.mark_fallback(reason)`` if a
    template was used), then call ``rt.finish()`` once. ``finish()`` logs the
    structured round line and returns the metrics dict; it is idempotent-safe.
    """

    eid: str
    round_no: int = 0
    _t0: float = field(default_factory=time.monotonic, repr=False)
    utterances: list[UtteranceMetric] = field(default_factory=list)

    @classmethod
    def start(cls, eid: str, round_no: int = 0) -> "RoundTimer":
        return cls(eid=eid, round_no=round_no)

    @contextmanager
    def utterance(
        self, actor_id: str, role: str, side: Optional[str] = None
    ) -> Iterator[UtteranceMetric]:
        """Context manager timing a single actor turn.

        Increments the process-wide utterance counter on exit, and the fallback
        counter if ``mark_fallback`` was called. Never raises out of the metrics
        path — exceptions from the wrapped body propagate normally (we still
        record timing/fallback in a ``finally``).
        """
        m = UtteranceMetric(actor_id=actor_id, role=role, side=side)
        m._t0 = time.monotonic()
        self.utterances.append(m)
        try:
            yield m
        finally:
            try:
                m.gen_ms = int(round((time.monotonic() - m._t0) * 1000))
                global _TOTAL_UTTERANCES, _TOTAL_FALLBACKS
                _TOTAL_UTTERANCES += 1
                if m.fallback:
                    _TOTAL_FALLBACKS += 1
                    reason = m.fallback_reason or "unknown"
                    _FALLBACK_BY_REASON[reason] = _FALLBACK_BY_REASON.get(reason, 0) + 1
            except Exception:  # noqa: BLE001 — metrics must never break the round
                pass

    def record_utterance(
        self,
        actor_id: str,
        role: str,
        side: Optional[str],
        gen_ms: int,
        fallback: bool = False,
        fallback_reason: Optional[str] = None,
    ) -> None:
        """Record an already-timed utterance (for callers that time it themselves,
        e.g. the streaming path where the generator is drained inline)."""
        m = UtteranceMetric(
            actor_id=actor_id,
            role=role,
            side=side,
            gen_ms=int(gen_ms),
            fallback=bool(fallback),
            fallback_reason=fallback_reason,
        )
        self.utterances.append(m)
        try:
            global _TOTAL_UTTERANCES, _TOTAL_FALLBACKS
            _TOTAL_UTTERANCES += 1
            if m.fallback:
                _TOTAL_FALLBACKS += 1
                reason = m.fallback_reason or "unknown"
                _FALLBACK_BY_REASON[reason] = _FALLBACK_BY_REASON.get(reason, 0) + 1
        except Exception:  # noqa: BLE001
            pass

    def summary(self) -> dict[str, object]:
        """Compute (but do not log) the round metrics dict."""
        round_ms = int(round((time.monotonic() - self._t0) * 1000))
        gen_values = [float(u.gen_ms) for u in self.utterances]
        fallbacks = sum(1 for u in self.utterances if u.fallback)
        n = len(self.utterances)
        return {
            "event": "round",
            "eid": self.eid,
            "round_no": self.round_no,
            "round_ms": round_ms,
            "utterances": n,
            "fallbacks": fallbacks,
            "fallback_rate": round((fallbacks / n) if n else 0.0, 4),
            "gen_ms": {
                "p50": int(_percentile(gen_values, 50)),
                "p95": int(_percentile(gen_values, 95)),
                "max": int(max(gen_values)) if gen_values else 0,
            },
            "actors": [
                {
                    "actor_id": u.actor_id,
                    "role": u.role,
                    "side": u.side,
                    "gen_ms": u.gen_ms,
                    "fallback": u.fallback,
                    "fallback_reason": u.fallback_reason,
                }
                for u in self.utterances
            ],
        }

    def finish(self) -> dict[str, object]:
        """Log the structured round line and return the metrics dict.

        Best-effort logging: a logging failure is swallowed so the round still
        returns its metrics to programmatic callers (spike script / tests).
        """
        data = self.summary()
        try:
            log.info("battle.latency %s", json.dumps(data, separators=(",", ":")))
        except Exception:  # noqa: BLE001
            pass
        return data
