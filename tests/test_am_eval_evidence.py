from copy import deepcopy
from pathlib import Path

import pytest

from agent_memory.am_eval_dataset import DatasetError, load_dataset_snapshot
from agent_memory.am_eval_evidence import (
    DATASET_ID,
    EXPECTED_CASE_COUNT,
    EXPECTED_MANIFEST_SHA256,
    run_evidence_cases,
    validate_evidence_dataset,
)
from agent_memory.redaction import redact_text

ROOT = Path(__file__).parents[1]
MANIFEST_PATH = ROOT / "benchmarks/am-eval-v1/datasets/evidence-integrity-gold-v1/manifest.json"


def test_frozen_evidence_gold_has_exact_probe_and_split_coverage() -> None:
    snapshot = load_dataset_snapshot(MANIFEST_PATH)

    summary = validate_evidence_dataset(snapshot.manifest, snapshot.cases)

    assert snapshot.manifest_sha256 == EXPECTED_MANIFEST_SHA256
    assert summary["dataset_id"] == DATASET_ID
    assert summary["case_count"] == EXPECTED_CASE_COUNT == 9
    assert summary["persisted_surface_count"] == 27
    assert summary["split_counts"] == {"development": 5, "validation": 4}


def test_evidence_gold_rejects_unpinned_or_retyped_cases() -> None:
    snapshot = load_dataset_snapshot(MANIFEST_PATH)
    unpinned = deepcopy(snapshot.manifest)
    unpinned["files"][0]["sha256"] = "0" * 64
    with pytest.raises(DatasetError, match="official frozen dataset"):
        validate_evidence_dataset(unpinned, snapshot.cases)

    changed = list(deepcopy(snapshot.cases))
    changed[0]["case_id"] = "evidence-redaction-999"
    with pytest.raises(DatasetError, match="case IDs"):
        validate_evidence_dataset(snapshot.manifest, tuple(changed))


def test_evidence_gold_matches_current_redaction_rules_and_is_a_fixed_point() -> None:
    snapshot = load_dataset_snapshot(MANIFEST_PATH)
    for case in snapshot.cases:
        result = redact_text(case["input"]["text"])
        assert [finding.kind for finding in result.findings] == case["expected"]["finding_kinds"]
        assert all(fragment not in result.text for fragment in case["input"]["forbidden_fragments"])
        assert redact_text(result.text).findings == ()


def test_evidence_case_loader_rejects_unbounded_or_missing_probes() -> None:
    snapshot = load_dataset_snapshot(MANIFEST_PATH)
    case = deepcopy(snapshot.cases[0])
    case["input"]["forbidden_fragments"] = ["not-in-text"]

    from agent_memory.am_eval_dataset import validate_evidence_integrity_case

    with pytest.raises(DatasetError, match="invalid probes"):
        validate_evidence_integrity_case(case)


def test_evidence_runner_rejects_nonautomated_namespace_before_database_access() -> None:
    snapshot = load_dataset_snapshot(MANIFEST_PATH)
    with pytest.raises(DatasetError, match="namespace must be automated"):
        run_evidence_cases(
            None,
            cases=snapshot.cases,
            namespace="hermes:production",
        )
