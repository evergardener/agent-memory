import json
from datetime import datetime
from pathlib import Path

import pytest

from agent_memory.am_eval_dataset import (
    DatasetError,
    dataset_summary,
    load_dataset,
    load_jsonl,
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
