"""OpenAI API backend for the full-log summary pipeline."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from .summary_pipeline import BackendOutputError, PipelineError, extract_json_object
except ImportError:  # Direct execution through gen_summary.py.
    from summary_pipeline import BackendOutputError, PipelineError, extract_json_object


class OpenAISummaryBackend:
    """Call one OpenAI model for every leaf, merge, and final reduction stage."""

    def __init__(
        self,
        *,
        model: str,
        env_path: Path | None = None,
        timeout_seconds: float = 600,
        max_retries: int = 4,
        reasoning_effort: str = "medium",
        client: Any | None = None,
    ) -> None:
        if env_path is not None and env_path.is_file():
            try:
                from dotenv import load_dotenv  # type: ignore
            except ModuleNotFoundError as exc:
                raise PipelineError(
                    "python-dotenv is required to load the configured OpenAI .env file"
                ) from exc
            load_dotenv(env_path, override=False)

        self.model = model
        self.name = f"openai:{model}"
        self.reasoning_effort = reasoning_effort
        self.call_metadata: list[dict[str, Any]] = []

        if client is not None:
            self.client = client
            return

        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GPT_API_KEY")
        if not api_key:
            location = f" in {env_path}" if env_path is not None else ""
            raise PipelineError(
                f"OPENAI_API_KEY (or GPT_API_KEY) is not set{location}"
            )
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        try:
            from openai import OpenAI  # type: ignore
        except ModuleNotFoundError as exc:
            raise PipelineError("The OpenAI backend requires the openai Python package") from exc
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    @staticmethod
    def _usage_metadata(usage: Any) -> dict[str, Any]:
        if usage is None:
            return {}
        details = getattr(usage, "completion_tokens_details", None)
        result = {
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
        if details is not None:
            result["reasoning_tokens"] = getattr(details, "reasoning_tokens", None)
        return {key: value for key, value in result.items() if value is not None}

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
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
            "max_completion_tokens": max_output_tokens,
            "reasoning_effort": self.reasoning_effort,
            "response_format": {"type": "json_object"},
        }
        # Current GPT-5 reasoning models reject temperature=0 and accept only
        # their default. Preserve zero as "no sampling override" for this backend.
        if temperature > 0:
            request["temperature"] = temperature
        try:
            response = self.client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001
            raise PipelineError(f"OpenAI API request failed during {stage}: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice is not None else None
        self.call_metadata.append(
            {
                "stage": stage,
                "response_id": getattr(response, "id", None),
                "model": getattr(response, "model", self.model),
                "finish_reason": getattr(choice, "finish_reason", None),
                "temperature_sent": request.get("temperature"),
                "usage": self._usage_metadata(getattr(response, "usage", None)),
            }
        )
        if not isinstance(content, str) or not content.strip():
            raise BackendOutputError(
                f"OpenAI returned no text during {stage}; "
                f"finish_reason={getattr(choice, 'finish_reason', None)}"
            )
        result = extract_json_object(content)
        if result is None:
            preview = content.replace("\n", " ")[:500]
            raise BackendOutputError(
                f"OpenAI returned non-JSON output during {stage}: {preview}"
            )
        return result
