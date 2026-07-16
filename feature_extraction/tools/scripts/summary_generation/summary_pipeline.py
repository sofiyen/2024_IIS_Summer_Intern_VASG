#!/usr/bin/env python3
"""Model-agnostic preparation and reduction for full-log summaries."""

from __future__ import annotations

import copy
import json
import math
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence


MSG_ID_RE = re.compile(r"\bmsg=audit\(([^)]+)\)")
FIELD_RE_TEMPLATE = r"(?:^|\s){name}=(?:\"([^\"]*)\"|(\S+))"
FEATURE_NAME_RE = re.compile(r"^(\d+)_(\d+)$")


class PipelineError(RuntimeError):
    """Base error raised by the summary pipeline."""


class IncompleteInputError(PipelineError):
    """Raised when strict mode receives missing or invalid feature chunks."""

    def __init__(self, message: str, coverage: "CoverageReport | None" = None) -> None:
        super().__init__(message)
        self.coverage = coverage


class InputTooLargeError(PipelineError):
    """Raised when one indivisible input unit cannot fit the model context."""


class BackendOutputError(PipelineError):
    """Raised when a summarizer returns malformed or ungrounded output."""


class FeatureSource(Protocol):
    """Hot-pluggable source of Log2Feat results."""

    name: str

    def expected_chunks(self, log_id: int) -> list[str]: ...

    def load_chunks(self, log_id: int) -> list["ChunkLoadResult"]: ...

    def raw_chunk_path(self, chunk_stem: str) -> Path | None: ...


class SummaryBackend(Protocol):
    """Hot-pluggable model backend used at every reduction stage."""

    name: str

    def generate(
        self,
        *,
        stage: str,
        system_prompt: str,
        payload: dict[str, Any],
        max_output_tokens: int,
        temperature: float,
    ) -> dict[str, Any]: ...


class TokenCounter(Protocol):
    """Counts the actual request shape sent to a summarization backend."""

    name: str
    exact: bool

    def count_text(self, text: str) -> int: ...

    def count_request(self, system_prompt: str, payload: dict[str, Any]) -> int: ...


@dataclass
class ChunkLoadResult:
    log_id: int
    chunk_id: int
    stem: str
    path: Path
    feature: dict[str, Any] | None
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.feature is not None and self.error is None


@dataclass
class CoverageReport:
    expected_chunks: list[str]
    valid_chunks: list[str]
    invalid_chunks: dict[str, str]
    missing_chunks: list[str]
    extra_chunks: list[str]

    @property
    def complete(self) -> bool:
        return not self.invalid_chunks and not self.missing_chunks

    @property
    def coverage(self) -> float:
        if not self.expected_chunks:
            return 0.0
        return len(self.valid_chunks) / len(self.expected_chunks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_chunks": len(self.expected_chunks),
            "valid_chunks": len(self.valid_chunks),
            "invalid_chunks": self.invalid_chunks,
            "missing_chunks": self.missing_chunks,
            "extra_chunks": self.extra_chunks,
            "coverage": self.coverage,
        }


@dataclass
class OverlapReport:
    input_feature_entries: int = 0
    output_feature_entries: int = 0
    removed_overlap_entries: int = 0
    ambiguous_overlap_entries: int = 0
    ambiguous_raw_identity_matches: int = 0
    removed_by_boundary: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreparedTimeline:
    log_id: int
    feature_source: str
    representation: str
    coverage: CoverageReport
    overlap: OverlapReport
    chunks: list[dict[str, Any]]
    units: list[dict[str, Any]]


@dataclass
class ReductionConfig:
    context_limit: int = 8192
    input_token_budget: int = 5800
    max_output_tokens: int = 1400
    temperature: float = 0.0


@dataclass
class PromptSet:
    leaf: str
    merge: str
    final: str


def _field(line: str, name: str) -> str | None:
    match = re.search(FIELD_RE_TEMPLATE.format(name=re.escape(name)), line)
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def _natural_stem_key(stem: str) -> tuple[int, int, str]:
    match = FEATURE_NAME_RE.fullmatch(stem)
    if not match:
        return (math.inf, math.inf, stem)
    return (int(match.group(1)), int(match.group(2)), stem)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse a direct or fenced/embedded JSON object."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(candidate[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def validate_feature(feature: dict[str, Any]) -> str | None:
    events = feature.get("events")
    if not isinstance(events, list):
        return "root field 'events' is not a list"
    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            return f"events[{event_index}] is not an object"
        entries = event.get("entries")
        if not isinstance(entries, list):
            return f"events[{event_index}].entries is not a list"
        if any(not isinstance(entry, dict) for entry in entries):
            return f"events[{event_index}].entries contains a non-object"
    summary = feature.get("summary")
    if summary is not None and not isinstance(summary, str):
        return "optional root field 'summary' is not a string"
    return None


class DirectoryFeatureSource:
    """Load feature JSON/TXT files and expected chunks from directories."""

    def __init__(
        self,
        *,
        features_dir: Path,
        truncated_dir: Path | None,
        name: str,
    ) -> None:
        self.features_dir = features_dir
        self.truncated_dir = truncated_dir
        self.name = name

    def expected_chunks(self, log_id: int) -> list[str]:
        if self.truncated_dir is None:
            return [item.stem for item in self.load_chunks(log_id)]
        stems = {
            path.stem
            for path in self.truncated_dir.glob(f"{log_id}_*.log")
            if FEATURE_NAME_RE.fullmatch(path.stem)
        }
        return sorted(stems, key=_natural_stem_key)

    def load_chunks(self, log_id: int) -> list[ChunkLoadResult]:
        selected: dict[str, Path] = {}
        for suffix in (".txt", ".json"):
            for path in self.features_dir.glob(f"{log_id}_*{suffix}"):
                if not FEATURE_NAME_RE.fullmatch(path.stem):
                    continue
                # JSON takes precedence when both formats exist for the same chunk.
                if path.stem not in selected or suffix == ".json":
                    selected[path.stem] = path

        results: list[ChunkLoadResult] = []
        for stem, path in sorted(selected.items(), key=lambda item: _natural_stem_key(item[0])):
            match = FEATURE_NAME_RE.fullmatch(stem)
            assert match is not None
            try:
                feature = extract_json_object(path.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                feature = None
                error = f"read error: {exc}"
            else:
                error = "no complete JSON object" if feature is None else validate_feature(feature)
            results.append(
                ChunkLoadResult(
                    log_id=int(match.group(1)),
                    chunk_id=int(match.group(2)),
                    stem=stem,
                    path=path,
                    feature=feature if error is None else None,
                    error=error,
                )
            )
        return results

    def raw_chunk_path(self, chunk_stem: str) -> Path | None:
        if self.truncated_dir is None:
            return None
        path = self.truncated_dir / f"{chunk_stem}.log"
        return path if path.is_file() else None


def build_coverage(
    expected: Sequence[str], loaded: Sequence[ChunkLoadResult]
) -> CoverageReport:
    expected_set = set(expected)
    by_stem = {item.stem: item for item in loaded}
    valid = [stem for stem in expected if stem in by_stem and by_stem[stem].valid]
    invalid = {
        stem: by_stem[stem].error or "invalid feature"
        for stem in expected
        if stem in by_stem and not by_stem[stem].valid
    }
    missing = [stem for stem in expected if stem not in by_stem]
    extra = sorted((set(by_stem) - expected_set), key=_natural_stem_key)
    return CoverageReport(
        expected_chunks=list(expected),
        valid_chunks=valid,
        invalid_chunks=invalid,
        missing_chunks=missing,
        extra_chunks=extra,
    )


def parse_raw_audit_entries(path: Path) -> list[dict[str, Any]]:
    """Parse factual fields from raw msg groups for identity matching only."""
    ordered_ids: list[str] = []
    lines_by_id: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = MSG_ID_RE.search(line)
        if not match:
            continue
        msg_id = match.group(1)
        if msg_id not in lines_by_id:
            ordered_ids.append(msg_id)
            lines_by_id[msg_id] = []
        lines_by_id[msg_id].append(line)

    parsed: list[dict[str, Any]] = []
    for msg_id in ordered_ids:
        item: dict[str, Any] = {"source_entry_id": msg_id, "paths": []}
        for line in lines_by_id[msg_id]:
            record_type = _field(line, "type")
            if record_type == "SYSCALL":
                for key in ("pid", "ppid", "syscall"):
                    value = _field(line, key)
                    if value is not None:
                        item[key] = int(value) if value.isdigit() else value
                for key in ("comm", "exe"):
                    value = _field(line, key)
                    if value is not None:
                        item[key] = value
                success = _field(line, "success")
                if success is not None:
                    item["success"] = success.lower() in {"yes", "true", "1"}
            elif record_type == "PROCTITLE":
                marker = "proctitle="
                if marker in line:
                    item["proctitle"] = line.split(marker, 1)[1].strip()
            elif record_type == "CWD":
                cwd = _field(line, "cwd")
                if cwd is not None:
                    item["cwd"] = cwd
            elif record_type == "PATH":
                name = _field(line, "name")
                nametype = _field(line, "nametype")
                if name is not None:
                    path_item: dict[str, str] = {"name": name}
                    if nametype is not None:
                        path_item["nametype"] = nametype
                    item["paths"].append(path_item)
        parsed.append(item)
    return parsed


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in {"yes", "true"}:
            return True
        if stripped.lower() in {"no", "false"}:
            return False
        if stripped.isdigit():
            return int(stripped)
        return stripped
    return value


def _normalized_paths(entry: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    paths: list[tuple[str, str]] = []
    for item in entry.get("paths") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        paths.append((str(item["name"]).strip(), str(item.get("nametype", "")).strip()))
    return tuple(sorted(paths))


def canonical_entry_fingerprint(entry: dict[str, Any]) -> tuple[Any, ...]:
    return (
        *(
            _normalize_scalar(entry.get(key))
            for key in ("pid", "ppid", "comm", "exe", "syscall", "success", "proctitle", "cwd")
        ),
        _normalized_paths(entry),
    )


def _feature_matches_raw(feature: dict[str, Any], raw: dict[str, Any]) -> bool:
    factual_keys = ("pid", "ppid", "comm", "exe", "syscall", "success", "proctitle", "cwd")
    compared = 0
    for key in factual_keys:
        if key not in feature or feature[key] is None:
            continue
        compared += 1
        if _normalize_scalar(feature[key]) != _normalize_scalar(raw.get(key)):
            return False
    feature_paths = set(_normalized_paths(feature))
    if feature_paths:
        compared += 1
        if not feature_paths.issubset(set(_normalized_paths(raw))):
            return False
    # Require the core process/syscall identity, not a path-only coincidence.
    has_core = any(key in feature and feature[key] is not None for key in ("pid", "exe", "comm", "syscall"))
    return compared > 0 and has_core


def _flatten_entry_refs(chunk: dict[str, Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    refs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for event in chunk.get("events") or []:
        entries = event.get("entries") or []
        for entry in entries:
            refs.append((entry, entries))
    return refs


def attach_source_entry_ids(chunk: dict[str, Any], raw_path: Path | None) -> int:
    """Attach raw audit identities in feature order; return ambiguous match count."""
    if raw_path is None:
        return 0
    raw_entries = parse_raw_audit_entries(raw_path)
    cursor = 0
    ambiguous = 0
    for feature_entry, _ in _flatten_entry_refs(chunk):
        candidates = [
            index
            for index in range(cursor, len(raw_entries))
            if _feature_matches_raw(feature_entry, raw_entries[index])
        ]
        if not candidates:
            continue
        chosen = candidates[0]
        if len(candidates) == 1:
            feature_entry["source_entry_id"] = raw_entries[chosen]["source_entry_id"]
        else:
            # Repetitive syscall-only entries cannot be assigned a stable raw ID
            # confidently. Leave them unidentified so boundary fingerprinting can
            # decide only from the adjacent suffix/prefix.
            ambiguous += 1
        cursor = chosen + 1
    return ambiguous


def _remove_entry_objects(chunk: dict[str, Any], removals: set[int]) -> int:
    removed = 0
    kept_events: list[dict[str, Any]] = []
    for event in chunk.get("events") or []:
        entries = event.get("entries") or []
        kept = [entry for entry in entries if id(entry) not in removals]
        removed += len(entries) - len(kept)
        if kept:
            event["entries"] = kept
            kept_events.append(event)
    chunk["events"] = kept_events
    return removed


def _longest_suffix_prefix_match(
    previous_entries: Sequence[dict[str, Any]],
    current_entries: Sequence[dict[str, Any]],
    cap: int,
) -> int:
    limit = min(cap, len(previous_entries), len(current_entries))
    for length in range(limit, 0, -1):
        left = [canonical_entry_fingerprint(entry) for entry in previous_entries[-length:]]
        right = [canonical_entry_fingerprint(entry) for entry in current_entries[:length]]
        if left == right:
            return length
    return 0


def normalize_overlaps(
    chunks: list[dict[str, Any]],
    *,
    feature_source: FeatureSource,
    overlap_entries: int,
) -> OverlapReport:
    report = OverlapReport(
        input_feature_entries=sum(len(_flatten_entry_refs(chunk)) for chunk in chunks)
    )
    for chunk in chunks:
        report.ambiguous_raw_identity_matches += attach_source_entry_ids(
            chunk, feature_source.raw_chunk_path(chunk["chunk_id"])
        )

    for previous, current in zip(chunks, chunks[1:]):
        previous_key = _natural_stem_key(previous["chunk_id"])
        current_key = _natural_stem_key(current["chunk_id"])
        if previous_key[0] != current_key[0] or current_key[1] != previous_key[1] + 1:
            # Invalid/missing features create a gap. Those surviving chunks are
            # chronological neighbors, but not an actual chunk boundary.
            continue
        previous_entries = [entry for entry, _ in _flatten_entry_refs(previous)]
        current_entries = [entry for entry, _ in _flatten_entry_refs(current)]
        previous_ids = {
            entry.get("source_entry_id")
            for entry in previous_entries
            if entry.get("source_entry_id")
        }
        duplicate_entries = [
            entry
            for entry in current_entries
            if entry.get("source_entry_id") in previous_ids
        ][:overlap_entries]

        if not duplicate_entries:
            fallback_length = _longest_suffix_prefix_match(
                previous_entries, current_entries, overlap_entries
            )
            duplicate_entries = current_entries[:fallback_length]

        if not duplicate_entries:
            continue
        boundary = f"{previous['chunk_id']}->{current['chunk_id']}"
        report.removed_by_boundary[boundary] = [
            str(entry.get("source_entry_id") or "fingerprint-match")
            for entry in duplicate_entries
        ]
        report.removed_overlap_entries += _remove_entry_objects(
            current, {id(entry) for entry in duplicate_entries}
        )

    report.output_feature_entries = sum(len(_flatten_entry_refs(chunk)) for chunk in chunks)
    return report


def _annotate_chunk(stem: str, feature: dict[str, Any]) -> dict[str, Any]:
    cloned = copy.deepcopy(feature)
    cloned["chunk_id"] = stem
    for event in cloned.get("events") or []:
        event["source_chunk"] = stem
        for entry in event.get("entries") or []:
            # Notes are model-authored interpretations, not raw audit evidence.
            entry.pop("notes", None)
            entry["source_chunk"] = stem
    return cloned


def build_timeline_units(
    chunks: Sequence[dict[str, Any]], representation: str
) -> list[dict[str, Any]]:
    if representation not in {"entries_only", "entries_and_summary", "summary_only"}:
        raise ValueError(f"Unknown representation: {representation}")
    units: list[dict[str, Any]] = []
    for chunk in chunks:
        stem = chunk["chunk_id"]
        unit_count_before = len(units)
        if representation != "summary_only":
            for event in chunk.get("events") or []:
                units.append(
                    {
                        "source_chunk": stem,
                        "kind": "structured_event",
                        "event": event,
                    }
                )
        if representation != "entries_only" and chunk.get("summary"):
            units.append(
                {
                    "source_chunk": stem,
                    "kind": "untrusted_chunk_summary",
                    "summary": chunk["summary"],
                }
            )
        if len(units) == unit_count_before:
            units.append(
                {
                    "source_chunk": stem,
                    "kind": "empty_feature_chunk",
                    "meaning": "No usable evidence exists in the selected representation for this chunk.",
                }
            )
    return units


def prepare_timeline(
    *,
    log_id: int,
    feature_source: FeatureSource,
    representation: str,
    overlap_entries: int = 3,
    allow_incomplete: bool = False,
) -> PreparedTimeline:
    expected = feature_source.expected_chunks(log_id)
    loaded = feature_source.load_chunks(log_id)
    if not expected:
        raise IncompleteInputError(f"No expected chunks found for log {log_id}")
    coverage = build_coverage(expected, loaded)
    if not coverage.complete and not allow_incomplete:
        raise IncompleteInputError(
            f"Log {log_id} feature coverage is incomplete: "
            f"{json.dumps(coverage.to_dict(), ensure_ascii=False)}",
            coverage=coverage,
        )

    expected_set = set(expected)
    chunks = [
        _annotate_chunk(item.stem, item.feature)
        for item in loaded
        if item.valid and item.stem in expected_set and item.feature is not None
    ]
    chunks.sort(key=lambda item: _natural_stem_key(item["chunk_id"]))
    overlap = normalize_overlaps(
        chunks,
        feature_source=feature_source,
        overlap_entries=overlap_entries,
    )
    return PreparedTimeline(
        log_id=log_id,
        feature_source=feature_source.name,
        representation=representation,
        coverage=coverage,
        overlap=overlap,
        chunks=chunks,
        units=build_timeline_units(chunks, representation),
    )


class HeuristicTokenCounter:
    """Non-official fallback for planning/tests; approximately four chars/token."""

    name = "heuristic_chars_div_4"
    exact = False

    def count_text(self, text: str) -> int:
        return max(1, math.ceil(len(text) / 4))

    def count_request(self, system_prompt: str, payload: dict[str, Any]) -> int:
        user_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return self.count_text(system_prompt + "\n" + user_text) + 8


class HuggingFaceTokenCounter:
    """Tokenizer-backed request counting for local GPT-OSS summarization."""

    exact = True

    def __init__(
        self,
        model_or_path: str,
        *,
        reasoning_effort: str = "medium",
        tokenizer: Any | None = None,
    ) -> None:
        if tokenizer is None:
            try:
                from transformers import AutoTokenizer  # type: ignore
            except ModuleNotFoundError as exc:
                raise PipelineError(
                    "transformers is required for --tokenizer; run in the unsloth/axolotl environment"
                ) from exc
            tokenizer = AutoTokenizer.from_pretrained(model_or_path, use_fast=True)
        self.tokenizer = tokenizer
        self.name = f"huggingface:{model_or_path}"
        self.reasoning_effort = reasoning_effort

    def count_text(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def count_request(self, system_prompt: str, payload: dict[str, Any]) -> int:
        user_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        try:
            ids = self.tokenizer.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
                reasoning_effort=self.reasoning_effort,
            )
            return len(ids)
        except (AttributeError, ValueError, TypeError):
            return self.count_text(system_prompt + "\n" + user_text) + 8


class CommandSummaryBackend:
    """Invoke any local/API adapter through a JSON-over-stdin command contract."""

    def __init__(self, command: str, name: str | None = None, timeout_seconds: int = 600) -> None:
        self.command = shlex.split(command)
        if not self.command:
            raise ValueError("backend command cannot be empty")
        self.name = name or f"command:{self.command[0]}"
        self.timeout_seconds = timeout_seconds
        self.call_metadata: list[dict[str, Any]] = []

    def generate(
        self,
        *,
        stage: str,
        system_prompt: str,
        payload: dict[str, Any],
        max_output_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        request = {
            "stage": stage,
            "system_prompt": system_prompt,
            "payload": payload,
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
        }
        completed = subprocess.run(
            self.command,
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise PipelineError(
                f"Backend command exited {completed.returncode}: {completed.stderr.strip()}"
            )
        result = extract_json_object(completed.stdout)
        if result is None:
            raise BackendOutputError("Backend command did not return one JSON object")
        if isinstance(result.get("output"), dict):
            metadata = result.get("metadata")
            if isinstance(metadata, dict):
                self.call_metadata.append(metadata)
            return result["output"]
        return result


class DeterministicTestBackend:
    """Offline backend for tests and wiring smoke checks; not a model baseline."""

    name = "deterministic-test"

    def generate(
        self,
        *,
        stage: str,
        system_prompt: str,
        payload: dict[str, Any],
        max_output_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        del system_prompt, max_output_tokens, temperature
        source_chunks = ordered_source_chunks(payload)
        if "summaries" in payload:
            children = payload["summaries"]
            actions = [action for child in children for action in child.get("ordered_actions", [])]
            relationships = [
                item for child in children for item in child.get("process_relationships", [])
            ]
            state_changes = [item for child in children for item in child.get("state_changes", [])]
            uncertainties = [item for child in children for item in child.get("uncertainties", [])]
        else:
            actions = []
            relationships = []
            state_changes = []
            uncertainties = []
            for index, chunk in enumerate(source_chunks, 1):
                actions.append(
                    {
                        "sequence": index,
                        "action": f"Structured evidence was retained from {chunk}",
                        "processes": [],
                        "artifacts": [],
                        "evidence_chunks": [chunk],
                        "confidence": "unspecified",
                    }
                )
        for index, action in enumerate(actions, 1):
            action["sequence"] = index
        result: dict[str, Any] = {
            "chunk_range": [source_chunks[0], source_chunks[-1]] if source_chunks else [],
            "ordered_actions": actions,
            "process_relationships": relationships,
            "state_changes": state_changes,
            "uncertainties": uncertainties,
        }
        if stage == "final":
            result["summary"] = (
                f"Deterministic test summary covering {len(source_chunks)} source chunks."
            )
        return result


def ordered_source_chunks(value: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            source_chunk = item.get("source_chunk")
            if isinstance(source_chunk, str) and source_chunk not in seen:
                seen.add(source_chunk)
                found.append(source_chunk)
            evidence = item.get("evidence_chunks")
            if isinstance(evidence, list):
                for chunk in evidence:
                    if isinstance(chunk, str) and chunk not in seen:
                        seen.add(chunk)
                        found.append(chunk)
            for range_key in ("source_chunks", "chunk_range"):
                source_range = item.get(range_key)
                if isinstance(source_range, list):
                    for chunk in source_range:
                        if isinstance(chunk, str) and FEATURE_NAME_RE.fullmatch(chunk) and chunk not in seen:
                            seen.add(chunk)
                            found.append(chunk)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return sorted(found, key=_natural_stem_key)


def _split_oversized_unit(
    unit: dict[str, Any],
    *,
    prompt: str,
    counter: TokenCounter,
    config: ReductionConfig,
) -> list[dict[str, Any]]:
    event = unit.get("event")
    entries = event.get("entries") if isinstance(event, dict) else None
    if not isinstance(entries, list) or len(entries) <= 1:
        raise InputTooLargeError(
            f"One timeline unit from {unit.get('source_chunk')} cannot fit the configured context"
        )
    split_units: list[dict[str, Any]] = []
    for entry in entries:
        split_event = copy.deepcopy(event)
        split_event["entries"] = [entry]
        split_unit = {
            "source_chunk": unit["source_chunk"],
            "kind": "structured_event_fragment",
            "event": split_event,
        }
        request_tokens = counter.count_request(prompt, {"timeline": [split_unit]})
        if request_tokens + config.max_output_tokens > config.context_limit:
            raise InputTooLargeError(
                f"One feature entry from {unit.get('source_chunk')} cannot fit the configured context"
            )
        if counter.count_text(json.dumps({"timeline": [split_unit]}, ensure_ascii=False)) > config.input_token_budget:
            raise InputTooLargeError(
                f"One feature entry from {unit.get('source_chunk')} exceeds the input token budget"
            )
        split_units.append(split_unit)
    return split_units


def _expand_oversized_units(
    units: Sequence[dict[str, Any]],
    *,
    prompt: str,
    counter: TokenCounter,
    config: ReductionConfig,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for unit in units:
        payload = {"timeline": [unit]}
        request_tokens = counter.count_request(prompt, payload)
        payload_tokens = counter.count_text(json.dumps(payload, ensure_ascii=False))
        if (
            request_tokens + config.max_output_tokens <= config.context_limit
            and payload_tokens <= config.input_token_budget
        ):
            expanded.append(unit)
        else:
            expanded.extend(
                _split_oversized_unit(
                    unit,
                    prompt=prompt,
                    counter=counter,
                    config=config,
                )
            )
    return expanded


def pack_adjacent(
    items: Sequence[dict[str, Any]],
    *,
    payload_key: str,
    prompt: str,
    counter: TokenCounter,
    config: ReductionConfig,
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in items:
        candidate = [*current, item]
        payload = {payload_key: candidate}
        request_tokens = counter.count_request(prompt, payload)
        payload_tokens = counter.count_text(json.dumps(payload, ensure_ascii=False))
        fits = (
            request_tokens + config.max_output_tokens <= config.context_limit
            and payload_tokens <= config.input_token_budget
        )
        if fits:
            current = candidate
            continue
        if not current:
            raise InputTooLargeError("One reduction item cannot fit the configured token budget")
        groups.append(current)
        current = [item]
        single_payload = {payload_key: current}
        single_request_tokens = counter.count_request(prompt, single_payload)
        single_payload_tokens = counter.count_text(
            json.dumps(single_payload, ensure_ascii=False)
        )
        if single_request_tokens + config.max_output_tokens > config.context_limit:
            raise InputTooLargeError("One reduction item cannot fit the configured context")
        if single_payload_tokens > config.input_token_budget:
            raise InputTooLargeError("One reduction item cannot fit the configured input budget")
    if current:
        groups.append(current)
    return groups


def _validate_summary_result(
    result: dict[str, Any], *, allowed_chunks: set[str], require_narrative: bool
) -> None:
    actions = result.get("ordered_actions")
    if not isinstance(actions, list):
        raise BackendOutputError("Backend output is missing ordered_actions[]")
    if require_narrative and not isinstance(result.get("summary"), str):
        raise BackendOutputError("Final backend output is missing summary text")
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise BackendOutputError(f"ordered_actions[{index}] is not an object")
        evidence = action.get("evidence_chunks")
        if not isinstance(evidence, list) or not evidence:
            raise BackendOutputError(f"ordered_actions[{index}] has no evidence_chunks")
        unknown = {str(chunk) for chunk in evidence} - allowed_chunks
        if unknown:
            raise BackendOutputError(
                f"ordered_actions[{index}] cites unknown chunks: {sorted(unknown)}"
            )


def _call_backend(
    *,
    backend: SummaryBackend,
    counter: TokenCounter,
    config: ReductionConfig,
    stage: str,
    prompt: str,
    payload: dict[str, Any],
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    input_tokens = counter.count_request(prompt, payload)
    if input_tokens + config.max_output_tokens > config.context_limit:
        raise InputTooLargeError(
            f"{stage} request would use {input_tokens}+{config.max_output_tokens} "
            f"> {config.context_limit} tokens"
        )
    result = backend.generate(
        stage=stage,
        system_prompt=prompt,
        payload=payload,
        max_output_tokens=config.max_output_tokens,
        temperature=config.temperature,
    )
    allowed_chunks = set(ordered_source_chunks(payload))
    _validate_summary_result(
        result, allowed_chunks=allowed_chunks, require_narrative=stage == "final"
    )
    result["source_chunks"] = sorted(allowed_chunks, key=_natural_stem_key)
    trace.append(
        {
            "call_index": len(trace) + 1,
            "stage": stage,
            "input_tokens": input_tokens,
            "source_chunks": sorted(allowed_chunks, key=_natural_stem_key),
        }
    )
    return result


def plan_reduction(
    *,
    prepared: PreparedTimeline,
    prompts: PromptSet,
    counter: TokenCounter,
    config: ReductionConfig,
) -> dict[str, Any]:
    full_payload = {"timeline": prepared.units}
    direct_tokens = counter.count_request(prompts.final, full_payload)
    if (
        direct_tokens + config.max_output_tokens <= config.context_limit
        and counter.count_text(json.dumps(full_payload, ensure_ascii=False))
        <= config.input_token_budget
    ):
        return {
            "strategy": "direct",
            "direct_input_tokens": direct_tokens,
            "leaf_groups": [ordered_source_chunks(full_payload)],
        }
    units = _expand_oversized_units(
        prepared.units,
        prompt=prompts.leaf,
        counter=counter,
        config=config,
    )
    groups = pack_adjacent(
        units,
        payload_key="timeline",
        prompt=prompts.leaf,
        counter=counter,
        config=config,
    )
    return {
        "strategy": "hierarchical",
        "direct_input_tokens": direct_tokens,
        "leaf_group_count": len(groups),
        "leaf_groups": [ordered_source_chunks(group) for group in groups],
        "leaf_group_input_tokens": [
            counter.count_request(prompts.leaf, {"timeline": group}) for group in groups
        ],
    }


def run_reduction(
    *,
    prepared: PreparedTimeline,
    prompts: PromptSet,
    backend: SummaryBackend,
    counter: TokenCounter,
    config: ReductionConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = plan_reduction(
        prepared=prepared, prompts=prompts, counter=counter, config=config
    )
    trace: list[dict[str, Any]] = []
    if plan["strategy"] == "direct":
        result = _call_backend(
            backend=backend,
            counter=counter,
            config=config,
            stage="final",
            prompt=prompts.final,
            payload={"timeline": prepared.units},
            trace=trace,
        )
        return result, {**plan, "tree_depth": 1, "calls": trace}

    units = _expand_oversized_units(
        prepared.units,
        prompt=prompts.leaf,
        counter=counter,
        config=config,
    )
    leaf_groups = pack_adjacent(
        units,
        payload_key="timeline",
        prompt=prompts.leaf,
        counter=counter,
        config=config,
    )
    current = [
        _call_backend(
            backend=backend,
            counter=counter,
            config=config,
            stage="leaf",
            prompt=prompts.leaf,
            payload={"timeline": group},
            trace=trace,
        )
        for group in leaf_groups
    ]
    depth = 1

    while True:
        final_payload = {"summaries": current}
        if (
            counter.count_request(prompts.final, final_payload) + config.max_output_tokens
            <= config.context_limit
            and counter.count_text(json.dumps(final_payload, ensure_ascii=False))
            <= config.input_token_budget
        ):
            final = _call_backend(
                backend=backend,
                counter=counter,
                config=config,
                stage="final",
                prompt=prompts.final,
                payload=final_payload,
                trace=trace,
            )
            return final, {**plan, "tree_depth": depth + 1, "calls": trace}

        merge_groups = pack_adjacent(
            current,
            payload_key="summaries",
            prompt=prompts.merge,
            counter=counter,
            config=config,
        )
        if all(len(group) == 1 for group in merge_groups):
            raise InputTooLargeError(
                "Adjacent summaries cannot be reduced under the configured token budget"
            )
        next_level: list[dict[str, Any]] = []
        for group in merge_groups:
            if len(group) == 1:
                next_level.append(group[0])
                continue
            next_level.append(
                _call_backend(
                    backend=backend,
                    counter=counter,
                    config=config,
                    stage="merge",
                    prompt=prompts.merge,
                    payload={"summaries": group},
                    trace=trace,
                )
            )
        current = next_level
        depth += 1


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
