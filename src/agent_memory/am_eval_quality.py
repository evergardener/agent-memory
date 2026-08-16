from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .am_eval_atomic_runner import (
    OUTPUT_SCHEMA_VERSION,
    RUNNER_VERSION,
    discover_runtime_source_root,
    load_evaluation_plan_file,
    resolve_runtime_identity,
    validate_atomic_run_ledger,
    validate_execution_plan,
    validate_output_runtime_identity,
)
from .am_eval_dataset import (
    DatasetError,
    decode_strict_json_object,
    load_dataset_snapshot,
    read_file_snapshot,
)
from .am_eval_environment import runtime_environment_identity

SHA256_CHARACTERS = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class ClaimKey:
    case_id: str
    fact_id: str


def _safe_divide(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def validate_execution_plan_confirmation(
    output: dict[str, Any], *, confirm_plan_sha256: str, confirm_source_sha256: str
) -> None:
    plan_value = confirm_plan_sha256.casefold()
    source_value = confirm_source_sha256.casefold()
    output_plan_value = output.get("execution_plan_sha256")
    output_source_value = (output.get("system") or {}).get("source_sha256")
    if (
        len(plan_value) != 64
        or any(character not in SHA256_CHARACTERS for character in plan_value)
        or not isinstance(output_plan_value, str)
        or output_plan_value.casefold() != plan_value
    ):
        raise DatasetError("atomic output execution plan SHA-256 confirmation mismatch")
    if (
        len(source_value) != 64
        or any(character not in SHA256_CHARACTERS for character in source_value)
        or not isinstance(output_source_value, str)
        or output_source_value.casefold() != source_value
    ):
        raise DatasetError("atomic output runtime source SHA-256 confirmation mismatch")


def load_atomic_output(
    path: Path, *, case_ids: set[str], confirm_sha256: str | None = None
) -> dict[str, Any]:
    snapshot = read_file_snapshot(path)
    if confirm_sha256 is not None and snapshot.sha256 != confirm_sha256.casefold():
        raise DatasetError("atomic evaluation output SHA-256 confirmation mismatch")
    try:
        output = decode_strict_json_object(
            snapshot.payload, label=f"atomic evaluation output {path}"
        )
    except DatasetError as error:
        raise DatasetError(f"invalid atomic evaluation output: {path}") from error
    required_keys = {
        "case_count",
        "cases",
        "contains_memory_text",
        "contains_production_data",
        "dataset_id",
        "dataset_manifest_sha256",
        "dataset_visibility",
        "execution_plan_sha256",
        "external_data_sent",
        "job_statuses",
        "model",
        "model_called",
        "model_invocations",
        "policy_version",
        "run_id",
        "run_status",
        "runner_version",
        "schema_version",
        "system",
    }
    if set(output) != required_keys or output.get("schema_version") != OUTPUT_SCHEMA_VERSION:
        raise DatasetError("unsupported atomic evaluation output schema")
    if output.get("runner_version") != RUNNER_VERSION:
        raise DatasetError("unsupported atomic evaluation runner version")
    if output.get("contains_memory_text") is not True:
        raise DatasetError("atomic evaluation output must declare contains_memory_text=true")
    if not isinstance(output.get("dataset_id"), str) or not output["dataset_id"]:
        raise DatasetError("atomic evaluation output requires dataset_id")
    if not isinstance(output.get("run_id"), str) or not output["run_id"]:
        raise DatasetError("atomic evaluation output requires run_id")
    if not isinstance(output.get("model_called"), bool):
        raise DatasetError("atomic evaluation output requires model_called")
    if not isinstance(output.get("contains_production_data"), bool):
        raise DatasetError("atomic evaluation output requires contains_production_data")
    if not isinstance(output.get("external_data_sent"), bool):
        raise DatasetError("atomic evaluation output requires external_data_sent")
    if output.get("dataset_visibility") not in {"open", "private", "restricted"}:
        raise DatasetError("atomic evaluation output requires dataset_visibility")
    if not isinstance(output.get("model"), str) or not output["model"]:
        raise DatasetError("atomic evaluation output requires model")
    if not isinstance(output.get("policy_version"), str) or not output["policy_version"]:
        raise DatasetError("atomic evaluation output requires policy_version")
    dataset_sha = output.get("dataset_manifest_sha256")
    if (
        not isinstance(dataset_sha, str)
        or len(dataset_sha) != 64
        or any(character not in SHA256_CHARACTERS for character in dataset_sha.casefold())
    ):
        raise DatasetError("atomic evaluation output requires dataset manifest SHA-256")
    execution_plan_sha = output.get("execution_plan_sha256")
    if (
        not isinstance(execution_plan_sha, str)
        or len(execution_plan_sha) != 64
        or any(character not in SHA256_CHARACTERS for character in execution_plan_sha.casefold())
    ):
        raise DatasetError("atomic evaluation output requires execution plan SHA-256")
    system = output.get("system")
    if (
        not isinstance(system, dict)
        or set(system)
        != {
            "environment_sha256",
            "name",
            "revision",
            "source_file_count",
            "source_sha256",
            "version",
        }
        or not all(
            isinstance(system.get(key), str) and system[key]
            for key in ("name", "version", "revision", "source_sha256")
        )
        or isinstance(system.get("source_file_count"), bool)
        or not isinstance(system.get("source_file_count"), int)
        or system["source_file_count"] <= 0
        or len(system["source_sha256"]) != 64
        or any(
            character not in SHA256_CHARACTERS for character in system["source_sha256"].casefold()
        )
        or not isinstance(system.get("environment_sha256"), str)
        or len(system["environment_sha256"]) != 64
        or any(character not in SHA256_CHARACTERS for character in system["environment_sha256"])
    ):
        raise DatasetError("atomic evaluation output requires system metadata")
    cases = output.get("cases")
    if not isinstance(cases, list):
        raise DatasetError("atomic evaluation output requires a cases array")
    validate_atomic_run_ledger(
        output,
        expected_case_count=len(case_ids),
        require_complete=True,
    )
    seen: set[str] = set()
    for item in cases:
        case_id = str(item.get("case_id") or "") if isinstance(item, dict) else ""
        if not case_id or case_id in seen or case_id not in case_ids:
            raise DatasetError(f"invalid or duplicate atomic output case_id: {case_id!r}")
        predictions = item.get("facts")
        recalls = item.get("recalls", [])
        if not isinstance(predictions, list) or not isinstance(recalls, list):
            raise DatasetError(f"atomic output case {case_id} requires facts and recalls")
        prediction_ids: set[str] = set()
        for prediction in predictions:
            if not isinstance(prediction, dict):
                raise DatasetError(f"atomic output case {case_id} has an invalid fact")
            prediction_id = str(prediction.get("prediction_id") or "")
            source_ids = prediction.get("source_ids")
            if (
                not prediction_id
                or prediction_id in prediction_ids
                or not isinstance(prediction.get("statement"), str)
                or not prediction["statement"]
                or prediction.get("fact_type") not in {"long_term", "stage", "current", "observed"}
                or prediction.get("memory_state") not in {"active", "candidate", "evidence_only"}
                or not isinstance(prediction.get("recallable"), bool)
                or isinstance(prediction.get("evidence_index"), bool)
                or not isinstance(prediction.get("evidence_index"), int)
                or isinstance(prediction.get("span_start"), bool)
                or not isinstance(prediction.get("span_start"), int)
                or isinstance(prediction.get("span_end"), bool)
                or not isinstance(prediction.get("span_end"), int)
                or not isinstance(source_ids, list)
                or not all(isinstance(item, str) and item for item in source_ids)
            ):
                raise DatasetError(f"atomic output case {case_id} has an invalid fact")
            prediction_ids.add(prediction_id)
        recall_ids: set[str] = set()
        for recall in recalls:
            recall_id = str(recall.get("query_id") or "") if isinstance(recall, dict) else ""
            source_ids = recall.get("source_ids") if isinstance(recall, dict) else None
            if (
                not recall_id
                or recall_id in recall_ids
                or not isinstance(recall.get("prediction_id"), str)
                or not recall["prediction_id"]
                or not isinstance(source_ids, list)
                or not all(isinstance(item, str) and item for item in source_ids)
            ):
                raise DatasetError(f"atomic output case {case_id} has an invalid recall")
            recall_ids.add(recall_id)
        seen.add(case_id)
    if seen != case_ids:
        missing = ", ".join(sorted(case_ids - seen))
        raise DatasetError(f"atomic output is missing dataset cases: {missing}")
    return output


def evaluate_atomic_quality(
    cases: tuple[dict[str, Any], ...], output: dict[str, Any]
) -> dict[str, Any]:
    gold_cases = {case["case_id"]: case for case in cases if case["suite"] == "atomic_fact"}
    output_cases = {item["case_id"]: item for item in output["cases"]}
    gold_claims: set[ClaimKey] = set()
    matched_claims: set[ClaimKey] = set()
    prediction_count = 0
    correct_prediction_count = 0
    exact_span_count = 0
    expected_queries: set[tuple[str, str]] = set()
    correct_citations = 0

    for case_id, case in gold_cases.items():
        evidence = case["input"]["evidence"]
        evidence_ids = case["input"]["evidence_ids"]
        expected_facts = {item["fact_id"]: item for item in case["expected"]["facts"]}
        statement_index = {
            (
                item["statement"],
                item["evidence_index"],
                item["fact_type"],
                item["memory_state"],
                item["recallable"],
            ): item
            for item in expected_facts.values()
        }
        predictions = output_cases[case_id]["facts"]
        matched_prediction_ids: dict[str, str] = {}
        for fact_id in expected_facts:
            gold_claims.add(ClaimKey(case_id, fact_id))
        for prediction in predictions:
            prediction_count += 1
            key = (
                prediction["statement"],
                prediction["evidence_index"],
                prediction["fact_type"],
                prediction["memory_state"],
                prediction["recallable"],
            )
            expected = statement_index.get(key)
            if expected is None:
                continue
            claim_key = ClaimKey(case_id, expected["fact_id"])
            if claim_key in matched_claims:
                continue
            correct_prediction_count += 1
            matched_claims.add(claim_key)
            matched_prediction_ids[prediction["prediction_id"]] = expected["fact_id"]
            index = prediction["evidence_index"]
            start = prediction["span_start"]
            end = prediction["span_end"]
            if (
                index == expected["evidence_index"]
                and start == expected["span_start"]
                and end == expected["span_end"]
                and 0 <= index < len(evidence)
                and 0 <= start < end <= len(evidence[index])
                and evidence[index][start:end] == prediction["statement"]
            ):
                exact_span_count += 1

        query_gold = {
            item["query_id"]: set(item["expected_fact_ids"])
            for item in case["expected"].get("recall_queries", [])
        }
        supplied_query_ids = {item["query_id"] for item in output_cases[case_id].get("recalls", [])}
        unknown_query_ids = supplied_query_ids - set(query_gold)
        if unknown_query_ids:
            raise DatasetError(
                f"atomic output case {case_id} has unknown recall queries: "
                + ", ".join(sorted(unknown_query_ids))
            )
        for query_id in query_gold:
            expected_queries.add((case_id, query_id))
        seen_queries: set[str] = set()
        for recall in output_cases[case_id].get("recalls", []):
            query_id = recall["query_id"]
            if query_id in seen_queries or query_id not in query_gold:
                continue
            seen_queries.add(query_id)
            matched_fact_id = matched_prediction_ids.get(recall["prediction_id"])
            if matched_fact_id not in query_gold[query_id]:
                continue
            source_ids = set(recall["source_ids"])
            prediction = next(
                item for item in predictions if item["prediction_id"] == recall["prediction_id"]
            )
            expected = expected_facts[matched_fact_id]
            expected_source = evidence_ids[expected["evidence_index"]]
            if expected_source in source_ids and expected_source in set(prediction["source_ids"]):
                correct_citations += 1

    recall_count = len(expected_queries)
    precision = _safe_divide(correct_prediction_count, prediction_count)
    recall = _safe_divide(len(matched_claims), len(gold_claims))
    span_accuracy = _safe_divide(exact_span_count, correct_prediction_count)
    citation_accuracy = _safe_divide(correct_citations, recall_count)
    metrics: dict[str, dict[str, float | int]] = {}
    missing: list[str] = []
    for metric_id, value, sample_count in (
        ("M01", precision, prediction_count),
        ("M02", recall, len(gold_claims)),
        ("M03", span_accuracy, correct_prediction_count),
        ("M07", citation_accuracy, recall_count),
    ):
        if sample_count:
            metrics[metric_id] = {"value": value, "sample_count": sample_count}
        else:
            missing.append(metric_id)
    return {
        "schema_version": "am-eval-atomic-quality-result-v2",
        "runner_version": output["runner_version"],
        "dataset_id": output["dataset_id"],
        "run_id": output["run_id"],
        "run_status": output["run_status"],
        "case_count": output["case_count"],
        "job_statuses": output["job_statuses"],
        "model_invocations": output["model_invocations"],
        "system": output["system"],
        "dataset_manifest_sha256": output["dataset_manifest_sha256"],
        "execution_plan_sha256": output["execution_plan_sha256"],
        "model": output["model"],
        "policy_version": output["policy_version"],
        "model_called": output["model_called"],
        "contains_production_data": output["contains_production_data"],
        "external_data_sent": output["external_data_sent"],
        "dataset_visibility": output["dataset_visibility"],
        "sample_counts": {
            "gold_claims": len(gold_claims),
            "predictions": prediction_count,
            "matched_claims": len(matched_claims),
            "exact_spans": exact_span_count,
            "recall_queries": recall_count,
            "correct_citations": correct_citations,
        },
        "metrics": metrics,
        "missing_metric_ids": missing,
        "complete": not missing,
        "contains_memory_text": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score AM-Eval atomic facts and citations.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--confirm-plan-sha256", required=True)
    parser.add_argument("--confirm-source-sha256", required=True)
    parser.add_argument("--confirm-output-sha256", required=True)
    arguments = parser.parse_args()
    try:
        dataset = load_dataset_snapshot(arguments.manifest)
        manifest = dataset.manifest
        cases = tuple(case for case in dataset.cases if case["suite"] == "atomic_fact")
        if not cases:
            raise DatasetError("dataset has no atomic_fact cases")
        output = load_atomic_output(
            arguments.output,
            case_ids={case["case_id"] for case in cases},
            confirm_sha256=arguments.confirm_output_sha256,
        )
        _path, _sha, plan_payload = load_evaluation_plan_file(
            arguments.plan,
            confirm_sha256=arguments.confirm_plan_sha256,
            forbidden_root=discover_runtime_source_root(),
        )
        plan = decode_strict_json_object(plan_payload, label="atomic execution plan")
        plan = validate_execution_plan(
            plan,
            manifest=manifest,
            manifest_sha256=dataset.manifest_sha256,
            cases=cases,
        )
        scorer_environment = runtime_environment_identity()
        if plan["environment"] != scorer_environment:
            raise DatasetError("quality scorer runtime environment differs from the execution plan")
        scorer_identity = resolve_runtime_identity()
        if (
            plan["run"]["system_revision"] != scorer_identity.revision
            or plan["run"]["system_version"] != scorer_identity.version
            or plan["run"]["source_sha256"] != scorer_identity.source_sha256
            or plan["run"]["source_file_count"] != scorer_identity.source_file_count
        ):
            raise DatasetError("quality scorer runtime identity differs from the execution plan")
        validate_execution_plan_confirmation(
            output,
            confirm_plan_sha256=arguments.confirm_plan_sha256,
            confirm_source_sha256=arguments.confirm_source_sha256,
        )
        validate_output_runtime_identity(output, plan=plan)
        manifest_sha256 = dataset.manifest_sha256
        if output["dataset_id"] != manifest.get("dataset_id"):
            raise DatasetError("atomic output dataset_id does not match the manifest")
        if output["dataset_manifest_sha256"] != manifest_sha256:
            raise DatasetError("atomic output dataset SHA-256 does not match the manifest")
        result = evaluate_atomic_quality(cases, output)
        result["schema_version"] = "am-eval-atomic-quality-result-v3"
        result["dataset_blind"] = manifest.get("blind") is True
        result["scorer_runtime_identity"] = {
            "provenance": scorer_identity.provenance,
            "revision": scorer_identity.revision,
            "source_file_count": scorer_identity.source_file_count,
            "source_sha256": scorer_identity.source_sha256,
            "version": scorer_identity.version,
        }
        result["scorer_runtime_environment"] = scorer_environment
        result["input_artifact_sha256"] = arguments.confirm_output_sha256.casefold()
    except (DatasetError, UnicodeError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
