"""latency_spike.py — local-vs-hosted battle latency go/no-go (WS-0-LAT).

Instrument-FIRST deliverable: give a human the numbers to decide whether the LIVE
battle path can stay on local Ollama or needs a hosted candidate. It runs N WARM
self-play battle rounds locally and prints p50 / p95 round latency + the fallback
rate (the share of utterances that degraded to a template, with the WHY split:
timeout vs empty). Optionally it runs the SAME utterance prompt through a hosted
candidate (``--hosted groq/llama-3.1-8b-instant``) so you can compare a single
hosted generation's latency head-to-head.

It is runnable OFFLINE and degrades gracefully:
  * No Ollama  -> every local round falls back to templates instantly; you'll see
    a ~100% fallback rate and near-zero gen latency. The script still prints the
    p50/p95 of whatever it measured and flags the fallback rate so the result is
    obviously "local model not answering" rather than a crash.
  * No hosted keys -> the hosted leg reports "skipped: no key for <provider>"
    instead of erroring.

Usage (run with apps/api on sys.path — e.g. from the api container, or
`uv run --project apps/api python -m scripts.latency_spike ...` from the repo):

    python -m scripts.latency_spike                       # 3 local warm rounds
    python -m scripts.latency_spike --rounds 5 --model gemma3:1b
    python -m scripts.latency_spike --hosted groq/llama-3.1-8b-instant
    python -m scripts.latency_spike --json                # machine-readable only

Prints a human summary to stderr and a single JSON object to stdout (so it can be
piped). NEVER requires Redis (uses the headless self-play engine) or live keys.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import traceback
from typing import Any

# Intended to run with apps/api on sys.path.
from app.config import settings  # noqa: E402
from app.debate import latency_metrics  # noqa: E402
from app.debate.orchestrator import (  # noqa: E402
    Combatant,
    _build_actor_messages,
    _run_self_play_async,
)

DEFAULT_TOPIC = "Pineapple belongs on pizza."


def _percentile(values: list[float], pct: float) -> float:
    return latency_metrics._percentile(values, pct)


def _mk_monsters(model: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Two plain-dict combatants for headless self-play (no DB/Redis)."""
    party = {
        "id": "spike-party",
        "name": "Rookie",
        "type": "LOGOS",
        "owner": "player",
        "level": 2,
        "max_hp": 100,
        "skills": [],
        "model": model,
    }
    enemy = {
        "id": "spike-enemy",
        "name": "Sophist",
        "type": "LOGOS",
        "owner": "wild",
        "level": 2,
        "max_hp": 100,
        "skills": [],
        "model": model,
    }
    return party, enemy


async def _spike_local(topic: str, rounds: int, model: str, warmup: bool) -> dict[str, Any]:
    """Run `rounds` headless self-play battles; collect per-round latency + fallbacks.

    Self-play uses the SAME ``_generate_utterance`` + ``score_round`` path as a live
    battle (minus Redis/WS), so the gen latency + fallback behavior are representative.
    Each self-play call is one "battle"; we treat each as a round sample.
    """
    party, enemy = _mk_monsters(model)

    # Optional warmup battle so the model is loaded before we time (cold-load tax
    # would skew the first sample otherwise). Best-effort.
    if warmup:
        try:
            await _run_self_play_async(party, enemy, topic, rounds=1)
        except Exception:  # noqa: BLE001
            pass

    # Reset the process-wide fallback counters so the reported rate reflects ONLY
    # this spike's timed rounds.
    latency_metrics.reset_counters()

    round_ms: list[float] = []
    per_round: list[dict[str, Any]] = []
    errors: list[str] = []
    for i in range(max(1, rounds)):
        t0 = time.monotonic()
        try:
            result = await _run_self_play_async(party, enemy, topic, rounds=1)
            ms = (time.monotonic() - t0) * 1000.0
            round_ms.append(ms)
            per_round.append(
                {
                    "round": i,
                    "round_ms": round(ms, 1),
                    "net_score": result.get("net_score"),
                    "result": result.get("result"),
                }
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")
            per_round.append({"round": i, "error": f"{type(e).__name__}: {e}"})

    counters = latency_metrics.counters_snapshot()
    return {
        "mode": "local",
        "model": model,
        "rounds_timed": len(round_ms),
        "round_ms_p50": round(_percentile(round_ms, 50), 1),
        "round_ms_p95": round(_percentile(round_ms, 95), 1),
        "round_ms_max": round(max(round_ms), 1) if round_ms else 0.0,
        "fallback_rate": counters["fallback_rate"],
        "fallback_by_reason": counters["fallback_by_reason"],
        "utterances": counters["utterances"],
        "fallbacks": counters["fallbacks"],
        "errors": errors,
        "per_round": per_round,
    }


async def _spike_hosted(spec: str, topic: str, samples: int) -> dict[str, Any]:
    """Time `samples` single generations through a hosted candidate spec.

    Uses the WS-0-LAT candidate runner so a ``groq/...`` spec routes to the hosted
    adapter. Degrades gracefully: with no key for the provider the runner returns a
    miss and we report it as 'skipped' rather than erroring.
    """
    from app.gateway.candidates import resolve_candidate, run_candidate

    cand = resolve_candidate(spec)
    if cand.backend != "hosted":
        return {
            "mode": "hosted",
            "spec": spec,
            "skipped": f"'{spec}' resolves to {cand.backend}/{cand.provider}, not a hosted provider",
        }

    # Build a representative single-utterance prompt (same shape a battle turn uses).
    actor = Combatant(
        monster_id="spike-hosted",
        name="Sophist",
        type="LOGOS",
        role="enemy",
        hp=100,
        max_hp=100,
        side="against",
    )
    messages = _build_actor_messages(
        actor,
        topic,
        transcript=[],
        action={"behavior": None, "skill": None, "target": None, "tone": None},
        memories=[],
        name_lookup={},
    )

    from app.llm.hosted_adapter import _has_key

    if not _has_key(cand.provider):
        return {
            "mode": "hosted",
            "spec": spec,
            "provider": cand.provider,
            "skipped": f"no key for {cand.provider} (set its API key to A/B against local)",
        }

    gen_ms: list[float] = []
    misses = 0
    sample_text = ""
    for _ in range(max(1, samples)):
        res = await run_candidate(cand, messages, temperature=0.8, max_tokens=64)
        if res.ok:
            gen_ms.append(float(res.latency_ms))
            sample_text = sample_text or res.text[:120]
        else:
            misses += 1

    return {
        "mode": "hosted",
        "spec": spec,
        "provider": cand.provider,
        "model": cand.model,
        "samples_ok": len(gen_ms),
        "samples_failed": misses,
        "gen_ms_p50": round(_percentile(gen_ms, 50), 1),
        "gen_ms_p95": round(_percentile(gen_ms, 95), 1),
        "sample_preview": sample_text,
    }


def _human_summary(report: dict[str, Any]) -> str:
    lines: list[str] = ["", "=== battle latency go/no-go (WS-0-LAT) ==="]
    lines.append(f"topic: {report['topic']!r}")
    lines.append(f"gateway_fallback_enabled: {report['gateway_fallback_enabled']}")

    loc = report.get("local") or {}
    lines.append("")
    lines.append(f"[LOCAL] model={loc.get('model')}  rounds={loc.get('rounds_timed')}")
    lines.append(
        f"  round latency  p50={loc.get('round_ms_p50')}ms  "
        f"p95={loc.get('round_ms_p95')}ms  max={loc.get('round_ms_max')}ms"
    )
    fr = loc.get("fallback_rate", 0.0)
    lines.append(
        f"  fallback rate  {fr:.0%}  "
        f"({loc.get('fallbacks')}/{loc.get('utterances')} utterances)  "
        f"by reason: {loc.get('fallback_by_reason') or '{}'}"
    )
    if fr >= 0.5:
        lines.append(
            "  ⚠ HIGH fallback rate — local model is timing out / not answering. "
            "Either Ollama is down or the model is too slow on this box."
        )
    if loc.get("errors"):
        lines.append(f"  errors: {loc['errors']}")

    host = report.get("hosted")
    if host:
        lines.append("")
        if host.get("skipped"):
            lines.append(f"[HOSTED] {host.get('spec')} — skipped: {host['skipped']}")
        else:
            lines.append(
                f"[HOSTED] {host.get('spec')}  ok={host.get('samples_ok')} "
                f"failed={host.get('samples_failed')}"
            )
            lines.append(
                f"  gen latency  p50={host.get('gen_ms_p50')}ms  "
                f"p95={host.get('gen_ms_p95')}ms"
            )

    lines.append("")
    lines.append(
        "GO/NO-GO: keep battles LOCAL if local p95 is acceptable AND fallback rate "
        "is low; otherwise consider routing the infrequent / opening tasks through a "
        "hosted candidate (this script does NOT change the battle path — it only measures)."
    )
    lines.append("")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "topic": args.topic,
        "gateway_fallback_enabled": bool(settings.gateway_fallback_enabled),
    }
    report["local"] = await _spike_local(
        args.topic, args.rounds, args.model, warmup=not args.no_warmup
    )
    if args.hosted:
        report["hosted"] = await _spike_hosted(args.hosted, args.topic, args.hosted_samples)
    return report


def main() -> int:
    p = argparse.ArgumentParser(description="Battle latency local-vs-hosted go/no-go spike.")
    p.add_argument("--topic", default=DEFAULT_TOPIC, help="Debate topic to spike.")
    p.add_argument("--rounds", type=int, default=3, help="Local warm rounds to time (default 3).")
    p.add_argument("--model", default=settings.actor_model, help="Local actor/judge model.")
    p.add_argument(
        "--hosted",
        default=None,
        help="Optional hosted candidate spec to A/B, e.g. groq/llama-3.1-8b-instant.",
    )
    p.add_argument(
        "--hosted-samples", type=int, default=3, help="Hosted single-gen samples (default 3)."
    )
    p.add_argument("--no-warmup", action="store_true", help="Skip the warmup battle.")
    p.add_argument("--json", action="store_true", help="Print ONLY the JSON report to stdout.")
    args = p.parse_args()

    try:
        report = asyncio.run(run(args))
    except Exception:  # noqa: BLE001
        print(json.dumps({"fatal_error": traceback.format_exc()}))
        return 1

    if not args.json:
        sys.stderr.write(_human_summary(report))
        sys.stderr.flush()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
