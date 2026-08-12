from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection, connect
from psycopg.conninfo import conninfo_to_dict

from .am_eval_dataset import DatasetError, load_dataset, sha256_file
from .am_eval_private_gold import validate_frozen_private_gold, validate_private_input
from .config import Settings
from .ids import stable_uuid
from .model_adapter import ModelProfile
from .repository import ingest_turn, recall
from .schemas import IngestEvent, IngestTurnRequest, ProviderContext, RecallBudget, RecallRequest
from .worker import (
    ATOMIC_EXTRACTION_VERSION,
    DEFAULT_TRUSTED_OBSERVATION_TOOLS,
    process_one,
    select_turn_evidence,
)

RUNNER_VERSION = "am-eval-atomic-runner-v1"
OUTPUT_SCHEMA_VERSION = "am-eval-atomic-output-v1"
DEFAULT_NAMESPACE = "hermes:automated-tests:am-eval-atomic"
SHA256_CHARACTERS = frozenset("0123456789abcdef")
GIT_REVISION_LENGTH = 40


@dataclass(frozen=True)
class PreparedCase:
    case: dict[str, Any]
    turn_id: UUID
    event_ids: tuple[UUID, ...]


def _validate_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(
        character not in SHA256_CHARACTERS for character in value.casefold()
    ):
        raise DatasetError(f"{name} must be a 64-character SHA-256")


def validate_private_output(path: Path, *, forbidden_root: Path | None = None) -> Path:
    expanded = path.expanduser()
    if any(candidate.is_symlink() for candidate in (expanded, *expanded.parents)):
        raise DatasetError("atomic runner output cannot use a symlink")
    resolved = expanded.resolve()
    if forbidden_root is not None:
        try:
            resolved.relative_to(forbidden_root.resolve())
        except ValueError:
            pass
        else:
            raise DatasetError("atomic runner output must be outside the source repository")
    if resolved.exists():
        raise DatasetError("atomic runner output already exists")
    resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(resolved.parent.stat().st_mode)
    if mode & 0o077:
        raise DatasetError("atomic runner output directory must be mode 0700 or stricter")
    return resolved


def validate_run_metadata(*, run_id: str, system_revision: str, system_version: str) -> None:
    if not run_id.strip():
        raise DatasetError("atomic runner requires a non-empty run ID")
    if (
        len(system_revision) != GIT_REVISION_LENGTH
        or any(
            character not in SHA256_CHARACTERS
            for character in system_revision.casefold()
        )
    ):
        raise DatasetError("system revision must be a 40-character Git commit SHA")
    if not system_version.strip():
        raise DatasetError("atomic runner requires a non-empty system version")


def validate_isolated_database_url(database_url: str) -> dict[str, str]:
    values = conninfo_to_dict(database_url)
    host = values.get("host", "")
    database = values.get("dbname", "")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise DatasetError("atomic runner database must use a loopback host")
    if not database.startswith("am_eval_"):
        raise DatasetError("atomic runner database name must start with am_eval_")
    return values


def validate_evaluation_api_key_file(
    settings: Settings, *, forbidden_root: Path | None = None
) -> Path:
    if settings.model_api_key.get_secret_value():
        raise DatasetError(
            "atomic runner forbids AGENT_MEMORY_MODEL_API_KEY; use a private key file"
        )
    if not settings.model_api_key_file:
        raise DatasetError("AGENT_MEMORY_MODEL_API_KEY_FILE is required")
    expanded = Path(settings.model_api_key_file).expanduser()
    if any(candidate.is_symlink() for candidate in (expanded, *expanded.parents)):
        raise DatasetError("atomic runner API key path cannot use a symlink")
    resolved = expanded.resolve()
    if forbidden_root is not None:
        try:
            resolved.relative_to(forbidden_root.resolve())
        except ValueError:
            pass
        else:
            raise DatasetError("atomic runner API key file must be outside the source repository")
    if not resolved.is_file():
        raise DatasetError("atomic runner API key file must be a regular file")
    parent_mode = stat.S_IMODE(resolved.parent.stat().st_mode)
    if parent_mode & 0o077:
        raise DatasetError("atomic runner API key directory must be mode 0700 or stricter")
    file_stat = resolved.stat()
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise DatasetError("atomic runner API key file must be mode 0600 or stricter")
    if file_stat.st_nlink != 1:
        raise DatasetError("atomic runner API key file cannot have multiple hard links")
    try:
        if not resolved.read_text(encoding="utf-8").strip():
            raise DatasetError("atomic runner API key file is empty")
    except (OSError, UnicodeError) as error:
        raise DatasetError("atomic runner API key file cannot be read") from error
    return resolved


def validate_runtime_settings(
    settings: Settings,
    *,
    namespace: str,
    plan_sha256: str,
    expected_model: str,
    expected_api_base: str,
) -> ModelProfile:
    if namespace != settings.namespace:
        raise DatasetError("runner namespace must match AGENT_MEMORY_NAMESPACE")
    if not namespace.startswith("hermes:automated-tests:"):
        raise DatasetError("atomic runner requires an automated test namespace")
    _validate_sha256(plan_sha256, "plan_sha256")
    if not settings.model_evaluation_mode:
        raise DatasetError("AGENT_MEMORY_MODEL_EVALUATION_MODE must be enabled")
    if settings.model_evaluation_plan_sha.casefold() != plan_sha256.casefold():
        raise DatasetError("configured evaluation plan SHA does not match the manifest")
    if settings.worker_role != "model":
        raise DatasetError("atomic runner requires AGENT_MEMORY_WORKER_ROLE=model")
    if settings.model_auto_backfill_enabled:
        raise DatasetError("atomic runner forbids automatic model backfill")
    if settings.model_max_retries != 0:
        raise DatasetError("atomic runner requires model retries=0 for a fixed request budget")
    if not settings.model_allow_external_data:
        raise DatasetError("external model data authorization is required")
    validate_evaluation_api_key_file(settings, forbidden_root=Path(__file__).parents[2])
    profile = ModelProfile.from_settings(settings)
    if profile.model != expected_model:
        raise DatasetError("configured model differs from --expected-model")
    if (profile.api_base or "").rstrip("/") != expected_api_base.rstrip("/"):
        raise DatasetError("configured API base differs from --expected-api-base")
    if not profile.api_key:
        raise DatasetError("model API key is required")
    return profile


def benchmark_turn_id(*, namespace: str, manifest_sha256: str, case_id: str) -> UUID:
    namespace_id = stable_uuid("namespace", namespace)
    source_id = stable_uuid(
        "source", f"{namespace_id}:am-eval-atomic:benchmark-runner"
    )
    session_id = stable_uuid(
        "session", f"{source_id}:am-eval-atomic:{manifest_sha256[:16]}"
    )
    return stable_uuid("turn", f"{session_id}:{case_id}")


def benchmark_idempotency_key(
    *, namespace: str, manifest_sha256: str, case_id: str
) -> str:
    return (
        f"am-eval-atomic:{stable_uuid('namespace', namespace)}:"
        f"{manifest_sha256}:{case_id}"
    )


def build_plan(
    *, cases: tuple[dict[str, Any], ...], namespace: str, manifest_sha256: str
) -> dict[str, Any]:
    if not namespace.startswith("hermes:automated-tests:"):
        raise DatasetError("atomic benchmark plan requires an automated namespace")
    _validate_sha256(manifest_sha256, "manifest_sha256")
    turn_ids = tuple(
        benchmark_turn_id(
            namespace=namespace,
            manifest_sha256=manifest_sha256,
            case_id=str(case["case_id"]),
        )
        for case in cases
    )
    return {
        "schema_version": "am-eval-atomic-plan-v1",
        "namespace": namespace,
        "manifest_sha256": manifest_sha256,
        "case_count": len(cases),
        "model_call_budget": len(cases),
        "turn_allowlist_csv": ",".join(str(turn_id) for turn_id in turn_ids),
        "contains_memory_text": False,
        "model_called": False,
        "external_data_sent": False,
    }


def prepare_cases(
    connection: Connection,
    *,
    cases: tuple[dict[str, Any], ...],
    namespace: str,
    manifest_sha256: str,
    occurred_at: datetime,
    allowed_tool_names: frozenset[str] = DEFAULT_TRUSTED_OBSERVATION_TOOLS,
) -> tuple[PreparedCase, ...]:
    prepared: list[PreparedCase] = []
    for case in cases:
        case_id = str(case["case_id"])
        external_session_id = f"am-eval-atomic:{manifest_sha256[:16]}"
        external_turn_id = case_id
        context = ProviderContext(
            shared_namespace=namespace,
            source_profile="am-eval-atomic",
            source_instance="benchmark-runner",
            external_session_id=external_session_id,
            external_turn_id=external_turn_id,
            correlation_id=uuid4(),
        )
        evidence = case["input"]["evidence"]
        evidence_types = case["input"].get("evidence_types") or ["user_message"] * len(
            evidence
        )
        tool_names = case["input"].get("tool_names") or [""] * len(evidence)
        selected = select_turn_evidence(
            [
                (
                    index,
                    case["input"]["evidence_ids"][index - 1],
                    evidence_type,
                    evidence[index - 1],
                    occurred_at,
                    tool_names[index - 1],
                )
                for index, evidence_type in enumerate(evidence_types, start=1)
            ],
            allowed_tool_names=allowed_tool_names,
        )
        if [item.content for item in selected] != evidence:
            raise DatasetError(
                f"atomic runner evidence is not fully eligible in production order: {case_id}"
            )
        request = IngestTurnRequest(
            context=context,
            idempotency_key=benchmark_idempotency_key(
                namespace=namespace,
                manifest_sha256=manifest_sha256,
                case_id=case_id,
            ),
            occurred_at=occurred_at,
            events=[
                IngestEvent(
                    type=evidence_types[evidence_index - 1],
                    sequence=evidence_index,
                    content=text,
                    tool_name=tool_names[evidence_index - 1] or None,
                )
                for evidence_index, text in enumerate(evidence, start=1)
            ],
        )
        event_ids, _job_ids, duplicate = ingest_turn(connection, request)
        if duplicate:
            raise DatasetError(f"atomic runner requires a fresh database: duplicate {case_id}")
        if len(event_ids) != len(evidence):
            raise DatasetError(f"atomic runner did not ingest every evidence item: {case_id}")
        turn_id = benchmark_turn_id(
            namespace=namespace,
            manifest_sha256=manifest_sha256,
            case_id=case_id,
        )
        connection.execute(
            """UPDATE ops.jobs SET status='cancelled',updated_at=now()
               WHERE namespace_id=%s AND input_ref=ANY(%s::uuid[])
                 AND kind IN ('extract_facts','build_unified_turn')""",
            (stable_uuid("namespace", namespace), [*event_ids, turn_id]),
        )
        job_id = stable_uuid(
            "job", f"extract_atomic_turn:{ATOMIC_EXTRACTION_VERSION}:{turn_id}"
        )
        connection.execute(
            """INSERT INTO ops.jobs(
                 id,namespace_id,kind,idempotency_key,input_ref
               ) VALUES (%s,%s,'extract_atomic_turn',%s,%s)""",
            (
                job_id,
                stable_uuid("namespace", namespace),
                f"extract_atomic_turn:{ATOMIC_EXTRACTION_VERSION}:{turn_id}",
                turn_id,
            ),
        )
        prepared.append(PreparedCase(case, turn_id, tuple(event_ids)))
    return tuple(prepared)


def process_model_jobs(
    connection: Connection,
    *,
    prepared: tuple[PreparedCase, ...],
    namespace: str,
) -> dict[str, int]:
    namespace_id = stable_uuid("namespace", namespace)
    for item in prepared:
        row = connection.execute(
            """UPDATE ops.jobs SET status='running',attempt_count=attempt_count+1,
                     lease_until=now() + interval '15 minutes',updated_at=now()
               WHERE namespace_id=%s AND kind='extract_atomic_turn'
                 AND input_ref=%s AND status='pending'
               RETURNING id,namespace_id,kind,input_ref,input_version""",
            (namespace_id, item.turn_id),
        ).fetchone()
        if row is None:
            raise DatasetError(f"missing pending model job for {item.case['case_id']}")
        process_one(connection, row)
        connection.execute(
            """UPDATE ops.jobs SET status='failed',run_after=now(),lease_until=NULL,
                     updated_at=now()
               WHERE id=%s AND status IN ('pending','retry','running')""",
            (row[0],),
        )
        connection.commit()
    counts = dict(
        connection.execute(
            """SELECT status,count(*) FROM ops.jobs
               WHERE namespace_id=%s AND kind='extract_atomic_turn'
               GROUP BY status""",
            (namespace_id,),
        ).fetchall()
    )
    return {str(key): int(value) for key, value in counts.items()}


def _fact_rows(
    connection: Connection, *, namespace: str, turn_id: UUID
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT f.id,f.statement,f.fact_type,f.memory_state,f.evidence_span_start,
                  f.evidence_span_end,fe.event_id
           FROM memory.facts f
           JOIN memory.fact_evidence fe ON fe.fact_id=f.id
           JOIN evidence.events e ON e.id=fe.event_id
           WHERE f.namespace_id=%s AND e.turn_id=%s
             AND f.extraction_method='model-verbatim'
             AND f.extraction_version=%s
           ORDER BY f.id,fe.event_id""",
        (stable_uuid("namespace", namespace), turn_id, ATOMIC_EXTRACTION_VERSION),
    ).fetchall()
    predictions: dict[UUID, dict[str, Any]] = {}
    for fact_id, statement, fact_type, memory_state, span_start, span_end, event_id in rows:
        prediction = predictions.setdefault(
            fact_id,
            {
                "prediction_id": str(fact_id),
                "statement": statement,
                "fact_type": fact_type,
                "memory_state": memory_state,
                "recallable": memory_state == "active",
                "evidence_index": -1,
                "span_start": span_start,
                "span_end": span_end,
                "source_ids": [],
            },
        )
        prediction["source_ids"].append(str(event_id))
    return list(predictions.values())


def build_private_output(
    connection: Connection,
    *,
    prepared: tuple[PreparedCase, ...],
    namespace: str,
    dataset_id: str,
    manifest_sha256: str,
    run_id: str,
    system_revision: str,
    system_version: str,
    model: str,
    contains_production_data: bool,
    dataset_visibility: str,
    model_called: bool,
    external_data_sent: bool,
) -> dict[str, Any]:
    output_cases: list[dict[str, Any]] = []
    for item in prepared:
        predictions = _fact_rows(connection, namespace=namespace, turn_id=item.turn_id)
        event_index = {str(event_id): index for index, event_id in enumerate(item.event_ids)}
        logical_evidence_ids = item.case["input"]["evidence_ids"]
        event_to_logical = {
            str(event_id): logical_evidence_ids[index]
            for index, event_id in enumerate(item.event_ids)
        }
        for prediction in predictions:
            prediction["evidence_index"] = event_index[prediction["source_ids"][0]]
            prediction["source_ids"] = [
                event_to_logical[source_id]
                for source_id in prediction["source_ids"]
                if source_id in event_to_logical
            ]
        recalls: list[dict[str, Any]] = []
        for query in item.case["expected"].get("recall_queries", []):
            request = RecallRequest(
                context=ProviderContext(
                    shared_namespace=namespace,
                    source_profile="am-eval-atomic",
                    source_instance="benchmark-runner",
                    external_session_id=f"recall:{item.case['case_id']}",
                    external_turn_id=query["query_id"],
                    correlation_id=uuid4(),
                ),
                query=query["query"],
                intent="explicit",
                budget=RecallBudget(max_items=8, max_chars=4200),
            )
            recalled, _truncated = recall(connection, request)
            if recalled:
                recalls.append(
                    {
                        "query_id": query["query_id"],
                        "prediction_id": str(recalled[0].memory_id),
                        "source_ids": [
                            event_to_logical[str(value)]
                            for value in recalled[0].source_ids
                            if str(value) in event_to_logical
                        ],
                    }
                )
        output_cases.append(
            {"case_id": item.case["case_id"], "facts": predictions, "recalls": recalls}
        )
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "dataset_id": dataset_id,
        "dataset_manifest_sha256": manifest_sha256,
        "run_id": run_id,
        "system": {
            "name": "agent-memory",
            "version": system_version,
            "revision": system_revision,
        },
        "model": model,
        "policy_version": ATOMIC_EXTRACTION_VERSION,
        "contains_memory_text": True,
        "contains_production_data": contains_production_data,
        "dataset_visibility": dataset_visibility,
        "model_called": model_called,
        "external_data_sent": external_data_sent,
        "cases": output_cases,
    }


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def build_efficiency_input(
    *,
    output: dict[str, Any],
    job_statuses: dict[str, int],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    active = 0
    candidate = 0
    for case in output["cases"]:
        for fact in case["facts"]:
            if fact["memory_state"] == "active":
                active += 1
            elif fact["memory_state"] == "candidate":
                candidate += 1
    terminal_success = job_statuses.get("done", 0)
    terminal_failure = job_statuses.get("failed", 0)
    unfinished = sum(
        count
        for status, count in job_statuses.items()
        if status not in {"done", "failed", "cancelled"}
    )
    return {
        "schema_version": "am-eval-efficiency-input-v1",
        "run_id": output["run_id"],
        "scope": "isolated",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "system_revision": output["system"]["revision"],
        "policy_version": output["policy_version"],
        "counts": {
            "auto_admitted_count": active,
            "manual_review_count": candidate,
            "terminal_model_success_count": terminal_success,
            "terminal_model_failure_count": terminal_failure,
            "unfinished_model_job_count": unfinished,
        },
        "contains_memory_text": False,
        "contains_production_data": output["contains_production_data"],
        "external_data_sent": output["external_data_sent"],
    }


def benchmark_run_complete(*, job_statuses: dict[str, int], case_count: int) -> bool:
    return job_statuses == {"done": case_count}


def validate_model_call_budget(*, max_model_calls: int, case_count: int) -> None:
    if max_model_calls <= 0 or max_model_calls != case_count:
        raise DatasetError(
            "model call budget must exactly match the frozen atomic case count"
        )


def emit_run_summary(
    *,
    run_id: str,
    case_count: int,
    job_statuses: dict[str, int],
    output_path: Path,
    efficiency_output_path: Path,
    external_data_sent: bool,
) -> None:
    complete = benchmark_run_complete(
        job_statuses=job_statuses,
        case_count=case_count,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETE" if complete else "FAILED",
                "run_id": run_id,
                "case_count": case_count,
                "job_statuses": job_statuses,
                "output": str(output_path),
                "efficiency_output": str(efficiency_output_path),
                "contains_memory_text": False,
                "external_data_sent": external_data_sent,
            },
            sort_keys=True,
        )
    )
    if not complete:
        raise SystemExit(2)


def external_data_confirmation(manifest: dict[str, Any]) -> str:
    if manifest.get("contains_production_data") is True:
        return "SEND_REDACTED_PRODUCTION_DERIVED_BENCHMARK_TO_EXTERNAL_MODEL"
    return "SEND_SYNTHETIC_BENCHMARK_TO_EXTERNAL_MODEL"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a frozen atomic-fact benchmark in an isolated database."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--efficiency-output", required=True, type=Path)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--system-revision", required=True)
    parser.add_argument("--system-version", required=True)
    parser.add_argument("--expected-model", required=True)
    parser.add_argument("--expected-api-base", required=True)
    parser.add_argument("--max-model-calls", required=True, type=int)
    parser.add_argument("--confirm-sha256", required=True)
    parser.add_argument("--confirm-external-data", required=True)
    return parser


def plan_main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a metadata-only allowlist for the atomic benchmark runner."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    arguments = parser.parse_args()
    manifest_path = arguments.manifest.expanduser().resolve()
    manifest_sha256 = sha256_file(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contains_production_data") is True:
        manifest_path = validate_private_input(arguments.manifest)
    cases = tuple(
        case for case in load_dataset(manifest_path) if case["suite"] == "atomic_fact"
    )
    if not cases:
        parser.error("dataset has no atomic_fact cases")
    if manifest.get("contains_production_data") is True:
        validate_frozen_private_gold(manifest, cases)
    print(
        json.dumps(
            build_plan(
                cases=cases,
                namespace=arguments.namespace,
                manifest_sha256=manifest_sha256,
            ),
            sort_keys=True,
        )
    )


def main() -> None:
    arguments = build_parser().parse_args()
    manifest_path = arguments.manifest.expanduser().resolve()
    manifest_sha256 = sha256_file(manifest_path)
    if arguments.confirm_sha256.casefold() != manifest_sha256:
        raise SystemExit("--confirm-sha256 does not match the frozen manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contains_production_data") is True:
        manifest_path = validate_private_input(arguments.manifest)
    required_confirmation = external_data_confirmation(manifest)
    if arguments.confirm_external_data != required_confirmation:
        raise SystemExit("explicit external-data confirmation is required")
    validate_run_metadata(
        run_id=arguments.run_id,
        system_revision=arguments.system_revision,
        system_version=arguments.system_version,
    )
    cases = tuple(
        case for case in load_dataset(manifest_path) if case["suite"] == "atomic_fact"
    )
    if not cases:
        raise SystemExit("dataset has no atomic_fact cases")
    if manifest.get("contains_production_data") is True:
        validate_frozen_private_gold(manifest, cases)
    validate_model_call_budget(
        max_model_calls=arguments.max_model_calls,
        case_count=len(cases),
    )
    settings = Settings()
    validate_isolated_database_url(settings.database_url)
    profile = validate_runtime_settings(
        settings,
        namespace=arguments.namespace,
        plan_sha256=manifest_sha256,
        expected_model=arguments.expected_model,
        expected_api_base=arguments.expected_api_base,
    )
    configured_ids = set(settings.model_evaluation_turn_ids)
    expected_ids = {
        benchmark_turn_id(
            namespace=arguments.namespace,
            manifest_sha256=manifest_sha256,
            case_id=str(case["case_id"]),
        )
        for case in cases
    }
    if configured_ids != expected_ids:
        raise SystemExit("AGENT_MEMORY_MODEL_EVALUATION_TURN_ALLOWLIST mismatch")
    expected_fact_limit = max(len(case["expected"]["facts"]) for case in cases)
    if settings.model_max_atomic_facts < expected_fact_limit:
        raise SystemExit("AGENT_MEMORY_MODEL_MAX_ATOMIC_FACTS is below the gold case maximum")
    source_root = Path(__file__).parents[2]
    output_path = validate_private_output(arguments.output, forbidden_root=source_root)
    efficiency_output_path = validate_private_output(
        arguments.efficiency_output, forbidden_root=source_root
    )
    if output_path == efficiency_output_path:
        raise SystemExit("private and efficiency outputs must differ")
    started_at = datetime.now(UTC)
    with connect(settings.database_url) as connection:
        namespace_rows = connection.execute(
            "SELECT count(*) FROM core.namespaces"
        ).fetchone()[0]
        if namespace_rows:
            raise SystemExit("atomic runner requires a dedicated empty database")
        prepared = prepare_cases(
            connection,
            cases=cases,
            namespace=arguments.namespace,
            manifest_sha256=manifest_sha256,
            occurred_at=datetime.now(UTC),
            allowed_tool_names=settings.trusted_observation_tools,
        )
        if {item.turn_id for item in prepared} != expected_ids:
            raise SystemExit("atomic runner generated an unexpected turn ID")
        job_statuses = process_model_jobs(
            connection, prepared=prepared, namespace=arguments.namespace
        )
        payload = build_private_output(
            connection,
            prepared=prepared,
            namespace=arguments.namespace,
            dataset_id=manifest["dataset_id"],
            manifest_sha256=manifest_sha256,
            run_id=arguments.run_id,
            system_revision=arguments.system_revision,
            system_version=arguments.system_version,
            model=profile.model,
            contains_production_data=manifest["contains_production_data"],
            dataset_visibility=manifest["visibility"],
            model_called=True,
            external_data_sent=profile.sends_data_externally,
        )
    payload["job_statuses"] = job_statuses
    write_private_json(output_path, payload)
    efficiency_payload = build_efficiency_input(
        output=payload,
        job_statuses=job_statuses,
        window_start=started_at,
        window_end=datetime.now(UTC),
    )
    write_private_json(efficiency_output_path, efficiency_payload)
    emit_run_summary(
        run_id=arguments.run_id,
        case_count=len(cases),
        job_statuses=job_statuses,
        output_path=output_path,
        efficiency_output_path=efficiency_output_path,
        external_data_sent=payload["external_data_sent"],
    )


if __name__ == "__main__":
    main()
