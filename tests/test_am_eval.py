import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest

import agent_memory.am_eval as am_eval
from agent_memory.am_eval import (
    ATTESTATION_SCHEMA_VERSION,
    RUN_SCHEMA_VERSION,
    evaluate_run,
    formal_run_payload_sha256,
    render_markdown,
)
from agent_memory.am_eval_atomic_runner import RuntimeIdentity
from agent_memory.am_eval_attestation import assemble_attestation
from agent_memory.am_eval_environment import (
    RUNTIME_DISTRIBUTIONS,
    build_runtime_environment_identity,
)


def _environment() -> dict:
    return build_runtime_environment_identity(
        distribution_versions={name: "1" for name in RUNTIME_DISTRIBUTIONS},
        distribution_content_sha256={name: "1" * 64 for name in RUNTIME_DISTRIBUTIONS},
        distribution_file_counts={name: 1 for name in RUNTIME_DISTRIBUTIONS},
        python_implementation="cpython",
        python_version="3.12.0",
        python_cache_tag="cpython-312",
        platform_tag="macosx-test",
    )


def _spec() -> dict:
    return {
        "benchmark_id": "am-eval-test",
        "hard_gates": [],
        "metrics": [
            {
                "id": "M01",
                "name": "accuracy",
                "dimension": "quality",
                "weight": 60,
                "required": True,
                "scoring": {"mode": "higher", "target": 0.9},
            },
            {
                "id": "M02",
                "name": "false matches",
                "dimension": "quality",
                "weight": 40,
                "required": True,
                "scoring": {"mode": "lower", "target": 0.01, "zero_score_at": 0.1},
            },
        ],
        "release_policy": {"minimum_score": 85},
    }


def _run() -> dict:
    revision = "a" * 40
    source_sha256 = "b" * 64
    environment_sha256 = _environment()["sha256"]
    dataset_sha256 = "c" * 64
    track = "recommended-product"
    dataset_visibility = "private"
    blind = True
    image_name = "ghcr.io/evergardener/agent-memory-api"
    manifest_digest = "sha256:" + "8" * 64
    image_reference = f"{image_name}@{manifest_digest}"
    image_platform = "linux/arm64"

    def artifact(
        *,
        schema_version: str,
        producer: str,
        sha256: str,
        measurement_ids: list[str],
        model_called: bool,
        scope: str,
        execution_plan_sha256: str | None = None,
    ) -> dict:
        value = {
            "schema_version": schema_version,
            "producer": producer,
            "sha256": sha256,
            "measurement_ids": measurement_ids,
            "system_environment_sha256": environment_sha256,
            "system_revision": revision,
            "system_source_sha256": source_sha256,
            "dataset_manifest_sha256": dataset_sha256,
            "track": track,
            "dataset_visibility": dataset_visibility,
            "blind": blind,
            "image_reference": image_reference,
            "image_platform": image_platform,
            "contains_memory_text": False,
            "model_called": model_called,
            "scope": scope,
        }
        if execution_plan_sha256 is not None:
            value["execution_plan_sha256"] = execution_plan_sha256
        return value

    run = {
        "schema_version": RUN_SCHEMA_VERSION,
        "benchmark_id": "am-eval-test",
        "run_id": "round-1",
        "system": {
            "environment_sha256": environment_sha256,
            "name": "agent-memory",
            "version": "test",
            "revision": revision,
            "source_sha256": source_sha256,
            "source_file_count": 73,
        },
        "track": track,
        "dataset": {
            "id": "private-gold",
            "sha256": dataset_sha256,
            "visibility": dataset_visibility,
            "blind": blind,
        },
        "execution_artifact": {
            "type": "oci-image",
            "image_name": image_name,
            "manifest_digest": manifest_digest,
            "platform": image_platform,
        },
        "hard_gates": {},
        "metrics": {
            "M01": {
                "value": 0.9,
                "sample_count": 10,
                "evidence": ["atomic-quality"],
            },
            "M02": {
                "value": 0.01,
                "sample_count": 900,
                "evidence": ["atomic-quality"],
            },
        },
        "attestation": {
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "claim": "OFFICIAL_AM_EVAL_RUN",
            "system_environment_sha256": environment_sha256,
            "system_revision": revision,
            "system_source_sha256": source_sha256,
            "dataset_manifest_sha256": dataset_sha256,
            "track": track,
            "image_reference": image_reference,
            "image_platform": image_platform,
            "artifacts": {
                "atomic-quality": artifact(
                    schema_version="am-eval-atomic-quality-attestation-v2",
                    producer="agent-memory-am-eval-attestation-assembler",
                    sha256="e" * 64,
                    measurement_ids=["M01", "M02"],
                    model_called=True,
                    scope="private-blind",
                    execution_plan_sha256="f" * 64,
                ),
            },
        },
    }
    return _resign(run)


def _resign(run: dict) -> dict:
    run["attestation"]["run_payload_sha256"] = formal_run_payload_sha256(run)
    return run


def _artifact_payloads(run: dict) -> dict[str, bytes]:
    supplied = {**run["hard_gates"], **run["metrics"]}
    payloads: dict[str, bytes] = {}
    for artifact_id, descriptor in tuple(run["attestation"]["artifacts"].items()):
        if artifact_id == "atomic-quality":
            system = run["system"]
            quality_metrics = {
                "M01": {
                    "sample_count": run["metrics"].get("M01", {}).get("sample_count", 10),
                    "value": run["metrics"].get("M01", {}).get("value", 0.9),
                },
                "M02": {
                    "sample_count": run["metrics"].get("M02", {}).get("sample_count", 900),
                    "value": run["metrics"].get("M02", {}).get("value", 0.01),
                },
                "M03": {"sample_count": 9, "value": 1.0},
                "M07": {"sample_count": 10, "value": 1.0},
            }
            source = {
                "schema_version": "am-eval-atomic-quality-result-v3",
                "runner_version": "am-eval-atomic-runner-v7",
                "dataset_id": run["dataset"]["id"],
                "run_id": run["run_id"],
                "run_status": "complete",
                "case_count": 10,
                "job_statuses": {"done": 10},
                "model_invocations": {
                    "budget": 10,
                    "attempted": 10,
                    "terminal_success": 10,
                    "terminal_failure": 0,
                },
                "system": system,
                "dataset_manifest_sha256": run["dataset"]["sha256"],
                "execution_plan_sha256": "f" * 64,
                "model": "ocg/qwen3.7-plus",
                "policy_version": "atomic-admission-v3",
                "model_called": True,
                "contains_production_data": False,
                "external_data_sent": True,
                "dataset_visibility": run["dataset"]["visibility"],
                "dataset_blind": run["dataset"]["blind"],
                "sample_counts": {
                    "gold_claims": 900,
                    "predictions": 10,
                    "matched_claims": 9,
                    "exact_spans": 9,
                    "recall_queries": 10,
                    "correct_citations": 10,
                },
                "metrics": quality_metrics,
                "missing_metric_ids": [],
                "complete": True,
                "contains_memory_text": False,
                "scorer_runtime_identity": {
                    "provenance": "image-build-metadata",
                    "revision": system["revision"],
                    "source_file_count": system["source_file_count"],
                    "source_sha256": system["source_sha256"],
                    "version": system["version"],
                },
                "scorer_runtime_environment": _environment(),
                "input_artifact_sha256": "7" * 64,
            }
            source_payload = json.dumps(
                source,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            artifact = assemble_attestation(
                source_payload,
                source_sha256=hashlib.sha256(source_payload).hexdigest(),
                kind="quality",
                track=run["track"],
                image_reference=(
                    f"{run['execution_artifact']['image_name']}@"
                    f"{run['execution_artifact']['manifest_digest']}"
                ),
                image_platform=run["execution_artifact"]["platform"],
                measurement_ids=frozenset(descriptor["measurement_ids"]),
            )
            artifact.update({key: value for key, value in descriptor.items() if key != "sha256"})
        else:
            artifact = {key: value for key, value in descriptor.items() if key != "sha256"}
            artifact["measurements"] = {
                item_id: {
                    "sample_count": supplied[item_id]["sample_count"],
                    "value": supplied[item_id]["value"],
                }
                for item_id in artifact["measurement_ids"]
            }
        payload = json.dumps(
            artifact,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        payloads[artifact_id] = payload
        run["attestation"]["artifacts"][artifact_id] = {
            "sha256": hashlib.sha256(payload).hexdigest()
        }
    _resign(run)
    return payloads


@contextmanager
def _registered_test_spec(spec: dict):
    benchmark_id = spec["benchmark_id"]
    semantic_sha256 = am_eval.specification_semantic_sha256(spec)
    previous = am_eval.OFFICIAL_SPECIFICATIONS.get(benchmark_id)
    am_eval.OFFICIAL_SPECIFICATIONS[benchmark_id] = {
        "artifact_sha256": semantic_sha256,
        "semantic_sha256": semantic_sha256,
    }
    try:
        yield semantic_sha256
    finally:
        if previous is None:
            del am_eval.OFFICIAL_SPECIFICATIONS[benchmark_id]
        else:
            am_eval.OFFICIAL_SPECIFICATIONS[benchmark_id] = previous


def _evaluate_with_payloads(spec: dict, run: dict, payloads: dict[str, bytes] | None) -> dict:
    execution_artifact = run["execution_artifact"]
    with _registered_test_spec(spec) as spec_sha256:
        return evaluate_run(
            spec,
            run,
            artifact_payloads=payloads,
            confirm_spec_sha256=spec_sha256,
            confirm_image_reference=(
                f"{execution_artifact['image_name']}@{execution_artifact['manifest_digest']}"
            ),
            confirm_image_platform=execution_artifact["platform"],
        )


def _evaluate(spec: dict, run: dict) -> dict:
    return _evaluate_with_payloads(spec, run, _artifact_payloads(run))


@pytest.fixture(autouse=True)
def _formal_scorer_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        am_eval,
        "resolve_runtime_identity",
        lambda: RuntimeIdentity(
            revision="a" * 40,
            version="test",
            source_sha256="b" * 64,
            source_file_count=73,
            provenance="image-build-metadata",
            source_root=am_eval.Path("/private/tmp/am-eval-test"),
        ),
    )
    monkeypatch.setattr(
        am_eval,
        "runtime_environment_identity",
        _environment,
    )


def _drop_metric(run: dict, metric_id: str) -> None:
    del run["metrics"][metric_id]
    artifact = run["attestation"]["artifacts"]["atomic-quality"]
    artifact["measurement_ids"].remove(metric_id)
    _resign(run)


def test_complete_run_passes_and_renders_markdown() -> None:
    spec = _spec()
    result = _evaluate(spec, _run())

    assert result["schema_version"] == "am-eval-result-v3"
    assert result["decision"] == "PASS"
    assert result["release_ready"] is True
    assert result["attestation"]["status"] == "verified"
    assert result["quality_summary"]["measured_score"] == 100
    assert result["quality_summary"]["coverage_percent"] == 100
    assert result["specification"]["semantic_sha256"] == (
        am_eval.specification_semantic_sha256(spec)
    )
    assert result["scorer_runtime_identity"]["source_sha256"] == "b" * 64
    assert result["scorer_runtime_environment"]["sha256"] == _environment()["sha256"]
    assert "| M01 | quality | accuracy | measured |" in render_markdown(result)


def test_registered_official_specification_matches_the_repository_artifact() -> None:
    path = Path(__file__).parents[1] / "benchmarks/am-eval-v1/spec.json"
    payload = path.read_bytes()
    spec = json.loads(payload)
    registered = am_eval.OFFICIAL_SPECIFICATIONS["am-eval-v1"]

    assert hashlib.sha256(payload).hexdigest() == registered["artifact_sha256"]
    assert am_eval.specification_semantic_sha256(spec) == registered["semantic_sha256"]


def test_missing_measurements_are_not_silently_treated_as_passed() -> None:
    run = _run()
    _drop_metric(run, "M02")

    result = _evaluate(_spec(), run)

    assert result["decision"] == "INCOMPLETE"
    assert result["quality_summary"]["coverage_percent"] == 60
    assert result["hard_gate_summary"]["not_measured"] == 0
    assert result["quality_summary"]["missing_required_ids"] == ["M02"]


def test_hard_gate_failure_overrides_quality_score() -> None:
    spec = _spec()
    spec["hard_gates"] = [
        {
            "id": "G01",
            "name": "no leaks",
            "operator": "eq",
            "threshold": 0,
            "required": True,
        }
    ]
    run = deepcopy(_run())
    run.pop("schema_version")
    run.pop("attestation")
    run["hard_gates"] = {
        "G01": {"value": 1, "sample_count": 10, "evidence": ["diagnostic-gate"]}
    }

    result = evaluate_run(spec, run)

    assert result["decision"] == "HARD_GATE_FAILED"
    assert result["release_ready"] is False


def test_unknown_or_invalid_measurements_fail_closed() -> None:
    run = _run()
    run["metrics"]["M99"] = {"value": 1, "sample_count": 1}
    with pytest.raises(ValueError, match="unknown metric"):
        _evaluate(_spec(), run)

    run = _run()
    run["metrics"]["M01"]["sample_count"] = 0
    _resign(run)
    with pytest.raises(ValueError, match="invalid value"):
        _evaluate(_spec(), run)

    spec = _spec()
    spec["hard_gates"] = [
        {
            "id": "G01",
            "name": "no leaks",
            "operator": "eq",
            "threshold": 0,
            "required": True,
        }
    ]
    run = deepcopy(_run())
    run.pop("schema_version")
    run.pop("attestation")
    run["hard_gates"] = {
        "G01": {"value": 0, "sample_count": 0, "evidence": ["diagnostic-gate"]}
    }
    with pytest.raises(ValueError, match="hard gate G01 requires a positive sample_count"):
        evaluate_run(spec, run)


def test_non_finite_or_out_of_range_measurements_fail_closed() -> None:
    spec = _spec()
    spec["metrics"][0]["minimum"] = 0
    spec["metrics"][0]["maximum"] = 1
    run = _run()
    run["metrics"]["M01"]["value"] = 1.1
    _resign(run)
    with pytest.raises(ValueError, match="differs from scorer counts"):
        _evaluate(spec, run)

    run["metrics"]["M01"]["value"] = float("nan")
    with pytest.raises(ValueError, match="not JSON compliant"):
        _resign(run)


def test_formal_run_rejects_fabricated_identity_dataset_and_empty_evidence() -> None:
    run = _run()
    run["unbound"] = True
    _resign(run)
    with pytest.raises(ValueError, match="run has an invalid schema"):
        _evaluate(_spec(), run)

    run = _run()
    run["system"]["revision"] = "not-a-git-sha"
    _resign(run)
    with pytest.raises(ValueError, match="system identity is invalid"):
        _evaluate(_spec(), run)

    run = _run()
    run["dataset"]["sha256"] = "not-a-sha"
    _resign(run)
    with pytest.raises(ValueError, match="dataset_manifest_sha256"):
        _evaluate(_spec(), run)

    run = _run()
    run["metrics"]["M01"]["evidence"] = []
    _resign(run)
    with pytest.raises(ValueError, match="single attestation artifact"):
        _evaluate(_spec(), run)

    run = _run()
    run["metrics"]["M01"]["unbound"] = True
    _resign(run)
    with pytest.raises(ValueError, match="measurement M01 has an invalid schema"):
        _evaluate(_spec(), run)


def test_formal_run_rejects_fixture_oracle_and_synthetic_quality_claims() -> None:
    run = _run()
    run["system"]["name"] = "fixture-oracle"
    _resign(run)
    with pytest.raises(ValueError, match="must identify agent-memory"):
        _evaluate(_spec(), run)

    run = _run()
    run["attestation"]["artifacts"]["atomic-quality"]["model_called"] = False
    _resign(run)
    with pytest.raises(ValueError, match="model_called differs"):
        _evaluate(_spec(), run)

    run = _run()
    run["dataset"].update({"visibility": "open", "blind": False})
    run["attestation"]["artifacts"]["atomic-quality"].update(
        {"dataset_visibility": "open", "blind": False}
    )
    _resign(run)
    with pytest.raises(ValueError, match="private or restricted dataset"):
        _evaluate(_spec(), run)


def test_formal_run_rejects_legacy_generic_measurement_artifact() -> None:
    run = _run()
    payloads = _artifact_payloads(run)
    artifact = json.loads(payloads["atomic-quality"])
    artifact["schema_version"] = "am-eval-measurement-attestation-v1"
    artifact["producer"] = "agent-memory-am-eval-attestation-assembler"
    payload = json.dumps(
        artifact,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    payloads["atomic-quality"] = payload
    run["attestation"]["artifacts"]["atomic-quality"] = {
        "sha256": hashlib.sha256(payload).hexdigest()
    }
    _resign(run)

    with pytest.raises(ValueError, match="unsupported schema"):
        _evaluate_with_payloads(_spec(), run, payloads)


def test_formal_run_requires_the_registered_official_specification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    payloads = _artifact_payloads(run)
    spec = _spec()
    execution_artifact = run["execution_artifact"]
    image_reference = (
        f"{execution_artifact['image_name']}@{execution_artifact['manifest_digest']}"
    )

    with pytest.raises(ValueError, match="registered official specification"):
        evaluate_run(
            spec,
            run,
            artifact_payloads=payloads,
            confirm_spec_sha256="0" * 64,
            confirm_image_reference=image_reference,
            confirm_image_platform=execution_artifact["platform"],
        )

    spec_sha256 = am_eval.specification_semantic_sha256(spec)
    monkeypatch.setitem(
        am_eval.OFFICIAL_SPECIFICATIONS,
        spec["benchmark_id"],
        {"artifact_sha256": spec_sha256, "semantic_sha256": spec_sha256},
    )
    with pytest.raises(ValueError, match="confirmation mismatch"):
        evaluate_run(
            spec,
            run,
            artifact_payloads=payloads,
            confirm_spec_sha256="0" * 64,
            confirm_image_reference=image_reference,
            confirm_image_platform=execution_artifact["platform"],
        )

    forged_spec = deepcopy(spec)
    forged_spec["release_policy"]["minimum_score"] = 0
    with pytest.raises(ValueError, match="semantics differ"):
        evaluate_run(
            forged_spec,
            run,
            artifact_payloads=payloads,
            confirm_spec_sha256=spec_sha256,
            confirm_image_reference=image_reference,
            confirm_image_platform=execution_artifact["platform"],
        )


def test_formal_run_rejects_scorer_source_or_environment_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    monkeypatch.setattr(
        am_eval,
        "resolve_runtime_identity",
        lambda: RuntimeIdentity(
            revision="d" * 40,
            version="test",
            source_sha256="b" * 64,
            source_file_count=73,
            provenance="image-build-metadata",
            source_root=am_eval.Path("/private/tmp/am-eval-test"),
        ),
    )
    with pytest.raises(ValueError, match="scorer source identity differs"):
        _evaluate(_spec(), run)

    run = _run()
    monkeypatch.setattr(
        am_eval,
        "resolve_runtime_identity",
        lambda: RuntimeIdentity(
            revision="a" * 40,
            version="test",
            source_sha256="b" * 64,
            source_file_count=73,
            provenance="image-build-metadata",
            source_root=am_eval.Path("/private/tmp/am-eval-test"),
        ),
    )
    monkeypatch.setattr(
        am_eval,
        "runtime_environment_identity",
        lambda: {"sha256": "8" * 64},
    )
    with pytest.raises(ValueError, match="scorer environment differs"):
        _evaluate(_spec(), run)


def test_formal_run_rejects_system_identity_laundering() -> None:
    run = _run()
    payloads = _artifact_payloads(run)
    run["system"]["name"] = "honcho"
    _resign(run)

    with pytest.raises(ValueError, match="must identify agent-memory"):
        _evaluate_with_payloads(_spec(), run, payloads)


def test_formal_run_rejects_unregistered_execution_image_repository() -> None:
    run = _run()
    payloads = _artifact_payloads(run)
    run["execution_artifact"]["image_name"] = "ghcr.io/example/honcho"
    image_reference = (
        f"{run['execution_artifact']['image_name']}@"
        f"{run['execution_artifact']['manifest_digest']}"
    )
    run["attestation"]["image_reference"] = image_reference
    artifact = json.loads(payloads["atomic-quality"])
    artifact["image_reference"] = image_reference
    payload = json.dumps(
        artifact,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    payloads["atomic-quality"] = payload
    run["attestation"]["artifacts"]["atomic-quality"] = {
        "sha256": hashlib.sha256(payload).hexdigest()
    }
    _resign(run)

    with pytest.raises(ValueError, match="registered Agent Memory execution image"):
        _evaluate_with_payloads(_spec(), run, payloads)


def test_formal_run_requires_confirmed_digest_image_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    artifacts = _artifact_payloads(run)
    spec = _spec()
    with (
        _registered_test_spec(spec) as spec_sha256,
        pytest.raises(ValueError, match="image reference confirmation mismatch"),
    ):
        evaluate_run(
            spec,
            run,
            artifact_payloads=artifacts,
            confirm_spec_sha256=spec_sha256,
            confirm_image_reference=(
                "ghcr.io/evergardener/agent-memory-api@sha256:" + "7" * 64
            ),
            confirm_image_platform="linux/arm64",
        )

    run = _run()
    monkeypatch.setattr(
        am_eval,
        "resolve_runtime_identity",
        lambda: RuntimeIdentity(
            revision="a" * 40,
            version="test",
            source_sha256="b" * 64,
            source_file_count=73,
            provenance="git-checkout",
            source_root=am_eval.Path("/private/tmp/am-eval-test"),
        ),
    )
    with pytest.raises(ValueError, match="inside a verified image"):
        _evaluate(_spec(), run)


def test_formal_run_attestation_detects_payload_and_artifact_drift() -> None:
    run = _run()
    artifacts = _artifact_payloads(run)
    run["metrics"]["M01"]["value"] = 0.8
    with pytest.raises(ValueError, match="payload SHA-256 mismatch"):
        _evaluate_with_payloads(_spec(), run, artifacts)

    run = _run()
    run["attestation"]["artifacts"]["atomic-quality"]["producer"] = "fixture-oracle"
    with pytest.raises(ValueError, match="ineligible producer"):
        _evaluate(_spec(), run)

    run = _run()
    artifacts = _artifact_payloads(run)
    run["metrics"]["M01"]["value"] = 0.8
    _resign(run)
    with pytest.raises(ValueError, match="measurement M01 value mismatch"):
        _evaluate_with_payloads(_spec(), run, artifacts)

    run = _run()
    artifacts = _artifact_payloads(run)
    artifacts["atomic-quality"] += b"\n"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _evaluate_with_payloads(_spec(), run, artifacts)


def test_self_consistent_formal_run_without_actual_artifacts_fails_closed() -> None:
    run = _run()
    _artifact_payloads(run)

    with pytest.raises(ValueError, match="requires actual artifact payloads"):
        _evaluate_with_payloads(_spec(), run, None)


def test_legacy_complete_run_cannot_become_release_ready() -> None:
    run = deepcopy(_run())
    _artifact_payloads(run)
    del run["schema_version"]
    del run["attestation"]

    result = evaluate_run(_spec(), run)

    assert result["decision"] == "ATTESTATION_REQUIRED"
    assert result["release_ready"] is False
    assert result["attestation"]["status"] == "unattested"
