import hashlib
from copy import deepcopy

import pytest

from agent_memory.am_eval import formal_run_payload_sha256
from agent_memory.am_eval_attestation import (
    EPISODE_PROCEDURE_ATTESTATION_SCHEMA_VERSION,
    EVIDENCE_ATTESTATION_SCHEMA_VERSION,
    LIFECYCLE_ATTESTATION_SCHEMA_VERSION,
    RECALL_ATTESTATION_SCHEMA_VERSION,
    RELIABILITY_ATTESTATION_SCHEMA_VERSION,
)
from agent_memory.am_eval_bundle import ArtifactRecord, assemble_deterministic_run
from agent_memory.am_eval_dataset import DatasetError

IMAGE_REFERENCE = "local.agent-memory.test/agent-memory-api@sha256:" + "9" * 64
RUN_ID = "hermes:automated-tests:am-eval-unified-test"
TRACK = "deterministic-composite"


def _system() -> dict:
    return {
        "environment_sha256": "e" * 64,
        "name": "agent-memory",
        "revision": "a" * 40,
        "source_file_count": 79,
        "source_sha256": "b" * 64,
        "version": "1.0.0rc9",
    }


def _records() -> dict[str, ArtifactRecord]:
    coverage = {
        "episode-procedure": {
            "G04",
            "G08",
            "M04",
            "M09",
            "M10",
            "M11",
            "M12",
            "M13",
            "M14",
        },
        "evidence": {"G01", "G03", "G09"},
        "lifecycle": {"G05", "G06", "M15", "M16", "M17"},
        "recall": {"G02", "M05", "M06", "M08", "M21"},
        "reliability": {"G07", "G10", "M18", "M19", "M20"},
    }
    schemas = {
        "episode-procedure": EPISODE_PROCEDURE_ATTESTATION_SCHEMA_VERSION,
        "evidence": EVIDENCE_ATTESTATION_SCHEMA_VERSION,
        "lifecycle": LIFECYCLE_ATTESTATION_SCHEMA_VERSION,
        "recall": RECALL_ATTESTATION_SCHEMA_VERSION,
        "reliability": RELIABILITY_ATTESTATION_SCHEMA_VERSION,
    }
    records = {}
    for index, kind in enumerate(sorted(coverage), start=1):
        dataset_sha = f"{index:x}" * 64
        artifact = {
            "schema_version": schemas[kind],
            "track": TRACK,
            "image_reference": IMAGE_REFERENCE,
            "image_platform": "linux/arm64",
            "system_environment_sha256": _system()["environment_sha256"],
            "dataset_manifest_sha256": dataset_sha,
            "measurements": {
                item_id: {"sample_count": 1, "value": 1.0}
                for item_id in sorted(coverage[kind])
            },
        }
        source = {
            "run_id": RUN_ID,
            "system": _system(),
            "dataset_id": f"agent-memory-{kind}-gold-v1",
            "dataset_visibility": "open",
            "dataset_blind": False,
        }
        payload = f"artifact-{kind}".encode()
        records[kind] = ArtifactRecord(
            artifact=artifact,
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            source=source,
        )
    return records


def _assemble(records: dict[str, ArtifactRecord]) -> dict:
    return assemble_deterministic_run(
        records,
        benchmark_id="am-eval-v1",
        confirm_run_id=RUN_ID,
        confirm_track=TRACK,
        confirm_image_reference=IMAGE_REFERENCE,
        confirm_image_platform="linux/arm64",
    )


def test_assemble_deterministic_run_covers_every_non_model_measurement() -> None:
    run = _assemble(_records())

    assert run["schema_version"] == "am-eval-run-v3"
    assert run["attestation"]["schema_version"] == "am-eval-run-attestation-v2"
    assert set(run["hard_gates"]) == {f"G{index:02d}" for index in range(1, 11)}
    assert set(run["metrics"]) == {
        "M04",
        "M05",
        "M06",
        "M08",
        *(f"M{index:02d}" for index in range(9, 22)),
    }
    assert [item["id"] for item in run["datasets"]] == sorted(
        item["id"] for item in run["datasets"]
    )
    assert run["attestation"]["run_payload_sha256"] == formal_run_payload_sha256(run)


def test_assemble_deterministic_run_rejects_mixed_runtime_environment() -> None:
    records = _records()
    changed = deepcopy(records["recall"].artifact)
    changed["system_environment_sha256"] = "f" * 64
    records["recall"] = ArtifactRecord(
        artifact=changed,
        payload=records["recall"].payload,
        sha256=records["recall"].sha256,
        source=records["recall"].source,
    )

    with pytest.raises(DatasetError, match="runtime environment"):
        _assemble(records)


def test_assemble_deterministic_run_rejects_missing_artifact_kind() -> None:
    records = _records()
    records.pop("reliability")

    with pytest.raises(DatasetError, match="exactly five"):
        _assemble(records)


def test_assemble_deterministic_run_rejects_duplicate_dataset_manifest() -> None:
    records = _records()
    changed_artifact = deepcopy(records["recall"].artifact)
    changed_artifact["dataset_manifest_sha256"] = records["evidence"].artifact[
        "dataset_manifest_sha256"
    ]
    records["recall"] = ArtifactRecord(
        artifact=changed_artifact,
        payload=records["recall"].payload,
        sha256=records["recall"].sha256,
        source=records["recall"].source,
    )

    with pytest.raises(DatasetError, match="unique dataset manifests"):
        _assemble(records)


def test_assemble_deterministic_run_rejects_confirmation_drift() -> None:
    with pytest.raises(DatasetError, match="confirmation"):
        assemble_deterministic_run(
            _records(),
            benchmark_id="am-eval-v1",
            confirm_run_id=RUN_ID + "-other",
            confirm_track=TRACK,
            confirm_image_reference=IMAGE_REFERENCE,
            confirm_image_platform="linux/arm64",
        )
