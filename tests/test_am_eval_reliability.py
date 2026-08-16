from pathlib import Path

import pytest

from agent_memory.am_eval_dataset import DatasetError, load_dataset_snapshot
from agent_memory.am_eval_reliability import (
    EXPECTED_MANIFEST_SHA256,
    _private_file_snapshot,
    validate_reliability_dataset,
)

MANIFEST = (
    Path(__file__).parents[1]
    / "benchmarks/am-eval-v1/datasets/reliability-gold-v1/manifest.json"
)


def test_reliability_gold_is_frozen_and_complete() -> None:
    snapshot = load_dataset_snapshot(MANIFEST)

    assert snapshot.manifest_sha256 == EXPECTED_MANIFEST_SHA256
    assert validate_reliability_dataset(snapshot.manifest, snapshot.cases) == {
        "schema_version": "am-eval-reliability-validation-v1",
        "dataset_id": "agent-memory-reliability-gold-v1",
        "case_count": 5,
        "suite_counts": {"backup_restore": 1, "idempotency": 2, "worker_recovery": 2},
        "split_counts": {"development": 2, "validation": 3},
        "status": "PASS",
    }


def test_reliability_gold_rejects_case_contract_retyping() -> None:
    snapshot = load_dataset_snapshot(MANIFEST)
    cases = tuple(dict(case) for case in snapshot.cases)
    cases[0]["case_id"] = "worker-outage-forged"

    with pytest.raises(DatasetError, match="case IDs"):
        validate_reliability_dataset(snapshot.manifest, cases)


def test_reliability_gold_rejects_production_or_memory_claims() -> None:
    snapshot = load_dataset_snapshot(MANIFEST)
    manifest = {**snapshot.manifest, "contains_production_data": True}

    with pytest.raises(DatasetError, match="synthetic"):
        validate_reliability_dataset(manifest, snapshot.cases)


def test_private_reliability_artifact_rejects_symlink_parent(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    artifact = private / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    artifact.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)

    with pytest.raises(DatasetError, match="cannot use a symlink"):
        _private_file_snapshot(alias / "artifact.json", label="test artifact")
