import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from agent_memory.am_eval_dataset import DatasetError, load_dataset_snapshot
from agent_memory.am_eval_recall import (
    DATASET_ID,
    EXPECTED_MANIFEST_SHA256,
    EXPECTED_NEGATIVE_COUNT,
    EXPECTED_POSITIVE_COUNT,
    EXPECTED_QUERY_COUNT,
    run_recall_cases,
    validate_recall_dataset,
)

ROOT = Path(__file__).parents[1]
MANIFEST_PATH = ROOT / "benchmarks/am-eval-v1/datasets/deterministic-gold-v1/manifest.json"


def _dataset():
    snapshot = load_dataset_snapshot(MANIFEST_PATH)
    cases = tuple(case for case in snapshot.cases if case["suite"] == "recall")
    return snapshot.manifest, snapshot.manifest_sha256, cases


def test_frozen_recall_gold_has_exact_positive_negative_and_split_coverage() -> None:
    manifest, manifest_sha256, _recall_cases = _dataset()
    snapshot = load_dataset_snapshot(MANIFEST_PATH)

    summary = validate_recall_dataset(manifest, snapshot.cases)

    assert manifest_sha256 == EXPECTED_MANIFEST_SHA256
    assert summary["dataset_id"] == DATASET_ID
    assert summary["query_count"] == EXPECTED_QUERY_COUNT == 110
    assert summary["positive_count"] == EXPECTED_POSITIVE_COUNT == 10
    assert summary["negative_count"] == EXPECTED_NEGATIVE_COUNT == 100
    assert summary["split_counts"] == {"development": 85, "validation": 25}


def test_recall_gold_rejects_unpinned_or_retyped_cases() -> None:
    manifest, _manifest_sha256, _recall_cases = _dataset()
    snapshot = load_dataset_snapshot(MANIFEST_PATH)
    unpinned = deepcopy(manifest)
    next(item for item in unpinned["files"] if item["path"] == "recall.jsonl")["sha256"] = (
        "0" * 64
    )
    with pytest.raises(DatasetError, match="official frozen dataset"):
        validate_recall_dataset(unpinned, snapshot.cases)

    changed = list(deepcopy(snapshot.cases))
    recall_index = next(index for index, case in enumerate(changed) if case["suite"] == "recall")
    changed[recall_index]["expected"]["top_k"] = 5
    with pytest.raises(DatasetError, match="positive recall case"):
        validate_recall_dataset(manifest, tuple(changed))


def test_recall_runner_emits_metadata_only_recomputable_ledgers() -> None:
    manifest, _manifest_sha256, cases = _dataset()
    validate_recall_dataset(manifest, load_dataset_snapshot(MANIFEST_PATH).cases)
    namespace = "hermes:automated-tests:recall-unit"
    expected_memory_id = uuid4()
    distractor_id = uuid4()
    positive_queries = {
        case["input"]["query"] for case in cases if case["expected"]["memory_key"] is not None
    }

    def client(request_namespace: str, _case_id: str, query: str):
        if request_namespace != namespace:
            return 403, [], 0.25
        if query in positive_queries:
            return 200, [str(expected_memory_id), str(distractor_id)], 1.0
        return 200, [], 2.0

    result = run_recall_cases(
        cases=cases,
        namespace=namespace,
        expected_memory_id=expected_memory_id,
        recall_client=client,
    )

    assert result["status"] == "PASS"
    assert result["counts"] == {
        "positive_queries": 10,
        "top1_matches": 10,
        "recall_at_5_matches": 10,
        "negative_queries": 100,
        "negative_false_matches": 0,
        "namespace_probes": 6,
        "namespace_unauthorized_recall_items": 0,
        "namespace_denials": 6,
    }
    assert result["latency"]["sample_count"] == 110
    encoded = json.dumps(result, ensure_ascii=False)
    assert "暂停邮件" not in encoded
    assert "PostgreSQL" not in encoded


def test_recall_runner_fails_closed_on_cross_namespace_leak() -> None:
    _manifest, _manifest_sha256, cases = _dataset()
    namespace = "hermes:automated-tests:recall-leak"
    expected_memory_id = uuid4()
    positive_queries = {
        case["input"]["query"] for case in cases if case["expected"]["memory_key"] is not None
    }

    def client(request_namespace: str, _case_id: str, query: str):
        if request_namespace != namespace:
            return 200, [str(expected_memory_id)], 1.0
        if query in positive_queries:
            return 200, [str(expected_memory_id)], 1.0
        return 200, [], 1.0

    result = run_recall_cases(
        cases=cases,
        namespace=namespace,
        expected_memory_id=expected_memory_id,
        recall_client=client,
    )

    assert result["status"] == "FAIL"
    assert result["counts"]["namespace_unauthorized_recall_items"] == 6
    assert result["counts"]["namespace_denials"] == 0


def test_recall_runner_rejects_nonautomated_namespace_before_client_access() -> None:
    _manifest, _manifest_sha256, cases = _dataset()
    with pytest.raises(DatasetError, match="namespace must be automated"):
        run_recall_cases(
            cases=cases,
            namespace="hermes:production",
            expected_memory_id=uuid4(),
            recall_client=lambda *_args: pytest.fail("client should not be called"),
        )
