"""Wall-clock latency probe for the debate battle loop (Agent 4).

Why this exists
---------------
End-to-end battle wall-clock on CPU Ollama was UNSIZED — and it's the real UX
killer for a turn-based debate RPG, far more than scoring accuracy. This script
puts a number on it: it times raw ``gateway.complete()`` calls (the per-utterance
cost) and, if the orchestrator is importable, one full headless debate round
(the per-turn cost the player actually feels), then prints p50/p95/mean and a
one-line verdict against a ~5s/turn budget.

Design
------
  * Local-first & failure-tolerant: if Ollama (or whatever the default provider
    is) is unreachable, it prints a clear "gateway unreachable" message and
    exits 0 — so it's safe to run on a bare host or in CI without a model server.
  * No side effects on owned modules: it only *calls* the public gateway /
    orchestrator surfaces; it imports the debate round lazily and degrades if the
    seam isn't present.

Usage
-----
    # From apps/api, with the venv active (or `uv run`):
    python -m app.scripts.bench_latency                 # defaults: 5 complete() calls
    python -m app.scripts.bench_latency -n 10           # 10 gateway.complete() samples
    python -m app.scripts.bench_latency --model gemma   # probe a specific model/alias
    python -m app.scripts.bench_latency --rounds 2      # also time N debate rounds
    python -m app.scripts.bench_latency --budget 5.0    # per-turn budget in seconds
    python -m app.scripts.bench_latency --no-round      # skip the orchestrator round

Exit code is 0 even when the gateway is unreachable (so it never breaks CI); it
is non-zero only on an unexpected internal error.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from typing import Optional

# --- Probe prompt: short, judge-like, deterministic-ish. --------------------
_PROBE_MESSAGES = [
    {"role": "system", "content": "You are a terse debate combatant. One sentence only."},
    {"role": "user", "content": "Argue, in one sentence, that cats make better pets than dogs."},
]


def _pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile (small N friendly; no interpolation surprises)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def _summary(label: str, samples: list[float], budget: float) -> str:
    if not samples:
        return f"{label}: no samples collected."
    p50 = _pct(samples, 50)
    p95 = _pct(samples, 95)
    mean = statistics.fmean(samples)
    verdict = "OK" if p95 <= budget else "OVER BUDGET"
    return (
        f"{label}: n={len(samples)} "
        f"p50={p50:.2f}s p95={p95:.2f}s mean={mean:.2f}s "
        f"(budget={budget:.1f}s/turn) -> {verdict}"
    )


async def _check_gateway() -> tuple[bool, str]:
    """Return (reachable, detail). Uses gateway.health() which pings the provider."""
    try:
        from app.gateway.gateway import gateway
    except Exception as e:  # noqa: BLE001
        return False, f"could not import gateway: {e}"
    try:
        info = await gateway.health()
    except Exception as e:  # noqa: BLE001
        return False, f"health() raised: {e}"
    if info.get("ok"):
        models = info.get("models") or []
        return True, f"provider={info.get('provider')} models={len(models)}"
    return False, info.get("error", "provider reported not-ok")


async def _bench_complete(n: int, model: Optional[str]) -> list[float]:
    """Time N gateway.complete() calls. Stops early on the first hard failure."""
    from app.gateway.gateway import gateway

    samples: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        try:
            await gateway.complete(_PROBE_MESSAGES, model=model, max_tokens=64)
        except Exception as e:  # noqa: BLE001
            print(f"  complete() call {i + 1}/{n} failed: {e}")
            break
        dt = time.perf_counter() - t0
        samples.append(dt)
        print(f"  complete() call {i + 1}/{n}: {dt:.2f}s")
    return samples


def _stub_monster(mid: str, name: str, mtype: str, model: Optional[str]) -> dict:
    return {
        "id": mid,
        "name": name,
        "type": mtype,
        "level": 1,
        "max_hp": 100,
        "persona": {},
        "harness": {},
        "skills": [],
        "model": model,
    }


async def _bench_round(rounds: int, model: Optional[str]) -> list[float]:
    """Time `rounds` full headless debate rounds via the orchestrator seam.

    Each timed sample is ONE debate round (one pass over both combatants + a
    judge call) — the unit the player waits on. Degrades to an empty result if
    the orchestrator isn't importable.
    """
    try:
        from app.debate.orchestrator import _run_self_play_async
    except Exception as e:  # noqa: BLE001
        print(f"  orchestrator not importable ({e}); skipping debate-round probe.")
        return []

    party = _stub_monster("bench-party", "Probe-A", "LOGOS", model)
    enemy = _stub_monster("bench-enemy", "Probe-B", "PATHOS", model)
    topic = "Cats make better pets than dogs."

    samples: list[float] = []
    for i in range(rounds):
        t0 = time.perf_counter()
        try:
            await _run_self_play_async(party, enemy, topic, rounds=1)
        except Exception as e:  # noqa: BLE001
            print(f"  debate round {i + 1}/{rounds} failed: {e}")
            break
        dt = time.perf_counter() - t0
        samples.append(dt)
        print(f"  debate round {i + 1}/{rounds}: {dt:.2f}s")
    return samples


async def _amain(args: argparse.Namespace) -> int:
    print("== Debate RPG latency probe ==")
    reachable, detail = await _check_gateway()
    if not reachable:
        print(f"gateway unreachable: {detail}")
        print(
            "Start the model server (e.g. Ollama at OLLAMA_BASE_URL) and retry. "
            "Nothing was measured."
        )
        return 0  # graceful: never fail CI just because the model server is down.
    print(f"gateway reachable ({detail})")

    model_label = args.model or "default"
    print(f"\n[1] gateway.complete() x{args.n}  (model={model_label})")
    complete_samples = await _bench_complete(args.n, args.model)

    round_samples: list[float] = []
    if not args.no_round and args.rounds > 0:
        print(f"\n[2] debate round x{args.rounds}  (orchestrator, model={model_label})")
        round_samples = await _bench_round(args.rounds, args.model)

    print("\n== Verdict ==")
    print(_summary("gateway.complete()", complete_samples, args.budget))
    if not args.no_round:
        # A round contains two utterances + a judge call, so its natural budget
        # is larger than a single turn. Report against ~3x the per-turn budget.
        print(_summary("debate round", round_samples, args.budget * 3))

    # Best-effort cleanup of the shared async client.
    try:
        from app.gateway.gateway import gateway

        await gateway.aclose()
    except Exception:  # noqa: BLE001
        pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Wall-clock latency probe for the debate loop.")
    parser.add_argument("-n", type=int, default=5, help="number of gateway.complete() samples")
    parser.add_argument("--model", default=None, help="model id/alias (default: gateway default)")
    parser.add_argument("--rounds", type=int, default=2, help="number of debate rounds to time")
    parser.add_argument("--no-round", action="store_true", help="skip the orchestrator round probe")
    parser.add_argument("--budget", type=float, default=5.0, help="per-turn wall-clock budget (s)")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
