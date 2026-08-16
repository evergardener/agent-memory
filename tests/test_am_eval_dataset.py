import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

import agent_memory.am_eval_dataset as dataset_module
from agent_memory.am_eval_dataset import (
    MAX_SNAPSHOT_BYTES,
    DatasetError,
    dataset_summary,
    load_dataset,
    load_dataset_snapshot,
    load_jsonl,
    read_file_snapshot,
    sha256_file,
    validate_atomic_fact_case,
)
from agent_memory.unified_memory import (
    parse_date_range,
    parse_episode,
    parse_preference,
    parse_temporal_rule,
    procedure_applicability,
)

DATASET_MANIFEST = (
    Path(__file__).parents[1]
    / "benchmarks/am-eval-v1/datasets/deterministic-gold-v1/manifest.json"
)
CASES = load_dataset(DATASET_MANIFEST)


def _cases(suite: str) -> tuple[dict, ...]:
    return tuple(case for case in CASES if case["suite"] == suite)


@pytest.mark.parametrize("case", _cases("preference"), ids=lambda case: case["case_id"])
def test_preference_dataset(case: dict) -> None:
    result = parse_preference(case["input"]["text"])
    expected = case["expected"]

    assert (result is not None) is expected["selected"]
    if result is not None:
        for field in ("aspect", "topic", "polarity"):
            assert getattr(result, field) == expected[field]


@pytest.mark.parametrize("case", _cases("date_range"), ids=lambda case: case["case_id"])
def test_date_range_dataset(case: dict) -> None:
    occurred_at = datetime.fromisoformat(case["input"]["occurred_at"])
    started_at, ended_at, precision, resolution = parse_date_range(
        case["input"]["text"], occurred_at
    )
    expected = case["expected"]

    assert started_at == (
        datetime.fromisoformat(expected["started_at"]) if expected["started_at"] else None
    )
    assert ended_at == (
        datetime.fromisoformat(expected["ended_at"]) if expected["ended_at"] else None
    )
    assert precision == expected["precision"]
    for key, value in expected["resolution"].items():
        assert resolution[key] == value


@pytest.mark.parametrize(
    "case", _cases("temporal_rule"), ids=lambda case: case["case_id"]
)
def test_temporal_rule_dataset(case: dict) -> None:
    result = parse_temporal_rule(case["input"]["text"])
    expected = case["expected"]

    assert (result is not None) is expected["selected"]
    if result is not None:
        for field in ("rule_type", "label", "month", "day", "year"):
            assert getattr(result, field) == expected[field]


@pytest.mark.parametrize("case", _cases("episode"), ids=lambda case: case["case_id"])
def test_episode_dataset(case: dict) -> None:
    result = parse_episode(
        case["input"]["text"], datetime.fromisoformat(case["input"]["occurred_at"])
    )
    expected = case["expected"]

    assert (result is not None) is expected["selected"]
    if result is None:
        return
    assert result.episode_type == expected["episode_type"]
    assert result.accepted is expected["accepted"]
    assert [(item.name, item.role) for item in result.entities] == [
        tuple(item) for item in expected["entities"]
    ]
    assert [item.kind for item in result.steps] == expected["steps"]
    assert not {item.name for item in result.entities} & set(expected["excluded_entities"])


@pytest.mark.parametrize("case", _cases("procedure"), ids=lambda case: case["case_id"])
def test_procedure_dataset(case: dict) -> None:
    values = case["input"]
    result = procedure_applicability(
        values["expected_environment"],
        values["actual_environment"],
        valid_to=datetime.fromisoformat(values["valid_to"]) if values.get("valid_to") else None,
        now=datetime.fromisoformat(values["now"]),
    )

    assert result["status"] == case["expected"]["status"]
    assert result["auto_apply"] is case["expected"]["auto_apply"]


def test_dataset_manifest_is_frozen_complete_and_non_production() -> None:
    summary = dataset_summary(DATASET_MANIFEST)

    assert summary == {
        "schema_version": "am-eval-dataset-validation-v1",
        "dataset_id": "agent-memory-deterministic-gold-v1",
        "manifest_sha256": summary["manifest_sha256"],
        "case_count": 176,
        "suite_counts": {
            "date_range": 6,
            "episode": 4,
            "preference": 48,
            "procedure": 4,
            "recall": 110,
            "temporal_rule": 4,
        },
        "split_counts": {"development": 126, "validation": 50},
        "status": "PASS",
    }
    assert len(summary["manifest_sha256"]) == 64


def test_jsonl_loader_fails_closed_on_duplicate_cases(tmp_path: Path) -> None:
    case = {
        "schema_version": "am-eval-case-v1",
        "case_id": "duplicate",
        "suite": "preference",
        "split": "development",
        "input": {"text": "test"},
        "expected": {"selected": False},
    }
    path = tmp_path / "duplicate.jsonl"
    path.write_text("\n".join((json.dumps(case), json.dumps(case))), encoding="utf-8")

    with pytest.raises(DatasetError, match="duplicate case_id"):
        load_jsonl(path)


def test_manifest_loader_rejects_path_escape(tmp_path: Path) -> None:
    manifest = {
        "schema_version": "am-eval-dataset-manifest-v1",
        "dataset_id": "escape",
        "case_count": 1,
        "contains_production_data": False,
        "contains_memory_text": True,
        "external_data_sent": False,
        "files": [
            {
                "path": "../outside.jsonl",
                "sha256": "0" * 64,
                "case_count": 1,
                "suites": ["preference"],
            }
        ],
        "suite_counts": {"preference": 1},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DatasetError, match="escapes manifest root"):
        load_dataset(path)


def test_open_manifest_cannot_expose_cases_as_blind(tmp_path: Path) -> None:
    case = {
        "schema_version": "am-eval-case-v1",
        "case_id": "exposed-blind",
        "suite": "preference",
        "split": "blind",
        "input": {"text": "test"},
        "expected": {"selected": False},
    }
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps(case), encoding="utf-8")
    manifest = {
        "schema_version": "am-eval-dataset-manifest-v1",
        "dataset_id": "exposed-blind",
        "case_count": 1,
        "contains_production_data": False,
        "contains_memory_text": True,
        "external_data_sent": False,
        "visibility": "open",
        "blind_cases": 1,
        "files": [
            {
                "path": "cases.jsonl",
                "sha256": sha256_file(dataset),
                "case_count": 1,
                "suites": ["preference"],
            }
        ],
        "suite_counts": {"preference": 1},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DatasetError, match="open dataset cannot contain blind"):
        load_dataset(path)


def test_private_manifest_can_hold_blind_production_derived_gold(tmp_path: Path) -> None:
    case = {
        "schema_version": "am-eval-case-v1",
        "case_id": "private-blind",
        "suite": "preference",
        "split": "blind",
        "input": {"text": "脱敏后的私有样本"},
        "expected": {"selected": False},
    }
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps(case), encoding="utf-8")
    manifest = {
        "schema_version": "am-eval-dataset-manifest-v1",
        "dataset_id": "private-blind",
        "case_count": 1,
        "contains_production_data": True,
        "contains_memory_text": True,
        "external_data_sent": False,
        "visibility": "private",
        "blind_cases": 1,
        "files": [
            {
                "path": "cases.jsonl",
                "sha256": sha256_file(dataset),
                "case_count": 1,
                "suites": ["preference"],
            }
        ],
        "suite_counts": {"preference": 1},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert load_dataset(path) == (case,)


def test_dataset_hash_and_parser_use_the_same_file_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_case = {
        "schema_version": "am-eval-case-v1",
        "case_id": "original-snapshot",
        "suite": "preference",
        "split": "development",
        "input": {"text": "original"},
        "expected": {"selected": False},
    }
    replacement_case = {
        **original_case,
        "case_id": "replacement-after-hash",
        "input": {"text": "replacement"},
    }
    original_payload = (json.dumps(original_case) + "\n").encode()
    dataset = tmp_path / "cases.jsonl"
    dataset.write_bytes(original_payload)
    manifest = {
        "schema_version": "am-eval-dataset-manifest-v1",
        "dataset_id": "snapshot-race",
        "case_count": 1,
        "contains_production_data": False,
        "contains_memory_text": True,
        "external_data_sent": False,
        "visibility": "open",
        "blind_cases": 0,
        "files": [
            {
                "path": "cases.jsonl",
                "sha256": hashlib.sha256(original_payload).hexdigest(),
                "case_count": 1,
                "suites": ["preference"],
            }
        ],
        "suite_counts": {"preference": 1},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_digest = dataset_module._payload_sha256
    replaced = False

    def replace_after_snapshot(payload: bytes) -> str:
        nonlocal replaced
        digest = original_digest(payload)
        if payload == original_payload and not replaced:
            replaced = True
            dataset.write_text(json.dumps(replacement_case) + "\n", encoding="utf-8")
        return digest

    monkeypatch.setattr(dataset_module, "_payload_sha256", replace_after_snapshot)

    assert load_dataset(manifest_path) == (original_case,)
    assert replaced is True
    assert json.loads(dataset.read_text()) == replacement_case


def test_dataset_snapshot_returns_one_bound_manifest_and_case_view(tmp_path: Path) -> None:
    case = {
        "schema_version": "am-eval-case-v1",
        "case_id": "bound-snapshot",
        "suite": "preference",
        "split": "development",
        "input": {"text": "bound"},
        "expected": {"selected": False},
    }
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps(case) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": "am-eval-dataset-manifest-v1",
        "dataset_id": "bound-snapshot",
        "case_count": 1,
        "contains_production_data": False,
        "contains_memory_text": True,
        "external_data_sent": False,
        "visibility": "open",
        "blind_cases": 0,
        "files": [
            {
                "path": "cases.jsonl",
                "sha256": sha256_file(dataset),
                "case_count": 1,
                "suites": ["preference"],
            }
        ],
        "suite_counts": {"preference": 1},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    snapshot = load_dataset_snapshot(manifest_path)

    assert snapshot.path == manifest_path
    assert snapshot.manifest == manifest
    assert snapshot.cases == (case,)
    assert snapshot.manifest_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def test_dataset_snapshot_rejects_symlinked_file(tmp_path: Path) -> None:
    case = {
        "schema_version": "am-eval-case-v1",
        "case_id": "symlinked-case",
        "suite": "preference",
        "split": "development",
        "input": {"text": "test"},
        "expected": {"selected": False},
    }
    real_dataset = tmp_path / "real.jsonl"
    real_dataset.write_text(json.dumps(case) + "\n", encoding="utf-8")
    linked_dataset = tmp_path / "linked.jsonl"
    linked_dataset.symlink_to(real_dataset)
    manifest = {
        "schema_version": "am-eval-dataset-manifest-v1",
        "dataset_id": "symlinked-dataset",
        "case_count": 1,
        "contains_production_data": False,
        "contains_memory_text": True,
        "external_data_sent": False,
        "visibility": "open",
        "blind_cases": 0,
        "files": [
            {
                "path": "linked.jsonl",
                "sha256": sha256_file(real_dataset),
                "case_count": 1,
                "suites": ["preference"],
            }
        ],
        "suite_counts": {"preference": 1},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DatasetError, match="opened safely"):
        load_dataset(manifest_path)


def test_atomic_fact_gold_rejects_non_verbatim_span() -> None:
    case = {
        "schema_version": "am-eval-case-v1",
        "case_id": "bad-span",
        "suite": "atomic_fact",
        "split": "development",
        "input": {
            "evidence_ids": ["e1"],
            "evidence": ["项目 Orchid 使用 PostgreSQL"],
        },
        "expected": {
            "facts": [
                {
                    "fact_id": "f1",
                    "statement": "Orchid 使用 PostgreSQL",
                    "fact_type": "long_term",
                    "memory_state": "active",
                    "recallable": True,
                    "evidence_index": 0,
                    "span_start": 0,
                    "span_end": 21,
                    "entities": [],
                }
            ],
            "recall_queries": [],
        },
    }

    with pytest.raises(DatasetError, match="invalid exact span"):
        validate_atomic_fact_case(case)


def test_snapshot_reader_rejects_oversized_files_before_reading(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_SNAPSHOT_BYTES + 1)

    with pytest.raises(DatasetError, match="snapshot size limit"):
        sha256_file(oversized)


def test_snapshot_reader_only_allows_non_owner_read_only_runtime_hardlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "runtime-metadata"
    linked = tmp_path / "runtime-metadata-link"
    original.write_bytes(b"immutable-runtime-metadata")
    linked.hardlink_to(original)
    original.chmod(0o444)

    with pytest.raises(DatasetError, match="single-link"):
        read_file_snapshot(original)
    with pytest.raises(DatasetError, match="single-link"):
        read_file_snapshot(original, allow_read_only_hardlinks=True)

    monkeypatch.setattr(dataset_module.os, "geteuid", lambda: original.stat().st_uid + 1)
    snapshot = read_file_snapshot(original, allow_read_only_hardlinks=True)

    assert snapshot.payload == b"immutable-runtime-metadata"


def test_atomic_fact_gold_rejects_multiple_user_messages() -> None:
    case = {
        "schema_version": "am-eval-case-v1",
        "case_id": "bad-evidence-order",
        "suite": "atomic_fact",
        "split": "development",
        "input": {
            "evidence_ids": ["e1", "e2"],
            "evidence": ["first", "second"],
            "evidence_types": ["user_message", "user_message"],
            "tool_names": ["", ""],
        },
        "expected": {"facts": [], "no_memory_reason": "invalid", "recall_queries": []},
    }

    with pytest.raises(DatasetError, match="invalid evidence metadata"):
        validate_atomic_fact_case(case)


def test_atomic_fact_gold_rejects_tool_name_on_user_evidence() -> None:
    case = {
        "schema_version": "am-eval-case-v1",
        "case_id": "bad-tool-name",
        "suite": "atomic_fact",
        "split": "development",
        "input": {
            "evidence_ids": ["e1"],
            "evidence": ["first"],
            "evidence_types": ["user_message"],
            "tool_names": ["terminal"],
        },
        "expected": {"facts": [], "no_memory_reason": "invalid", "recall_queries": []},
    }

    with pytest.raises(DatasetError, match="invalid evidence metadata"):
        validate_atomic_fact_case(case)
