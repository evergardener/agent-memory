import base64
import hashlib
import json
from copy import deepcopy

import pytest

import agent_memory.am_eval as am_eval
import agent_memory.am_eval_attestation as am_eval_attestation
from agent_memory.am_eval import evaluate_run, formal_run_payload_sha256
from agent_memory.am_eval_atomic_runner import RuntimeIdentity
from agent_memory.am_eval_attestation import (
    assemble_attestation,
    validate_attested_source,
)
from agent_memory.am_eval_dataset import DatasetError
from agent_memory.am_eval_environment import (
    RUNTIME_DISTRIBUTIONS,
    build_runtime_environment_identity,
)


def _environment() -> dict:
    return build_runtime_environment_identity(
        distribution_versions={name: "1" for name in RUNTIME_DISTRIBUTIONS},
        distribution_content_sha256={name: "2" * 64 for name in RUNTIME_DISTRIBUTIONS},
        distribution_file_counts={name: 1 for name in RUNTIME_DISTRIBUTIONS},
        python_implementation="cpython",
        python_version="3.12.0",
        python_cache_tag="cpython-312",
        platform_tag="linux-test",
    )


def _system() -> dict:
    return {
        "environment_sha256": _environment()["sha256"],
        "name": "agent-memory",
        "revision": "a" * 40,
        "source_file_count": 73,
        "source_sha256": "b" * 64,
        "version": "1.0.0rc9",
    }


def _scorer_identity() -> dict:
    system = _system()
    return {
        "provenance": "image-build-metadata",
        "revision": system["revision"],
        "source_file_count": system["source_file_count"],
        "source_sha256": system["source_sha256"],
        "version": system["version"],
    }


def _quality_result() -> dict:
    return {
        "schema_version": "am-eval-atomic-quality-result-v3",
        "runner_version": "am-eval-atomic-runner-v7",
        "dataset_id": "private-gold-v1",
        "run_id": "formal-run-1",
        "run_status": "complete",
        "case_count": 10,
        "job_statuses": {"done": 10},
        "model_invocations": {
            "budget": 10,
            "attempted": 10,
            "terminal_success": 10,
            "terminal_failure": 0,
        },
        "system": _system(),
        "dataset_manifest_sha256": "c" * 64,
        "execution_plan_sha256": "d" * 64,
        "model": "ocg/qwen3.7-plus",
        "policy_version": "atomic-admission-v3",
        "model_called": True,
        "contains_production_data": False,
        "external_data_sent": True,
        "dataset_visibility": "private",
        "dataset_blind": True,
        "sample_counts": {
            "gold_claims": 10,
            "predictions": 10,
            "matched_claims": 9,
            "exact_spans": 8,
            "recall_queries": 8,
            "correct_citations": 7,
        },
        "metrics": {
            "M01": {"sample_count": 10, "value": 0.9},
            "M02": {"sample_count": 10, "value": 0.9},
            "M03": {"sample_count": 9, "value": 8 / 9},
            "M07": {"sample_count": 8, "value": 7 / 8},
        },
        "missing_metric_ids": [],
        "complete": True,
        "contains_memory_text": False,
        "scorer_runtime_identity": _scorer_identity(),
        "scorer_runtime_environment": _environment(),
        "input_artifact_sha256": "e" * 64,
    }


def _efficiency_result() -> dict:
    return {
        "schema_version": "am-eval-efficiency-result-v5",
        "run_id": "formal-run-1",
        "scope": "isolated",
        "window_start": "2026-08-01T00:00:00+08:00",
        "window_end": "2026-08-02T00:00:00+08:00",
        "system_revision": "a" * 40,
        "system_version": "1.0.0rc9",
        "system_source_file_count": 73,
        "system_source_sha256": "b" * 64,
        "system_environment_sha256": _environment()["sha256"],
        "policy_version": "atomic-admission-v3",
        "metrics": {
            "M22": {"sample_count": 100, "value": 0.1},
            "M23": {"sample_count": 10, "value": 0.0},
        },
        "missing_metric_ids": [],
        "complete": True,
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": True,
        "counts": {
            "auto_admitted_count": 90,
            "manual_review_count": 10,
            "terminal_model_success_count": 10,
            "terminal_model_failure_count": 0,
            "unfinished_model_job_count": 0,
        },
        "run_status": "complete",
        "case_count": 10,
        "job_statuses": {"done": 10},
        "model_called": True,
        "model_invocations": {
            "budget": 10,
            "attempted": 10,
            "terminal_success": 10,
            "terminal_failure": 0,
        },
        "model": "ocg/qwen3.7-plus",
        "execution_plan_sha256": "d" * 64,
        "dataset_id": "private-gold-v1",
        "dataset_manifest_sha256": "c" * 64,
        "dataset_visibility": "private",
        "dataset_blind": True,
        "scorer_runtime_identity": _scorer_identity(),
        "scorer_runtime_environment": _environment(),
        "input_artifact_sha256": "f" * 64,
    }


def _payload(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _assemble(source: dict, *, kind: str) -> dict:
    payload = _payload(source)
    return assemble_attestation(
        payload,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        kind=kind,
        track="recommended-product",
        image_reference="ghcr.io/evergardener/agent-memory-api@sha256:" + "9" * 64,
        image_platform="linux/arm64",
    )


def test_quality_attestation_derives_metrics_from_embedded_scorer_result() -> None:
    artifact = _assemble(_quality_result(), kind="quality")

    source = validate_attested_source(artifact)

    assert artifact["measurement_ids"] == ["M01", "M02", "M03", "M07"]
    assert artifact["measurements"] == source["metrics"]
    assert artifact["source_artifact_schema_version"] == source["schema_version"]
    assert base64.b64decode(artifact["source_artifact_base64"]) == _payload(source)


def test_attestation_rejects_retyped_metrics_and_embedded_source_drift() -> None:
    artifact = _assemble(_quality_result(), kind="quality")
    artifact["measurements"]["M01"]["value"] = 1.0
    with pytest.raises(DatasetError, match="measurements differ"):
        validate_attested_source(artifact)

    artifact = _assemble(_quality_result(), kind="quality")
    source_payload = bytearray(base64.b64decode(artifact["source_artifact_base64"]))
    source_payload[-2] ^= 1
    artifact["source_artifact_base64"] = base64.b64encode(source_payload).decode()
    with pytest.raises(DatasetError, match="SHA-256 mismatch"):
        validate_attested_source(artifact)


def test_quality_result_metrics_must_match_scorer_counts() -> None:
    source = _quality_result()
    source["metrics"]["M01"]["value"] = 1.0
    with pytest.raises(DatasetError, match="differs from scorer counts"):
        _assemble(source, kind="quality")

    source = _quality_result()
    source["job_statuses"] = {"failed": 10}
    source["run_status"] = "failed"
    source["model_invocations"]["terminal_success"] = 0
    source["model_invocations"]["terminal_failure"] = 10
    with pytest.raises(DatasetError, match="complete scoreable model run"):
        _assemble(source, kind="quality")


def test_efficiency_attestation_derives_only_isolated_terminal_failure_rate() -> None:
    artifact = _assemble(_efficiency_result(), kind="efficiency")

    validate_attested_source(artifact)

    assert artifact["measurement_ids"] == ["M23"]
    assert artifact["measurements"] == {"M23": {"sample_count": 10, "value": 0.0}}
    assert artifact["terminal_jobs_complete"] is True


def test_efficiency_result_metric_must_match_bound_counts() -> None:
    source = deepcopy(_efficiency_result())
    source["metrics"]["M23"]["value"] = 0.01

    with pytest.raises(DatasetError, match="M23 differs from scorer counts"):
        _assemble(source, kind="efficiency")


def test_assembler_requires_confirmed_source_and_exact_oci_digest() -> None:
    source = _quality_result()
    payload = _payload(source)
    with pytest.raises(DatasetError, match="confirmation mismatch"):
        assemble_attestation(
            payload,
            source_sha256="0" * 64,
            kind="quality",
            track="recommended-product",
            image_reference="ghcr.io/evergardener/agent-memory-api@sha256:" + "9" * 64,
            image_platform="linux/arm64",
        )
    with pytest.raises(DatasetError, match="exact OCI image reference"):
        assemble_attestation(
            payload,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            kind="quality",
            track="recommended-product",
            image_reference="ghcr.io/evergardener/agent-memory-api:latest",
            image_platform="linux/arm64",
        )


def test_assembler_cli_writes_a_private_sourced_artifact(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "quality-result.json"
    payload = _payload(_quality_result())
    source.write_bytes(payload)
    output = tmp_path / "quality-attestation.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "agent-memory-assemble-eval-attestation",
            "quality",
            str(source),
            "--confirm-source-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--track",
            "recommended-product",
            "--image-reference",
            "ghcr.io/evergardener/agent-memory-api@sha256:" + "9" * 64,
            "--image-platform",
            "linux/arm64",
            "--output",
            str(output),
        ],
    )

    am_eval_attestation.main()

    artifact = json.loads(output.read_text())
    validate_attested_source(artifact)
    assert output.stat().st_mode & 0o777 == 0o600


def test_formal_run_accepts_sourced_isolated_m23_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _assemble(_efficiency_result(), kind="efficiency")
    artifact_payload = _payload(artifact)
    system = _system()
    image_name = "ghcr.io/evergardener/agent-memory-api"
    manifest_digest = "sha256:" + "9" * 64
    image_reference = f"{image_name}@{manifest_digest}"
    run = {
        "schema_version": "am-eval-run-v2",
        "benchmark_id": "am-eval-m23-test",
        "run_id": "formal-run-1",
        "system": system,
        "track": "recommended-product",
        "dataset": {
            "id": "private-gold-v1",
            "sha256": "c" * 64,
            "visibility": "private",
            "blind": True,
        },
        "execution_artifact": {
            "type": "oci-image",
            "image_name": image_name,
            "manifest_digest": manifest_digest,
            "platform": "linux/arm64",
        },
        "hard_gates": {},
        "metrics": {
            "M23": {
                "value": 0.0,
                "sample_count": 10,
                "evidence": ["efficiency"],
            }
        },
        "attestation": {
            "schema_version": "am-eval-run-attestation-v1",
            "claim": "OFFICIAL_AM_EVAL_RUN",
            "system_environment_sha256": system["environment_sha256"],
            "system_revision": system["revision"],
            "system_source_sha256": system["source_sha256"],
            "dataset_manifest_sha256": "c" * 64,
            "track": "recommended-product",
            "image_reference": image_reference,
            "image_platform": "linux/arm64",
            "artifacts": {"efficiency": {"sha256": hashlib.sha256(artifact_payload).hexdigest()}},
        },
    }
    run["attestation"]["run_payload_sha256"] = formal_run_payload_sha256(run)
    spec = {
        "benchmark_id": "am-eval-m23-test",
        "hard_gates": [],
        "metrics": [
            {
                "id": "M23",
                "name": "terminal failure rate",
                "dimension": "efficiency",
                "weight": 100,
                "required": True,
                "minimum": 0,
                "maximum": 1,
                "scoring": {"mode": "lower", "target": 0.01, "zero_score_at": 0.1},
            }
        ],
        "release_policy": {"minimum_score": 85},
    }
    monkeypatch.setattr(
        am_eval,
        "resolve_runtime_identity",
        lambda: RuntimeIdentity(
            revision=system["revision"],
            version=system["version"],
            source_sha256=system["source_sha256"],
            source_file_count=system["source_file_count"],
            provenance="image-build-metadata",
            source_root=am_eval.Path("/private/tmp/am-eval-test"),
        ),
    )
    monkeypatch.setattr(am_eval, "runtime_environment_identity", _environment)

    result = evaluate_run(
        spec,
        run,
        artifact_payloads={"efficiency": artifact_payload},
        confirm_image_reference=image_reference,
        confirm_image_platform="linux/arm64",
    )

    assert result["decision"] == "PASS"
    assert result["attestation"]["status"] == "verified"
