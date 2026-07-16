#!/usr/bin/env python3
"""JSON-over-stdin adapter that exposes OpenAISummaryBackend to command mode."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from openai_backend import OpenAISummaryBackend


BASE_DIR = Path(__file__).resolve().parents[3]
DEFAULT_ENV = BASE_DIR / "tools/scripts/feature_generation/.env"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument(
        "--reasoning-effort", choices=("low", "medium", "high"), default="medium"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request = json.load(sys.stdin)
    backend = OpenAISummaryBackend(
        model=args.model,
        env_path=args.env_file,
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
        reasoning_effort=args.reasoning_effort,
    )
    output = backend.generate(
        stage=request["stage"],
        system_prompt=request["system_prompt"],
        payload=request["payload"],
        max_output_tokens=request["max_output_tokens"],
        temperature=request["temperature"],
    )
    print(json.dumps({"output": output, "metadata": backend.call_metadata[-1]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
