"""Benchmark gateway model candidates and print a redacted Pareto frontier.

Usage from ``apps/api``:
    uv run python -m app.scripts.bench_models --role actor --runs 3
    uv run python -m app.scripts.bench_models --role judge --runs 3

The output is one JSON object and never includes provider API keys.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback

from app.gateway.gateway import gateway
from app.gateway import pareto


async def _run(role: str, runs: int) -> dict:
    return await pareto.benchmark_role(gateway, role, runs=runs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=[pareto.ACTOR_ROLE, pareto.JUDGE_ROLE], required=True)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    try:
        result = asyncio.run(_run(args.role, args.runs))
    except Exception:  # noqa: BLE001
        print(json.dumps({"role": args.role, "fatal_error": traceback.format_exc()}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
