from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


class DatasetError(ValueError):
    """Raised when an AM-Eval dataset fails its frozen-data contract."""


ALLOWED_CASE_SUITES = {
    "atomic_fact",
    "date_range",
    "episode",
    "preference",
    "procedure",
    "recall",
    "temporal_rule",
}
ALLOWED_SPLITS = {"development", "validation", "blind"}
ATOMIC_FACT_TYPES = {"long_term", "stage", "current", "observed"}
ATOMIC_MEMORY_STATES = {"active", "candidate", "evidence_only"}
ATOMIC_EVIDENCE_TYPES = {"user_message", "tool_result", "environment_observation"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as error:
                raise DatasetError(f"invalid JSONL at {path}:{line_number}") from error
            if not isinstance(case, dict):
                raise DatasetError(f"case at {path}:{line_number} must be an object")
            if case.get("schema_version") != "am-eval-case-v1":
                raise DatasetError(f"unsupported case schema at {path}:{line_number}")
            case_id = str(case.get("case_id") or "")
            if not case_id or case_id in seen_ids:
                raise DatasetError(f"missing or duplicate case_id at {path}:{line_number}")
            suite = str(case.get("suite") or "")
            if suite not in ALLOWED_CASE_SUITES:
                raise DatasetError(f"unsupported suite {suite!r} at {path}:{line_number}")
            split = str(case.get("split") or "")
            if split not in ALLOWED_SPLITS:
                raise DatasetError(f"unsupported split {split!r} at {path}:{line_number}")
            if not isinstance(case.get("input"), dict) or not isinstance(
                case.get("expected"), dict
            ):
                raise DatasetError(f"case {case_id} requires input and expected objects")
            seen_ids.add(case_id)
            cases.append(case)
    if not cases:
        raise DatasetError(f"dataset file is empty: {path}")
    return tuple(cases)


def validate_atomic_fact_case(case: dict[str, Any]) -> None:
    """Validate exact-span gold used by construction and citation metrics."""
    if case.get("suite") != "atomic_fact":
        return
    evidence = case["input"].get("evidence")
    evidence_ids = case["input"].get("evidence_ids")
    evidence_types = case["input"].get("evidence_types")
    tool_names = case["input"].get("tool_names")
    facts = case["expected"].get("facts")
    recall_queries = case["expected"].get("recall_queries", [])
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(item, str) and item for item in evidence)
    ):
        raise DatasetError(f"atomic fact case {case['case_id']} requires evidence strings")
    if (
        not isinstance(evidence_ids, list)
        or len(evidence_ids) != len(evidence)
        or not all(isinstance(item, str) and item for item in evidence_ids)
        or len(set(evidence_ids)) != len(evidence_ids)
    ):
        raise DatasetError(f"atomic fact case {case['case_id']} requires unique evidence IDs")
    if evidence_types is None:
        evidence_types = ["user_message"] * len(evidence)
    if tool_names is None:
        tool_names = [""] * len(evidence)
    if (
        not isinstance(evidence_types, list)
        or len(evidence_types) != len(evidence)
        or any(item not in ATOMIC_EVIDENCE_TYPES for item in evidence_types)
        or evidence_types.count("user_message") > 1
        or len(evidence_types) > 7
        or not isinstance(tool_names, list)
        or len(tool_names) != len(evidence)
        or not all(isinstance(item, str) for item in tool_names)
        or any(
            evidence_type == "tool_result" and not tool_name
            for evidence_type, tool_name in zip(evidence_types, tool_names, strict=True)
        )
        or any(
            evidence_type != "tool_result" and tool_name
            for evidence_type, tool_name in zip(evidence_types, tool_names, strict=True)
        )
    ):
        raise DatasetError(f"atomic fact case {case['case_id']} has invalid evidence metadata")
    if not isinstance(facts, list):
        raise DatasetError(f"atomic fact case {case['case_id']} requires a facts array")
    if not facts and not isinstance(case["expected"].get("no_memory_reason"), str):
        raise DatasetError(
            f"atomic fact case {case['case_id']} requires no_memory_reason when empty"
        )
    fact_ids: set[str] = set()
    claim_spans: set[tuple[str, int]] = set()
    for fact in facts:
        if not isinstance(fact, dict):
            raise DatasetError(f"atomic fact case {case['case_id']} has an invalid fact")
        fact_id = str(fact.get("fact_id") or "")
        statement = fact.get("statement")
        evidence_index = fact.get("evidence_index")
        span_start = fact.get("span_start")
        span_end = fact.get("span_end")
        entities = fact.get("entities", [])
        if not fact_id or fact_id in fact_ids:
            raise DatasetError(f"atomic fact case {case['case_id']} has duplicate fact_id")
        if (
            not isinstance(statement, str)
            or not statement
            or isinstance(evidence_index, bool)
            or not isinstance(evidence_index, int)
            or not 0 <= evidence_index < len(evidence)
            or isinstance(span_start, bool)
            or not isinstance(span_start, int)
            or isinstance(span_end, bool)
            or not isinstance(span_end, int)
            or not 0 <= span_start < span_end <= len(evidence[evidence_index])
            or evidence[evidence_index][span_start:span_end] != statement
            or fact.get("fact_type") not in ATOMIC_FACT_TYPES
            or fact.get("memory_state") not in ATOMIC_MEMORY_STATES
            or not isinstance(fact.get("recallable"), bool)
            or not isinstance(entities, list)
        ):
            raise DatasetError(f"atomic fact case {case['case_id']} has an invalid exact span")
        claim_span = (statement, evidence_index)
        if claim_span in claim_spans:
            raise DatasetError(f"atomic fact case {case['case_id']} has a duplicate claim")
        for entity in entities:
            if (
                not isinstance(entity, dict)
                or not isinstance(entity.get("name"), str)
                or not entity["name"]
                or entity["name"] not in statement
                or not isinstance(entity.get("type"), str)
                or not entity["type"]
                or not isinstance(entity.get("role"), str)
                or not entity["role"]
            ):
                raise DatasetError(f"atomic fact case {case['case_id']} has an invalid entity")
        fact_ids.add(fact_id)
        claim_spans.add(claim_span)
    if not isinstance(recall_queries, list):
        raise DatasetError(f"atomic fact case {case['case_id']} has invalid recall queries")
    query_ids: set[str] = set()
    for query in recall_queries:
        if not isinstance(query, dict):
            raise DatasetError(f"atomic fact case {case['case_id']} has an invalid query")
        query_id = str(query.get("query_id") or "")
        expected_fact_ids = query.get("expected_fact_ids")
        if (
            not query_id
            or query_id in query_ids
            or not isinstance(query.get("query"), str)
            or not query["query"]
            or not isinstance(expected_fact_ids, list)
            or not expected_fact_ids
            or not all(item in fact_ids for item in expected_fact_ids)
        ):
            raise DatasetError(f"atomic fact case {case['case_id']} has an invalid query")
        query_ids.add(query_id)


def load_dataset(manifest_path: Path) -> tuple[dict[str, Any], ...]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetError(f"invalid dataset manifest: {manifest_path}") from error
    if manifest.get("schema_version") != "am-eval-dataset-manifest-v1":
        raise DatasetError("unsupported dataset manifest schema")
    if not isinstance(manifest.get("dataset_id"), str) or not manifest["dataset_id"]:
        raise DatasetError("dataset manifest requires dataset_id")
    if not isinstance(manifest.get("contains_memory_text"), bool):
        raise DatasetError("dataset manifest must declare contains_memory_text")
    if manifest.get("external_data_sent") is not False:
        raise DatasetError("dataset validation requires external_data_sent=false")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise DatasetError("dataset manifest must declare at least one file")

    root = manifest_path.parent
    all_cases: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise DatasetError("dataset manifest file entries must be objects")
        relative = str(item.get("path") or "")
        if not relative or relative in seen_paths:
            raise DatasetError(f"missing or duplicate dataset path: {relative!r}")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as error:
            raise DatasetError(f"dataset path escapes manifest root: {relative}") from error
        if not candidate.is_file():
            raise DatasetError(f"dataset file does not exist: {relative}")
        actual_sha256 = sha256_file(candidate)
        if actual_sha256 != item.get("sha256"):
            raise DatasetError(f"dataset SHA-256 mismatch: {relative}")
        cases = load_jsonl(candidate)
        for case in cases:
            validate_atomic_fact_case(case)
        if len(cases) != int(item.get("case_count", -1)):
            raise DatasetError(f"dataset case count mismatch: {relative}")
        declared_suites = set(item.get("suites") or [])
        actual_suites = {str(case["suite"]) for case in cases}
        if actual_suites != declared_suites:
            raise DatasetError(f"dataset suite declaration mismatch: {relative}")
        seen_paths.add(relative)
        all_cases.extend(cases)

    case_ids = [str(case["case_id"]) for case in all_cases]
    if len(case_ids) != len(set(case_ids)):
        raise DatasetError("case_id values must be unique across the dataset")
    expected_total = int(manifest.get("case_count", -1))
    if len(all_cases) != expected_total:
        raise DatasetError("dataset manifest total case count mismatch")
    declared_counts = manifest.get("suite_counts")
    actual_counts = Counter(str(case["suite"]) for case in all_cases)
    if declared_counts != dict(sorted(actual_counts.items())):
        raise DatasetError("dataset manifest suite counts mismatch")
    contains_production_data = manifest.get("contains_production_data")
    visibility = manifest.get("visibility")
    if not isinstance(contains_production_data, bool):
        raise DatasetError("dataset manifest must declare contains_production_data")
    if visibility not in {"open", "private", "restricted"}:
        raise DatasetError("dataset manifest requires a supported visibility")
    if visibility == "open" and contains_production_data:
        raise DatasetError("an open dataset cannot contain production data")
    blind_count = sum(case["split"] == "blind" for case in all_cases)
    declared_blind_count = manifest.get("blind_cases")
    if (
        isinstance(declared_blind_count, bool)
        or not isinstance(declared_blind_count, int)
        or declared_blind_count < 0
        or blind_count != declared_blind_count
    ):
        raise DatasetError("dataset manifest blind case count mismatch")
    if visibility == "open" and blind_count:
        raise DatasetError("an open dataset cannot contain blind cases")
    return tuple(all_cases)


def dataset_summary(manifest_path: Path) -> dict[str, Any]:
    cases = load_dataset(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "schema_version": "am-eval-dataset-validation-v1",
        "dataset_id": manifest["dataset_id"],
        "manifest_sha256": sha256_file(manifest_path),
        "case_count": len(cases),
        "suite_counts": dict(sorted(Counter(case["suite"] for case in cases).items())),
        "split_counts": dict(sorted(Counter(case["split"] for case in cases).items())),
        "status": "PASS",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a frozen AM-Eval JSONL dataset.")
    parser.add_argument("manifest", type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            dataset_summary(arguments.manifest),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
