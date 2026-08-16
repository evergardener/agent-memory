from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

from .am_eval_atomic_runner import (
    discover_runtime_source_root,
    validate_atomic_run_ledger,
    validate_private_output,
    write_private_json,
)
from .am_eval_dataset import DatasetError, read_file_snapshot
from .am_eval_environment import validate_runtime_environment_identity
from .am_eval_lifecycle import (
    DATASET_ID as LIFECYCLE_DATASET_ID,
)
from .am_eval_lifecycle import (
    EXPECTED_CASE_COUNT as LIFECYCLE_CASE_COUNT,
)
from .am_eval_lifecycle import (
    EXPECTED_MANIFEST_SHA256 as LIFECYCLE_MANIFEST_SHA256,
)
from .am_eval_lifecycle import (
    REQUIRED_ACTION_COUNTS as LIFECYCLE_ACTION_COUNTS,
)
from .am_eval_lifecycle import (
    REQUIRED_INVARIANT_COUNTS as LIFECYCLE_INVARIANT_COUNTS,
)
from .am_eval_recall import (
    DATASET_ID as RECALL_DATASET_ID,
)
from .am_eval_recall import (
    EXPECTED_MANIFEST_SHA256 as RECALL_MANIFEST_SHA256,
)
from .am_eval_recall import (
    EXPECTED_NAMESPACE_PROBE_COUNT as RECALL_NAMESPACE_PROBE_COUNT,
)
from .am_eval_recall import (
    EXPECTED_NEGATIVE_COUNT as RECALL_NEGATIVE_COUNT,
)
from .am_eval_recall import (
    EXPECTED_POSITIVE_COUNT as RECALL_POSITIVE_COUNT,
)
from .am_eval_recall import (
    EXPECTED_QUERY_COUNT as RECALL_QUERY_COUNT,
)
from .am_eval_recall import (
    RECALL_RESULT_SCHEMA_VERSION,
)

ASSEMBLER_NAME = "agent-memory-am-eval-attestation-assembler"
QUALITY_ATTESTATION_SCHEMA_VERSION = "am-eval-atomic-quality-attestation-v2"
EFFICIENCY_ATTESTATION_SCHEMA_VERSION = "am-eval-efficiency-attestation-v2"
LIFECYCLE_ATTESTATION_SCHEMA_VERSION = "am-eval-lifecycle-attestation-v2"
RECALL_ATTESTATION_SCHEMA_VERSION = "am-eval-recall-attestation-v1"
QUALITY_RESULT_SCHEMA_VERSION = "am-eval-atomic-quality-result-v3"
EFFICIENCY_RESULT_SCHEMA_VERSION = "am-eval-efficiency-result-v5"
LIFECYCLE_RESULT_SCHEMA_VERSION = "am-eval-lifecycle-run-v2"
QUALITY_METRIC_IDS = frozenset({"M01", "M02", "M03", "M07"})
LIFECYCLE_MEASUREMENT_IDS = frozenset({"G05", "G06", "M15", "M16", "M17"})
RECALL_MEASUREMENT_IDS = frozenset({"G02", "M05", "M06", "M08", "M21"})
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


def lifecycle_measurements(payload: dict[str, Any]) -> dict[str, dict[str, float | int]]:
    invariant_counts = payload["invariant_pass_counts"]
    namespace_expected = LIFECYCLE_INVARIANT_COUNTS["namespace_denied"]
    current_expected = LIFECYCLE_INVARIANT_COUNTS["current_hidden"]
    purge_expected = LIFECYCLE_INVARIANT_COUNTS["purge_residue_zero"]
    purge_cases = sum(item["action"] == "purge" for item in payload["cases"])
    purge_passed = sum(
        item["action"] == "purge" and item["status"] == "PASS" for item in payload["cases"]
    )
    return {
        "G05": {
            "sample_count": current_expected,
            "value": current_expected - invariant_counts["current_hidden"],
        },
        "G06": {
            "sample_count": purge_expected,
            "value": purge_expected - invariant_counts["purge_residue_zero"],
        },
        "M15": {
            "sample_count": payload["case_count"],
            "value": payload["passed"] / payload["case_count"],
        },
        "M16": {
            "sample_count": purge_cases,
            "value": purge_passed / purge_cases,
        },
        "M17": {
            "sample_count": namespace_expected,
            "value": invariant_counts["namespace_denied"] / namespace_expected,
        },
    }


def validate_lifecycle_result(payload: object) -> dict[str, Any]:
    required_keys = {
        "action_counts",
        "case_count",
        "cases",
        "contains_memory_text",
        "contains_production_data",
        "dataset_blind",
        "dataset_id",
        "dataset_validation",
        "dataset_visibility",
        "external_data_sent",
        "failed",
        "invariant_pass_counts",
        "manifest_sha256",
        "model_called",
        "passed",
        "run_id",
        "runner_runtime_environment",
        "runner_runtime_identity",
        "schema_version",
        "status",
        "system",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required_keys
        or payload.get("schema_version") != LIFECYCLE_RESULT_SCHEMA_VERSION
    ):
        raise DatasetError("lifecycle result has an invalid schema")
    if (
        payload.get("dataset_id") != LIFECYCLE_DATASET_ID
        or payload.get("manifest_sha256") != LIFECYCLE_MANIFEST_SHA256
        or payload.get("dataset_visibility") != "open"
        or payload.get("dataset_blind") is not False
        or payload.get("dataset_validation") != "PASS"
        or payload.get("contains_memory_text") is not False
        or payload.get("contains_production_data") is not False
        or payload.get("external_data_sent") is not False
        or payload.get("model_called") is not False
    ):
        raise DatasetError("lifecycle result dataset or data-governance binding is invalid")
    _require_string(payload, "run_id", label="lifecycle result")
    if (
        payload.get("case_count") != LIFECYCLE_CASE_COUNT
        or payload.get("passed") != LIFECYCLE_CASE_COUNT
        or payload.get("failed") != 0
        or payload.get("status") != "PASS"
        or payload.get("action_counts") != LIFECYCLE_ACTION_COUNTS
        or payload.get("invariant_pass_counts") != LIFECYCLE_INVARIANT_COUNTS
    ):
        raise DatasetError("lifecycle result is not a complete frozen lifecycle run")
    cases = payload.get("cases")
    if (
        not isinstance(cases, list)
        or len(cases) != LIFECYCLE_CASE_COUNT
        or any(
            not isinstance(item, dict)
            or set(item) != {"action", "case_id", "error_code", "status"}
            or not isinstance(item.get("case_id"), str)
            or not item["case_id"]
            or item.get("action") not in LIFECYCLE_ACTION_COUNTS
            or item.get("status") != "PASS"
            or item.get("error_code") is not None
            for item in cases
        )
        or len({item["case_id"] for item in cases}) != LIFECYCLE_CASE_COUNT
        or dict(sorted(Counter(item["action"] for item in cases).items()))
        != LIFECYCLE_ACTION_COUNTS
    ):
        raise DatasetError("lifecycle result cases are incomplete")
    _validate_system_identity(
        payload.get("system"),
        payload.get("runner_runtime_identity"),
        payload.get("runner_runtime_environment"),
        label="lifecycle result",
    )
    lifecycle_measurements(payload)
    return payload


def recall_measurements(payload: dict[str, Any]) -> dict[str, dict[str, float | int]]:
    counts = payload["counts"]
    return {
        "G02": {
            "sample_count": counts["namespace_probes"],
            "value": counts["namespace_unauthorized_recall_items"],
        },
        "M05": {
            "sample_count": counts["positive_queries"],
            "value": counts["top1_matches"] / counts["positive_queries"],
        },
        "M06": {
            "sample_count": counts["positive_queries"],
            "value": counts["recall_at_5_matches"] / counts["positive_queries"],
        },
        "M08": {
            "sample_count": counts["negative_queries"],
            "value": counts["negative_false_matches"] / counts["negative_queries"],
        },
        "M21": {
            "sample_count": payload["latency"]["sample_count"],
            "value": payload["latency"]["p95_ms"],
        },
    }


def _uuid_string(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


def validate_recall_result(payload: object) -> dict[str, Any]:
    required_keys = {
        "contains_memory_text",
        "contains_production_data",
        "counts",
        "dataset_blind",
        "dataset_contains_memory_text",
        "dataset_id",
        "dataset_validation",
        "dataset_visibility",
        "expected_memory_id",
        "external_data_sent",
        "latency",
        "manifest_sha256",
        "model_called",
        "namespace_ledger",
        "query_count",
        "query_ledger",
        "run_id",
        "runner_runtime_environment",
        "runner_runtime_identity",
        "schema_version",
        "status",
        "system",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required_keys
        or payload.get("schema_version") != RECALL_RESULT_SCHEMA_VERSION
    ):
        raise DatasetError("recall result has an invalid schema")
    if (
        payload.get("dataset_id") != RECALL_DATASET_ID
        or payload.get("manifest_sha256") != RECALL_MANIFEST_SHA256
        or payload.get("dataset_visibility") != "open"
        or payload.get("dataset_blind") is not False
        or payload.get("dataset_contains_memory_text") is not True
        or payload.get("dataset_validation") != "PASS"
        or payload.get("contains_memory_text") is not False
        or payload.get("contains_production_data") is not False
        or payload.get("external_data_sent") is not False
        or payload.get("model_called") is not False
        or payload.get("status") != "PASS"
    ):
        raise DatasetError("recall result dataset or data-governance binding is invalid")
    run_id = _require_string(payload, "run_id", label="recall result")
    if not run_id.startswith("hermes:automated-tests:"):
        raise DatasetError("recall result requires an automated run ID")
    expected_memory_id = payload.get("expected_memory_id")
    if not _uuid_string(expected_memory_id):
        raise DatasetError("recall result expected memory ID is invalid")

    query_ledger = payload.get("query_ledger")
    query_keys = {
        "case_id",
        "false_match",
        "kind",
        "latency_ms",
        "recall_at_5_match",
        "returned_memory_ids",
        "top1_match",
    }
    if (
        not isinstance(query_ledger, list)
        or len(query_ledger) != RECALL_QUERY_COUNT
        or payload.get("query_count") != RECALL_QUERY_COUNT
    ):
        raise DatasetError("recall result query ledger is incomplete")
    seen_case_ids: set[str] = set()
    positive_count = 0
    negative_count = 0
    top1_matches = 0
    recall_at_5_matches = 0
    negative_false_matches = 0
    latency_samples: list[float] = []
    for item in query_ledger:
        if not isinstance(item, dict) or set(item) != query_keys:
            raise DatasetError("recall result query entry has an invalid schema")
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen_case_ids:
            raise DatasetError("recall result query IDs are missing or duplicated")
        seen_case_ids.add(case_id)
        returned = item.get("returned_memory_ids")
        if (
            not isinstance(returned, list)
            or len(returned) > 5
            or len(returned) != len(set(returned))
            or not all(_uuid_string(value) for value in returned)
        ):
            raise DatasetError("recall result returned memory IDs are invalid")
        latency = item.get("latency_ms")
        if (
            isinstance(latency, bool)
            or not isinstance(latency, (int, float))
            or not math.isfinite(float(latency))
            or latency < 0
        ):
            raise DatasetError("recall result latency sample is invalid")
        latency_samples.append(float(latency))
        if item.get("kind") == "positive":
            if (
                not case_id.startswith("recall-pos-")
                or not isinstance(item.get("top1_match"), bool)
                or not isinstance(item.get("recall_at_5_match"), bool)
                or item.get("false_match") is not None
                or item["top1_match"]
                != bool(returned and returned[0] == expected_memory_id)
                or item["recall_at_5_match"] != (expected_memory_id in returned)
            ):
                raise DatasetError("recall result positive query ledger is inconsistent")
            positive_count += 1
            top1_matches += item["top1_match"]
            recall_at_5_matches += item["recall_at_5_match"]
        elif item.get("kind") == "negative":
            if (
                not case_id.startswith("recall-neg-")
                or item.get("top1_match") is not None
                or item.get("recall_at_5_match") is not None
                or not isinstance(item.get("false_match"), bool)
                or item["false_match"] != bool(returned)
            ):
                raise DatasetError("recall result negative query ledger is inconsistent")
            negative_count += 1
            negative_false_matches += item["false_match"]
        else:
            raise DatasetError("recall result query kind is invalid")
    if (positive_count, negative_count) != (RECALL_POSITIVE_COUNT, RECALL_NEGATIVE_COUNT):
        raise DatasetError("recall result query coverage is incomplete")
    expected_case_ids = {
        *(f"recall-pos-{index:03d}" for index in range(1, 11)),
        *(f"recall-neg-uuid-{index:03d}" for index in range(1, 26)),
        *(f"recall-neg-hash-{index:03d}" for index in range(1, 26)),
        *(f"recall-neg-text-{index:03d}" for index in range(1, 51)),
    }
    if seen_case_ids != expected_case_ids:
        raise DatasetError("recall result query IDs differ from the frozen dataset")

    namespace_ledger = payload.get("namespace_ledger")
    namespace_keys = {"case_id", "denied", "returned_memory_ids", "status_code"}
    if (
        not isinstance(namespace_ledger, list)
        or len(namespace_ledger) != RECALL_NAMESPACE_PROBE_COUNT
    ):
        raise DatasetError("recall result namespace ledger is incomplete")
    namespace_denials = 0
    unauthorized_items = 0
    namespace_ids: set[str] = set()
    for item in namespace_ledger:
        if not isinstance(item, dict) or set(item) != namespace_keys:
            raise DatasetError("recall result namespace entry has an invalid schema")
        case_id = item.get("case_id")
        returned = item.get("returned_memory_ids")
        if (
            not isinstance(case_id, str)
            or not case_id.startswith("recall-pos-")
            or case_id in namespace_ids
            or not isinstance(returned, list)
            or len(returned) > 5
            or not all(_uuid_string(value) for value in returned)
            or isinstance(item.get("status_code"), bool)
            or not isinstance(item.get("status_code"), int)
            or not isinstance(item.get("denied"), bool)
            or item["denied"] != (item["status_code"] == 403 and not returned)
        ):
            raise DatasetError("recall result namespace ledger is inconsistent")
        namespace_ids.add(case_id)
        namespace_denials += item["denied"]
        unauthorized_items += len(returned)
    if namespace_ids != {f"recall-pos-{index:03d}" for index in range(1, 7)}:
        raise DatasetError("recall result namespace probe IDs differ from the frozen contract")

    counts = payload.get("counts")
    expected_counts = {
        "positive_queries": positive_count,
        "top1_matches": top1_matches,
        "recall_at_5_matches": recall_at_5_matches,
        "negative_queries": negative_count,
        "negative_false_matches": negative_false_matches,
        "namespace_probes": len(namespace_ledger),
        "namespace_unauthorized_recall_items": unauthorized_items,
        "namespace_denials": namespace_denials,
    }
    if counts != expected_counts:
        raise DatasetError("recall result counts differ from the query ledger")
    latency = payload.get("latency")
    p95 = round(statistics.quantiles(latency_samples, n=100, method="inclusive")[94], 6)
    if latency != {
        "boundary": "loopback-http-api",
        "sample_count": RECALL_QUERY_COUNT,
        "p95_ms": p95,
        "quantile_method": "statistics.quantiles-inclusive-n100-index94",
    }:
        raise DatasetError("recall result latency summary differs from the query ledger")
    if (
        top1_matches < 9
        or recall_at_5_matches < 9
        or negative_false_matches > 1
        or unauthorized_items != 0
        or namespace_denials != RECALL_NAMESPACE_PROBE_COUNT
        or p95 > 1000
    ):
        raise DatasetError("recall result does not pass the frozen thresholds")
    _validate_system_identity(
        payload.get("system"),
        payload.get("runner_runtime_identity"),
        payload.get("runner_runtime_environment"),
        label="recall result",
    )
    recall_measurements(payload)
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
    elif schema == LIFECYCLE_ATTESTATION_SCHEMA_VERSION:
        validated = validate_lifecycle_result(source)
        measurement_ids = LIFECYCLE_MEASUREMENT_IDS
        expected_scope = "isolated-lifecycle"
    elif schema == RECALL_ATTESTATION_SCHEMA_VERSION:
        validated = validate_recall_result(source)
        measurement_ids = RECALL_MEASUREMENT_IDS
        expected_scope = "isolated-recall"
    else:
        raise DatasetError("unsupported sourced attestation schema")
    if artifact.get("source_artifact_schema_version") != validated["schema_version"]:
        raise DatasetError("attestation source schema binding mismatch")
    if artifact.get("source_artifact_sha256") != hashlib.sha256(payload).hexdigest():
        raise DatasetError("attestation source SHA-256 binding mismatch")
    if schema == LIFECYCLE_ATTESTATION_SCHEMA_VERSION:
        source_measurements = lifecycle_measurements(validated)
    elif schema == RECALL_ATTESTATION_SCHEMA_VERSION:
        source_measurements = recall_measurements(validated)
    else:
        source_measurements = validated["metrics"]
    expected_measurements = {
        metric_id: source_measurements[metric_id] for metric_id in measurement_ids
    }
    if artifact.get("measurement_ids") != sorted(measurement_ids):
        raise DatasetError("attestation measurement IDs differ from the source result")
    if artifact.get("measurements") != expected_measurements:
        raise DatasetError("attestation measurements differ from the source result")
    if artifact.get("scope") != expected_scope:
        raise DatasetError("attestation scope differs from the source result")
    source_system = (
        validated["system"]
        if schema
        in {
            QUALITY_ATTESTATION_SCHEMA_VERSION,
            LIFECYCLE_ATTESTATION_SCHEMA_VERSION,
            RECALL_ATTESTATION_SCHEMA_VERSION,
        }
        else {
            "environment_sha256": validated["system_environment_sha256"],
            "revision": validated["system_revision"],
            "source_sha256": validated["system_source_sha256"],
        }
    )
    source_bindings = {
        "blind": validated["dataset_blind"],
        "dataset_manifest_sha256": (
            validated["manifest_sha256"]
            if schema in {LIFECYCLE_ATTESTATION_SCHEMA_VERSION, RECALL_ATTESTATION_SCHEMA_VERSION}
            else validated["dataset_manifest_sha256"]
        ),
        "dataset_visibility": validated["dataset_visibility"],
        "model_called": validated["model_called"],
        "system_environment_sha256": source_system["environment_sha256"],
        "system_revision": source_system["revision"],
        "system_source_sha256": source_system["source_sha256"],
    }
    if schema not in {LIFECYCLE_ATTESTATION_SCHEMA_VERSION, RECALL_ATTESTATION_SCHEMA_VERSION}:
        source_bindings["execution_plan_sha256"] = validated["execution_plan_sha256"]
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
        source_measurements = source["metrics"]
        source_manifest_sha256 = source["dataset_manifest_sha256"]
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
        source_measurements = source["metrics"]
        source_manifest_sha256 = source["dataset_manifest_sha256"]
    elif kind == "lifecycle":
        source = validate_lifecycle_result(source)
        schema = LIFECYCLE_ATTESTATION_SCHEMA_VERSION
        selected_measurements = LIFECYCLE_MEASUREMENT_IDS
        scope = "isolated-lifecycle"
        system = source["system"]
        source_measurements = lifecycle_measurements(source)
        source_manifest_sha256 = source["manifest_sha256"]
    elif kind == "recall":
        source = validate_recall_result(source)
        schema = RECALL_ATTESTATION_SCHEMA_VERSION
        selected_measurements = RECALL_MEASUREMENT_IDS
        scope = "isolated-recall"
        system = source["system"]
        source_measurements = recall_measurements(source)
        source_manifest_sha256 = source["manifest_sha256"]
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
            metric_id: source_measurements[metric_id] for metric_id in sorted(selected_measurements)
        },
        "system_environment_sha256": system["environment_sha256"],
        "system_revision": system["revision"],
        "system_source_sha256": system["source_sha256"],
        "dataset_manifest_sha256": source_manifest_sha256,
        "track": track,
        "dataset_visibility": source["dataset_visibility"],
        "blind": source["dataset_blind"],
        "image_reference": image_reference,
        "image_platform": image_platform,
        "contains_memory_text": False,
        "model_called": source["model_called"],
        "scope": scope,
    }
    if kind not in {"lifecycle", "recall"}:
        artifact["execution_plan_sha256"] = source["execution_plan_sha256"]
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
    parser.add_argument("kind", choices=("quality", "efficiency", "lifecycle", "recall"))
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
