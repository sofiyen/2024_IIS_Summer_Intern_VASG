"""Persistent local Unsloth backend for the full-log summary pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from .summary_pipeline import BackendOutputError, PipelineError, extract_json_object
except ImportError:  # Direct execution through gen_summary.py.
    from summary_pipeline import BackendOutputError, PipelineError, extract_json_object


FINAL_CHANNEL_MARKER = "<|channel|>final<|message|>"
END_MARKERS = ("<|return|>", "<|end|>")


def extract_gpt_oss_final(raw: str) -> str | None:
    """Return the last GPT-OSS final-channel message, if generation reached it."""
    index = raw.rfind(FINAL_CHANNEL_MARKER)
    if index == -1:
        return None
    content = raw[index + len(FINAL_CHANNEL_MARKER) :]
    ends = [position for marker in END_MARKERS if (position := content.find(marker)) >= 0]
    if ends:
        content = content[: min(ends)]
    return content.strip()


class UnslothSummaryBackend:
    """Load one GPT-OSS checkpoint and reuse it for every reduction-tree call."""

    def __init__(
        self,
        *,
        adapter_path: Path,
        context_limit: int,
        load_in_4bit: bool = True,
        top_p: float = 0.9,
        reasoning_effort: str = "medium",
        name: str = "gpt-oss-20b-dpo-curated-v3",
    ) -> None:
        if not adapter_path.is_dir():
            raise PipelineError(f"Unsloth adapter directory does not exist: {adapter_path}")
        if not (adapter_path / "adapter_config.json").is_file():
            raise PipelineError(f"Unsloth adapter_config.json is missing: {adapter_path}")
        try:
            import torch  # type: ignore
            from unsloth import FastLanguageModel  # type: ignore
        except ModuleNotFoundError as exc:
            raise PipelineError(
                "The Unsloth backend requires the unsloth environment and its PyTorch dependencies"
            ) from exc
        if not torch.cuda.is_available():
            raise PipelineError("The Unsloth GPT-OSS backend requires an available CUDA GPU")

        self.name = name
        self.adapter_path = adapter_path
        self.context_limit = context_limit
        self.top_p = top_p
        self.reasoning_effort = reasoning_effort
        self.torch = torch

        try:
            self.model, self.tokenizer = FastLanguageModel.from_pretrained(
                model_name=str(adapter_path),
                max_seq_length=context_limit,
                dtype=None,
                load_in_4bit=load_in_4bit,
            )
            FastLanguageModel.for_inference(self.model)
        except Exception as exc:  # noqa: BLE001
            raise PipelineError(f"Failed to load Unsloth checkpoint {adapter_path}: {exc}") from exc

    def generate(
        self,
        *,
        stage: str,
        system_prompt: str,
        payload: dict[str, Any],
        max_output_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        user_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        try:
            input_ids = self.tokenizer.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
                reasoning_effort=self.reasoning_effort,
                return_tensors="pt",
            ).to(self.model.device)
        except Exception as exc:  # noqa: BLE001
            raise PipelineError(f"Failed to tokenize {stage} summary request: {exc}") from exc

        if input_ids.shape[-1] + max_output_tokens > self.context_limit:
            raise PipelineError(
                f"{stage} request exceeds local model context after chat templating: "
                f"{input_ids.shape[-1]}+{max_output_tokens}>{self.context_limit}"
            )

        generation: dict[str, Any] = {
            "max_new_tokens": max_output_tokens,
            "eos_token_id": self.tokenizer.eos_token_id,
            "pad_token_id": self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
        }
        if temperature > 0:
            generation.update(
                {
                    "do_sample": True,
                    "temperature": temperature,
                    "top_p": self.top_p,
                }
            )
        else:
            generation["do_sample"] = False

        try:
            with self.torch.inference_mode():
                output_ids = self.model.generate(input_ids, **generation)
        except Exception as exc:  # noqa: BLE001
            raise PipelineError(f"Unsloth generation failed during {stage}: {exc}") from exc

        generated_ids = output_ids[0, input_ids.shape[-1] :]
        raw = self.tokenizer.decode(generated_ids, skip_special_tokens=False)
        final_text = extract_gpt_oss_final(raw)
        if final_text is None:
            raise BackendOutputError(
                f"GPT-OSS did not reach its final channel during {stage}; "
                "the output budget may have been consumed by reasoning"
            )
        result = extract_json_object(final_text)
        if result is None:
            preview = final_text.replace("\n", " ")[:500]
            raise BackendOutputError(
                f"GPT-OSS returned non-JSON final output during {stage}: {preview}"
            )
        return result
