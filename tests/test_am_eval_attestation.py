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
    evidence_measurements,
    lifecycle_measurements,
    recall_measurements,
    validate_attested_source,
)
from agent_memory.am_eval_dataset import DatasetError
from agent_memory.am_eval_environment import (
    RUNTIME_DISTRIBUTIONS,
    build_runtime_environment_identity,
)
from agent_memory.am_eval_evidence import EXPECTED_MANIFEST_SHA256 as EVIDENCE_MANIFEST_SHA256
from agent_memory.am_eval_lifecycle import (
    EXPECTED_MANIFEST_SHA256,
    REQUIRED_ACTION_COUNTS,
    REQUIRED_INVARIANT_COUNTS,
)
from agent_memory.am_eval_recall import EXPECTED_MANIFEST_SHA256 as RECALL_MANIFEST_SHA256


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


def _lifecycle_result() -> dict:
    cases = []
    index = 0
    for action, count in REQUIRED_ACTION_COUNTS.items():
        for _offset in range(count):
            index += 1
            cases.append(
                {
                    "case_id": f"lifecycle-{index:03d}",
                    "action": action,
                    "status": "PASS",
                    "error_code": None,
                }
            )
    return {
        "schema_version": "am-eval-lifecycle-run-v2",
        "run_id": "formal-run-1",
        "case_count": 20,
        "passed": 20,
        "failed": 0,
        "action_counts": dict(REQUIRED_ACTION_COUNTS),
        "invariant_pass_counts": dict(REQUIRED_INVARIANT_COUNTS),
        "cases": cases,
        "status": "PASS",
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
        "dataset_id": "agent-memory-lifecycle-gold-v1",
        "manifest_sha256": EXPECTED_MANIFEST_SHA256,
        "dataset_visibility": "open",
        "dataset_blind": False,
        "dataset_validation": "PASS",
        "model_called": False,
        "system": _system(),
        "runner_runtime_identity": _scorer_identity(),
        "runner_runtime_environment": _environment(),
    }


def _recall_result() -> dict:
    expected_memory_id = "00000000-0000-4000-8000-000000000001"
    query_ledger = [
        {
            "case_id": f"recall-pos-{index:03d}",
            "kind": "positive",
            "returned_memory_ids": [expected_memory_id],
            "top1_match": True,
            "recall_at_5_match": True,
            "false_match": None,
            "latency_ms": 1.0,
        }
        for index in range(1, 11)
    ]
    for category, count in (("uuid", 25), ("hash", 25), ("text", 50)):
        query_ledger.extend(
            {
                "case_id": f"recall-neg-{category}-{index:03d}",
                "kind": "negative",
                "returned_memory_ids": [],
                "top1_match": None,
                "recall_at_5_match": None,
                "false_match": False,
                "latency_ms": 1.0,
            }
            for index in range(1, count + 1)
        )
    namespace_ledger = [
        {
            "case_id": f"recall-pos-{index:03d}",
            "status_code": 403,
            "returned_memory_ids": [],
            "denied": True,
        }
        for index in range(1, 7)
    ]
    return {
        "schema_version": "am-eval-recall-run-v1",
        "run_id": "hermes:automated-tests:recall-formal",
        "dataset_id": "agent-memory-deterministic-gold-v1",
        "manifest_sha256": RECALL_MANIFEST_SHA256,
        "dataset_visibility": "open",
        "dataset_blind": False,
        "dataset_contains_memory_text": True,
        "dataset_validation": "PASS",
        "expected_memory_id": expected_memory_id,
        "query_count": 110,
        "query_ledger": query_ledger,
        "namespace_ledger": namespace_ledger,
        "counts": {
            "positive_queries": 10,
            "top1_matches": 10,
            "recall_at_5_matches": 10,
            "negative_queries": 100,
            "negative_false_matches": 0,
            "namespace_probes": 6,
            "namespace_unauthorized_recall_items": 0,
            "namespace_denials": 6,
        },
        "latency": {
            "boundary": "loopback-http-api",
            "sample_count": 110,
            "p95_ms": 1.0,
            "quantile_method": "statistics.quantiles-inclusive-n100-index94",
        },
        "status": "PASS",
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
        "model_called": False,
        "system": _system(),
        "runner_runtime_identity": _scorer_identity(),
        "runner_runtime_environment": _environment(),
    }


def _evidence_result() -> dict:
    expected_findings = (
        {"credential_assignment": 1, "cn_id": 1},
        {"credential_assignment": 1},
        {"provider_api_key": 1},
        {"provider_api_key": 1},
        {"aws_access_key": 1},
        {"private_key": 1},
        {"cn_id": 1},
        {"credential_assignment": 2, "cn_id": 1},
        {},
    )
    cases = [
        {
            "case_id": f"evidence-redaction-{index:03d}",
            "finding_counts": dict(finding_counts),
            "expected_finding_counts": dict(finding_counts),
            "persisted_surface_count": 3,
            "remaining_sensitive_finding_count": 0,
            "forbidden_fragment_occurrences": 0,
            "sensitive_leak_surface_count": 0,
            "active_fact_has_evidence": True,
            "trace_complete": True,
        }
        for index, finding_counts in enumerate(expected_findings, start=1)
    ]
    return {
        "schema_version": "am-eval-evidence-integrity-run-v1",
        "run_id": "hermes:automated-tests:evidence-formal",
        "dataset_id": "agent-memory-evidence-integrity-gold-v1",
        "manifest_sha256": EVIDENCE_MANIFEST_SHA256,
        "dataset_visibility": "open",
        "dataset_blind": False,
        "dataset_contains_memory_text": True,
        "dataset_validation": "PASS",
        "case_count": 9,
        "cases": cases,
        "counts": {
            "cases": 9,
            "persisted_surfaces": 27,
            "sensitive_leak_surfaces": 0,
            "active_facts": 9,
            "active_facts_without_evidence": 0,
            "traceable_facts": 9,
            "trace_failures": 0,
        },
        "quality_report_snapshot": {
            "evidence_traceability": True,
            "raw_sensitive_fact_leakage": True,
            "facts": 9,
            "traceable_facts": 9,
            "raw_sensitive_facts": 0,
        },
        "status": "PASS",
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
        "model_called": False,
        "system": _system(),
        "runner_runtime_identity": _scorer_identity(),
        "runner_runtime_environment": _environment(),
    }


def _payload(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _assemble(source: dict, *, kind: str, track: str = "recommended-product") -> dict:
    payload = _payload(source)
    return assemble_attestation(
        payload,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        kind=kind,
        track=track,
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


def test_lifecycle_attestation_derives_frozen_gates_and_governance_metrics() -> None:
    source = _lifecycle_result()
    artifact = _assemble(source, kind="lifecycle", track="deterministic-lifecycle")

    validate_attested_source(artifact)

    assert artifact["measurement_ids"] == ["G05", "G06", "M15", "M16", "M17"]
    assert artifact["measurements"] == lifecycle_measurements(source)
    assert artifact["measurements"]["G05"] == {"sample_count": 3, "value": 0}
    assert artifact["measurements"]["M15"] == {"sample_count": 20, "value": 1.0}


def test_lifecycle_attestation_rejects_invariant_counter_retyping() -> None:
    source = _lifecycle_result()
    source["invariant_pass_counts"]["namespace_denied"] = 5

    with pytest.raises(DatasetError, match="complete frozen lifecycle run"):
        _assemble(source, kind="lifecycle")


def test_recall_attestation_derives_only_from_query_ledgers() -> None:
    source = _recall_result()
    artifact = _assemble(source, kind="recall", track="deterministic-recall")

    validate_attested_source(artifact)

    assert artifact["measurement_ids"] == ["G02", "M05", "M06", "M08", "M21"]
    assert artifact["measurements"] == recall_measurements(source)
    assert artifact["measurements"]["G02"] == {"sample_count": 6, "value": 0}
    assert artifact["measurements"]["M21"] == {"sample_count": 110, "value": 1.0}


def test_recall_attestation_rejects_count_rank_and_latency_retyping() -> None:
    source = _recall_result()
    source["counts"]["top1_matches"] = 9
    with pytest.raises(DatasetError, match="counts differ"):
        _assemble(source, kind="recall")

    source = _recall_result()
    source["query_ledger"][0]["top1_match"] = False
    with pytest.raises(DatasetError, match="positive query ledger"):
        _assemble(source, kind="recall")

    source = _recall_result()
    source["latency"]["p95_ms"] = 0.5
    with pytest.raises(DatasetError, match="latency summary differs"):
        _assemble(source, kind="recall")


def test_formal_artifact_validator_accepts_sourced_recall_measurements() -> None:
    source = _recall_result()
    artifact = _assemble(source, kind="recall", track="deterministic-recall")
    payload = _payload(artifact)
    measurements = recall_measurements(source)
    run = {
        "run_id": source["run_id"],
        "track": "deterministic-recall",
        "system": _system(),
        "dataset": {
            "id": source["dataset_id"],
            "sha256": RECALL_MANIFEST_SHA256,
            "visibility": "open",
            "blind": False,
        },
        "execution_artifact": {
            "image_name": "ghcr.io/evergardener/agent-memory-api",
            "manifest_digest": "sha256:" + "9" * 64,
            "platform": "linux/arm64",
        },
    }

    covered = am_eval._validate_artifact(
        "recall",
        {"sha256": hashlib.sha256(payload).hexdigest()},
        payload,
        run=run,
        supplied_measurements=measurements,
    )

    assert covered == {"G02", "M05", "M06", "M08", "M21"}


def test_evidence_attestation_derives_persistence_and_trace_gates() -> None:
    source = _evidence_result()
    artifact = _assemble(source, kind="evidence", track="deterministic-evidence")

    validate_attested_source(artifact)

    assert artifact["measurement_ids"] == ["G01", "G03", "G09"]
    assert artifact["measurements"] == evidence_measurements(source)
    assert artifact["measurements"]["G01"] == {"sample_count": 27, "value": 0}
    assert artifact["measurements"]["G09"] == {"sample_count": 9, "value": 1.0}


def test_evidence_attestation_rejects_leak_trace_and_summary_retyping() -> None:
    source = _evidence_result()
    source["cases"][0]["remaining_sensitive_finding_count"] = 1
    with pytest.raises(DatasetError, match="did not pass persistence"):
        _assemble(source, kind="evidence")

    source = _evidence_result()
    source["cases"][0]["trace_complete"] = False
    with pytest.raises(DatasetError, match="did not pass persistence"):
        _assemble(source, kind="evidence")

    source = _evidence_result()
    source["counts"]["active_facts_without_evidence"] = 1
    with pytest.raises(DatasetError, match="counts differ"):
        _assemble(source, kind="evidence")


def test_formal_artifact_validator_accepts_sourced_evidence_measurements() -> None:
    source = _evidence_result()
    artifact = _assemble(source, kind="evidence", track="deterministic-evidence")
    payload = _payload(artifact)
    measurements = evidence_measurements(source)
    run = {
        "run_id": source["run_id"],
        "track": "deterministic-evidence",
        "system": _system(),
        "dataset": {
            "id": source["dataset_id"],
            "sha256": EVIDENCE_MANIFEST_SHA256,
            "visibility": "open",
            "blind": False,
        },
        "execution_artifact": {
            "image_name": "ghcr.io/evergardener/agent-memory-api",
            "manifest_digest": "sha256:" + "9" * 64,
            "platform": "linux/arm64",
        },
    }

    covered = am_eval._validate_artifact(
        "evidence",
        {"sha256": hashlib.sha256(payload).hexdigest()},
        payload,
        run=run,
        supplied_measurements=measurements,
    )

    assert covered == {"G01", "G03", "G09"}


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


def test_formal_run_accepts_sourced_lifecycle_measurements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _lifecycle_result()
    artifact = _assemble(source, kind="lifecycle", track="deterministic-lifecycle")
    artifact_payload = _payload(artifact)
    system = _system()
    image_name = "ghcr.io/evergardener/agent-memory-api"
    manifest_digest = "sha256:" + "9" * 64
    image_reference = f"{image_name}@{manifest_digest}"
    measurements = lifecycle_measurements(source)
    run = {
        "schema_version": "am-eval-run-v2",
        "benchmark_id": "am-eval-lifecycle-test",
        "run_id": "formal-run-1",
        "system": system,
        "track": "deterministic-lifecycle",
        "dataset": {
            "id": "agent-memory-lifecycle-gold-v1",
            "sha256": EXPECTED_MANIFEST_SHA256,
            "visibility": "open",
            "blind": False,
        },
        "execution_artifact": {
            "type": "oci-image",
            "image_name": image_name,
            "manifest_digest": manifest_digest,
            "platform": "linux/arm64",
        },
        "hard_gates": {
            item_id: {**measurements[item_id], "evidence": ["lifecycle"]}
            for item_id in ("G05", "G06")
        },
        "metrics": {
            item_id: {**measurements[item_id], "evidence": ["lifecycle"]}
            for item_id in ("M15", "M16", "M17")
        },
        "attestation": {
            "schema_version": "am-eval-run-attestation-v1",
            "claim": "OFFICIAL_AM_EVAL_RUN",
            "system_environment_sha256": system["environment_sha256"],
            "system_revision": system["revision"],
            "system_source_sha256": system["source_sha256"],
            "dataset_manifest_sha256": EXPECTED_MANIFEST_SHA256,
            "track": "deterministic-lifecycle",
            "image_reference": image_reference,
            "image_platform": "linux/arm64",
            "artifacts": {"lifecycle": {"sha256": hashlib.sha256(artifact_payload).hexdigest()}},
        },
    }
    run["attestation"]["run_payload_sha256"] = formal_run_payload_sha256(run)
    spec = {
        "benchmark_id": "am-eval-lifecycle-test",
        "hard_gates": [
            {
                "id": item_id,
                "name": item_id,
                "operator": "eq",
                "threshold": 0,
                "required": True,
            }
            for item_id in ("G05", "G06")
        ],
        "metrics": [
            {
                "id": item_id,
                "name": item_id,
                "dimension": "lifecycle",
                "weight": weight,
                "required": True,
                "minimum": 0,
                "maximum": 1,
                "scoring": {"mode": "higher", "target": 1.0},
            }
            for item_id, weight in (("M15", 34), ("M16", 33), ("M17", 33))
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
        artifact_payloads={"lifecycle": artifact_payload},
        confirm_image_reference=image_reference,
        confirm_image_platform="linux/arm64",
    )

    assert result["decision"] == "PASS"
    assert result["hard_gate_summary"]["passed"] == 2
