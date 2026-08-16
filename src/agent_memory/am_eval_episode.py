from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection, connect

from .am_eval_atomic_runner import (
    discover_runtime_source_root,
    resolve_runtime_identity,
    validate_isolated_database_url,
    validate_private_output,
    write_private_json,
)
from .am_eval_dataset import DatasetError, load_dataset_snapshot
from .am_eval_environment import runtime_environment_identity
from .current_state import upsert_current_item
from .ids import stable_uuid
from .repository import ingest_turn, trace_memory
from .schemas import IngestEvent, IngestTurnRequest, ProviderContext
from .unified_memory import (
    create_procedure,
    parse_date_range,
    parse_episode,
    parse_temporal_rule,
    procedure_applicability,
    process_unified_turn,
    set_episode_review,
    set_procedure_state,
)

DATASET_ID = "agent-memory-episode-procedure-gold-v1"
EXPECTED_MANIFEST_SHA256 = "8f212b3c435a7109a03ada28cf0a015f0f07339354e79a92dad4f4e3ed9b6fb7"
EXPECTED_CASE_FILE_SHA256 = "6e85413ef239ab46cf5caf9fb8057f0935b5976f3cb3380c2e500df27f8c0b6f"
EXPECTED_SUITE_COUNTS = {
    "date_range": 6,
    "episode": 4,
    "episode_procedure_db": 3,
    "procedure": 4,
    "temporal_rule": 4,
}
EXPECTED_SPLIT_COUNTS = {"development": 11, "validation": 10}
EPISODE_PROCEDURE_RESULT_SCHEMA_VERSION = "am-eval-episode-procedure-run-v1"


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise DatasetError(message)


def validate_episode_procedure_dataset(
    manifest: dict[str, Any], cases: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    if manifest.get("dataset_id") != DATASET_ID:
        raise DatasetError(f"episode/procedure dataset_id must be {DATASET_ID}")
    if (
        manifest.get("contains_production_data") is not False
        or manifest.get("contains_memory_text") is not True
        or manifest.get("external_data_sent") is not False
        or manifest.get("visibility") != "open"
        or manifest.get("blind_cases") != 0
    ):
        raise DatasetError(
            "episode/procedure gold must be synthetic, open, local-only, and non-blind"
        )
    expected_file = {
        "path": "cases.jsonl",
        "sha256": EXPECTED_CASE_FILE_SHA256,
        "case_count": 21,
        "suites": sorted(EXPECTED_SUITE_COUNTS),
    }
    if manifest.get("files") != [expected_file]:
        raise DatasetError(
            "episode/procedure file contract differs from the official frozen dataset"
        )
    suite_counts = Counter(case["suite"] for case in cases)
    split_counts = Counter(case["split"] for case in cases)
    if (
        len(cases) != 21
        or manifest.get("case_count") != 21
        or manifest.get("suite_counts") != EXPECTED_SUITE_COUNTS
        or dict(sorted(suite_counts.items())) != EXPECTED_SUITE_COUNTS
        or dict(sorted(split_counts.items())) != EXPECTED_SPLIT_COUNTS
    ):
        raise DatasetError("episode/procedure gold case coverage is incomplete")
    expected_ids = {
        *(f"date-{index:03d}" for index in range(1, 7)),
        *(f"time-{index:03d}" for index in range(1, 5)),
        *(f"episode-{index:03d}" for index in range(1, 5)),
        *(f"procedure-{index:03d}" for index in range(1, 5)),
        "supersession-current-001",
        "supersession-preference-001",
        "lineage-procedure-001",
    }
    if {case["case_id"] for case in cases} != expected_ids:
        raise DatasetError("episode/procedure case IDs differ from the frozen dataset")
    return {
        "schema_version": "am-eval-episode-procedure-validation-v1",
        "dataset_id": DATASET_ID,
        "case_count": 21,
        "suite_counts": EXPECTED_SUITE_COUNTS,
        "split_counts": EXPECTED_SPLIT_COUNTS,
        "status": "PASS",
        "contains_production_data": False,
        "external_data_sent": False,
    }


def _run_temporal_case(case: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    if case["suite"] == "date_range":
        started_at, ended_at, precision, resolution = parse_date_range(
            case["input"]["text"], datetime.fromisoformat(case["input"]["occurred_at"])
        )
        actual = {
            "started_at": started_at.isoformat() if started_at else None,
            "ended_at": ended_at.isoformat() if ended_at else None,
            "precision": precision,
        }
        passed = all(actual[key] == expected[key] for key in actual) and all(
            resolution.get(key) == value for key, value in expected["resolution"].items()
        )
    else:
        result = parse_temporal_rule(case["input"]["text"])
        passed = (result is not None) is expected["selected"]
        if result is not None:
            passed = passed and all(
                getattr(result, key) == expected[key]
                for key in ("rule_type", "label", "month", "day", "year")
            )
    return {"case_id": case["case_id"], "passed": passed, "suite": case["suite"]}


def _run_episode_case(case: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    result = parse_episode(
        case["input"]["text"], datetime.fromisoformat(case["input"]["occurred_at"])
    )
    selected = result is not None
    expected_pairs = {tuple(item) for item in expected.get("entities", [])}
    actual_pairs = (
        {(item.name, item.role) for item in result.entities} if result is not None else set()
    )
    source_profile_confused = bool(
        result is not None
        and case["input"]["source_profile"].casefold()
        in {item.name.casefold() for item in result.entities}
    )
    structure_exact = selected is expected["selected"]
    if result is not None:
        structure_exact = structure_exact and (
            result.episode_type == expected["episode_type"]
            and result.accepted is expected["accepted"]
            and [(item.name, item.role) for item in result.entities]
            == [tuple(item) for item in expected["entities"]]
            and [item.kind for item in result.steps] == expected["steps"]
            and not {item.name for item in result.entities} & set(expected["excluded_entities"])
        )
    return {
        "case_id": case["case_id"],
        "entity_role_correct": len(expected_pairs & actual_pairs),
        "entity_role_expected": len(expected_pairs),
        "entity_role_unexpected": len(actual_pairs - expected_pairs),
        "selected": selected,
        "source_profile_confused": source_profile_confused,
        "structure_exact": structure_exact,
    }


def _run_procedure_case(case: dict[str, Any]) -> dict[str, Any]:
    values = case["input"]
    result = procedure_applicability(
        values["expected_environment"],
        values["actual_environment"],
        valid_to=(datetime.fromisoformat(values["valid_to"]) if values.get("valid_to") else None),
        now=datetime.fromisoformat(values["now"]),
    )
    expected = case["expected"]
    return {
        "auto_apply": result["auto_apply"],
        "case_id": case["case_id"],
        "expected_status": expected["status"],
        "status": result["status"],
        "status_exact": result["status"] == expected["status"],
    }


def _context(namespace: str, case_id: str) -> ProviderContext:
    return ProviderContext(
        shared_namespace=namespace,
        source_profile="am-eval",
        source_instance="episode-procedure-runner",
        external_session_id="episode-procedure-gate",
        external_turn_id=case_id,
        correlation_id=uuid4(),
    )


def _ingest(
    connection: Connection,
    *,
    namespace: str,
    case_id: str,
    events: list[IngestEvent],
) -> tuple[list[UUID], UUID]:
    event_ids, _job_ids, duplicate = ingest_turn(
        connection,
        IngestTurnRequest(
            context=_context(namespace, case_id),
            idempotency_key=f"episode-procedure:{case_id}",
            occurred_at=datetime(2026, 7, 12, 8, 30, tzinfo=UTC),
            events=events,
        ),
    )
    if duplicate or len(event_ids) != len(events):
        raise DatasetError(f"database case {case_id} did not create its frozen events")
    turn_id = connection.execute(
        "SELECT turn_id FROM evidence.events WHERE id=%s", (event_ids[0],)
    ).fetchone()[0]
    return event_ids, turn_id


def _insert_fact(
    connection: Connection,
    *,
    namespace_id: UUID,
    case_id: str,
    event_id: UUID,
    statement: str,
    fact_type: str,
) -> UUID:
    fact_id = stable_uuid("fact", f"{namespace_id}:{case_id}")
    connection.execute(
        """INSERT INTO memory.facts(
             id,namespace_id,statement,fact_type,confidence,memory_state,source_profile,
             extraction_method,valid_from
           ) VALUES (%s,%s,%s,%s,0.95,'active','am-eval','episode-procedure-v1',%s)""",
        (
            fact_id,
            namespace_id,
            statement,
            fact_type,
            datetime(2026, 7, 12, 8, 30, tzinfo=UTC),
        ),
    )
    connection.execute(
        "INSERT INTO memory.fact_evidence(fact_id,event_id) VALUES (%s,%s)",
        (fact_id, event_id),
    )
    connection.execute(
        """INSERT INTO retrieval.documents(
             id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state
           ) VALUES (%s,%s,'fact',%s,%s,'active')""",
        (stable_uuid("document", str(fact_id)), namespace_id, fact_id, statement),
    )
    return fact_id


def _run_current_supersession(connection: Connection, *, namespace: str, case_id: str) -> set[str]:
    namespace_id = stable_uuid("namespace", namespace)
    first_events, _turn = _ingest(
        connection,
        namespace=namespace,
        case_id=f"{case_id}-old",
        events=[IngestEvent(type="user_message", sequence=1, content="当前邮件提醒已暂停")],
    )
    old_fact = _insert_fact(
        connection,
        namespace_id=namespace_id,
        case_id=f"{case_id}-old",
        event_id=first_events[0],
        statement="当前邮件提醒已暂停",
        fact_type="current",
    )
    first = upsert_current_item(
        connection,
        namespace_id=namespace_id,
        topic_key="mail-reminder",
        summary="邮件提醒暂停",
        valid_from=datetime(2026, 7, 12, 8, 30, tzinfo=UTC),
        expires_at=datetime(2026, 7, 13, 8, 30, tzinfo=UTC),
        source_fact_id=old_fact,
        actor_type="worker",
        actor_id="episode-procedure-runner",
        reason="frozen current supersession case",
    )
    second_events, _turn = _ingest(
        connection,
        namespace=namespace,
        case_id=f"{case_id}-new",
        events=[IngestEvent(type="user_message", sequence=1, content="当前邮件提醒已恢复")],
    )
    new_fact = _insert_fact(
        connection,
        namespace_id=namespace_id,
        case_id=f"{case_id}-new",
        event_id=second_events[0],
        statement="当前邮件提醒已恢复",
        fact_type="current",
    )
    upsert_current_item(
        connection,
        namespace_id=namespace_id,
        topic_key="mail-reminder",
        summary="邮件提醒恢复",
        valid_from=datetime(2026, 7, 12, 9, 30, tzinfo=UTC),
        expires_at=datetime(2026, 7, 13, 9, 30, tzinfo=UTC),
        source_fact_id=new_fact,
        actor_type="worker",
        actor_id="episode-procedure-runner",
        reason="frozen current supersession case",
        expected_version=first["version"],
    )
    invariants: set[str] = set()
    states = dict(
        connection.execute(
            "SELECT id,memory_state FROM memory.facts WHERE id=ANY(%s)",
            ([old_fact, new_fact],),
        ).fetchall()
    )
    if states.get(old_fact) == "dormant":
        invariants.add("old_fact_dormant")
    if states.get(new_fact) == "active":
        invariants.add("new_fact_active")
    current = connection.execute(
        """SELECT source_fact_id FROM state.current_items
           WHERE namespace_id=%s AND topic_key='mail-reminder' AND status='active'""",
        (namespace_id,),
    ).fetchall()
    if current == [(new_fact,)]:
        invariants.add("single_current_item")
    audit_count = connection.execute(
        """SELECT count(*) FROM audit.events WHERE namespace_id=%s
           AND target_type='current_state' AND action IN ('state.set','state.update')""",
        (namespace_id,),
    ).fetchone()[0]
    if audit_count == 2:
        invariants.add("current_audit_preserved")
    return invariants


def _run_preference_supersession(
    connection: Connection, *, namespace: str, case_id: str
) -> set[str]:
    namespace_id = stable_uuid("namespace", namespace)
    event_ids: list[UUID] = []
    for suffix, content in (("old", "以后通过邮件提醒我"), ("new", "以后通过短信提醒我")):
        events, turn_id = _ingest(
            connection,
            namespace=namespace,
            case_id=f"{case_id}-{suffix}",
            events=[IngestEvent(type="user_message", sequence=1, content=content)],
        )
        event_ids.extend(events)
        process_unified_turn(
            connection,
            (uuid4(), namespace_id, "unified_rebuild", turn_id, 1),
        )
    rows = connection.execute(
        """SELECT id,state,supersedes_id FROM memory.preference_assertions
           WHERE namespace_id=%s AND aspect='提醒方式'""",
        (namespace_id,),
    ).fetchall()
    prior = next((row for row in rows if row[1] == "superseded"), None)
    replacement = next((row for row in rows if row[1] == "active"), None)
    invariants: set[str] = set()
    if len(rows) == 2 and prior is not None:
        invariants.add("prior_preference_superseded")
    if len(rows) == 2 and replacement is not None:
        invariants.add("replacement_preference_active")
    if prior is not None and replacement is not None and replacement[2] == prior[0]:
        invariants.add("supersedes_link_exact")
    evidence_count = connection.execute(
        """SELECT count(DISTINCT event_id) FROM memory.preference_evidence
           WHERE preference_id=ANY(%s)""",
        ([row[0] for row in rows],),
    ).fetchone()[0]
    if len(rows) == 2 and evidence_count == len(event_ids) == 2:
        invariants.add("preference_evidence_preserved")
    return invariants


def _run_procedure_lineage(connection: Connection, *, namespace: str, case_id: str) -> set[str]:
    namespace_id = stable_uuid("namespace", namespace)
    events, turn_id = _ingest(
        connection,
        namespace=namespace,
        case_id=case_id,
        events=[
            IngestEvent(
                type="user_message",
                sequence=1,
                content="当前 n8n 服务异常，后续继续排查",
            ),
            IngestEvent(
                type="tool_result",
                tool_name="health_probe",
                sequence=2,
                content="n8n 已修复，验证通过",
            ),
        ],
    )
    fact_id = _insert_fact(
        connection,
        namespace_id=namespace_id,
        case_id=case_id,
        event_id=events[0],
        statement="n8n 服务异常已排查并恢复",
        fact_type="long_term",
    )
    process_unified_turn(
        connection,
        (uuid4(), namespace_id, "unified_rebuild", turn_id, 1),
    )
    episode = connection.execute(
        """SELECT id,version FROM memory.episodes WHERE namespace_id=%s
           AND episode_type='technical'""",
        (namespace_id,),
    ).fetchone()
    if episode is None:
        raise DatasetError("procedure lineage did not create a technical episode")
    confirmed_episode = set_episode_review(
        connection,
        namespace_key=namespace,
        episode_id=episode[0],
        expected_version=episode[1],
        action="confirm",
        actor_id="episode-procedure-runner",
        reason="frozen procedure lineage case",
        correlation_id=uuid4(),
    )
    procedure = create_procedure(
        connection,
        namespace_key=namespace,
        title="n8n recovery procedure",
        goal="restore n8n and verify health",
        scope={"service": "n8n"},
        preconditions=["target environment confirmed"],
        environment_fingerprint={"host": "test-host", "service": "n8n"},
        risk_level="medium",
        valid_from=None,
        valid_to=None,
        episode_id=episode[0],
        steps=[
            {
                "instruction": "check n8n service health",
                "expected_observation": "service state is readable",
                "success_condition": "n8n healthy",
                "failure_condition": "service unavailable",
                "stop_condition": "stop on environment mismatch or missing permission",
                "required_permission": "read-only",
                "risk_level": "low",
            }
        ],
        supersedes_procedure_id=None,
        expected_superseded_version=None,
        actor_id="episode-procedure-runner",
        reason="frozen procedure lineage case",
        correlation_id=uuid4(),
    )
    active = set_procedure_state(
        connection,
        namespace_key=namespace,
        procedure_id=procedure["id"],
        expected_version=procedure["version"],
        action="confirm",
        actor_id="episode-procedure-runner",
        reason="frozen procedure lineage case",
        correlation_id=uuid4(),
    )
    invariants: set[str] = set()
    if active and active["state"] == "active":
        invariants.add("procedure_active")
    if confirmed_episode and confirmed_episode["state"] == "active":
        invariants.add("support_episode_active")
    support = connection.execute(
        """SELECT count(DISTINCT support.episode_id),
                  count(DISTINCT episode_fact.fact_id),
                  count(DISTINCT fact_evidence.event_id),
                  count(DISTINCT step.id) FILTER (
                    WHERE step.status='confirmed'
                      AND step.step_kind IN ('result','resolution','verification')
                  )
           FROM memory.procedure_support support
           LEFT JOIN memory.episode_facts episode_fact
             ON episode_fact.episode_id=support.episode_id
           LEFT JOIN memory.fact_evidence fact_evidence
             ON fact_evidence.fact_id=episode_fact.fact_id
           LEFT JOIN memory.episode_steps step ON step.episode_id=support.episode_id
           WHERE support.procedure_id=%s AND support.support_kind='success'""",
        (procedure["id"],),
    ).fetchone()
    if support[0] == 1:
        invariants.add("support_episode_active")
    if support[1] == 1:
        invariants.add("support_fact_linked")
    trace = trace_memory(connection, namespace, fact_id)
    if (
        support[2] == 1
        and trace is not None
        and [item.evidence_id for item in trace.evidence] == [events[0]]
    ):
        invariants.add("support_evidence_linked")
    if support[3] >= 1:
        invariants.add("verified_step_present")
    applicability = procedure_applicability(
        active["environment_fingerprint"] if active else {},
        {"host": "test-host", "service": "n8n"},
    )
    if applicability["auto_apply"] is False:
        invariants.add("procedure_auto_apply_false")
    return invariants


def _run_database_cases(
    connection: Connection,
    *,
    cases: tuple[dict[str, Any], ...],
    namespace: str,
) -> list[dict[str, Any]]:
    if connection.execute("SELECT count(*) FROM core.namespaces").fetchone()[0] != 0:
        raise DatasetError("episode/procedure runner requires a dedicated empty database")
    runners = {
        "current_supersession": _run_current_supersession,
        "preference_supersession": _run_preference_supersession,
        "procedure_lineage": _run_procedure_lineage,
    }
    ledger: list[dict[str, Any]] = []
    for case in cases:
        scenario = case["input"]["scenario"]
        actual = runners[scenario](connection, namespace=namespace, case_id=case["case_id"])
        expected = set(case["expected"]["invariants"])
        ledger.append(
            {
                "case_id": case["case_id"],
                "invariants": {name: name in actual for name in sorted(expected)},
                "passed": actual == expected,
                "scenario": scenario,
            }
        )
    connection.commit()
    return ledger


def run_episode_procedure_cases(
    connection: Connection,
    *,
    cases: tuple[dict[str, Any], ...],
    namespace: str,
) -> dict[str, Any]:
    if not namespace.startswith("hermes:automated-tests:"):
        raise DatasetError("episode/procedure namespace must be automated")
    temporal_ledger = [
        _run_temporal_case(case)
        for case in cases
        if case["suite"] in {"date_range", "temporal_rule"}
    ]
    episode_ledger = [_run_episode_case(case) for case in cases if case["suite"] == "episode"]
    procedure_ledger = [_run_procedure_case(case) for case in cases if case["suite"] == "procedure"]
    database_cases = tuple(case for case in cases if case["suite"] == "episode_procedure_db")
    database_ledger = _run_database_cases(connection, cases=database_cases, namespace=namespace)
    selected_episodes = [item for item in episode_ledger if item["selected"]]
    dangerous_cases = [item for item in procedure_ledger if item["expected_status"] != "applicable"]
    supersession_cases = [
        item for item in database_ledger if item["scenario"].endswith("supersession")
    ]
    lineage_cases = [item for item in database_ledger if item["scenario"] == "procedure_lineage"]
    counts = {
        "temporal_cases": len(temporal_ledger),
        "temporal_passed": sum(item["passed"] for item in temporal_ledger),
        "episode_cases": len(episode_ledger),
        "episode_passed": sum(item["structure_exact"] for item in episode_ledger),
        "selected_episode_cases": len(selected_episodes),
        "profile_subject_confusions": sum(
            item["source_profile_confused"] for item in selected_episodes
        ),
        "entity_role_expected": sum(item["entity_role_expected"] for item in episode_ledger),
        "entity_role_correct": sum(item["entity_role_correct"] for item in episode_ledger),
        "entity_role_unexpected": sum(item["entity_role_unexpected"] for item in episode_ledger),
        "procedure_cases": len(procedure_ledger),
        "procedure_status_passed": sum(item["status_exact"] for item in procedure_ledger),
        "unauthorized_auto_apply": sum(item["auto_apply"] for item in procedure_ledger),
        "dangerous_procedure_cases": len(dangerous_cases),
        "dangerous_auto_apply": sum(item["auto_apply"] for item in dangerous_cases),
        "database_cases": len(database_ledger),
        "database_passed": sum(item["passed"] for item in database_ledger),
        "supersession_cases": len(supersession_cases),
        "supersession_passed": sum(item["passed"] for item in supersession_cases),
        "procedure_lineage_cases": len(lineage_cases),
        "procedure_lineage_passed": sum(item["passed"] for item in lineage_cases),
    }
    passed = counts == {
        "temporal_cases": 10,
        "temporal_passed": 10,
        "episode_cases": 4,
        "episode_passed": 4,
        "selected_episode_cases": 2,
        "profile_subject_confusions": 0,
        "entity_role_expected": 6,
        "entity_role_correct": 6,
        "entity_role_unexpected": 0,
        "procedure_cases": 4,
        "procedure_status_passed": 4,
        "unauthorized_auto_apply": 0,
        "dangerous_procedure_cases": 3,
        "dangerous_auto_apply": 0,
        "database_cases": 3,
        "database_passed": 3,
        "supersession_cases": 2,
        "supersession_passed": 2,
        "procedure_lineage_cases": 1,
        "procedure_lineage_passed": 1,
    }
    return {
        "schema_version": "am-eval-episode-procedure-ledger-v1",
        "status": "PASS" if passed else "FAIL",
        "case_count": len(cases),
        "counts": counts,
        "temporal_ledger": temporal_ledger,
        "episode_ledger": episode_ledger,
        "procedure_ledger": procedure_ledger,
        "database_ledger": database_ledger,
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
        "model_called": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run frozen temporal, episode, and procedure probes in an isolated database."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", default="hermes:automated-tests:am-eval-episode")
    parser.add_argument("--confirm-sha256", required=True)
    arguments = parser.parse_args()
    output_target = None
    try:
        dataset = load_dataset_snapshot(arguments.manifest)
        if arguments.confirm_sha256.casefold() != dataset.manifest_sha256:
            raise DatasetError("--confirm-sha256 does not match the frozen manifest")
        if dataset.manifest_sha256 != EXPECTED_MANIFEST_SHA256:
            raise DatasetError(
                "episode/procedure manifest differs from the official frozen dataset"
            )
        validation = validate_episode_procedure_dataset(dataset.manifest, dataset.cases)
        database_url = os.getenv("AGENT_MEMORY_DATABASE_URL", "")
        if not database_url:
            raise DatasetError("AGENT_MEMORY_DATABASE_URL is required")
        validate_isolated_database_url(database_url)
        output_target = validate_private_output(
            arguments.output,
            forbidden_root=discover_runtime_source_root(),
        )
        runtime_identity = resolve_runtime_identity()
        runtime_environment = runtime_environment_identity()
        with connect(database_url) as connection:
            result = run_episode_procedure_cases(
                connection,
                cases=dataset.cases,
                namespace=arguments.namespace,
            )
        output = {
            **result,
            "schema_version": EPISODE_PROCEDURE_RESULT_SCHEMA_VERSION,
            "run_id": arguments.namespace,
            "dataset_id": DATASET_ID,
            "manifest_sha256": dataset.manifest_sha256,
            "dataset_visibility": dataset.manifest["visibility"],
            "dataset_blind": False,
            "dataset_contains_memory_text": dataset.manifest["contains_memory_text"],
            "dataset_validation": validation["status"],
            "system": {
                "environment_sha256": runtime_environment["sha256"],
                "name": "agent-memory",
                "revision": runtime_identity.revision,
                "source_file_count": runtime_identity.source_file_count,
                "source_sha256": runtime_identity.source_sha256,
                "version": runtime_identity.version,
            },
            "runner_runtime_identity": {
                "provenance": runtime_identity.provenance,
                "revision": runtime_identity.revision,
                "source_file_count": runtime_identity.source_file_count,
                "source_sha256": runtime_identity.source_sha256,
                "version": runtime_identity.version,
            },
            "runner_runtime_environment": runtime_environment,
        }
        write_private_json(output_target, output)
    except (DatasetError, json.JSONDecodeError, OSError, ValueError) as error:
        if output_target is not None:
            output_target.close()
        parser.error(str(error))
    print(
        json.dumps(
            {
                "status": output["status"],
                "case_count": output["case_count"],
                "counts": output["counts"],
                "output": str(arguments.output.expanduser().resolve()),
            },
            sort_keys=True,
        )
    )
    if output["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
