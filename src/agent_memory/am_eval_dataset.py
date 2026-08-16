from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class DatasetError(ValueError):
    """Raised when an AM-Eval dataset fails its frozen-data contract."""


class PathLike(Protocol):
    def __fspath__(self) -> str: ...


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    payload: bytes
    sha256: str


@dataclass(frozen=True)
class DatasetSnapshot:
    path: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    cases: tuple[dict[str, Any], ...]


MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


ALLOWED_CASE_SUITES = {
    "atomic_fact",
    "date_range",
    "episode",
    "episode_procedure_db",
    "evidence_integrity",
    "preference",
    "procedure",
    "recall",
    "backup_restore",
    "idempotency",
    "temporal_rule",
    "lifecycle",
    "worker_recovery",
}
ALLOWED_SPLITS = {"development", "validation", "blind"}
ATOMIC_FACT_TYPES = {"long_term", "stage", "current", "observed"}
ATOMIC_MEMORY_STATES = {"active", "candidate", "evidence_only"}
ATOMIC_EVIDENCE_TYPES = {"user_message", "tool_result", "environment_observation"}
LIFECYCLE_ACTIONS = {
    "confirm",
    "correct",
    "forget",
    "isolate",
    "purge",
    "current_resolve",
    "current_expire",
    "entity_merge",
    "entity_unmerge",
    "entity_split",
}
LIFECYCLE_INVARIANTS = {
    "action_audited",
    "correction_evidence_preserved",
    "current_hidden",
    "entity_links_preserved",
    "evidence_preserved",
    "namespace_denied",
    "purge_confirmation_required",
    "purge_residue_zero",
    "recall_excluded",
    "state_changed",
    "stale_version_rejected",
}
EVIDENCE_FINDING_KINDS = {
    "aws_access_key",
    "cn_id",
    "credential_assignment",
    "private_key",
    "provider_api_key",
}
EPISODE_PROCEDURE_DB_SCENARIOS = {
    "current_supersession": {
        "current_audit_preserved",
        "new_fact_active",
        "old_fact_dormant",
        "single_current_item",
    },
    "preference_supersession": {
        "preference_evidence_preserved",
        "prior_preference_superseded",
        "replacement_preference_active",
        "supersedes_link_exact",
    },
    "procedure_lineage": {
        "procedure_active",
        "procedure_auto_apply_false",
        "support_episode_active",
        "support_evidence_linked",
        "support_fact_linked",
        "verified_step_present",
    },
}
RELIABILITY_SCENARIOS = {
    "pending_outage": {
        "evidence_persisted_before_recovery",
        "jobs_pending_before_recovery",
        "jobs_done_after_recovery",
        "evidence_hash_unchanged",
    },
    "expired_lease": {
        "expired_running_job_reclaimed",
        "attempt_count_incremented",
        "job_done_after_recovery",
        "evidence_hash_unchanged",
    },
    "sequential_replay": {
        "single_winner",
        "single_event",
        "single_turn",
        "single_job_set",
    },
    "concurrent_replay": {
        "single_winner",
        "single_event",
        "single_turn",
        "single_job_set",
    },
    "pg_dump_restore": {
        "all_table_counts_match",
        "evidence_hash_matches",
        "vault_ciphertext_matches",
        "vault_decrypts_with_same_root_key",
        "migration_revision_matches",
    },
}


def _absolute_path(path: Path | PathLike) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _open_snapshot_file(path: Path | PathLike) -> tuple[Path, int]:
    absolute = _absolute_path(path)
    components = absolute.parts[1:]
    if not components or components[-1] in {"", ".", ".."}:
        raise DatasetError(f"dataset path must name a file: {absolute}")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(absolute.anchor, directory_flags)
    try:
        for component in components[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(components[-1], file_flags, dir_fd=directory_fd)
    except OSError as error:
        raise DatasetError(f"dataset path cannot be opened safely: {absolute}") from error
    finally:
        os.close(directory_fd)
    os.set_inheritable(descriptor, False)
    return absolute, descriptor


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_file_snapshot(path: Path | PathLike) -> FileSnapshot:
    absolute, descriptor = _open_snapshot_file(path)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise DatasetError(f"dataset path must be a single-link regular file: {absolute}")
        if before.st_size > MAX_SNAPSHOT_BYTES:
            raise DatasetError(f"dataset file exceeds the snapshot size limit: {absolute}")
        payload = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            payload.extend(chunk)
            if len(payload) > MAX_SNAPSHOT_BYTES:
                raise DatasetError(f"dataset file exceeds the snapshot size limit: {absolute}")
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or len(payload) != after.st_size:
            raise DatasetError(f"dataset file changed while being read: {absolute}")
        frozen_payload = bytes(payload)
        return FileSnapshot(
            path=absolute,
            payload=frozen_payload,
            sha256=_payload_sha256(frozen_payload),
        )
    except OSError as error:
        raise DatasetError(f"dataset path cannot be read safely: {absolute}") from error
    finally:
        os.close(descriptor)


def sha256_file(path: Path | PathLike) -> str:
    return read_file_snapshot(path).sha256


def _parse_jsonl(payload: bytes, *, path: Path) -> tuple[dict[str, Any], ...]:
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise DatasetError(f"invalid UTF-8 JSONL at {path}") from error
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
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
        if not isinstance(case.get("input"), dict) or not isinstance(case.get("expected"), dict):
            raise DatasetError(f"case {case_id} requires input and expected objects")
        seen_ids.add(case_id)
        cases.append(case)
    if not cases:
        raise DatasetError(f"dataset file is empty: {path}")
    return tuple(cases)


def load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    snapshot = read_file_snapshot(path)
    return _parse_jsonl(snapshot.payload, path=snapshot.path)


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


def validate_lifecycle_case(case: dict[str, Any]) -> None:
    """Validate one deterministic lifecycle operation and its required invariants."""
    if case.get("suite") != "lifecycle":
        return
    lifecycle_input = case["input"]
    expected = case["expected"]
    action = lifecycle_input.get("action")
    variant = lifecycle_input.get("variant")
    invariants = expected.get("invariants")
    if action not in LIFECYCLE_ACTIONS:
        raise DatasetError(f"lifecycle case {case['case_id']} has an invalid action")
    if not isinstance(variant, str) or not variant or len(variant) > 64:
        raise DatasetError(f"lifecycle case {case['case_id']} requires a bounded variant")
    if set(lifecycle_input) != {"action", "variant"}:
        raise DatasetError(f"lifecycle case {case['case_id']} has unsupported input fields")
    if expected.get("success") is not True:
        raise DatasetError(f"lifecycle case {case['case_id']} requires expected.success=true")
    if (
        not isinstance(invariants, list)
        or not invariants
        or len(invariants) != len(set(invariants))
        or any(item not in LIFECYCLE_INVARIANTS for item in invariants)
    ):
        raise DatasetError(f"lifecycle case {case['case_id']} has invalid invariants")
    if set(expected) != {"success", "invariants"}:
        raise DatasetError(f"lifecycle case {case['case_id']} has unsupported expected fields")


def validate_evidence_integrity_case(case: dict[str, Any]) -> None:
    """Validate one synthetic persisted-redaction and evidence-trace probe."""
    if case.get("suite") != "evidence_integrity":
        return
    evidence_input = case["input"]
    expected = case["expected"]
    if set(evidence_input) != {"forbidden_fragments", "text"}:
        raise DatasetError(
            f"evidence integrity case {case['case_id']} has unsupported input fields"
        )
    text = evidence_input.get("text")
    forbidden = evidence_input.get("forbidden_fragments")
    if (
        not isinstance(text, str)
        or not text
        or not isinstance(forbidden, list)
        or len(forbidden) != len(set(forbidden))
        or not all(isinstance(item, str) and item and item in text for item in forbidden)
    ):
        raise DatasetError(f"evidence integrity case {case['case_id']} has invalid probes")
    finding_kinds = expected.get("finding_kinds")
    if (
        set(expected) != {"finding_kinds", "persisted_surface_count"}
        or not isinstance(finding_kinds, list)
        or any(item not in EVIDENCE_FINDING_KINDS for item in finding_kinds)
        or expected.get("persisted_surface_count") != 3
    ):
        raise DatasetError(f"evidence integrity case {case['case_id']} has invalid expectations")


def validate_episode_procedure_db_case(case: dict[str, Any]) -> None:
    """Validate one metadata-only database scenario for temporal/procedure scoring."""
    if case.get("suite") != "episode_procedure_db":
        return
    scenario_input = case["input"]
    expected = case["expected"]
    if set(scenario_input) != {"scenario"} or set(expected) != {"invariants"}:
        raise DatasetError(f"episode/procedure DB case {case['case_id']} has an invalid schema")
    scenario = scenario_input.get("scenario")
    invariants = expected.get("invariants")
    if (
        scenario not in EPISODE_PROCEDURE_DB_SCENARIOS
        or not isinstance(invariants, list)
        or len(invariants) != len(set(invariants))
        or set(invariants) != EPISODE_PROCEDURE_DB_SCENARIOS[scenario]
    ):
        raise DatasetError(f"episode/procedure DB case {case['case_id']} has invalid invariants")


def validate_reliability_case(case: dict[str, Any]) -> None:
    """Validate one metadata-only worker, idempotency, or restore contract."""
    if case.get("suite") not in {"worker_recovery", "idempotency", "backup_restore"}:
        return
    scenario_input = case["input"]
    expected = case["expected"]
    if set(expected) != {"invariants"} or set(scenario_input) not in (
        {"scenario"},
        {"attempts", "scenario"},
    ):
        raise DatasetError(f"reliability case {case['case_id']} has an invalid schema")
    scenario = scenario_input.get("scenario")
    invariants = expected.get("invariants")
    if (
        scenario not in RELIABILITY_SCENARIOS
        or not isinstance(invariants, list)
        or len(invariants) != len(set(invariants))
        or set(invariants) != RELIABILITY_SCENARIOS[scenario]
    ):
        raise DatasetError(f"reliability case {case['case_id']} has invalid invariants")
    attempts = scenario_input.get("attempts")
    if scenario == "sequential_replay" and attempts != 2:
        raise DatasetError("sequential reliability replay requires two attempts")
    if scenario == "concurrent_replay" and attempts != 8:
        raise DatasetError("concurrent reliability replay requires eight attempts")
    if scenario not in {"sequential_replay", "concurrent_replay"} and attempts is not None:
        raise DatasetError(f"reliability case {case['case_id']} cannot declare attempts")


def load_dataset_snapshot(manifest_path: Path) -> DatasetSnapshot:
    manifest_snapshot = read_file_snapshot(manifest_path)
    try:
        manifest = json.loads(manifest_snapshot.payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DatasetError(f"invalid dataset manifest: {manifest_snapshot.path}") from error
    if not isinstance(manifest, dict):
        raise DatasetError(f"invalid dataset manifest: {manifest_snapshot.path}")
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

    root = manifest_snapshot.path.parent
    root_absolute = _absolute_path(root)
    all_cases: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise DatasetError("dataset manifest file entries must be objects")
        relative = str(item.get("path") or "")
        if not relative or relative in seen_paths:
            raise DatasetError(f"missing or duplicate dataset path: {relative!r}")
        candidate = _absolute_path(root / relative)
        try:
            candidate.relative_to(root_absolute)
        except ValueError as error:
            raise DatasetError(f"dataset path escapes manifest root: {relative}") from error
        file_snapshot = read_file_snapshot(candidate)
        if file_snapshot.sha256 != item.get("sha256"):
            raise DatasetError(f"dataset SHA-256 mismatch: {relative}")
        cases = _parse_jsonl(file_snapshot.payload, path=file_snapshot.path)
        for case in cases:
            validate_atomic_fact_case(case)
            validate_evidence_integrity_case(case)
            validate_episode_procedure_db_case(case)
            validate_lifecycle_case(case)
            validate_reliability_case(case)
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
    return DatasetSnapshot(
        path=manifest_snapshot.path,
        manifest=manifest,
        manifest_sha256=manifest_snapshot.sha256,
        cases=tuple(all_cases),
    )


def load_dataset(manifest_path: Path) -> tuple[dict[str, Any], ...]:
    return load_dataset_snapshot(manifest_path).cases


def dataset_summary(manifest_path: Path) -> dict[str, Any]:
    snapshot = load_dataset_snapshot(manifest_path)
    return {
        "schema_version": "am-eval-dataset-validation-v1",
        "dataset_id": snapshot.manifest["dataset_id"],
        "manifest_sha256": snapshot.manifest_sha256,
        "case_count": len(snapshot.cases),
        "suite_counts": dict(sorted(Counter(case["suite"] for case in snapshot.cases).items())),
        "split_counts": dict(sorted(Counter(case["split"] for case in snapshot.cases).items())),
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
