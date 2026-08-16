from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .am_eval_atomic_runner import resolve_runtime_identity
from .am_eval_attestation import (
    EFFICIENCY_ATTESTATION_SCHEMA_VERSION,
    EPISODE_PROCEDURE_ATTESTATION_SCHEMA_VERSION,
    EVIDENCE_ATTESTATION_SCHEMA_VERSION,
    LIFECYCLE_ATTESTATION_SCHEMA_VERSION,
    QUALITY_ATTESTATION_SCHEMA_VERSION,
    RECALL_ATTESTATION_SCHEMA_VERSION,
    RELIABILITY_ATTESTATION_SCHEMA_VERSION,
    validate_attested_source,
)
from .am_eval_dataset import DatasetError, read_file_snapshot
from .am_eval_environment import runtime_environment_identity

RUN_SCHEMA_VERSION = "am-eval-run-v2"
ATTESTATION_SCHEMA_VERSION = "am-eval-run-attestation-v1"
MULTI_DATASET_RUN_SCHEMA_VERSION = "am-eval-run-v3"
MULTI_DATASET_ATTESTATION_SCHEMA_VERSION = "am-eval-run-attestation-v2"
RESULT_SCHEMA_VERSION = "am-eval-result-v3"
SHA256_CHARACTERS = frozenset("0123456789abcdef")
GIT_REVISION_LENGTHS = frozenset({40, 64})
PROHIBITED_FORMAL_IDENTITY_MARKERS = ("fake", "fixture", "mock", "oracle")
MODEL_QUALITY_METRIC_IDS = frozenset({"M01", "M02", "M03", "M07"})
EFFICIENCY_METRIC_IDS = frozenset({"M22", "M23"})
OFFICIAL_SPECIFICATIONS = {
    "am-eval-v1": {
        "artifact_sha256": "a4ce9232ecafc37f9a3142a8e29020168af96aa6b4165e21fd64395186c7b679",
        "semantic_sha256": "b25f605699051d7a611afbaeb9a31a0a6de46ef342b5a2be88b898b917f10a80",
    }
}
ARTIFACT_PRODUCERS = {
    QUALITY_ATTESTATION_SCHEMA_VERSION: "agent-memory-am-eval-attestation-assembler",
    EFFICIENCY_ATTESTATION_SCHEMA_VERSION: "agent-memory-am-eval-attestation-assembler",
    LIFECYCLE_ATTESTATION_SCHEMA_VERSION: "agent-memory-am-eval-attestation-assembler",
    RECALL_ATTESTATION_SCHEMA_VERSION: "agent-memory-am-eval-attestation-assembler",
    EVIDENCE_ATTESTATION_SCHEMA_VERSION: "agent-memory-am-eval-attestation-assembler",
    EPISODE_PROCEDURE_ATTESTATION_SCHEMA_VERSION: ("agent-memory-am-eval-attestation-assembler"),
    RELIABILITY_ATTESTATION_SCHEMA_VERSION: "agent-memory-am-eval-attestation-assembler",
}


def specification_semantic_sha256(spec: dict[str, Any]) -> str:
    try:
        payload = json.dumps(
            spec,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("AM-Eval specification is not canonical JSON") from error
    return hashlib.sha256(payload).hexdigest()


def _validate_official_specification(
    spec: dict[str, Any],
    *,
    confirm_spec_sha256: str | None,
) -> dict[str, str]:
    benchmark_id = spec.get("benchmark_id")
    registered = OFFICIAL_SPECIFICATIONS.get(benchmark_id)
    if registered is None:
        raise ValueError("formal AM-Eval requires a registered official specification")
    artifact_sha256 = registered["artifact_sha256"]
    if confirm_spec_sha256 != artifact_sha256:
        raise ValueError("formal AM-Eval official specification SHA-256 confirmation mismatch")
    semantic_sha256 = specification_semantic_sha256(spec)
    if semantic_sha256 != registered["semantic_sha256"]:
        raise ValueError("formal AM-Eval specification semantics differ from the official contract")
    return {
        "artifact_sha256": artifact_sha256,
        "semantic_sha256": semantic_sha256,
    }


def _is_hex_digest(value: object, *, lengths: frozenset[int]) -> bool:
    return (
        isinstance(value, str)
        and len(value) in lengths
        and value == value.casefold()
        and all(character in SHA256_CHARACTERS for character in value)
    )


def _require_non_empty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")
    return value


def _validate_execution_artifact(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != {
        "image_name",
        "manifest_digest",
        "platform",
        "type",
    }:
        raise ValueError("formal run execution artifact has an invalid schema")
    if payload.get("type") != "oci-image":
        raise ValueError("formal run requires an OCI image execution artifact")
    image_name = _require_non_empty_string(payload.get("image_name"), label="image name")
    if (
        "@" in image_name
        or any(character.isspace() for character in image_name)
        or "/" not in image_name
        or ":" in image_name.rsplit("/", maxsplit=1)[-1]
    ):
        raise ValueError("formal run image name must be an untagged OCI repository name")
    manifest_digest = payload.get("manifest_digest")
    if (
        not isinstance(manifest_digest, str)
        or not manifest_digest.startswith("sha256:")
        or not _is_hex_digest(manifest_digest.removeprefix("sha256:"), lengths=frozenset({64}))
    ):
        raise ValueError("formal run requires an OCI manifest SHA-256 digest")
    platform_name = payload.get("platform")
    if platform_name not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("formal run requires a supported OCI platform")
    return {
        "image_name": image_name,
        "manifest_digest": manifest_digest,
        "platform": platform_name,
        "type": "oci-image",
    }


def formal_run_payload_sha256(run: dict[str, Any]) -> str:
    """Return the canonical digest covered by a formal run attestation."""

    covered = dict(run)
    attestation = covered.get("attestation")
    if isinstance(attestation, dict):
        covered["attestation"] = {
            key: value for key, value in attestation.items() if key != "run_payload_sha256"
        }
    payload = json.dumps(
        covered,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_dataset_descriptor(payload: object, *, formal: bool) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("run dataset must be an object")
    dataset_id = _require_non_empty_string(payload.get("id"), label="dataset id")
    dataset_sha256 = payload.get("sha256")
    if not _is_hex_digest(dataset_sha256, lengths=frozenset({64})):
        raise ValueError("dataset sha256 must be a lowercase SHA-256")
    if formal:
        if set(payload) != {"blind", "id", "sha256", "visibility"}:
            raise ValueError("formal run dataset has an invalid schema")
        if payload.get("visibility") not in {"open", "private", "restricted"}:
            raise ValueError("formal run dataset requires a supported visibility")
        if not isinstance(payload.get("blind"), bool):
            raise ValueError("formal run dataset requires a blind declaration")
    return {
        "blind": payload.get("blind"),
        "id": dataset_id,
        "sha256": dataset_sha256,
        "visibility": payload.get("visibility"),
    }


def _run_datasets(run: dict[str, Any]) -> list[dict[str, Any]]:
    if run.get("schema_version") == MULTI_DATASET_RUN_SCHEMA_VERSION:
        datasets = run.get("datasets")
        if not isinstance(datasets, list) or not datasets:
            raise ValueError("formal multi-dataset run requires datasets")
        validated = [_validate_dataset_descriptor(item, formal=True) for item in datasets]
        dataset_ids = [item["id"] for item in validated]
        dataset_sha256s = [item["sha256"] for item in validated]
        if len(set(dataset_ids)) != len(dataset_ids):
            raise ValueError("formal multi-dataset run requires unique dataset IDs")
        if len(set(dataset_sha256s)) != len(dataset_sha256s):
            raise ValueError("formal multi-dataset run requires unique dataset SHA-256 values")
        if validated != sorted(validated, key=lambda item: item["id"]):
            raise ValueError("formal multi-dataset run datasets must be sorted by id")
        return validated
    return [_validate_dataset_descriptor(run.get("dataset"), formal=True)]


def _dataset_for_artifact(run: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    artifact_sha256 = artifact.get("dataset_manifest_sha256")
    matches = [item for item in _run_datasets(run) if item["sha256"] == artifact_sha256]
    if len(matches) != 1:
        raise ValueError("attestation artifact dataset is not uniquely declared by the formal run")
    return matches[0]


def _validate_run_identity(run: dict[str, Any], *, formal: bool) -> None:
    _require_non_empty_string(run.get("run_id"), label="run_id")
    _require_non_empty_string(run.get("track"), label="track")

    system = run.get("system")
    if not isinstance(system, dict):
        raise ValueError("run system must be an object")
    system_name = _require_non_empty_string(system.get("name"), label="system name")
    _require_non_empty_string(system.get("version"), label="system version")
    if not _is_hex_digest(system.get("revision"), lengths=GIT_REVISION_LENGTHS):
        raise ValueError("system revision must be a full lowercase Git object ID")
    if formal:
        if set(system) != {
            "environment_sha256",
            "name",
            "revision",
            "source_file_count",
            "source_sha256",
            "version",
        }:
            raise ValueError("formal run system identity has an invalid schema")
        lowered_name = system_name.casefold()
        if any(marker in lowered_name for marker in PROHIBITED_FORMAL_IDENTITY_MARKERS):
            raise ValueError(
                "formal run system identity cannot be a fixture, mock, fake, or oracle"
            )
        if not _is_hex_digest(system.get("source_sha256"), lengths=frozenset({64})):
            raise ValueError("formal run system requires a lowercase source SHA-256")
        if not _is_hex_digest(system.get("environment_sha256"), lengths=frozenset({64})):
            raise ValueError("formal run system requires a runtime environment SHA-256")
        source_file_count = system.get("source_file_count")
        if (
            isinstance(source_file_count, bool)
            or not isinstance(source_file_count, int)
            or source_file_count <= 0
        ):
            raise ValueError("formal run system requires a positive source_file_count")

    if formal:
        _run_datasets(run)
    else:
        _validate_dataset_descriptor(run.get("dataset"), formal=False)


def _validate_formal_scorer_identity(run: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    identity = resolve_runtime_identity()
    environment = runtime_environment_identity()
    system = run["system"]
    if identity.provenance != "image-build-metadata":
        raise ValueError("formal AM-Eval scoring must run inside a verified image")
    if (
        identity.revision != system["revision"]
        or identity.version != system["version"]
        or identity.source_sha256 != system["source_sha256"]
        or identity.source_file_count != system["source_file_count"]
    ):
        raise ValueError("formal run scorer source identity differs from the claimed system")
    if environment["sha256"] != system["environment_sha256"]:
        raise ValueError("formal run scorer environment differs from the claimed system")
    return (
        {
            "provenance": identity.provenance,
            "revision": identity.revision,
            "source_file_count": identity.source_file_count,
            "source_sha256": identity.source_sha256,
            "version": identity.version,
        },
        environment,
    )


def _validate_legacy_evidence(measurement: dict[str, Any], *, item_id: str) -> None:
    evidence = measurement.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(item, str) and item.strip() for item in evidence)
    ):
        raise ValueError(f"measurement {item_id} requires non-empty evidence")


def _decode_artifact(artifact_id: str, payload: bytes) -> dict[str, Any]:
    try:
        artifact = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"attestation artifact {artifact_id} is not strict UTF-8 JSON") from error
    if not isinstance(artifact, dict):
        raise ValueError(f"attestation artifact {artifact_id} must be a JSON object")
    return artifact


def _load_confirmed_json(path: Path, expected_sha256: str, *, label: str) -> dict[str, Any]:
    snapshot = read_file_snapshot(path)
    if snapshot.sha256 != expected_sha256.casefold():
        raise DatasetError(f"{label} SHA-256 confirmation mismatch")
    try:
        value = json.loads(
            snapshot.payload.decode("utf-8"),
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value {constant}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise DatasetError(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise DatasetError(f"{label} must be a JSON object")
    return value


def _validate_artifact(
    artifact_id: str,
    descriptor: object,
    payload: bytes | None,
    *,
    run: dict[str, Any],
    supplied_measurements: dict[str, Any],
) -> set[str]:
    if not isinstance(descriptor, dict) or set(descriptor) != {"sha256"}:
        raise ValueError(f"attestation artifact {artifact_id} descriptor must contain only sha256")
    expected_sha256 = descriptor.get("sha256")
    if not _is_hex_digest(expected_sha256, lengths=frozenset({64})):
        raise ValueError(f"attestation artifact {artifact_id} requires a SHA-256")
    if payload is None:
        raise ValueError(f"formal run requires actual artifact bytes for {artifact_id}")
    if not isinstance(payload, bytes):
        raise ValueError(f"attestation artifact {artifact_id} payload must be bytes")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError(f"attestation artifact {artifact_id} SHA-256 mismatch")
    artifact = _decode_artifact(artifact_id, payload)

    schema_version = artifact.get("schema_version")
    producer = artifact.get("producer")
    if schema_version not in ARTIFACT_PRODUCERS:
        raise ValueError(f"attestation artifact {artifact_id} has an unsupported schema")
    if producer != ARTIFACT_PRODUCERS[schema_version]:
        raise ValueError(f"attestation artifact {artifact_id} has an ineligible producer")
    source_result: dict[str, Any] | None = None
    if schema_version in {
        QUALITY_ATTESTATION_SCHEMA_VERSION,
        EFFICIENCY_ATTESTATION_SCHEMA_VERSION,
        LIFECYCLE_ATTESTATION_SCHEMA_VERSION,
        RECALL_ATTESTATION_SCHEMA_VERSION,
        EVIDENCE_ATTESTATION_SCHEMA_VERSION,
        EPISODE_PROCEDURE_ATTESTATION_SCHEMA_VERSION,
        RELIABILITY_ATTESTATION_SCHEMA_VERSION,
    }:
        try:
            source_result = validate_attested_source(artifact)
        except DatasetError as error:
            raise ValueError(f"attestation artifact {artifact_id}: {error}") from error
    measurement_ids = artifact.get("measurement_ids")
    if (
        not isinstance(measurement_ids, list)
        or not measurement_ids
        or not all(isinstance(item, str) and item for item in measurement_ids)
        or len(set(measurement_ids)) != len(measurement_ids)
    ):
        raise ValueError(f"attestation artifact {artifact_id} has invalid measurement IDs")
    artifact_measurements = set(measurement_ids)
    if not artifact_measurements <= set(supplied_measurements):
        raise ValueError(f"attestation artifact {artifact_id} references an unmeasured item")

    artifact_values = artifact.get("measurements")
    if not isinstance(artifact_values, dict) or set(artifact_values) != artifact_measurements:
        raise ValueError(
            f"attestation artifact {artifact_id} measurement payload does not match its IDs"
        )
    for item_id in artifact_measurements:
        artifact_measurement = artifact_values[item_id]
        run_measurement = supplied_measurements[item_id]
        if not isinstance(artifact_measurement, dict) or set(artifact_measurement) != {
            "sample_count",
            "value",
        }:
            raise ValueError(
                f"attestation artifact {artifact_id} measurement {item_id} has an invalid shape"
            )
        if artifact_measurement != {
            "sample_count": run_measurement.get("sample_count"),
            "value": run_measurement.get("value"),
        }:
            raise ValueError(
                f"attestation artifact {artifact_id} measurement {item_id} value mismatch"
            )

    system = run["system"]
    dataset = _dataset_for_artifact(run, artifact)
    bindings = {
        "system_environment_sha256": system["environment_sha256"],
        "system_revision": system["revision"],
        "system_source_sha256": system["source_sha256"],
        "dataset_manifest_sha256": dataset["sha256"],
        "track": run["track"],
        "dataset_visibility": dataset["visibility"],
        "blind": dataset["blind"],
        "image_reference": (
            f"{run['execution_artifact']['image_name']}@"
            f"{run['execution_artifact']['manifest_digest']}"
        ),
        "image_platform": run["execution_artifact"]["platform"],
    }
    for key, expected in bindings.items():
        if artifact.get(key) != expected:
            raise ValueError(f"attestation artifact {artifact_id} {key} binding mismatch")
    if artifact.get("contains_memory_text") is not False:
        raise ValueError(f"attestation artifact {artifact_id} must not contain memory text")
    if not isinstance(artifact.get("model_called"), bool):
        raise ValueError(f"attestation artifact {artifact_id} requires model_called")

    quality_ids = artifact_measurements & MODEL_QUALITY_METRIC_IDS
    efficiency_ids = artifact_measurements & EFFICIENCY_METRIC_IDS
    if quality_ids:
        if schema_version != QUALITY_ATTESTATION_SCHEMA_VERSION:
            raise ValueError("formal model-quality metrics require an atomic-quality artifact")
        if artifact_measurements - MODEL_QUALITY_METRIC_IDS:
            raise ValueError("atomic-quality artifact cannot attest unrelated measurements")
        if artifact.get("model_called") is not True:
            raise ValueError("formal model-quality metrics require a real model call")
        if run["track"] != "recommended-product":
            raise ValueError("formal model-quality metrics require recommended-product track")
        if dataset["visibility"] not in {"private", "restricted"} or dataset["blind"] is not True:
            raise ValueError("formal model-quality metrics require a private blind dataset")
        if artifact.get("scope") != "private-blind":
            raise ValueError("formal model-quality artifact requires private-blind scope")
        if not _is_hex_digest(artifact.get("execution_plan_sha256"), lengths=frozenset({64})):
            raise ValueError("formal model-quality artifact requires an execution plan SHA-256")
        assert source_result is not None
        if (
            source_result["run_id"] != run["run_id"]
            or source_result["dataset_id"] != dataset["id"]
            or source_result["system"]["version"] != system["version"]
            or source_result["system"]["source_file_count"] != system["source_file_count"]
        ):
            raise ValueError("formal model-quality source identity binding mismatch")
    elif schema_version == QUALITY_ATTESTATION_SCHEMA_VERSION:
        raise ValueError("atomic-quality artifact must attest model-quality metrics")

    if efficiency_ids:
        if schema_version != EFFICIENCY_ATTESTATION_SCHEMA_VERSION:
            raise ValueError("formal efficiency metrics require an efficiency artifact")
        if artifact_measurements - EFFICIENCY_METRIC_IDS:
            raise ValueError("efficiency artifact cannot attest unrelated measurements")
        if "M22" in efficiency_ids and artifact.get("scope") != "production-shadow":
            raise ValueError("formal M22 requires production-shadow evidence")
        if "M23" in efficiency_ids:
            if artifact.get("model_called") is not True:
                raise ValueError("formal M23 requires real model terminal jobs")
            if artifact.get("terminal_jobs_complete") is not True:
                raise ValueError("formal M23 requires all model jobs to be terminal")
            assert source_result is not None
            if (
                source_result["run_id"] != run["run_id"]
                or source_result["dataset_id"] != dataset["id"]
                or source_result["system_version"] != system["version"]
                or source_result["system_source_file_count"] != system["source_file_count"]
            ):
                raise ValueError("formal efficiency source identity binding mismatch")
    elif schema_version == EFFICIENCY_ATTESTATION_SCHEMA_VERSION:
        raise ValueError("efficiency artifact must attest M22 or M23")

    if schema_version == LIFECYCLE_ATTESTATION_SCHEMA_VERSION:
        assert source_result is not None
        if artifact_measurements != {
            "G05",
            "G06",
            "M15",
            "M16",
            "M17",
        }:
            raise ValueError("lifecycle artifact has invalid measurement coverage")
        if (
            source_result["run_id"] != run["run_id"]
            or source_result["dataset_id"] != dataset["id"]
            or source_result["system"]["version"] != system["version"]
            or source_result["system"]["source_file_count"] != system["source_file_count"]
        ):
            raise ValueError("formal lifecycle source identity binding mismatch")
        if artifact.get("model_called") is not False:
            raise ValueError("lifecycle artifact must not claim a model call")

    if schema_version == RECALL_ATTESTATION_SCHEMA_VERSION:
        assert source_result is not None
        if artifact_measurements != {"G02", "M05", "M06", "M08", "M21"}:
            raise ValueError("recall artifact has invalid measurement coverage")
        if (
            source_result["run_id"] != run["run_id"]
            or source_result["dataset_id"] != dataset["id"]
            or source_result["system"]["version"] != system["version"]
            or source_result["system"]["source_file_count"] != system["source_file_count"]
        ):
            raise ValueError("formal recall source identity binding mismatch")
        if artifact.get("model_called") is not False:
            raise ValueError("recall artifact must not claim a model call")
        if artifact.get("scope") != "isolated-recall":
            raise ValueError("recall artifact requires isolated-recall scope")

    if schema_version == EVIDENCE_ATTESTATION_SCHEMA_VERSION:
        assert source_result is not None
        if artifact_measurements != {"G01", "G03", "G09"}:
            raise ValueError("evidence artifact has invalid measurement coverage")
        if (
            source_result["run_id"] != run["run_id"]
            or source_result["dataset_id"] != dataset["id"]
            or source_result["system"]["version"] != system["version"]
            or source_result["system"]["source_file_count"] != system["source_file_count"]
        ):
            raise ValueError("formal evidence source identity binding mismatch")
        if artifact.get("model_called") is not False:
            raise ValueError("evidence artifact must not claim a model call")
        if artifact.get("scope") != "isolated-evidence":
            raise ValueError("evidence artifact requires isolated-evidence scope")

    if schema_version == EPISODE_PROCEDURE_ATTESTATION_SCHEMA_VERSION:
        assert source_result is not None
        if artifact_measurements != {
            "G04",
            "G08",
            "M04",
            "M09",
            "M10",
            "M11",
            "M12",
            "M13",
            "M14",
        }:
            raise ValueError("episode/procedure artifact has invalid measurement coverage")
        if (
            source_result["run_id"] != run["run_id"]
            or source_result["dataset_id"] != dataset["id"]
            or source_result["system"]["version"] != system["version"]
            or source_result["system"]["source_file_count"] != system["source_file_count"]
        ):
            raise ValueError("formal episode/procedure source identity binding mismatch")
        if artifact.get("model_called") is not False:
            raise ValueError("episode/procedure artifact must not claim a model call")
        if artifact.get("scope") != "isolated-episode-procedure":
            raise ValueError("episode/procedure artifact requires isolated-episode-procedure scope")

    if schema_version == RELIABILITY_ATTESTATION_SCHEMA_VERSION:
        assert source_result is not None
        if artifact_measurements != {"G07", "G10", "M18", "M19", "M20"}:
            raise ValueError("reliability artifact has invalid measurement coverage")
        if (
            source_result["run_id"] != run["run_id"]
            or source_result["dataset_id"] != dataset["id"]
            or source_result["system"]["version"] != system["version"]
            or source_result["system"]["source_file_count"] != system["source_file_count"]
        ):
            raise ValueError("formal reliability source identity binding mismatch")
        if artifact.get("model_called") is not False:
            raise ValueError("reliability artifact must not claim a model call")
        if artifact.get("scope") != "isolated-reliability":
            raise ValueError("reliability artifact requires isolated-reliability scope")

    if (
        not quality_ids
        and not efficiency_ids
        and schema_version
        not in {
            LIFECYCLE_ATTESTATION_SCHEMA_VERSION,
            RECALL_ATTESTATION_SCHEMA_VERSION,
            EVIDENCE_ATTESTATION_SCHEMA_VERSION,
            EPISODE_PROCEDURE_ATTESTATION_SCHEMA_VERSION,
            RELIABILITY_ATTESTATION_SCHEMA_VERSION,
        }
    ):
        raise ValueError(f"attestation artifact {artifact_id} is ineligible for its measurements")

    allowed_keys = {
        "blind",
        "contains_memory_text",
        "dataset_manifest_sha256",
        "dataset_visibility",
        "image_platform",
        "image_reference",
        "measurement_ids",
        "measurements",
        "model_called",
        "producer",
        "schema_version",
        "scope",
        "source_artifact_base64",
        "source_artifact_schema_version",
        "source_artifact_sha256",
        "system_environment_sha256",
        "system_revision",
        "system_source_sha256",
        "track",
    }
    if quality_ids or efficiency_ids:
        allowed_keys.add("execution_plan_sha256")
    if "M23" in efficiency_ids:
        allowed_keys.add("terminal_jobs_complete")
    unknown_keys = set(artifact) - allowed_keys
    if unknown_keys:
        raise ValueError(
            f"attestation artifact {artifact_id} has unknown fields: "
            + ", ".join(sorted(unknown_keys))
        )
    return artifact_measurements


def _validate_formal_attestation(
    run: dict[str, Any],
    *,
    supplied_gates: dict[str, Any],
    supplied_metrics: dict[str, Any],
    artifact_payloads: dict[str, bytes] | None,
) -> None:
    attestation = run.get("attestation")
    if not isinstance(attestation, dict):
        raise ValueError("formal run requires an attestation object")
    multi_dataset = run.get("schema_version") == MULTI_DATASET_RUN_SCHEMA_VERSION
    dataset_binding_key = (
        "dataset_manifest_sha256s" if multi_dataset else "dataset_manifest_sha256"
    )
    required_attestation_keys = {
        "artifacts",
        "claim",
        dataset_binding_key,
        "image_platform",
        "image_reference",
        "run_payload_sha256",
        "schema_version",
        "system_environment_sha256",
        "system_revision",
        "system_source_sha256",
        "track",
    }
    if set(attestation) != required_attestation_keys:
        raise ValueError("formal run attestation has an invalid schema")
    expected_attestation_schema = (
        MULTI_DATASET_ATTESTATION_SCHEMA_VERSION
        if multi_dataset
        else ATTESTATION_SCHEMA_VERSION
    )
    if attestation.get("schema_version") != expected_attestation_schema:
        raise ValueError("unsupported formal run attestation schema")
    if attestation.get("claim") != "OFFICIAL_AM_EVAL_RUN":
        raise ValueError("formal run attestation must claim OFFICIAL_AM_EVAL_RUN")
    if attestation.get("run_payload_sha256") != formal_run_payload_sha256(run):
        raise ValueError("formal run attestation payload SHA-256 mismatch")

    datasets = _run_datasets(run)
    bindings: dict[str, object] = {
        "system_environment_sha256": run["system"]["environment_sha256"],
        "system_revision": run["system"]["revision"],
        "system_source_sha256": run["system"]["source_sha256"],
        dataset_binding_key: (
            sorted(item["sha256"] for item in datasets)
            if multi_dataset
            else datasets[0]["sha256"]
        ),
        "track": run["track"],
        "image_reference": (
            f"{run['execution_artifact']['image_name']}@"
            f"{run['execution_artifact']['manifest_digest']}"
        ),
        "image_platform": run["execution_artifact"]["platform"],
    }
    for key, expected in bindings.items():
        if attestation.get(key) != expected:
            raise ValueError(f"formal run attestation {key} binding mismatch")

    artifacts = attestation.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("formal run attestation requires measurement artifacts")
    if artifact_payloads is None:
        raise ValueError("formal run requires actual artifact payloads")
    if not isinstance(artifact_payloads, dict) or set(artifact_payloads) != set(artifacts):
        raise ValueError("formal run artifact payload IDs must exactly match the attestation")
    supplied_measurements = {**supplied_gates, **supplied_metrics}
    supplied_ids = set(supplied_measurements)
    covered_ids: set[str] = set()
    covered_dataset_sha256s: set[str] = set()
    measurement_artifact: dict[str, str] = {}
    for artifact_id, artifact in artifacts.items():
        _require_non_empty_string(artifact_id, label="attestation artifact id")
        artifact_payload = artifact_payloads.get(artifact_id)
        artifact_measurements = _validate_artifact(
            artifact_id,
            artifact,
            artifact_payload,
            run=run,
            supplied_measurements=supplied_measurements,
        )
        assert isinstance(artifact_payload, bytes)
        covered_dataset_sha256s.add(
            str(_decode_artifact(artifact_id, artifact_payload)["dataset_manifest_sha256"])
        )
        overlap = covered_ids & artifact_measurements
        if overlap:
            raise ValueError(
                "formal run measurements must have exactly one attestation artifact: "
                + ", ".join(sorted(overlap))
            )
        covered_ids.update(artifact_measurements)
        measurement_artifact.update({item_id: artifact_id for item_id in artifact_measurements})
    if covered_ids != supplied_ids:
        missing = ", ".join(sorted(supplied_ids - covered_ids))
        raise ValueError(f"formal run attestation does not cover measurements: {missing}")
    if multi_dataset and covered_dataset_sha256s != {item["sha256"] for item in datasets}:
        raise ValueError("formal run attestation does not use every declared dataset")

    for item_id, measurement in {**supplied_gates, **supplied_metrics}.items():
        if not isinstance(measurement, dict):
            raise ValueError(f"measurement {item_id} must be an object")
        if measurement.get("evidence") != [measurement_artifact[item_id]]:
            raise ValueError(
                f"measurement {item_id} must reference its single attestation artifact"
            )


def _compare(operator: str, value: float, threshold: float) -> bool:
    if operator == "eq":
        return value == threshold
    if operator == "lte":
        return value <= threshold
    if operator == "gte":
        return value >= threshold
    raise ValueError(f"unsupported gate operator: {operator}")


def _metric_score(rule: dict[str, Any], value: float) -> float:
    mode = str(rule["mode"])
    target = float(rule["target"])
    if mode == "higher":
        if target <= 0:
            raise ValueError("higher metric target must be positive")
        return min(100.0, max(0.0, value / target * 100.0))
    if mode == "lower":
        zero_score_at = float(rule["zero_score_at"])
        if zero_score_at <= target:
            raise ValueError("lower metric zero_score_at must exceed target")
        if value <= target:
            return 100.0
        if value >= zero_score_at:
            return 0.0
        return (zero_score_at - value) / (zero_score_at - target) * 100.0
    if mode == "exact":
        return 100.0 if value == target else 0.0
    raise ValueError(f"unsupported metric scoring mode: {mode}")


def _measurement_value(rule: dict[str, Any], measurement: dict[str, Any], item_id: str) -> float:
    raw_value = measurement.get("value")
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ValueError(f"measurement {item_id} value must be a number")
    value = float(raw_value)
    if not math.isfinite(value):
        raise ValueError(f"measurement {item_id} must be finite")
    if "minimum" in rule and value < float(rule["minimum"]):
        raise ValueError(f"measurement {item_id} is below its minimum")
    if "maximum" in rule and value > float(rule["maximum"]):
        raise ValueError(f"measurement {item_id} exceeds its maximum")
    return value


def evaluate_run(
    spec: dict[str, Any],
    run: dict[str, Any],
    *,
    artifact_payloads: dict[str, bytes] | None = None,
    confirm_spec_sha256: str | None = None,
    confirm_image_reference: str | None = None,
    confirm_image_platform: str | None = None,
) -> dict[str, Any]:
    benchmark_id = str(spec["benchmark_id"])
    if run.get("benchmark_id") != benchmark_id:
        raise ValueError("run benchmark_id does not match the specification")

    run_schema = run.get("schema_version")
    if run_schema not in {None, RUN_SCHEMA_VERSION, MULTI_DATASET_RUN_SCHEMA_VERSION}:
        raise ValueError("unsupported AM-Eval run schema")
    formal = run_schema in {RUN_SCHEMA_VERSION, MULTI_DATASET_RUN_SCHEMA_VERSION}
    specification_identity: dict[str, str] | None = None
    if formal:
        specification_identity = _validate_official_specification(
            spec,
            confirm_spec_sha256=confirm_spec_sha256,
        )
        required_run_keys = {
            "attestation",
            "benchmark_id",
            "execution_artifact",
            "hard_gates",
            "metrics",
            "run_id",
            "schema_version",
            "system",
            "track",
        }
        required_run_keys.add(
            "datasets" if run_schema == MULTI_DATASET_RUN_SCHEMA_VERSION else "dataset"
        )
        if set(run) not in (required_run_keys, required_run_keys | {"notes"}):
            raise ValueError("formal AM-Eval run has an invalid schema")
        notes = run.get("notes", [])
        if not isinstance(notes, list) or not all(
            isinstance(item, str) and item.strip() for item in notes
        ):
            raise ValueError("formal AM-Eval run notes must be non-empty strings")
    _validate_run_identity(run, formal=formal)
    scorer_identity: dict[str, Any] | None = None
    scorer_environment: dict[str, Any] | None = None
    if formal:
        execution_artifact = _validate_execution_artifact(run["execution_artifact"])
        expected_image_reference = (
            f"{execution_artifact['image_name']}@{execution_artifact['manifest_digest']}"
        )
        if confirm_image_reference != expected_image_reference:
            raise ValueError("formal run OCI image reference confirmation mismatch")
        if confirm_image_platform != execution_artifact["platform"]:
            raise ValueError("formal run OCI platform confirmation mismatch")
        scorer_identity, scorer_environment = _validate_formal_scorer_identity(run)

    gate_rules = {str(item["id"]): item for item in spec["hard_gates"]}
    metric_rules = {str(item["id"]): item for item in spec["metrics"]}
    supplied_gates = run.get("hard_gates", {})
    supplied_metrics = run.get("metrics", {})
    if not isinstance(supplied_gates, dict) or not isinstance(supplied_metrics, dict):
        raise ValueError("run hard_gates and metrics must be objects")
    unknown_gates = sorted(set(supplied_gates) - set(gate_rules))
    unknown_metrics = sorted(set(supplied_metrics) - set(metric_rules))
    if unknown_gates:
        raise ValueError(f"unknown hard gate measurements: {', '.join(unknown_gates)}")
    if unknown_metrics:
        raise ValueError(f"unknown metric measurements: {', '.join(unknown_metrics)}")
    if formal:
        for item_id, measurement in {**supplied_gates, **supplied_metrics}.items():
            if not isinstance(measurement, dict) or set(measurement) != {
                "evidence",
                "sample_count",
                "value",
            }:
                raise ValueError(f"formal measurement {item_id} has an invalid schema")
        _validate_formal_attestation(
            run,
            supplied_gates=supplied_gates,
            supplied_metrics=supplied_metrics,
            artifact_payloads=artifact_payloads,
        )
    else:
        for item_id, measurement in {**supplied_gates, **supplied_metrics}.items():
            if not isinstance(measurement, dict):
                raise ValueError(f"measurement {item_id} must be an object")
            _validate_legacy_evidence(measurement, item_id=item_id)

    gate_results: list[dict[str, Any]] = []
    for gate_id, rule in gate_rules.items():
        measurement = supplied_gates.get(gate_id)
        if measurement is None:
            gate_results.append(
                {
                    "id": gate_id,
                    "name": rule["name"],
                    "status": "not_measured",
                    "required": bool(rule.get("required", True)),
                }
            )
            continue
        if not isinstance(measurement, dict):
            raise ValueError(f"measurement {gate_id} must be an object")
        sample_count = measurement.get("sample_count", 0)
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
            raise ValueError(f"hard gate {gate_id} requires a positive sample_count")
        value = _measurement_value(rule, measurement, gate_id)
        passed = _compare(str(rule["operator"]), value, float(rule["threshold"]))
        gate_results.append(
            {
                "id": gate_id,
                "name": rule["name"],
                "status": "pass" if passed else "fail",
                "required": bool(rule.get("required", True)),
                "value": value,
                "sample_count": sample_count,
                "evidence": list(measurement.get("evidence", [])),
            }
        )

    total_weight = sum(float(item["weight"]) for item in metric_rules.values())
    if round(total_weight, 8) != 100.0:
        raise ValueError(f"metric weights must total 100, got {total_weight}")

    metric_results: list[dict[str, Any]] = []
    measured_weight = 0.0
    weighted_points = 0.0
    for metric_id, rule in metric_rules.items():
        measurement = supplied_metrics.get(metric_id)
        weight = float(rule["weight"])
        if measurement is None:
            metric_results.append(
                {
                    "id": metric_id,
                    "name": rule["name"],
                    "dimension": rule["dimension"],
                    "weight": weight,
                    "status": "not_measured",
                    "required": bool(rule.get("required", True)),
                }
            )
            continue
        if not isinstance(measurement, dict):
            raise ValueError(f"measurement {metric_id} must be an object")
        sample_count = measurement.get("sample_count", 0)
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
            raise ValueError(f"metric {metric_id} requires a positive sample_count")
        value = _measurement_value(rule, measurement, metric_id)
        score = _metric_score(dict(rule["scoring"]), value)
        measured_weight += weight
        weighted_points += weight * score / 100.0
        metric_results.append(
            {
                "id": metric_id,
                "name": rule["name"],
                "dimension": rule["dimension"],
                "weight": weight,
                "status": "measured",
                "required": bool(rule.get("required", True)),
                "value": value,
                "sample_count": sample_count,
                "score": round(score, 4),
                "evidence": list(measurement.get("evidence", [])),
            }
        )

    failed_gates = [item["id"] for item in gate_results if item["status"] == "fail"]
    missing_required_gates = [
        item["id"] for item in gate_results if item["required"] and item["status"] == "not_measured"
    ]
    missing_required_metrics = [
        item["id"]
        for item in metric_results
        if item["required"] and item["status"] == "not_measured"
    ]
    measured_score = weighted_points / measured_weight * 100.0 if measured_weight else 0.0
    minimum_score = float(spec["release_policy"]["minimum_score"])
    if failed_gates:
        decision = "HARD_GATE_FAILED"
    elif missing_required_gates or missing_required_metrics:
        decision = "INCOMPLETE"
    elif measured_score < minimum_score:
        decision = "QUALITY_BELOW_THRESHOLD"
    elif not formal:
        decision = "ATTESTATION_REQUIRED"
    else:
        decision = "PASS"

    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "benchmark_id": benchmark_id,
        "run_id": run["run_id"],
        "system": run["system"],
        "track": run["track"],
        "specification": specification_identity,
        "execution_artifact": run.get("execution_artifact"),
        "decision": decision,
        "release_ready": decision == "PASS",
        "attestation": {
            "schema_version": (
                MULTI_DATASET_ATTESTATION_SCHEMA_VERSION
                if run_schema == MULTI_DATASET_RUN_SCHEMA_VERSION
                else ATTESTATION_SCHEMA_VERSION
                if formal
                else "legacy-unattested"
            ),
            "status": "verified" if formal else "unattested",
        },
        "hard_gate_summary": {
            "passed": sum(item["status"] == "pass" for item in gate_results),
            "failed": len(failed_gates),
            "not_measured": sum(item["status"] == "not_measured" for item in gate_results),
            "failed_ids": failed_gates,
            "missing_required_ids": missing_required_gates,
        },
        "quality_summary": {
            "measured_score": round(measured_score, 4),
            "coverage_percent": round(measured_weight, 4),
            "weighted_points": round(weighted_points, 4),
            "minimum_score": minimum_score,
            "missing_required_ids": missing_required_metrics,
        },
        "hard_gates": gate_results,
        "metrics": metric_results,
        "notes": list(run.get("notes", [])),
    }
    if run_schema == MULTI_DATASET_RUN_SCHEMA_VERSION:
        result["datasets"] = run["datasets"]
    else:
        result["dataset"] = run["dataset"]
    if scorer_identity is not None and scorer_environment is not None:
        result["scorer_runtime_identity"] = scorer_identity
        result["scorer_runtime_environment"] = scorer_environment
    return result


def render_markdown(result: dict[str, Any]) -> str:
    quality = result["quality_summary"]
    gates = result["hard_gate_summary"]
    lines = [
        f"# {result['benchmark_id']} evaluation result",
        "",
        f"- Run: `{result['run_id']}`",
        f"- System: `{result['system']['name']} {result['system']['version']}`",
        f"- Revision: `{result['system']['revision']}`",
        f"- Track: `{result['track']}`",
        f"- Decision: `{result['decision']}`",
        f"- Measured score: `{quality['measured_score']:.2f}`",
        f"- Coverage: `{quality['coverage_percent']:.2f}%`",
        (
            f"- Hard gates: `{gates['passed']} passed / {gates['failed']} failed / "
            f"{gates['not_measured']} not measured`"
        ),
        "",
        "## Hard gates",
        "",
        "| ID | Gate | Status | Value |",
        "| --- | --- | --- | ---: |",
    ]
    for item in result["hard_gates"]:
        lines.append(
            f"| {item['id']} | {item['name']} | {item['status']} | {item.get('value', '—')} |"
        )
    lines.extend(
        [
            "",
            "## Quality metrics",
            "",
            "| ID | Dimension | Metric | Status | Value | Score | Weight |",
            "| --- | --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for item in result["metrics"]:
        lines.append(
            f"| {item['id']} | {item['dimension']} | {item['name']} | "
            f"{item['status']} | {item.get('value', '—')} | "
            f"{item.get('score', '—')} | {item['weight']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a run against an AM-Eval spec.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("run", type=Path)
    parser.add_argument("--confirm-spec-sha256", required=True)
    parser.add_argument("--confirm-run-sha256", required=True)
    parser.add_argument("--confirm-image-reference")
    parser.add_argument("--confirm-image-platform", choices=("linux/amd64", "linux/arm64"))
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="Actual formal evidence artifact; repeat once for every attested artifact ID.",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    arguments = parser.parse_args()
    try:
        spec = _load_confirmed_json(
            arguments.spec,
            arguments.confirm_spec_sha256,
            label="AM-Eval specification",
        )
        run = _load_confirmed_json(
            arguments.run,
            arguments.confirm_run_sha256,
            label="AM-Eval run",
        )
        artifact_payloads: dict[str, bytes] = {}
        for entry in arguments.artifact:
            artifact_id, separator, artifact_path = entry.partition("=")
            if (
                not separator
                or not artifact_id
                or not artifact_path
                or artifact_id in artifact_payloads
            ):
                parser.error("each --artifact must be a unique non-empty ID=PATH")
            artifact_payloads[artifact_id] = read_file_snapshot(Path(artifact_path)).payload
        result = evaluate_run(
            spec,
            run,
            artifact_payloads=artifact_payloads or None,
            confirm_spec_sha256=arguments.confirm_spec_sha256.casefold(),
            confirm_image_reference=arguments.confirm_image_reference,
            confirm_image_platform=arguments.confirm_image_platform,
        )
    except (DatasetError, ValueError) as error:
        parser.error(str(error))
    if arguments.format == "markdown":
        print(render_markdown(result), end="")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
