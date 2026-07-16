#!/usr/bin/env python3
"""Generate or plan a grounded full-log summary from Log2Feat features."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from summary_pipeline import (
    CommandSummaryBackend,
    DeterministicTestBackend,
    DirectoryFeatureSource,
    HeuristicTokenCounter,
    HuggingFaceTokenCounter,
    IncompleteInputError,
    PipelineError,
    PromptSet,
    ReductionConfig,
    atomic_write_json,
    plan_reduction,
    prepare_timeline,
    run_reduction,
)


BASE_DIR = Path(__file__).resolve().parents[3]
DEFAULT_TRUNCATED_DIR = BASE_DIR / "data/logs/truncated"
DEFAULT_OUTPUT_DIR = BASE_DIR / "data/summaries"
DEFAULT_PROMPT_DIR = BASE_DIR / "tools/prompts"
DEFAULT_UNSLOTH_ADAPTER = (
    BASE_DIR / "train/unsloth/outputs_dpo_curated_v3/checkpoint-32"
)
DEFAULT_OPENAI_ENV = BASE_DIR / "tools/scripts/feature_generation/.env"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a direct or hierarchical full-log summary from ordered chunk features."
    )
    parser.add_argument("--log-id", type=int, required=True)
    parser.add_argument("--features-dir", type=Path, required=True)
    parser.add_argument("--feature-model", required=True, help="Label recorded as the Log2Feat source")
    parser.add_argument("--truncated-dir", type=Path, default=DEFAULT_TRUNCATED_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--run-name",
        help="Result subdirectory; defaults to --feature-model for backward compatibility",
    )
    parser.add_argument(
        "--representation",
        choices=("entries_only", "entries_and_summary", "summary_only"),
        default="entries_and_summary",
    )
    parser.add_argument("--overlap-entries", type=int, default=3)
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Continue experimentally and mark the result INCOMPLETE_INPUT",
    )

    parser.add_argument(
        "--backend",
        choices=("plan", "command", "unsloth", "openai", "deterministic-test"),
        default="plan",
        help="plan makes no model calls; deterministic-test is only for smoke tests",
    )
    parser.add_argument(
        "--backend-command",
        help="Command receiving one JSON request on stdin and returning one JSON object",
    )
    parser.add_argument("--backend-name")
    parser.add_argument("--backend-timeout", type=int, default=600)
    parser.add_argument("--openai-model", default="gpt-5.4-mini")
    parser.add_argument(
        "--openai-env",
        type=Path,
        default=DEFAULT_OPENAI_ENV,
        help="Dotenv file containing OPENAI_API_KEY/GPT_API_KEY and optional OPENAI_BASE_URL",
    )
    parser.add_argument("--openai-max-retries", type=int, default=4)
    parser.add_argument(
        "--adapter-path",
        type=Path,
        default=DEFAULT_UNSLOTH_ADAPTER,
        help="LoRA checkpoint used by --backend unsloth",
    )
    parser.add_argument(
        "--load-in-4bit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Load the Unsloth model in 4-bit mode (default: true)",
    )
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high"),
        default="medium",
        help="GPT-OSS chat-template reasoning effort (default: medium, matching training)",
    )

    parser.add_argument(
        "--tokenizer",
        help="Hugging Face tokenizer name/path. Omit only for approximate planning/tests.",
    )
    parser.add_argument("--context-limit", type=int, default=8192)
    parser.add_argument("--input-token-budget", type=int, default=5800)
    parser.add_argument("--max-output-tokens", type=int, default=1400)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--leaf-prompt", type=Path, default=DEFAULT_PROMPT_DIR / "full_summary_leaf_prompt.txt"
    )
    parser.add_argument(
        "--merge-prompt", type=Path, default=DEFAULT_PROMPT_DIR / "full_summary_merge_prompt.txt"
    )
    parser.add_argument(
        "--final-prompt", type=Path, default=DEFAULT_PROMPT_DIR / "full_summary_final_prompt.txt"
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def load_prompts(args: argparse.Namespace) -> PromptSet:
    return PromptSet(
        leaf=args.leaf_prompt.read_text(encoding="utf-8"),
        merge=args.merge_prompt.read_text(encoding="utf-8"),
        final=args.final_prompt.read_text(encoding="utf-8"),
    )


def base_artifact(args: argparse.Namespace, *, source: DirectoryFeatureSource) -> dict:
    return {
        "log_id": args.log_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "feature_source": source.name,
        "feature_model": args.feature_model,
        "run_name": args.run_name or args.feature_model,
        "features_dir": str(args.features_dir.resolve()),
        "representation": args.representation,
    }


def main() -> int:
    args = parse_args()
    if args.overlap_entries < 0:
        raise SystemExit("--overlap-entries must be non-negative")
    if args.context_limit <= 0 or args.input_token_budget <= 0 or args.max_output_tokens <= 0:
        raise SystemExit("token budgets must be positive")
    if args.backend == "command" and not args.backend_command:
        raise SystemExit("--backend command requires --backend-command")

    output_path = args.output_dir / (args.run_name or args.feature_model) / f"{args.log_id}.json"
    if output_path.exists() and not args.overwrite:
        raise SystemExit(f"Output already exists (use --overwrite): {output_path}")

    source = DirectoryFeatureSource(
        features_dir=args.features_dir,
        truncated_dir=args.truncated_dir,
        name=f"directory:{args.feature_model}",
    )
    artifact = base_artifact(args, source=source)
    try:
        prepared = prepare_timeline(
            log_id=args.log_id,
            feature_source=source,
            representation=args.representation,
            overlap_entries=args.overlap_entries,
            allow_incomplete=args.allow_incomplete,
        )
    except IncompleteInputError as exc:
        artifact.update({"status": "INCOMPLETE_INPUT", "error": str(exc)})
        if exc.coverage is not None:
            artifact["coverage"] = exc.coverage.to_dict()
        atomic_write_json(output_path, artifact)
        print(f"[INCOMPLETE_INPUT] {exc}", file=sys.stderr)
        print(f"Wrote: {output_path}")
        return 2

    artifact.update(
        {
            "status": "INCOMPLETE_INPUT" if not prepared.coverage.complete else "COMPLETE",
            "coverage": prepared.coverage.to_dict(),
            "overlap": prepared.overlap.to_dict(),
            "source_chunks": [chunk["chunk_id"] for chunk in prepared.chunks],
            "timeline_units": len(prepared.units),
        }
    )
    tokenizer_path = args.tokenizer
    if args.backend == "unsloth" and tokenizer_path is None:
        tokenizer_path = str(args.adapter_path)
    backend = None
    try:
        if args.backend == "unsloth":
            # Import/load Unsloth before importing Transformers independently;
            # Unsloth patches the model stack during import. Reuse its tokenizer.
            from unsloth_backend import UnslothSummaryBackend

            backend = UnslothSummaryBackend(
                adapter_path=args.adapter_path,
                context_limit=args.context_limit,
                load_in_4bit=args.load_in_4bit,
                top_p=args.top_p,
                reasoning_effort=args.reasoning_effort,
                name=args.backend_name or "gpt-oss-20b-dpo-curated-v3",
            )
            artifact["adapter"] = str(args.adapter_path.resolve())
            counter = HuggingFaceTokenCounter(
                tokenizer_path,
                reasoning_effort=args.reasoning_effort,
                tokenizer=backend.tokenizer,
            )
        else:
            counter = (
                HuggingFaceTokenCounter(
                    tokenizer_path, reasoning_effort=args.reasoning_effort
                )
                if tokenizer_path
                else HeuristicTokenCounter()
            )
    except PipelineError as exc:
        artifact.update({"status": "PIPELINE_ERROR", "error": str(exc)})
        atomic_write_json(output_path, artifact)
        print(f"[PIPELINE_ERROR] {exc}", file=sys.stderr)
        print(f"Wrote: {output_path}")
        return 1

    prompts = load_prompts(args)
    config = ReductionConfig(
        context_limit=args.context_limit,
        input_token_budget=args.input_token_budget,
        max_output_tokens=args.max_output_tokens,
        temperature=args.temperature,
    )
    artifact.update(
        {
            "token_counter": {"name": counter.name, "exact": counter.exact},
            "generation": {
                "effective_context_limit": config.context_limit,
                "input_token_budget": config.input_token_budget,
                "max_output_tokens": config.max_output_tokens,
                "temperature": config.temperature,
                "batch_size": 1,
                "reasoning_effort": args.reasoning_effort,
            },
        }
    )

    try:
        if args.backend == "plan":
            artifact["status"] = (
                "INCOMPLETE_INPUT" if not prepared.coverage.complete else "PLANNED"
            )
            artifact["backend"] = "none"
            artifact["reduction"] = plan_reduction(
                prepared=prepared,
                prompts=prompts,
                counter=counter,
                config=config,
            )
        else:
            if args.backend == "command":
                backend = CommandSummaryBackend(
                    args.backend_command,
                    name=args.backend_name,
                    timeout_seconds=args.backend_timeout,
                )
            elif args.backend == "unsloth":
                assert backend is not None
            elif args.backend == "openai":
                from openai_backend import OpenAISummaryBackend

                backend = OpenAISummaryBackend(
                    model=args.openai_model,
                    env_path=args.openai_env,
                    timeout_seconds=args.backend_timeout,
                    max_retries=args.openai_max_retries,
                    reasoning_effort=args.reasoning_effort,
                )
            else:
                backend = DeterministicTestBackend()
            summary, reduction = run_reduction(
                prepared=prepared,
                prompts=prompts,
                backend=backend,
                counter=counter,
                config=config,
            )
            artifact.update(
                {
                    "backend": backend.name,
                    "strategy": reduction["strategy"],
                    "reduction": reduction,
                    "ordered_actions": summary["ordered_actions"],
                    "process_relationships": summary.get("process_relationships", []),
                    "state_changes": summary.get("state_changes", []),
                    "uncertainties": summary.get("uncertainties", []),
                    "summary": summary["summary"],
                }
            )
            if hasattr(backend, "call_metadata"):
                artifact["api_calls"] = backend.call_metadata
    except PipelineError as exc:
        artifact.update({"status": "PIPELINE_ERROR", "error": str(exc)})
        atomic_write_json(output_path, artifact)
        print(f"[PIPELINE_ERROR] {exc}", file=sys.stderr)
        print(f"Wrote: {output_path}")
        return 1

    atomic_write_json(output_path, artifact)
    print(json.dumps({
        "log_id": args.log_id,
        "status": artifact["status"],
        "strategy": artifact.get("strategy") or artifact.get("reduction", {}).get("strategy"),
        "coverage": artifact["coverage"]["coverage"],
        "output": str(output_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
