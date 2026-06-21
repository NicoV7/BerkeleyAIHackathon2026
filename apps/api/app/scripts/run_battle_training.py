"""Run a small dual-agent battle-training loop and print JSON results.

Usage from ``apps/api``:
    uv run python -m app.scripts.run_battle_training --cycles 1 --variants 1

This is prompt/genome optimization only. It does not update model weights and it
does not print provider credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback

from app.training.battle_loop import (
    LoopConfig,
    result_to_dict,
    run_battle_training,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run dual-agent battle harness training.")
    parser.add_argument("--topic", default=LoopConfig.topic)
    parser.add_argument("--cycles", type=int, default=LoopConfig.cycles)
    parser.add_argument("--rounds", type=int, default=LoopConfig.rounds)
    parser.add_argument("--variants", type=int, default=LoopConfig.variants)
    parser.add_argument("--model", default=LoopConfig.model)
    parser.add_argument("--seed", type=int, default=LoopConfig.seed)
    parser.add_argument("--latency-target-s", type=float, default=LoopConfig.latency_target_s)
    parser.add_argument("--quality-floor", type=float, default=LoopConfig.quality_floor)
    parser.add_argument("--accept-margin", type=float, default=LoopConfig.accept_margin)
    parser.add_argument(
        "--no-transcripts",
        action="store_true",
        help="omit transcript excerpts from the JSON output",
    )
    parser.add_argument(
        "--no-genomes",
        action="store_true",
        help="omit final genomes from the JSON output",
    )
    return parser


async def _run(args: argparse.Namespace) -> dict:
    cfg = LoopConfig(
        topic=args.topic,
        cycles=args.cycles,
        rounds=args.rounds,
        variants=args.variants,
        model=args.model,
        seed=args.seed,
        latency_target_s=args.latency_target_s,
        quality_floor=args.quality_floor,
        accept_margin=args.accept_margin,
    )
    result = await run_battle_training(cfg)
    return result_to_dict(
        result,
        include_transcripts=not args.no_transcripts,
        include_genomes=not args.no_genomes,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point that emits a single JSON object."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        payload = asyncio.run(_run(args))
    except Exception:  # noqa: BLE001
        print(json.dumps({"fatal_error": traceback.format_exc()}))
        return 1
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
