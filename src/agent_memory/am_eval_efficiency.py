from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .am_eval_dataset import DatasetError

INPUT_SCHEMA_VERSION = "am-eval-efficiency-input-v1"
SHA256_CHARACTERS = frozenset("0123456789abcdef")


def _count(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatasetError(f"efficiency count {key} must be a non-negative integer")
    return value


def validate_execution_plan_confirmation(
    payload: dict[str, Any], *, confirm_plan_sha256: str | None
) -> None:
    if payload.get("scope") != "isolated":
        return
    value = (confirm_plan_sha256 or "").casefold()
    payload_value = payload.get("execution_plan_sha256")
    if (
        len(value) != 64
        or any(character not in SHA256_CHARACTERS for character in value)
        or not isinstance(payload_value, str)
        or payload_value.casefold() != value
    ):
        raise DatasetError("isolated efficiency execution plan SHA-256 confirmation mismatch")


def validate_efficiency_input(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != INPUT_SCHEMA_VERSION:
        raise DatasetError("unsupported efficiency input schema")
    if payload.get("contains_memory_text") is not False:
        raise DatasetError("efficiency input must declare contains_memory_text=false")
    if payload.get("scope") not in {"isolated", "production-shadow"}:
        raise DatasetError("efficiency input requires an isolated or production-shadow scope")
    if payload["scope"] == "isolated":
        execution_plan_sha = payload.get("execution_plan_sha256")
        if (
            not isinstance(execution_plan_sha, str)
            or len(execution_plan_sha) != 64
            or any(
                character not in SHA256_CHARACTERS
                for character in execution_plan_sha.casefold()
            )
        ):
            raise DatasetError("isolated efficiency input requires execution plan SHA-256")
    for key in ("run_id", "window_start", "window_end"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise DatasetError(f"efficiency input requires {key}")
    for key in ("system_revision", "policy_version"):
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
        "schema_version": "am-eval-efficiency-result-v1",
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
        result["execution_plan_sha256"] = payload["execution_plan_sha256"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate AM-Eval governance and terminal model failure rates."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--confirm-plan-sha256")
    arguments = parser.parse_args()
    try:
        payload = json.loads(arguments.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        parser.error(f"invalid efficiency input: {error}")
    try:
        validate_execution_plan_confirmation(
            payload,
            confirm_plan_sha256=arguments.confirm_plan_sha256,
        )
        result = evaluate_efficiency(payload)
    except DatasetError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
