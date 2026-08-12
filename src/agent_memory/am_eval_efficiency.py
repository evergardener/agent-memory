from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .am_eval_atomic_runner import (
    discover_runtime_source_root,
    load_evaluation_plan_file,
    resolve_runtime_identity,
    validate_execution_plan,
    validate_output_runtime_identity,
)
from .am_eval_dataset import DatasetError, load_dataset, sha256_file

INPUT_SCHEMA_VERSION = "am-eval-efficiency-input-v2"
SHA256_CHARACTERS = frozenset("0123456789abcdef")


def _count(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatasetError(f"efficiency count {key} must be a non-negative integer")
    return value


def validate_execution_plan_confirmation(
    payload: dict[str, Any],
    *,
    confirm_plan_sha256: str | None,
    confirm_source_sha256: str | None,
) -> None:
    if payload.get("scope") != "isolated":
        return
    value = (confirm_plan_sha256 or "").casefold()
    source_value = (confirm_source_sha256 or "").casefold()
    payload_value = payload.get("execution_plan_sha256")
    payload_source_value = payload.get("system_source_sha256")
    if (
        len(value) != 64
        or any(character not in SHA256_CHARACTERS for character in value)
        or not isinstance(payload_value, str)
        or payload_value.casefold() != value
    ):
        raise DatasetError("isolated efficiency execution plan SHA-256 confirmation mismatch")
    if (
        len(source_value) != 64
        or any(character not in SHA256_CHARACTERS for character in source_value)
        or not isinstance(payload_source_value, str)
        or payload_source_value.casefold() != source_value
    ):
        raise DatasetError("isolated efficiency runtime source SHA-256 confirmation mismatch")


def validate_efficiency_input(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise DatasetError("efficiency input must be an object")
    if payload.get("schema_version") != INPUT_SCHEMA_VERSION:
        raise DatasetError("unsupported efficiency input schema")
    if payload.get("contains_memory_text") is not False:
        raise DatasetError("efficiency input must declare contains_memory_text=false")
    if payload.get("scope") not in {"isolated", "production-shadow"}:
        raise DatasetError("efficiency input requires an isolated or production-shadow scope")
    isolated_only_keys = {
        "execution_plan_sha256",
        "model",
        "system_source_file_count",
        "system_source_sha256",
        "system_version",
    }
    if payload["scope"] == "production-shadow" and any(
        key in payload for key in isolated_only_keys
    ):
        raise DatasetError(
            "production-shadow efficiency input cannot contain isolated execution identity"
        )
    if payload["scope"] == "isolated":
        execution_plan_sha = payload.get("execution_plan_sha256")
        source_sha = payload.get("system_source_sha256")
        if (
            not isinstance(execution_plan_sha, str)
            or len(execution_plan_sha) != 64
            or any(
                character not in SHA256_CHARACTERS
                for character in execution_plan_sha.casefold()
            )
        ):
            raise DatasetError("isolated efficiency input requires execution plan SHA-256")
        if (
            not isinstance(source_sha, str)
            or len(source_sha) != 64
            or any(
                character not in SHA256_CHARACTERS
                for character in source_sha.casefold()
            )
        ):
            raise DatasetError("isolated efficiency input requires runtime source SHA-256")
        source_file_count = payload.get("system_source_file_count")
        if (
            isinstance(source_file_count, bool)
            or not isinstance(source_file_count, int)
            or source_file_count <= 0
        ):
            raise DatasetError("isolated efficiency input requires runtime source file count")
    for key in ("run_id", "window_start", "window_end"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise DatasetError(f"efficiency input requires {key}")
    identity_keys = ("system_revision", "policy_version")
    if payload["scope"] == "isolated":
        identity_keys += ("system_version", "model")
    for key in identity_keys:
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise DatasetError(f"efficiency input requires {key}")
    if not isinstance(payload.get("contains_production_data"), bool):
        raise DatasetError("efficiency input must declare contains_production_data")
    if not isinstance(payload.get("external_data_sent"), bool):
        raise DatasetError("efficiency input must declare external_data_sent")
    try:
        window_start = datetime.fromisoformat(payload["window_start"])
        window_end = datetime.fromisoformat(payload["window_end"])
    except ValueError as error:
        raise DatasetError("efficiency input has an invalid time window") from error
    if window_start.tzinfo is None or window_end.tzinfo is None or window_end <= window_start:
        raise DatasetError("efficiency input requires an ordered timezone-aware window")
    counts = payload.get("counts")
    if not isinstance(counts, dict):
        raise DatasetError("efficiency input requires aggregate counts")
    for key in (
        "auto_admitted_count",
        "manual_review_count",
        "terminal_model_success_count",
        "terminal_model_failure_count",
        "unfinished_model_job_count",
    ):
        _count(counts, key)


def evaluate_efficiency(payload: dict[str, Any]) -> dict[str, Any]:
    validate_efficiency_input(payload)
    counts = payload["counts"]
    auto_admitted = _count(counts, "auto_admitted_count")
    manual_review = _count(counts, "manual_review_count")
    terminal_success = _count(counts, "terminal_model_success_count")
    terminal_failure = _count(counts, "terminal_model_failure_count")
    unfinished = _count(counts, "unfinished_model_job_count")
    governance_denominator = auto_admitted + manual_review
    terminal_denominator = terminal_success + terminal_failure
    metrics: dict[str, dict[str, float | int]] = {}
    missing: list[str] = []
    if governance_denominator:
        metrics["M22"] = {
            "value": manual_review / governance_denominator,
            "sample_count": governance_denominator,
        }
    else:
        missing.append("M22")
    if terminal_denominator and not unfinished:
        metrics["M23"] = {
            "value": terminal_failure / terminal_denominator,
            "sample_count": terminal_denominator,
        }
    else:
        missing.append("M23")
    result = {
        "schema_version": "am-eval-efficiency-result-v2",
        "run_id": payload["run_id"],
        "scope": payload["scope"],
        "window_start": payload["window_start"],
        "window_end": payload["window_end"],
        "system_revision": payload["system_revision"],
        "policy_version": payload["policy_version"],
        "metrics": metrics,
        "missing_metric_ids": missing,
        "complete": not missing,
        "contains_memory_text": False,
        "contains_production_data": payload["contains_production_data"],
        "external_data_sent": payload["external_data_sent"],
    }
    if payload.get("execution_plan_sha256") is not None:
        result["model"] = payload["model"]
        result["system_version"] = payload["system_version"]
        result["execution_plan_sha256"] = payload["execution_plan_sha256"]
        result["system_source_sha256"] = payload["system_source_sha256"]
        result["system_source_file_count"] = payload["system_source_file_count"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate AM-Eval governance and terminal model failure rates."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--confirm-source-sha256")
    arguments = parser.parse_args()
    try:
        payload = json.loads(arguments.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        parser.error(f"invalid efficiency input: {error}")
    try:
        validate_efficiency_input(payload)
        plan = None
        if payload["scope"] == "isolated":
            if arguments.plan is None or arguments.manifest is None:
                raise DatasetError(
                    "isolated efficiency scoring requires a manifest and execution plan"
                )
            _path, _sha, plan_payload = load_evaluation_plan_file(
                arguments.plan,
                confirm_sha256=arguments.confirm_plan_sha256 or "",
                forbidden_root=discover_runtime_source_root(),
            )
            plan = json.loads(plan_payload.decode("utf-8"))
            manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
            cases = tuple(
                case
                for case in load_dataset(arguments.manifest)
                if case["suite"] == "atomic_fact"
            )
            if not cases:
                raise DatasetError("dataset has no atomic_fact cases")
            plan = validate_execution_plan(
                plan,
                manifest=manifest,
                manifest_sha256=sha256_file(arguments.manifest),
                cases=cases,
            )
            scorer_identity = resolve_runtime_identity()
            if (
                plan["run"]["system_revision"] != scorer_identity.revision
                or plan["run"]["system_version"] != scorer_identity.version
                or plan["run"]["source_sha256"] != scorer_identity.source_sha256
                or plan["run"]["source_file_count"] != scorer_identity.source_file_count
            ):
                raise DatasetError(
                    "efficiency scorer runtime identity differs from the execution plan"
                )
        validate_execution_plan_confirmation(
            payload,
            confirm_plan_sha256=arguments.confirm_plan_sha256,
            confirm_source_sha256=arguments.confirm_source_sha256,
        )
        if plan is not None:
            validate_output_runtime_identity(
                {
                    "run_id": payload["run_id"],
                    "model": payload["model"],
                    "policy_version": payload["policy_version"],
                    "contains_production_data": payload["contains_production_data"],
                    "dataset_visibility": plan["dataset"]["visibility"],
                    "model_called": True,
                    "external_data_sent": payload["external_data_sent"],
                    "system": {
                        "name": "agent-memory",
                        "revision": payload["system_revision"],
                        "source_file_count": payload["system_source_file_count"],
                        "source_sha256": payload["system_source_sha256"],
                        "version": payload["system_version"],
                    },
                },
                plan=plan,
            )
        result = evaluate_efficiency(payload)
        if plan is not None:
            result["scorer_runtime_identity"] = {
                "provenance": scorer_identity.provenance,
                "revision": scorer_identity.revision,
                "source_file_count": scorer_identity.source_file_count,
                "source_sha256": scorer_identity.source_sha256,
                "version": scorer_identity.version,
            }
    except (DatasetError, UnicodeError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
