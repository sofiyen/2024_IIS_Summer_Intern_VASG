from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.scripts.summary_generation.summary_pipeline import (
    DeterministicTestBackend,
    DirectoryFeatureSource,
    HeuristicTokenCounter,
    IncompleteInputError,
    PromptSet,
    ReductionConfig,
    prepare_timeline,
    run_reduction,
)
from tools.scripts.summary_generation.unsloth_backend import extract_gpt_oss_final
from tools.scripts.summary_generation.openai_backend import OpenAISummaryBackend


def audit_group(
    msg_id: str,
    *,
    pid: int,
    ppid: int,
    syscall: int,
    comm: str,
    exe: str,
    path: str | None = None,
) -> str:
    lines = [
        f'node=n type=SYSCALL msg=audit({msg_id}): syscall={syscall} success=yes '
        f'ppid={ppid} pid={pid} comm="{comm}" exe="{exe}"\n'
    ]
    if path:
        lines.append(
            f'node=n type=PATH msg=audit({msg_id}): item=0 name="{path}" nametype=NORMAL\n'
        )
    return "".join(lines)


def entry(
    seq: int,
    *,
    pid: int,
    ppid: int,
    syscall: int,
    comm: str,
    exe: str,
    path: str | None = None,
    notes: str | None = None,
) -> dict:
    value = {
        "entry_seq": seq,
        "pid": pid,
        "ppid": ppid,
        "syscall": syscall,
        "success": True,
        "comm": comm,
        "exe": exe,
    }
    if path:
        value["paths"] = [{"name": path, "nametype": "NORMAL"}]
    if notes:
        value["notes"] = notes
    return value


def feature(entries: list[dict], summary: str | None = None) -> dict:
    value = {
        "events": [
            {
                "event_seq": 1,
                "event_key": {"type": "pid_exe_comm", "value": "test"},
                "entries": entries,
            }
        ]
        if entries
        else []
    }
    if summary is not None:
        value["summary"] = summary
    return value


class SummaryPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.features = self.root / "features"
        self.raw = self.root / "raw"
        self.features.mkdir()
        self.raw.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def source(self) -> DirectoryFeatureSource:
        return DirectoryFeatureSource(
            features_dir=self.features,
            truncated_dir=self.raw,
            name="fixture",
        )

    def write_feature(self, stem: str, value: dict, suffix: str = ".json") -> None:
        (self.features / f"{stem}{suffix}").write_text(json.dumps(value), encoding="utf-8")

    def test_numeric_order_and_structured_coverage(self) -> None:
        for chunk_id in (10, 2, 1):
            stem = f"7_{chunk_id}"
            (self.raw / f"{stem}.log").write_text(
                audit_group(
                    f"1.{chunk_id}:{chunk_id}",
                    pid=chunk_id,
                    ppid=1,
                    syscall=59,
                    comm="sh",
                    exe="/bin/sh",
                ),
                encoding="utf-8",
            )
            self.write_feature(
                stem,
                feature(
                    [
                        entry(
                            1,
                            pid=chunk_id,
                            ppid=1,
                            syscall=59,
                            comm="sh",
                            exe="/bin/sh",
                            notes="model interpretation must be removed",
                        )
                    ]
                ),
            )

        prepared = prepare_timeline(
            log_id=7,
            feature_source=self.source(),
            representation="entries_only",
        )
        self.assertEqual([chunk["chunk_id"] for chunk in prepared.chunks], ["7_1", "7_2", "7_10"])
        self.assertEqual(prepared.coverage.coverage, 1.0)
        first_entry = prepared.chunks[0]["events"][0]["entries"][0]
        self.assertNotIn("notes", first_entry)

    def test_strict_mode_reports_invalid_and_missing_chunks(self) -> None:
        for chunk_id in (1, 2, 3):
            (self.raw / f"9_{chunk_id}.log").write_text("", encoding="utf-8")
        self.write_feature("9_1", feature([]))
        (self.features / "9_2.txt").write_text("unfinished {", encoding="utf-8")

        with self.assertRaises(IncompleteInputError) as raised:
            prepare_timeline(
                log_id=9,
                feature_source=self.source(),
                representation="entries_only",
            )
        report = raised.exception.coverage
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.valid_chunks, ["9_1"])
        self.assertEqual(report.invalid_chunks, {"9_2": "no complete JSON object"})
        self.assertEqual(report.missing_chunks, ["9_3"])

    def test_raw_id_assisted_adjacent_overlap_removes_later_copy(self) -> None:
        raw_a = (
            audit_group("10.1:1", pid=10, ppid=1, syscall=59, comm="sh", exe="/bin/sh")
            + audit_group(
                "10.2:2",
                pid=11,
                ppid=10,
                syscall=257,
                comm="cat",
                exe="/bin/cat",
                path="/etc/shadow",
            )
        )
        raw_b = (
            audit_group(
                "10.2:2",
                pid=11,
                ppid=10,
                syscall=257,
                comm="cat",
                exe="/bin/cat",
                path="/etc/shadow",
            )
            + audit_group("10.3:3", pid=12, ppid=11, syscall=1, comm="cat", exe="/bin/cat")
        )
        (self.raw / "4_1.log").write_text(raw_a, encoding="utf-8")
        (self.raw / "4_2.log").write_text(raw_b, encoding="utf-8")
        duplicate = entry(
            1,
            pid=11,
            ppid=10,
            syscall=257,
            comm="cat",
            exe="/bin/cat",
            path="/etc/shadow",
        )
        self.write_feature("4_1", feature([duplicate]))
        self.write_feature(
            "4_2",
            feature(
                [
                    duplicate,
                    entry(2, pid=12, ppid=11, syscall=1, comm="cat", exe="/bin/cat"),
                ]
            ),
        )

        prepared = prepare_timeline(
            log_id=4,
            feature_source=self.source(),
            representation="entries_only",
            overlap_entries=1,
        )
        self.assertEqual(prepared.overlap.removed_overlap_entries, 1)
        later_entries = prepared.chunks[1]["events"][0]["entries"]
        self.assertEqual([item["pid"] for item in later_entries], [12])
        self.assertEqual(later_entries[0]["source_entry_id"], "10.3:3")

    def test_representation_modes_keep_summary_as_separate_untrusted_unit(self) -> None:
        (self.raw / "3_1.log").write_text(
            audit_group("1.1:1", pid=1, ppid=0, syscall=1, comm="x", exe="/x"),
            encoding="utf-8",
        )
        self.write_feature(
            "3_1",
            feature(
                [entry(1, pid=1, ppid=0, syscall=1, comm="x", exe="/x")],
                summary="possibly meaningful",
            ),
        )
        both = prepare_timeline(
            log_id=3,
            feature_source=self.source(),
            representation="entries_and_summary",
        )
        self.assertEqual(
            [unit["kind"] for unit in both.units],
            ["structured_event", "untrusted_chunk_summary"],
        )
        summaries = prepare_timeline(
            log_id=3,
            feature_source=self.source(),
            representation="summary_only",
        )
        self.assertEqual([unit["kind"] for unit in summaries.units], ["untrusted_chunk_summary"])

    def test_overlap_is_not_removed_across_missing_chunk_gap(self) -> None:
        for chunk_id in (1, 2, 3):
            (self.raw / f"8_{chunk_id}.log").write_text("", encoding="utf-8")
        repeated = entry(1, pid=4, ppid=1, syscall=1, comm="x", exe="/x")
        self.write_feature("8_1", feature([repeated]))
        self.write_feature("8_3", feature([repeated]))

        prepared = prepare_timeline(
            log_id=8,
            feature_source=self.source(),
            representation="entries_only",
            allow_incomplete=True,
        )
        self.assertEqual(prepared.overlap.removed_overlap_entries, 0)
        self.assertEqual(prepared.overlap.output_feature_entries, 2)

    def test_hierarchical_reduction_preserves_source_order(self) -> None:
        for chunk_id in range(1, 5):
            stem = f"5_{chunk_id}"
            (self.raw / f"{stem}.log").write_text(
                audit_group(
                    f"1.{chunk_id}:{chunk_id}",
                    pid=chunk_id,
                    ppid=0,
                    syscall=1,
                    comm="x",
                    exe="/x",
                    path=f"/tmp/{'z' * 180}{chunk_id}",
                ),
                encoding="utf-8",
            )
            self.write_feature(
                stem,
                feature(
                    [
                        entry(
                            1,
                            pid=chunk_id,
                            ppid=0,
                            syscall=1,
                            comm="x",
                            exe="/x",
                            path=f"/tmp/{'z' * 180}{chunk_id}",
                        )
                    ]
                ),
            )
        prepared = prepare_timeline(
            log_id=5,
            feature_source=self.source(),
            representation="entries_only",
        )
        result, metadata = run_reduction(
            prepared=prepared,
            prompts=PromptSet(leaf="leaf", merge="merge", final="final"),
            backend=DeterministicTestBackend(),
            counter=HeuristicTokenCounter(),
            config=ReductionConfig(
                context_limit=500,
                input_token_budget=300,
                max_output_tokens=50,
            ),
        )
        self.assertEqual(metadata["strategy"], "hierarchical")
        evidence_order = [
            action["evidence_chunks"][0] for action in result["ordered_actions"]
        ]
        self.assertEqual(evidence_order, ["5_1", "5_2", "5_3", "5_4"])
        self.assertIn("summary", result)

    def test_gpt_oss_final_channel_extraction(self) -> None:
        raw = (
            "<|channel|>analysis<|message|>internal reasoning<|end|>"
            '<|channel|>final<|message|>{"ordered_actions": []}<|return|>'
        )
        self.assertEqual(extract_gpt_oss_final(raw), '{"ordered_actions": []}')
        self.assertIsNone(extract_gpt_oss_final("analysis without a final channel"))

    def test_openai_backend_requests_json_and_records_usage(self) -> None:
        expected = {
            "summary": "grounded",
            "ordered_actions": [],
            "process_relationships": [],
            "state_changes": [],
            "uncertainties": [],
        }

        class FakeCompletions:
            def __init__(self) -> None:
                self.request: dict | None = None

            def create(self, **kwargs):
                self.request = kwargs
                return SimpleNamespace(
                    id="resp_test",
                    model="gpt-5.4-mini-2026-01-01",
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=json.dumps(expected)),
                            finish_reason="stop",
                        )
                    ],
                    usage=SimpleNamespace(
                        prompt_tokens=100,
                        completion_tokens=25,
                        total_tokens=125,
                        completion_tokens_details=SimpleNamespace(reasoning_tokens=10),
                    ),
                )

        completions = FakeCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        backend = OpenAISummaryBackend(model="gpt-5.4-mini", client=client)
        result = backend.generate(
            stage="final",
            system_prompt="Return JSON only.",
            payload={"timeline": []},
            max_output_tokens=3000,
            temperature=0.0,
        )

        self.assertEqual(result, expected)
        assert completions.request is not None
        self.assertEqual(completions.request["response_format"], {"type": "json_object"})
        self.assertEqual(completions.request["reasoning_effort"], "medium")
        self.assertNotIn("temperature", completions.request)
        self.assertIsNone(backend.call_metadata[0]["temperature_sent"])
        self.assertEqual(backend.call_metadata[0]["usage"]["reasoning_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
