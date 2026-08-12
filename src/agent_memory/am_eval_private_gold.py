from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .am_eval_dataset import (
    DatasetError,
    load_dataset,
    load_jsonl,
    sha256_file,
    validate_atomic_fact_case,
)
from .redaction import redact_text

ANNOTATION_SCHEMA_VERSION = "am-eval-private-gold-annotation-v1"
PRIVATE_GOLD_CONTRACT_VERSION = "am-eval-private-gold-contract-v1"
FREEZE_CONFIRMATION = "FREEZE_REDACTED_PRODUCTION_DERIVED_GOLD"
INIT_CONFIRMATION = "CREATE_PRIVATE_GOLD_WORKSPACE"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
CASE_IDENTIFIER = re.compile(r"^private-case-\d{6}$")
SCENARIO_IDENTIFIER = re.compile(r"^scenario-\d{6}$")
EVIDENCE_IDENTIFIER = re.compile(r"^e-private-case-\d{6}-\d{2}$")
FACT_IDENTIFIER = re.compile(r"^f-private-case-\d{6}-\d{2}$")
QUERY_IDENTIFIER = re.compile(r"^q-private-case-\d{6}-\d{2}$")
DATASET_IDENTIFIER = re.compile(r"^private-hermes-atomic-gold-v[1-9]\d*$")
UUID_TEXT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
IPV4 = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
MAC_ADDRESS = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f])")
PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
ABSOLUTE_PATH = re.compile(r"(?<![\w:])/(?:Users|home|root|etc|var|opt|srv|Volumes)/[^\s'\"，。]+")
URL = re.compile(r"https?://[^\s'\"，。]+", re.IGNORECASE)
IPV6_CANDIDATE = re.compile(r"(?<![0-9A-Za-z:])\[?[0-9A-Fa-f:]{2,}\]?(?![0-9A-Za-z:])")
WINDOWS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:\\(?:Users|Windows|ProgramData)\\[^\s'\"]+")
BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{8,}")
SSH_PUBLIC_KEY = re.compile(r"(?i)\bssh-(?:rsa|ed25519)\s+[A-Za-z0-9+/=]{20,}")

SCENARIO_CLASSES = {
    "life_event",
    "technical_incident",
    "preference",
    "temporal",
    "multi_profile",
    "tool_conflict",
    "procedure_safety",
    "negative_attack",
}
EXPECTED_SCENARIO_SPLITS = {"development": 0.60, "validation": 0.20, "blind": 0.20}
MIN_SCENARIOS = 50
MIN_GOLD_FACTS = 100
MAX_GOLD_FACTS = 150
MIN_RECALL_QUERIES = 200
MIN_NEGATIVE_CASES = 100
MINIMUM_SPLIT_CONTENT = {
    "development": {"facts": 50, "queries": 100, "negative": 50},
    "validation": {"facts": 15, "queries": 30, "negative": 15},
    "blind": {"facts": 15, "queries": 30, "negative": 15},
}


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ensure_outside_repository(path: Path) -> None:
    try:
        path.resolve().relative_to(_source_root())
    except ValueError:
        return
    raise DatasetError("private gold paths must stay outside the source repository")


def _reject_symlink_path(path: Path) -> None:
    expanded = path.expanduser()
    if any(candidate.is_symlink() for candidate in (expanded, *expanded.parents)):
        raise DatasetError("private gold paths cannot use symlinks")


def _check_private_mode(path: Path, *, expected_directory: bool) -> None:
    if expected_directory and not path.is_dir():
        raise DatasetError("private gold path must be a directory")
    if not expected_directory and not path.is_file():
        raise DatasetError("private gold path must be a file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        kind = "directory" if expected_directory else "file"
        raise DatasetError(f"private gold {kind} must not grant group or other access")


def validate_private_input(path: Path) -> Path:
    _reject_symlink_path(path)
    resolved = path.expanduser().resolve()
    _ensure_outside_repository(resolved)
    _check_private_mode(resolved, expected_directory=False)
    _check_private_mode(resolved.parent, expected_directory=True)
    return resolved


def _write_private_text(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    _write_private_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _new_private_directory(path: Path) -> Path:
    _reject_symlink_path(path)
    resolved = path.expanduser().resolve()
    _ensure_outside_repository(resolved)
    if resolved.exists():
        raise DatasetError("private gold output directory already exists")
    resolved.mkdir(mode=0o700, parents=False)
    _check_private_mode(resolved, expected_directory=True)
    return resolved


def _parse_review_time(value: Any, field: str, case_id: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise DatasetError(f"private gold case {case_id} requires {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise DatasetError(f"private gold case {case_id} has invalid {field}") from error
    if parsed.tzinfo is None:
        raise DatasetError(f"private gold case {case_id} requires timezone-aware {field}")
    return parsed


def _walk_strings(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")


def privacy_finding_kinds(case: dict[str, Any]) -> tuple[str, ...]:
    findings: set[str] = set()
    for _path, text in _walk_strings(
        {"input": case.get("input"), "expected": case.get("expected")}
    ):
        if redact_text(text).findings:
            findings.add("credential_or_secret")
        if EMAIL.search(text):
            findings.add("email")
        if IPV4.search(text):
            findings.add("ipv4")
        if MAC_ADDRESS.search(text):
            findings.add("mac_address")
        if PHONE.search(text):
            findings.add("phone")
        if ABSOLUTE_PATH.search(text):
            findings.add("absolute_path")
        if WINDOWS_PATH.search(text):
            findings.add("windows_path")
        if BEARER_TOKEN.search(text):
            findings.add("bearer_token")
        if SSH_PUBLIC_KEY.search(text):
            findings.add("ssh_public_key")
        for match in IPV6_CANDIDATE.finditer(text):
            candidate = match.group(0).strip("[]")
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.version == 6:
                findings.add("ipv6")
        for match in URL.finditer(text):
            hostname = (urlparse(match.group(0)).hostname or "").casefold()
            if hostname not in {"example.invalid", "localhost"} and not hostname.endswith(
                ".example.invalid"
            ):
                findings.add("url")
    return tuple(sorted(findings))


def _is_non_placeholder_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(HEX_SHA256.fullmatch(value))
        and len(set(value.casefold())) >= 8
    )


def _validate_annotation(case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["case_id"])
    annotation = case.get("annotation")
    if not isinstance(annotation, dict):
        raise DatasetError(f"private gold case {case_id} requires annotation metadata")
    if annotation.get("schema_version") != ANNOTATION_SCHEMA_VERSION:
        raise DatasetError(f"private gold case {case_id} has wrong annotation schema")
    scenario_id = annotation.get("scenario_id")
    scenario_class = annotation.get("scenario_class")
    if not isinstance(scenario_id, str) or not SCENARIO_IDENTIFIER.fullmatch(scenario_id):
        raise DatasetError(f"private gold case {case_id} has invalid scenario_id")
    if scenario_class not in SCENARIO_CLASSES:
        raise DatasetError(f"private gold case {case_id} has invalid scenario_class")
    for field in ("source_hmac_sha256", "annotator_id_hash", "reviewer_id_hash"):
        value = annotation.get(field)
        if not _is_non_placeholder_sha256(value):
            raise DatasetError(f"private gold case {case_id} requires non-placeholder {field}")
    if annotation["annotator_id_hash"] == annotation["reviewer_id_hash"]:
        raise DatasetError(f"private gold case {case_id} requires an independent reviewer")
    annotated_at = _parse_review_time(annotation.get("annotated_at"), "annotated_at", case_id)
    reviewed_at = _parse_review_time(annotation.get("reviewed_at"), "reviewed_at", case_id)
    if reviewed_at < annotated_at:
        raise DatasetError(f"private gold case {case_id} review precedes annotation")
    if annotation.get("decision") != "ACCEPT":
        raise DatasetError(f"private gold case {case_id} is not accepted")
    for field in ("redaction_verified", "exact_span_verified", "semantic_verified"):
        if annotation.get(field) is not True:
            raise DatasetError(f"private gold case {case_id} requires {field}=true")
    used_for_tuning = annotation.get("used_for_tuning")
    if not isinstance(used_for_tuning, bool):
        raise DatasetError(f"private gold case {case_id} requires used_for_tuning")
    if used_for_tuning and case["split"] != "development":
        raise DatasetError("validation and blind scenarios cannot be used for tuning")
    return annotation


def _validate_public_identifiers(case: dict[str, Any]) -> None:
    case_id = str(case["case_id"])
    if not CASE_IDENTIFIER.fullmatch(case_id) or UUID_TEXT.fullmatch(case_id):
        raise DatasetError(f"private gold case has unsafe case_id: {case_id}")
    for evidence_id in case["input"].get("evidence_ids") or []:
        if not EVIDENCE_IDENTIFIER.fullmatch(evidence_id) or UUID_TEXT.fullmatch(evidence_id):
            raise DatasetError(f"private gold case {case_id} has unsafe evidence ID")
    for fact in case["expected"].get("facts") or []:
        if not FACT_IDENTIFIER.fullmatch(str(fact.get("fact_id") or "")):
            raise DatasetError(f"private gold case {case_id} has unsafe fact ID")
    for query in case["expected"].get("recall_queries") or []:
        if not QUERY_IDENTIFIER.fullmatch(str(query.get("query_id") or "")):
            raise DatasetError(f"private gold case {case_id} has unsafe query ID")


def validate_private_gold_cases(cases: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    scenario_splits: dict[str, set[str]] = defaultdict(set)
    scenario_classes: dict[str, set[str]] = defaultdict(set)
    source_hmacs: set[str] = set()
    split_case_counts: Counter[str] = Counter()
    fact_count = 0
    recall_query_count = 0
    negative_case_count = 0
    tuned_case_count = 0
    split_fact_counts: Counter[str] = Counter()
    split_query_counts: Counter[str] = Counter()
    split_negative_counts: Counter[str] = Counter()

    for case in cases:
        if case.get("suite") != "atomic_fact":
            raise DatasetError("private gold freeze accepts atomic_fact cases only")
        validate_atomic_fact_case(case)
        _validate_public_identifiers(case)
        annotation = _validate_annotation(case)
        finding_kinds = privacy_finding_kinds(case)
        if finding_kinds:
            raise DatasetError(
                f"private gold case {case['case_id']} failed privacy scan: "
                + ",".join(finding_kinds)
            )
        source_hmac = annotation["source_hmac_sha256"].casefold()
        if source_hmac in source_hmacs:
            raise DatasetError("private gold source_hmac_sha256 values must be unique")
        source_hmacs.add(source_hmac)
        scenario_id = annotation["scenario_id"]
        scenario_splits[scenario_id].add(case["split"])
        scenario_classes[scenario_id].add(annotation["scenario_class"])
        split_case_counts[case["split"]] += 1
        facts = case["expected"]["facts"]
        fact_count += len(facts)
        queries = case["expected"].get("recall_queries", [])
        recall_query_count += len(queries)
        negative_case_count += not facts
        split_fact_counts[case["split"]] += len(facts)
        split_query_counts[case["split"]] += len(queries)
        split_negative_counts[case["split"]] += not facts
        tuned_case_count += annotation["used_for_tuning"]

    leaking_scenarios = sorted(
        scenario_id for scenario_id, splits in scenario_splits.items() if len(splits) != 1
    )
    if leaking_scenarios:
        raise DatasetError("private gold scenarios cannot cross dataset splits")
    mixed_classes = sorted(
        scenario_id for scenario_id, classes in scenario_classes.items() if len(classes) != 1
    )
    if mixed_classes:
        raise DatasetError("private gold scenarios must keep one scenario_class")
    scenario_count = len(scenario_splits)
    if scenario_count < MIN_SCENARIOS or scenario_count % 5:
        raise DatasetError("private gold requires at least 50 scenarios divisible by five")
    scenario_split_counts = Counter(next(iter(splits)) for splits in scenario_splits.values())
    expected_counts = {
        split: int(scenario_count * ratio) for split, ratio in EXPECTED_SCENARIO_SPLITS.items()
    }
    if dict(scenario_split_counts) != expected_counts:
        raise DatasetError("private gold scenario splits must be exactly 60/20/20")
    present_classes = {next(iter(classes)) for classes in scenario_classes.values()}
    missing_classes = SCENARIO_CLASSES - present_classes
    if missing_classes:
        raise DatasetError("private gold is missing required scenario classes")
    for split in EXPECTED_SCENARIO_SPLITS:
        split_classes = {
            next(iter(scenario_classes[scenario_id]))
            for scenario_id, splits in scenario_splits.items()
            if split in splits
        }
        if split_classes != SCENARIO_CLASSES:
            raise DatasetError("private gold requires every scenario class in every dataset split")
    if not MIN_GOLD_FACTS <= fact_count <= MAX_GOLD_FACTS:
        raise DatasetError("private gold requires 100-150 accepted facts")
    if recall_query_count < MIN_RECALL_QUERIES:
        raise DatasetError("private gold requires at least 200 recall queries")
    if negative_case_count < MIN_NEGATIVE_CASES:
        raise DatasetError("private gold requires at least 100 negative cases")
    if not all(split_case_counts[split] for split in EXPECTED_SCENARIO_SPLITS):
        raise DatasetError("private gold requires cases in every split")
    for split, minimums in MINIMUM_SPLIT_CONTENT.items():
        if (
            split_fact_counts[split] < minimums["facts"]
            or split_query_counts[split] < minimums["queries"]
            or split_negative_counts[split] < minimums["negative"]
        ):
            raise DatasetError(
                "private gold requires representative facts, queries and negatives in every split"
            )

    return {
        "schema_version": "am-eval-private-gold-validation-v1",
        "contract_version": PRIVATE_GOLD_CONTRACT_VERSION,
        "case_count": len(cases),
        "scenario_count": scenario_count,
        "scenario_split_counts": dict(sorted(scenario_split_counts.items())),
        "split_case_counts": dict(sorted(split_case_counts.items())),
        "scenario_classes": sorted(present_classes),
        "split_fact_counts": dict(sorted(split_fact_counts.items())),
        "split_query_counts": dict(sorted(split_query_counts.items())),
        "split_negative_counts": dict(sorted(split_negative_counts.items())),
        "gold_fact_count": fact_count,
        "recall_query_count": recall_query_count,
        "negative_case_count": negative_case_count,
        "tuned_case_count": tuned_case_count,
        "privacy_findings": 0,
        "status": "READY_TO_FREEZE",
        "contains_memory_text": False,
        "contains_production_data": True,
        "external_data_sent": False,
    }


def annotation_contract() -> dict[str, Any]:
    return {
        "schema_version": PRIVATE_GOLD_CONTRACT_VERSION,
        "case_schema_version": "am-eval-case-v1",
        "annotation_schema_version": ANNOTATION_SCHEMA_VERSION,
        "suite": "atomic_fact",
        "visibility": "private",
        "contains_production_data": True,
        "external_data_sent": False,
        "targets": {
            "minimum_scenarios": MIN_SCENARIOS,
            "scenario_split": EXPECTED_SCENARIO_SPLITS,
            "gold_fact_range": [MIN_GOLD_FACTS, MAX_GOLD_FACTS],
            "minimum_recall_queries": MIN_RECALL_QUERIES,
            "minimum_negative_cases": MIN_NEGATIVE_CASES,
            "minimum_split_content": MINIMUM_SPLIT_CONTENT,
            "required_scenario_classes": sorted(SCENARIO_CLASSES),
            "required_scenario_classes_per_split": True,
        },
        "identifier_patterns": {
            "dataset_id": DATASET_IDENTIFIER.pattern,
            "case_id": CASE_IDENTIFIER.pattern,
            "scenario_id": SCENARIO_IDENTIFIER.pattern,
            "evidence_id": EVIDENCE_IDENTIFIER.pattern,
            "fact_id": FACT_IDENTIFIER.pattern,
            "query_id": QUERY_IDENTIFIER.pattern,
        },
        "source_identity": (
            "Store only a private HMAC-SHA256 fingerprint; never copy a production session, "
            "turn, event, user, host or profile identifier."
        ),
        "review": {
            "decision": "ACCEPT",
            "independent_reviewer_required": True,
            "required_flags": [
                "redaction_verified",
                "exact_span_verified",
                "semantic_verified",
            ],
            "validation_and_blind_used_for_tuning": False,
        },
    }


def validate_frozen_private_gold(
    manifest: dict[str, Any], cases: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    if manifest.get("schema_version") != "am-eval-dataset-manifest-v1":
        raise DatasetError("frozen private gold requires the dataset manifest schema")
    dataset_id = manifest.get("dataset_id")
    if not isinstance(dataset_id, str) or not DATASET_IDENTIFIER.fullmatch(dataset_id):
        raise DatasetError("frozen private gold requires the official dataset_id pattern")
    if manifest.get("contains_production_data") is not True:
        raise DatasetError("frozen private gold must declare production-derived data")
    if manifest.get("contains_memory_text") is not True:
        raise DatasetError("frozen private gold must declare memory text")
    if manifest.get("external_data_sent") is not False:
        raise DatasetError("frozen private gold must not claim prior external transmission")
    if manifest.get("visibility") not in {"private", "restricted"}:
        raise DatasetError("frozen private gold must be private or restricted")
    if manifest.get("review_contract") != PRIVATE_GOLD_CONTRACT_VERSION:
        raise DatasetError("frozen private gold requires the approved review contract")
    validation = validate_private_gold_cases(cases)
    expected = {
        "case_count": validation["case_count"],
        "scenario_count": validation["scenario_count"],
        "scenario_split_counts": validation["scenario_split_counts"],
        "split_case_counts": validation["split_case_counts"],
        "split_fact_counts": validation["split_fact_counts"],
        "split_query_counts": validation["split_query_counts"],
        "split_negative_counts": validation["split_negative_counts"],
        "gold_fact_count": validation["gold_fact_count"],
        "recall_query_count": validation["recall_query_count"],
        "negative_case_count": validation["negative_case_count"],
        "blind_cases": validation["split_case_counts"]["blind"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise DatasetError(f"frozen private gold manifest mismatch: {key}")
    return validation


def case_template() -> dict[str, Any]:
    evidence = "用户明确表示每周六去示例公园散步。"
    statement = "每周六去示例公园散步"
    start = evidence.index(statement)
    return {
        "schema_version": "am-eval-case-v1",
        "case_id": "private-case-000001",
        "suite": "atomic_fact",
        "split": "development",
        "input": {
            "evidence_ids": ["e-private-case-000001-01"],
            "evidence": [evidence],
        },
        "expected": {
            "facts": [
                {
                    "fact_id": "f-private-case-000001-01",
                    "statement": statement,
                    "fact_type": "long_term",
                    "memory_state": "active",
                    "recallable": True,
                    "evidence_index": 0,
                    "span_start": start,
                    "span_end": start + len(statement),
                    "entities": [{"name": "示例公园", "type": "location", "role": "destination"}],
                }
            ],
            "recall_queries": [
                {
                    "query_id": "q-private-case-000001-01",
                    "query": "用户每周六去哪里散步",
                    "expected_fact_ids": ["f-private-case-000001-01"],
                }
            ],
        },
        "annotation": {
            "schema_version": ANNOTATION_SCHEMA_VERSION,
            "scenario_id": "scenario-000001",
            "scenario_class": "life_event",
            "source_hmac_sha256": "0" * 64,
            "annotator_id_hash": "1" * 64,
            "reviewer_id_hash": "2" * 64,
            "annotated_at": "2026-08-12T00:00:00+08:00",
            "reviewed_at": "2026-08-12T01:00:00+08:00",
            "decision": "ACCEPT",
            "redaction_verified": True,
            "exact_span_verified": True,
            "semantic_verified": True,
            "used_for_tuning": False,
        },
    }


def initialize_private_gold_workspace(path: Path) -> dict[str, Any]:
    output = _new_private_directory(path)
    try:
        _write_private_json(output / "annotation-contract.json", annotation_contract())
        _write_private_json(output / "case-template.json", case_template())
        _write_private_text(output / "cases.draft.jsonl", "")
    except BaseException:
        for child in output.iterdir():
            child.unlink(missing_ok=True)
        output.rmdir()
        raise
    return {
        "status": "PRIVATE_GOLD_WORKSPACE_CREATED",
        "directory": str(output),
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
    }


def freeze_private_gold(
    cases_path: Path,
    output_directory: Path,
    *,
    dataset_id: str,
) -> dict[str, Any]:
    source = validate_private_input(cases_path)
    if not DATASET_IDENTIFIER.fullmatch(dataset_id):
        raise DatasetError("private gold dataset_id must use the official versioned pattern")
    cases = load_jsonl(source)
    validation = validate_private_gold_cases(cases)
    output = _new_private_directory(output_directory)
    try:
        file_records: list[dict[str, Any]] = []
        for split in EXPECTED_SCENARIO_SPLITS:
            split_cases = tuple(case for case in cases if case["split"] == split)
            file_path = output / f"atomic-facts-{split}.jsonl"
            _write_private_text(
                file_path,
                "\n".join(
                    json.dumps(case, ensure_ascii=False, sort_keys=True) for case in split_cases
                )
                + "\n",
            )
            file_records.append(
                {
                    "path": file_path.name,
                    "sha256": sha256_file(file_path),
                    "case_count": len(split_cases),
                    "suites": ["atomic_fact"],
                }
            )
        manifest = {
            "schema_version": "am-eval-dataset-manifest-v1",
            "dataset_id": dataset_id,
            "frozen_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
            "case_count": len(cases),
            "contains_production_data": True,
            "contains_memory_text": True,
            "external_data_sent": False,
            "visibility": "private",
            "blind_cases": validation["split_case_counts"]["blind"],
            "files": file_records,
            "suite_counts": {"atomic_fact": len(cases)},
            "review_contract": PRIVATE_GOLD_CONTRACT_VERSION,
            "scenario_count": validation["scenario_count"],
            "scenario_split_counts": validation["scenario_split_counts"],
            "split_case_counts": validation["split_case_counts"],
            "split_fact_counts": validation["split_fact_counts"],
            "split_query_counts": validation["split_query_counts"],
            "split_negative_counts": validation["split_negative_counts"],
            "gold_fact_count": validation["gold_fact_count"],
            "recall_query_count": validation["recall_query_count"],
            "negative_case_count": validation["negative_case_count"],
        }
        manifest_path = output / "manifest.json"
        _write_private_json(manifest_path, manifest)
        loaded = load_dataset(manifest_path)
        if len(loaded) != len(cases):
            raise DatasetError("frozen private gold failed its manifest self-check")
        validate_frozen_private_gold(manifest, loaded)
        summary = {
            **validation,
            "schema_version": "am-eval-private-gold-freeze-v1",
            "status": "FROZEN",
            "dataset_id": dataset_id,
            "manifest_sha256": sha256_file(manifest_path),
            "output_directory": str(output),
        }
        _write_private_json(output / "freeze-summary.json", summary)
        return summary
    except BaseException:
        for child in output.iterdir():
            child.unlink(missing_ok=True)
        output.rmdir()
        raise


def init_main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a repository-external private AM-Eval gold workspace."
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument("--confirm", required=True)
    arguments = parser.parse_args()
    if arguments.confirm != INIT_CONFIRMATION:
        parser.error(f"--confirm must be {INIT_CONFIRMATION}")
    try:
        result = initialize_private_gold_workspace(arguments.directory)
    except (DatasetError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def validate_main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate redaction, review and split gates for private AM-Eval gold."
    )
    parser.add_argument("cases", type=Path)
    arguments = parser.parse_args()
    try:
        source = validate_private_input(arguments.cases)
        result = validate_private_gold_cases(load_jsonl(source))
    except (DatasetError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def freeze_main() -> None:
    parser = argparse.ArgumentParser(
        description="Freeze reviewed private AM-Eval gold into an immutable manifest."
    )
    parser.add_argument("cases", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--confirm", required=True)
    arguments = parser.parse_args()
    if arguments.confirm != FREEZE_CONFIRMATION:
        parser.error(f"--confirm must be {FREEZE_CONFIRMATION}")
    try:
        result = freeze_private_gold(
            arguments.cases,
            arguments.output,
            dataset_id=arguments.dataset_id,
        )
    except (DatasetError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    print("use an agent-memory private gold console entrypoint", file=sys.stderr)
    raise SystemExit(2)
