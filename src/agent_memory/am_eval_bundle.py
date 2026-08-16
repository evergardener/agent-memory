from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .am_eval import (
    MULTI_DATASET_ATTESTATION_SCHEMA_VERSION,
    MULTI_DATASET_RUN_SCHEMA_VERSION,
    formal_run_payload_sha256,
)
from .am_eval_atomic_runner import (
    discover_runtime_source_root,
    validate_private_output,
    write_private_json,
)
from .am_eval_attestation import (
    EPISODE_PROCEDURE_ATTESTATION_SCHEMA_VERSION,
    EVIDENCE_ATTESTATION_SCHEMA_VERSION,
    LIFECYCLE_ATTESTATION_SCHEMA_VERSION,
    RECALL_ATTESTATION_SCHEMA_VERSION,
    RELIABILITY_ATTESTATION_SCHEMA_VERSION,
    validate_attested_source,
)
from .am_eval_dataset import DatasetError, read_file_snapshot
from .am_eval_formal_contract import OFFICIAL_EXECUTION_IMAGE_NAMES

DETERMINISTIC_ARTIFACT_SCHEMAS = {
    "episode-procedure": EPISODE_PROCEDURE_ATTESTATION_SCHEMA_VERSION,
    "evidence": EVIDENCE_ATTESTATION_SCHEMA_VERSION,
    "lifecycle": LIFECYCLE_ATTESTATION_SCHEMA_VERSION,
    "recall": RECALL_ATTESTATION_SCHEMA_VERSION,
    "reliability": RELIABILITY_ATTESTATION_SCHEMA_VERSION,
}
DETERMINISTIC_GATE_IDS = frozenset(f"G{index:02d}" for index in range(1, 11))
DETERMINISTIC_METRIC_IDS = frozenset(
    {"M04", "M05", "M06", "M08"}
    | {f"M{index:02d}" for index in range(9, 22)}
)


@dataclass(frozen=True)
class ArtifactRecord:
    artifact: dict[str, Any]
    payload: bytes
    sha256: str
    source: dict[str, Any]


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


def load_artifact_record(kind: str, path: Path) -> ArtifactRecord:
    if kind not in DETERMINISTIC_ARTIFACT_SCHEMAS:
        raise DatasetError("unsupported deterministic artifact kind")
    snapshot = read_file_snapshot(path)
    artifact = _strict_json(snapshot.payload, label=f"{kind} attestation")
    if artifact.get("schema_version") != DETERMINISTIC_ARTIFACT_SCHEMAS[kind]:
        raise DatasetError(f"{kind} attestation schema does not match its kind")
    source = validate_attested_source(artifact)
    return ArtifactRecord(
        artifact=artifact,
        payload=snapshot.payload,
        sha256=snapshot.sha256,
        source=source,
    )


def _require_same(records: dict[str, ArtifactRecord], getter, *, label: str) -> Any:
    values = {json.dumps(getter(record), sort_keys=True) for record in records.values()}
    if len(values) != 1:
        raise DatasetError(f"deterministic artifacts do not share the same {label}")
    return getter(next(iter(records.values())))


def _execution_artifact(image_reference: str, image_platform: str) -> dict[str, str]:
    image_name, separator, manifest_digest = image_reference.rpartition("@")
    if (
        not separator
        or not image_name
        or image_name not in OFFICIAL_EXECUTION_IMAGE_NAMES
        or ":" in image_name.rsplit("/", maxsplit=1)[-1]
        or not manifest_digest.startswith("sha256:")
        or len(manifest_digest) != 71
        or any(character not in "0123456789abcdef" for character in manifest_digest[7:])
    ):
        raise DatasetError(
            "deterministic run requires an exact untagged OCI image reference from the "
            "registered Agent Memory repository"
        )
    if image_platform not in {"linux/amd64", "linux/arm64"}:
        raise DatasetError("deterministic run requires a supported OCI platform")
    return {
        "type": "oci-image",
        "image_name": image_name,
        "manifest_digest": manifest_digest,
        "platform": image_platform,
    }


def assemble_deterministic_run(
    records: dict[str, ArtifactRecord],
    *,
    benchmark_id: str,
    confirm_run_id: str,
    confirm_track: str,
    confirm_image_reference: str,
    confirm_image_platform: str,
) -> dict[str, Any]:
    if set(records) != set(DETERMINISTIC_ARTIFACT_SCHEMAS):
        raise DatasetError("deterministic run requires exactly five sourced artifact kinds")
    if not benchmark_id.strip() or benchmark_id != benchmark_id.strip():
        raise DatasetError("deterministic run requires a benchmark ID")

    run_id = _require_same(records, lambda record: record.source["run_id"], label="run ID")
    system = _require_same(records, lambda record: record.source["system"], label="system identity")
    track = _require_same(records, lambda record: record.artifact["track"], label="track")
    image_reference = _require_same(
        records,
        lambda record: record.artifact["image_reference"],
        label="image reference",
    )
    image_platform = _require_same(
        records,
        lambda record: record.artifact["image_platform"],
        label="image platform",
    )
    environment_sha256 = _require_same(
        records,
        lambda record: record.artifact["system_environment_sha256"],
        label="runtime environment",
    )
    if (
        run_id != confirm_run_id
        or track != confirm_track
        or image_reference != confirm_image_reference
        or image_platform != confirm_image_platform
    ):
        raise DatasetError("deterministic run confirmation does not match its artifacts")
    if system.get("environment_sha256") != environment_sha256:
        raise DatasetError("deterministic artifact environment differs from its source system")

    execution_artifact = _execution_artifact(image_reference, image_platform)
    datasets = sorted(
        (
            {
                "id": record.source["dataset_id"],
                "sha256": record.artifact["dataset_manifest_sha256"],
                "visibility": record.source["dataset_visibility"],
                "blind": record.source["dataset_blind"],
            }
            for record in records.values()
        ),
        key=lambda item: item["id"],
    )
    if len({item["id"] for item in datasets}) != len(datasets):
        raise DatasetError("deterministic artifacts require unique dataset IDs")
    if len({item["sha256"] for item in datasets}) != len(datasets):
        raise DatasetError("deterministic artifacts require unique dataset manifests")

    measurements: dict[str, dict[str, Any]] = {}
    for kind, record in sorted(records.items()):
        artifact_measurements = record.artifact["measurements"]
        overlap = set(measurements) & set(artifact_measurements)
        if overlap:
            raise DatasetError(
                "deterministic artifacts overlap measurements: " + ", ".join(sorted(overlap))
            )
        measurements.update(
            {
                item_id: {**value, "evidence": [kind]}
                for item_id, value in artifact_measurements.items()
            }
        )
    supplied_ids = set(measurements)
    expected_ids = DETERMINISTIC_GATE_IDS | DETERMINISTIC_METRIC_IDS
    if supplied_ids != expected_ids:
        raise DatasetError("deterministic artifacts do not provide the exact expected coverage")

    run: dict[str, Any] = {
        "schema_version": MULTI_DATASET_RUN_SCHEMA_VERSION,
        "benchmark_id": benchmark_id,
        "run_id": run_id,
        "system": system,
        "track": track,
        "datasets": datasets,
        "execution_artifact": execution_artifact,
        "hard_gates": {
            item_id: measurements[item_id] for item_id in sorted(DETERMINISTIC_GATE_IDS)
        },
        "metrics": {
            item_id: measurements[item_id] for item_id in sorted(DETERMINISTIC_METRIC_IDS)
        },
        "attestation": {
            "schema_version": MULTI_DATASET_ATTESTATION_SCHEMA_VERSION,
            "claim": "OFFICIAL_AM_EVAL_RUN",
            "system_environment_sha256": system["environment_sha256"],
            "system_revision": system["revision"],
            "system_source_sha256": system["source_sha256"],
            "dataset_manifest_sha256s": sorted(item["sha256"] for item in datasets),
            "track": track,
            "image_reference": image_reference,
            "image_platform": image_platform,
            "artifacts": {
                kind: {"sha256": record.sha256} for kind, record in sorted(records.items())
            },
        },
        "notes": [
            "Deterministic sourced partial run; external model quality and production shadow "
            "metrics remain unmeasured.",
            "Expected missing metrics: M01, M02, M03, M07, M22, M23.",
        ],
    }
    run["attestation"]["run_payload_sha256"] = formal_run_payload_sha256(run)
    return run


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble five deterministic sourced artifacts into a formal partial AM-Eval run."
        )
    )
    parser.add_argument(
        "--artifact",
        action="append",
        required=True,
        metavar="KIND=PATH",
    )
    parser.add_argument("--benchmark-id", default="am-eval-v1")
    parser.add_argument("--confirm-run-id", required=True)
    parser.add_argument("--confirm-track", required=True)
    parser.add_argument("--confirm-image-reference", required=True)
    parser.add_argument(
        "--confirm-image-platform",
        required=True,
        choices=("linux/amd64", "linux/arm64"),
    )
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    output_target = None
    try:
        paths: dict[str, Path] = {}
        for entry in arguments.artifact:
            kind, separator, path = entry.partition("=")
            if not separator or not kind or not path or kind in paths:
                raise DatasetError("each --artifact must be a unique non-empty KIND=PATH")
            paths[kind] = Path(path)
        records = {kind: load_artifact_record(kind, path) for kind, path in paths.items()}
        run = assemble_deterministic_run(
            records,
            benchmark_id=arguments.benchmark_id,
            confirm_run_id=arguments.confirm_run_id,
            confirm_track=arguments.confirm_track,
            confirm_image_reference=arguments.confirm_image_reference,
            confirm_image_platform=arguments.confirm_image_platform,
        )
        output_target = validate_private_output(
            arguments.output,
            forbidden_root=discover_runtime_source_root(),
        )
        write_private_json(output_target, run)
    except (DatasetError, KeyError, TypeError, ValueError) as error:
        if output_target is not None:
            output_target.close()
        parser.error(str(error))
    print(
        json.dumps(
            {
                "status": "PASS",
                "run_id": run["run_id"],
                "artifact_count": len(run["attestation"]["artifacts"]),
                "dataset_count": len(run["datasets"]),
                "hard_gate_count": len(run["hard_gates"]),
                "metric_count": len(run["metrics"]),
                "output": str(arguments.output.expanduser().resolve()),
                "sha256": hashlib.sha256(
                    json.dumps(
                        run,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n"
                ).hexdigest(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
