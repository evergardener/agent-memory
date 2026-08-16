import json
from copy import deepcopy
from pathlib import Path

import pytest

from agent_memory.am_eval_dataset import DatasetError, load_dataset, sha256_file
from agent_memory.am_eval_quality import (
    evaluate_atomic_quality,
    load_atomic_output,
    validate_execution_plan_confirmation,
)

MANIFEST = (
    Path(__file__).parents[1]
    / "benchmarks/am-eval-v1/datasets/atomic-quality-selftest-v1/manifest.json"
)
CASES = load_dataset(MANIFEST)


def _oracle_output() -> dict:
    output_cases = []
    for case in CASES:
        facts = []
        fact_to_prediction = {}
        for index, fact in enumerate(case["expected"]["facts"], start=1):
            prediction_id = f"prediction-{index}"
            fact_to_prediction[fact["fact_id"]] = prediction_id
            facts.append(
                {
                    "prediction_id": prediction_id,
                    "statement": fact["statement"],
                    "fact_type": fact["fact_type"],
                    "memory_state": fact["memory_state"],
                    "recallable": fact["recallable"],
                    "evidence_index": fact["evidence_index"],
                    "span_start": fact["span_start"],
                    "span_end": fact["span_end"],
                    "source_ids": [case["input"]["evidence_ids"][fact["evidence_index"]]],
                }
            )
        recalls = [
            {
                "query_id": query["query_id"],
                "prediction_id": fact_to_prediction[query["expected_fact_ids"][0]],
                "source_ids": [
                    case["input"]["evidence_ids"][
                        next(
                            fact["evidence_index"]
                            for fact in case["expected"]["facts"]
                            if fact["fact_id"] == query["expected_fact_ids"][0]
                        )
                    ]
                ],
            }
            for query in case["expected"].get("recall_queries", [])
        ]
        output_cases.append({"case_id": case["case_id"], "facts": facts, "recalls": recalls})
    return {
        "schema_version": "am-eval-atomic-output-v4",
        "runner_version": "am-eval-atomic-runner-v7",
        "dataset_id": "agent-memory-atomic-quality-selftest-v1",
        "run_id": "oracle-selftest",
        "run_status": "complete",
        "case_count": len(CASES),
        "job_statuses": {"done": len(CASES)},
        "model_invocations": {
            "budget": len(CASES),
            "attempted": len(CASES),
            "terminal_success": len(CASES),
            "terminal_failure": 0,
        },
        "dataset_manifest_sha256": sha256_file(MANIFEST),
        "execution_plan_sha256": "a" * 64,
        "system": {
            "environment_sha256": "b" * 64,
            "name": "fixture-oracle",
            "version": "1",
            "revision": "test",
            "source_file_count": 7,
            "source_sha256": "a" * 64,
        },
        "model": "fixture-oracle",
        "policy_version": "fixture-policy-v1",
        "contains_memory_text": True,
        "contains_production_data": False,
        "external_data_sent": False,
        "dataset_visibility": "open",
        "model_called": True,
        "cases": output_cases,
    }


def test_atomic_quality_dataset_has_positive_and_negative_cases() -> None:
    assert len(CASES) == 24
    assert sum(bool(case["expected"]["facts"]) for case in CASES) == 20
    assert sum(not case["expected"]["facts"] for case in CASES) == 4
    assert {case["split"] for case in CASES} == {"development", "validation"}


def test_oracle_proves_metric_arithmetic_without_claiming_model_quality() -> None:
    result = evaluate_atomic_quality(CASES, _oracle_output())

    assert result["system"]["name"] == "fixture-oracle"
    assert result["system"]["source_sha256"] == "a" * 64
    assert result["system"]["source_file_count"] == 7
    assert result["model"] == "fixture-oracle"
    assert result["policy_version"] == "fixture-policy-v1"
    assert result["sample_counts"] == {
        "gold_claims": 24,
        "predictions": 24,
        "matched_claims": 24,
        "exact_spans": 24,
        "recall_queries": 21,
        "correct_citations": 21,
    }
    assert {key: item["value"] for key, item in result["metrics"].items()} == {
        "M01": 1.0,
        "M02": 1.0,
        "M03": 1.0,
        "M07": 1.0,
    }
    assert result["complete"] is True
    assert result["contains_memory_text"] is False
    assert result["model_called"] is True
    assert result["run_status"] == "complete"
    assert result["job_statuses"] == {"done": len(CASES)}
    assert result["contains_production_data"] is False
    assert result["external_data_sent"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert "Evergarden" not in serialized
    assert "PostgreSQL" not in serialized


def test_false_positive_missing_claim_wrong_span_and_bad_citation_are_counted() -> None:
    output = _oracle_output()
    first = output["cases"][0]
    first["facts"].append(
        {
            "prediction_id": "unsupported",
            "statement": "周末常去",
            "fact_type": "long_term",
            "memory_state": "active",
            "recallable": True,
            "evidence_index": 0,
            "span_start": 10,
            "span_end": 14,
            "source_ids": ["e1"],
        }
    )
    output["cases"][1]["facts"] = []
    output["cases"][1]["recalls"] = []
    output["cases"][2]["facts"][0]["span_start"] = 1
    output["cases"][3]["recalls"][0]["source_ids"] = []

    result = evaluate_atomic_quality(CASES, output)

    assert result["metrics"]["M01"]["value"] == pytest.approx(23 / 24)
    assert result["metrics"]["M02"]["value"] == pytest.approx(23 / 24)
    assert result["metrics"]["M03"]["value"] == pytest.approx(22 / 23)
    assert result["metrics"]["M07"]["value"] == pytest.approx(19 / 21)


def test_atomic_output_loader_requires_exact_case_coverage(tmp_path: Path) -> None:
    output = _oracle_output()
    output["cases"].pop()
    path = tmp_path / "output.json"
    path.write_text(json.dumps(output), encoding="utf-8")

    with pytest.raises(DatasetError, match="missing dataset cases"):
        load_atomic_output(path, case_ids={case["case_id"] for case in CASES})


def test_atomic_output_loader_binds_the_confirmed_input_sha(tmp_path: Path) -> None:
    output = _oracle_output()
    path = tmp_path / "output.json"
    path.write_text(json.dumps(output), encoding="utf-8")

    with pytest.raises(DatasetError, match="output SHA-256 confirmation mismatch"):
        load_atomic_output(
            path,
            case_ids={case["case_id"] for case in CASES},
            confirm_sha256="f" * 64,
        )


def test_atomic_output_loader_rejects_failed_or_incomplete_runs(tmp_path: Path) -> None:
    output = _oracle_output()
    output["run_status"] = "failed"
    output["job_statuses"] = {"done": len(CASES) - 1, "failed": 1}
    output["model_invocations"]["terminal_success"] = len(CASES) - 1
    output["model_invocations"]["terminal_failure"] = 1
    path = tmp_path / "failed-output.json"
    path.write_text(json.dumps(output), encoding="utf-8")

    with pytest.raises(DatasetError, match="requires a complete model run"):
        load_atomic_output(path, case_ids={case["case_id"] for case in CASES})

    output = _oracle_output()
    output["model_invocations"]["attempted"] -= 1
    path = tmp_path / "incomplete-output.json"
    path.write_text(json.dumps(output), encoding="utf-8")

    with pytest.raises(DatasetError, match="invocation ledger differs"):
        load_atomic_output(path, case_ids={case["case_id"] for case in CASES})


def test_atomic_output_loader_rejects_unknown_schema_fields(tmp_path: Path) -> None:
    output = _oracle_output()
    output["unbound_status"] = "complete"
    path = tmp_path / "smuggled-output.json"
    path.write_text(json.dumps(output), encoding="utf-8")

    with pytest.raises(DatasetError, match="unsupported atomic evaluation output schema"):
        load_atomic_output(path, case_ids={case["case_id"] for case in CASES})


def test_atomic_output_loader_requires_dataset_sha(tmp_path: Path) -> None:
    output = _oracle_output()
    output["dataset_manifest_sha256"] = "not-a-sha"
    path = tmp_path / "output.json"
    path.write_text(json.dumps(output), encoding="utf-8")

    with pytest.raises(DatasetError, match="manifest SHA-256"):
        load_atomic_output(path, case_ids={case["case_id"] for case in CASES})


def test_atomic_output_loader_requires_execution_plan_sha(tmp_path: Path) -> None:
    output = _oracle_output()
    output["execution_plan_sha256"] = "not-a-sha"
    path = tmp_path / "output.json"
    path.write_text(json.dumps(output), encoding="utf-8")

    with pytest.raises(DatasetError, match="execution plan SHA-256"):
        load_atomic_output(path, case_ids={case["case_id"] for case in CASES})


def test_atomic_output_loader_requires_runtime_source_sha(tmp_path: Path) -> None:
    output = _oracle_output()
    output["system"]["source_sha256"] = "not-a-sha"
    path = tmp_path / "output.json"
    path.write_text(json.dumps(output), encoding="utf-8")

    with pytest.raises(DatasetError, match="system metadata"):
        load_atomic_output(path, case_ids={case["case_id"] for case in CASES})


def test_quality_scoring_requires_the_exact_execution_plan_sha() -> None:
    output = _oracle_output()
    validate_execution_plan_confirmation(
        output,
        confirm_plan_sha256="a" * 64,
        confirm_source_sha256="a" * 64,
    )

    with pytest.raises(DatasetError, match="confirmation mismatch"):
        validate_execution_plan_confirmation(
            output,
            confirm_plan_sha256="b" * 64,
            confirm_source_sha256="a" * 64,
        )

    with pytest.raises(DatasetError, match="runtime source SHA-256"):
        validate_execution_plan_confirmation(
            output,
            confirm_plan_sha256="a" * 64,
            confirm_source_sha256="b" * 64,
        )


def test_atomic_quality_rejects_unknown_recall_query() -> None:
    output = deepcopy(_oracle_output())
    output["cases"][0]["recalls"][0]["query_id"] = "unknown"

    with pytest.raises(DatasetError, match="unknown recall queries"):
        evaluate_atomic_quality(CASES, output)


def test_recalled_unscored_memory_counts_as_wrong_citation_not_invalid_output() -> None:
    output = _oracle_output()
    output["cases"][0]["recalls"][0]["prediction_id"] = "unrelated-memory-id"

    result = evaluate_atomic_quality(CASES, output)

    assert result["metrics"]["M07"]["value"] == pytest.approx(20 / 21)


def test_wrong_lifecycle_or_fact_type_is_not_a_correct_claim() -> None:
    output = _oracle_output()
    output["cases"][0]["facts"][0]["memory_state"] = "candidate"
    output["cases"][1]["facts"][0]["fact_type"] = "stage"

    result = evaluate_atomic_quality(CASES, output)

    assert result["sample_counts"]["matched_claims"] == 22
    assert result["metrics"]["M01"]["value"] == pytest.approx(22 / 24)
    assert result["metrics"]["M02"]["value"] == pytest.approx(22 / 24)


def test_zero_prediction_denominators_remain_unmeasured() -> None:
    output = _oracle_output()
    for case in output["cases"]:
        case["facts"] = []
        case["recalls"] = []

    result = evaluate_atomic_quality(CASES, output)

    assert "M01" not in result["metrics"]
    assert result["metrics"]["M02"]["value"] == 0
    assert "M03" not in result["metrics"]
    assert result["metrics"]["M07"]["value"] == 0
    assert result["missing_metric_ids"] == ["M01", "M03"]
    assert result["complete"] is False
