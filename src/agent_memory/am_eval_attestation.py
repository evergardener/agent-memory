from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .am_eval_atomic_runner import (
    discover_runtime_source_root,
    validate_atomic_run_ledger,
    validate_private_output,
    write_private_json,
)
from .am_eval_dataset import DatasetError, read_file_snapshot
from .am_eval_environment import validate_runtime_environment_identity

ASSEMBLER_NAME = "agent-memory-am-eval-attestation-assembler"
QUALITY_ATTESTATION_SCHEMA_VERSION = "am-eval-atomic-quality-attestation-v2"
EFFICIENCY_ATTESTATION_SCHEMA_VERSION = "am-eval-efficiency-attestation-v2"
QUALITY_RESULT_SCHEMA_VERSION = "am-eval-atomic-quality-result-v3"
EFFICIENCY_RESULT_SCHEMA_VERSION = "am-eval-efficiency-result-v5"
QUALITY_METRIC_IDS = frozenset({"M01", "M02", "M03", "M07"})
SHA256_CHARACTERS = frozenset("0123456789abcdef")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.casefold()
        and all(character in SHA256_CHARACTERS for character in value)
    )


def _strict_json(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value {constant}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise DatasetError(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise DatasetError(f"{label} must be a JSON object")
    return value


def _require_string(payload: dict[str, Any], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DatasetError(f"{label} requires {key}")
    return value


def _validate_measurements(
    payload: object,
    *,
    allowed_ids: frozenset[str],
    label: str,
) -> dict[str, dict[str, float | int]]:
    if not isinstance(payload, dict) or not payload or not set(payload) <= allowed_ids:
        raise DatasetError(f"{label} has unsupported or missing metrics")
    normalized: dict[str, dict[str, float | int]] = {}
    for metric_id, measurement in payload.items():
        if not isinstance(measurement, dict) or set(measurement) != {
            "sample_count",
            "value",
        }:
            raise DatasetError(f"{label} metric {metric_id} has an invalid schema")
        sample_count = measurement.get("sample_count")
        value = measurement.get("value")
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count <= 0
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise DatasetError(f"{label} metric {metric_id} has an invalid value")
        normalized[metric_id] = {"sample_count": sample_count, "value": value}
    return normalized


def _validate_system_identity(
    system: object,
    scorer_identity: object,
    scorer_environment: object,
    *,
    label: str,
) -> dict[str, Any]:
    system_keys = {
        "environment_sha256",
        "name",
        "revision",
        "source_file_count",
        "source_sha256",
        "version",
    }
    if not isinstance(system, dict) or set(system) != system_keys:
        raise DatasetError(f"{label} system identity has an invalid schema")
    if system.get("name") != "agent-memory":
        raise DatasetError(f"{label} must identify agent-memory")
    if not _is_sha256(system.get("source_sha256")) or not _is_sha256(
        system.get("environment_sha256")
    ):
        raise DatasetError(f"{label} system identity has an invalid digest")
    if (
        not isinstance(system.get("revision"), str)
        or len(system["revision"]) != 40
        or any(character not in SHA256_CHARACTERS for character in system["revision"])
        or not isinstance(system.get("version"), str)
        or not system["version"]
        or isinstance(system.get("source_file_count"), bool)
        or not isinstance(system["source_file_count"], int)
        or system["source_file_count"] <= 0
    ):
        raise DatasetError(f"{label} system identity is invalid")
    identity_keys = {
        "provenance",
        "revision",
        "source_file_count",
        "source_sha256",
        "version",
    }
    if not isinstance(scorer_identity, dict) or set(scorer_identity) != identity_keys:
        raise DatasetError(f"{label} scorer identity has an invalid schema")
    if scorer_identity.get("provenance") != "image-build-metadata":
        raise DatasetError(f"{label} scorer must run in a verified image")
    for key in ("revision", "source_file_count", "source_sha256", "version"):
        if scorer_identity.get(key) != system.get(key):
            raise DatasetError(f"{label} scorer identity differs from the system")
    validated_environment = validate_runtime_environment_identity(scorer_environment)
    if validated_environment["sha256"] != system["environment_sha256"]:
        raise DatasetError(f"{label} scorer environment differs from the system")
    return system


def validate_quality_result(payload: object) -> dict[str, Any]:
    required_keys = {
        "case_count",
        "complete",
        "contains_memory_text",
        "contains_production_data",
        "dataset_blind",
        "dataset_id",
        "dataset_manifest_sha256",
        "dataset_visibility",
        "execution_plan_sha256",
        "external_data_sent",
        "input_artifact_sha256",
        "job_statuses",
        "metrics",
        "missing_metric_ids",
        "model",
        "model_called",
        "model_invocations",
        "policy_version",
        "run_id",
        "run_status",
        "runner_version",
        "sample_counts",
        "schema_version",
        "scorer_runtime_environment",
        "scorer_runtime_identity",
        "system",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required_keys
        or payload.get("schema_version") != QUALITY_RESULT_SCHEMA_VERSION
    ):
        raise DatasetError("quality result has an invalid schema")
    if (
        payload.get("complete") is not True
        or payload.get("missing_metric_ids") != []
        or payload.get("contains_memory_text") is not False
        or payload.get("run_status") != "complete"
        or payload.get("model_called") is not True
    ):
        raise DatasetError("quality result is not a complete scoreable model run")
    validate_atomic_run_ledger(payload)
    if payload["job_statuses"] != {"done": payload["case_count"]}:
        raise DatasetError("quality result contains non-successful model jobs")
    if payload.get("dataset_visibility") not in {"private", "restricted"}:
        raise DatasetError("quality result requires a private or restricted dataset")
    if payload.get("dataset_blind") is not True:
        raise DatasetError("quality result requires a blind dataset")
    for key in (
        "dataset_manifest_sha256",
        "execution_plan_sha256",
        "input_artifact_sha256",
    ):
        if not _is_sha256(payload.get(key)):
            raise DatasetError(f"quality result requires {key}")
    for key in ("dataset_id", "model", "policy_version", "run_id", "runner_version"):
        _require_string(payload, key, label="quality result")
    metrics = _validate_measurements(
        payload.get("metrics"), allowed_ids=QUALITY_METRIC_IDS, label="quality result"
    )
    if set(metrics) != QUALITY_METRIC_IDS:
        raise DatasetError("quality result must contain all atomic quality metrics")
    sample_counts = payload.get("sample_counts")
    expected_count_keys = {
        "correct_citations",
        "exact_spans",
        "gold_claims",
        "matched_claims",
        "predictions",
        "recall_queries",
    }
    if (
        not isinstance(sample_counts, dict)
        or set(sample_counts) != expected_count_keys
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in sample_counts.values()
        )
    ):
        raise DatasetError("quality result sample counts have an invalid schema")
    denominators = {
        "M01": sample_counts["predictions"],
        "M02": sample_counts["gold_claims"],
        "M03": sample_counts["matched_claims"],
        "M07": sample_counts["recall_queries"],
    }
    numerators = {
        "M01": sample_counts["matched_claims"],
        "M02": sample_counts["matched_claims"],
        "M03": sample_counts["exact_spans"],
        "M07": sample_counts["correct_citations"],
    }
    for metric_id, denominator in denominators.items():
        if denominator <= 0 or metrics[metric_id] != {
            "sample_count": denominator,
            "value": numerators[metric_id] / denominator,
        }:
            raise DatasetError(f"quality result metric {metric_id} differs from scorer counts")
    _validate_system_identity(
        payload.get("system"),
        payload.get("scorer_runtime_identity"),
        payload.get("scorer_runtime_environment"),
        label="quality result",
    )
    return payload


def validate_efficiency_result(payload: object) -> dict[str, Any]:
    required_keys = {
        "case_count",
        "complete",
        "contains_memory_text",
        "contains_production_data",
        "dataset_blind",
        "dataset_id",
        "dataset_manifest_sha256",
        "dataset_visibility",
        "execution_plan_sha256",
        "external_data_sent",
        "input_artifact_sha256",
        "job_statuses",
        "counts",
        "metrics",
        "missing_metric_ids",
        "model",
        "model_called",
        "model_invocations",
        "policy_version",
        "run_id",
        "run_status",
        "schema_version",
        "scope",
        "scorer_runtime_environment",
        "scorer_runtime_identity",
        "system_environment_sha256",
        "system_revision",
        "system_source_file_count",
        "system_source_sha256",
        "system_version",
        "window_end",
        "window_start",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required_keys
        or payload.get("schema_version") != EFFICIENCY_RESULT_SCHEMA_VERSION
    ):
        raise DatasetError("efficiency result has an invalid schema")
    if (
        payload.get("scope") != "isolated"
        or payload.get("complete") is not True
        or payload.get("missing_metric_ids") != []
        or payload.get("contains_memory_text") is not False
        or payload.get("run_status") != "complete"
        or payload.get("model_called") is not True
    ):
        raise DatasetError("efficiency result is not a complete isolated model run")
    validate_atomic_run_ledger(payload)
    if payload["job_statuses"] != {"done": payload["case_count"]}:
        raise DatasetError("efficiency result contains non-successful model jobs")
    for key in (
        "dataset_manifest_sha256",
        "execution_plan_sha256",
        "input_artifact_sha256",
        "system_environment_sha256",
        "system_source_sha256",
    ):
        if not _is_sha256(payload.get(key)):
            raise DatasetError(f"efficiency result requires {key}")
    for key in (
        "dataset_id",
        "model",
        "policy_version",
        "run_id",
        "system_revision",
        "system_version",
    ):
        _require_string(payload, key, label="efficiency result")
    if payload.get("dataset_visibility") not in {"open", "private", "restricted"}:
        raise DatasetError("efficiency result has an invalid dataset visibility")
    if not isinstance(payload.get("dataset_blind"), bool):
        raise DatasetError("efficiency result requires a dataset blind declaration")
    metrics = _validate_measurements(
        payload.get("metrics"), allowed_ids=frozenset({"M22", "M23"}), label="efficiency result"
    )
    if set(metrics) != {"M22", "M23"}:
        raise DatasetError("efficiency result must contain M22 and M23")
    counts = payload.get("counts")
    count_keys = {
        "auto_admitted_count",
        "manual_review_count",
        "terminal_model_failure_count",
        "terminal_model_success_count",
        "unfinished_model_job_count",
    }
    if (
        not isinstance(counts, dict)
        or set(counts) != count_keys
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        )
    ):
        raise DatasetError("efficiency result counts have an invalid schema")
    governance_total = counts["auto_admitted_count"] + counts["manual_review_count"]
    terminal_total = counts["terminal_model_success_count"] + counts["terminal_model_failure_count"]
    if governance_total <= 0 or metrics["M22"] != {
        "sample_count": governance_total,
        "value": counts["manual_review_count"] / governance_total,
    }:
        raise DatasetError("efficiency result M22 differs from scorer counts")
    if (
        terminal_total <= 0
        or counts["unfinished_model_job_count"] != 0
        or metrics["M23"]
        != {
            "sample_count": terminal_total,
            "value": counts["terminal_model_failure_count"] / terminal_total,
        }
    ):
        raise DatasetError("efficiency result M23 differs from scorer counts")
    system = {
        "environment_sha256": payload["system_environment_sha256"],
        "name": "agent-memory",
        "revision": payload["system_revision"],
        "source_file_count": payload["system_source_file_count"],
        "source_sha256": payload["system_source_sha256"],
        "version": payload["system_version"],
    }
    _validate_system_identity(
        system,
        payload.get("scorer_runtime_identity"),
        payload.get("scorer_runtime_environment"),
        label="efficiency result",
    )
    return payload


def decode_attested_source(artifact: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    encoded = artifact.get("source_artifact_base64")
    expected_sha256 = artifact.get("source_artifact_sha256")
    if not isinstance(encoded, str) or not _is_sha256(expected_sha256):
        raise DatasetError("attestation requires an embedded source artifact")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise DatasetError("attestation source artifact is not canonical base64") from error
    if base64.b64encode(payload).decode("ascii") != encoded:
        raise DatasetError("attestation source artifact is not canonical base64")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise DatasetError("attestation source artifact SHA-256 mismatch")
    return payload, _strict_json(payload, label="attestation source artifact")


def validate_attested_source(artifact: dict[str, Any]) -> dict[str, Any]:
    payload, source = decode_attested_source(artifact)
    schema = artifact.get("schema_version")
    if schema == QUALITY_ATTESTATION_SCHEMA_VERSION:
        validated = validate_quality_result(source)
        supplied_ids = artifact.get("measurement_ids")
        if (
            not isinstance(supplied_ids, list)
            or not supplied_ids
            or not all(isinstance(item, str) for item in supplied_ids)
            or len(set(supplied_ids)) != len(supplied_ids)
            or not set(supplied_ids) <= QUALITY_METRIC_IDS
        ):
            raise DatasetError("quality attestation has invalid measurement IDs")
        measurement_ids = frozenset(supplied_ids)
        expected_scope = "private-blind"
    elif schema == EFFICIENCY_ATTESTATION_SCHEMA_VERSION:
        validated = validate_efficiency_result(source)
        measurement_ids = frozenset({"M23"})
        expected_scope = "isolated"
    else:
        raise DatasetError("unsupported sourced attestation schema")
    if artifact.get("source_artifact_schema_version") != validated["schema_version"]:
        raise DatasetError("attestation source schema binding mismatch")
    if artifact.get("source_artifact_sha256") != hashlib.sha256(payload).hexdigest():
        raise DatasetError("attestation source SHA-256 binding mismatch")
    expected_measurements = {
        metric_id: validated["metrics"][metric_id] for metric_id in measurement_ids
    }
    if artifact.get("measurement_ids") != sorted(measurement_ids):
        raise DatasetError("attestation measurement IDs differ from the source result")
    if artifact.get("measurements") != expected_measurements:
        raise DatasetError("attestation measurements differ from the source result")
    if artifact.get("scope") != expected_scope:
        raise DatasetError("attestation scope differs from the source result")
    source_system = (
        validated["system"]
        if schema == QUALITY_ATTESTATION_SCHEMA_VERSION
        else {
            "environment_sha256": validated["system_environment_sha256"],
            "revision": validated["system_revision"],
            "source_sha256": validated["system_source_sha256"],
        }
    )
    source_bindings = {
        "blind": validated["dataset_blind"],
        "dataset_manifest_sha256": validated["dataset_manifest_sha256"],
        "dataset_visibility": validated["dataset_visibility"],
        "execution_plan_sha256": validated["execution_plan_sha256"],
        "model_called": validated["model_called"],
        "system_environment_sha256": source_system["environment_sha256"],
        "system_revision": source_system["revision"],
        "system_source_sha256": source_system["source_sha256"],
    }
    for key, expected in source_bindings.items():
        if artifact.get(key) != expected:
            raise DatasetError(f"attestation {key} differs from the source result")
    return validated


def assemble_attestation(
    source_payload: bytes,
    *,
    source_sha256: str,
    kind: str,
    track: str,
    image_reference: str,
    image_platform: str,
    measurement_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    if hashlib.sha256(source_payload).hexdigest() != source_sha256.casefold():
        raise DatasetError("source result SHA-256 confirmation mismatch")
    source = _strict_json(source_payload, label="source result")
    if kind == "quality":
        source = validate_quality_result(source)
        schema = QUALITY_ATTESTATION_SCHEMA_VERSION
        selected_measurements = measurement_ids or QUALITY_METRIC_IDS
        if not selected_measurements or not selected_measurements <= QUALITY_METRIC_IDS:
            raise DatasetError("quality attestation has invalid measurement IDs")
        scope = "private-blind"
        system = source["system"]
    elif kind == "efficiency":
        source = validate_efficiency_result(source)
        schema = EFFICIENCY_ATTESTATION_SCHEMA_VERSION
        selected_measurements = frozenset({"M23"})
        scope = "isolated"
        system = {
            "environment_sha256": source["system_environment_sha256"],
            "revision": source["system_revision"],
            "source_sha256": source["system_source_sha256"],
        }
    else:
        raise DatasetError("unsupported attestation source kind")
    if not isinstance(track, str) or not track.strip() or track != track.strip():
        raise DatasetError("attestation requires a track")
    if (
        not isinstance(image_reference, str)
        or "@sha256:" not in image_reference
        or any(character.isspace() for character in image_reference)
        or not _is_sha256(image_reference.rsplit("@sha256:", maxsplit=1)[-1])
    ):
        raise DatasetError("attestation requires an exact OCI image reference")
    if image_platform not in {"linux/amd64", "linux/arm64"}:
        raise DatasetError("attestation requires a supported OCI platform")
    artifact = {
        "schema_version": schema,
        "producer": ASSEMBLER_NAME,
        "source_artifact_schema_version": source["schema_version"],
        "source_artifact_sha256": source_sha256.casefold(),
        "source_artifact_base64": base64.b64encode(source_payload).decode("ascii"),
        "measurement_ids": sorted(selected_measurements),
        "measurements": {
            metric_id: source["metrics"][metric_id] for metric_id in sorted(selected_measurements)
        },
        "system_environment_sha256": system["environment_sha256"],
        "system_revision": system["revision"],
        "system_source_sha256": system["source_sha256"],
        "dataset_manifest_sha256": source["dataset_manifest_sha256"],
        "track": track,
        "dataset_visibility": source["dataset_visibility"],
        "blind": source["dataset_blind"],
        "image_reference": image_reference,
        "image_platform": image_platform,
        "contains_memory_text": False,
        "model_called": source["model_called"],
        "scope": scope,
        "execution_plan_sha256": source["execution_plan_sha256"],
    }
    if kind == "efficiency":
        artifact["terminal_jobs_complete"] = source["job_statuses"] == {
            "done": source["case_count"]
        }
    validate_attested_source(artifact)
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bind a verified AM-Eval scorer result into a sourced attestation."
    )
    parser.add_argument("kind", choices=("quality", "efficiency"))
    parser.add_argument("source_result", type=Path)
    parser.add_argument("--confirm-source-sha256", required=True)
    parser.add_argument("--track", required=True)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--image-platform", required=True, choices=("linux/amd64", "linux/arm64"))
    parser.add_argument(
        "--measurement-id",
        action="append",
        default=[],
        help="Quality metric to attest; repeat as needed. Defaults to M01/M02/M03/M07.",
    )
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    target = None
    try:
        snapshot = read_file_snapshot(arguments.source_result)
        artifact = assemble_attestation(
            snapshot.payload,
            source_sha256=arguments.confirm_source_sha256,
            kind=arguments.kind,
            track=arguments.track,
            image_reference=arguments.image_reference,
            image_platform=arguments.image_platform,
            measurement_ids=(
                frozenset(arguments.measurement_id) if arguments.measurement_id else None
            ),
        )
        target = validate_private_output(
            arguments.output,
            forbidden_root=discover_runtime_source_root(),
        )
        write_private_json(target, artifact)
    except (DatasetError, UnicodeError, json.JSONDecodeError) as error:
        if target is not None:
            target.close()
        parser.error(str(error))


if __name__ == "__main__":
    main()
