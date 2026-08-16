from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection, connect
from psycopg import Error as PsycopgError

from .am_eval_atomic_runner import (
    discover_runtime_source_root,
    resolve_runtime_identity,
    validate_isolated_database_url,
    validate_private_output,
    write_private_json,
)
from .am_eval_dataset import DatasetError, load_dataset_snapshot, read_file_snapshot
from .am_eval_environment import runtime_environment_identity
from .repository import ingest_turn
from .schemas import IngestEvent, IngestTurnRequest, ProviderContext
from .vault import VaultCrypto, create_entry
from .worker import claim_job, process_one

DATASET_ID = "agent-memory-reliability-gold-v1"
EXPECTED_MANIFEST_SHA256 = "e66d612909e010f6f95f2724d52b49b921cafcb135b836d2ceca1fa68cdc3490"
EXPECTED_CASE_FILE_SHA256 = "1bb05791bc75c13bcd097e820fc4607cb27c338fb147124e30d6b29c30ff58ca"
EXPECTED_SUITE_COUNTS = {"backup_restore": 1, "idempotency": 2, "worker_recovery": 2}
EXPECTED_SPLIT_COUNTS = {"development": 2, "validation": 3}
PREPARE_SCHEMA_VERSION = "am-eval-reliability-prepare-v1"
RELIABILITY_RESULT_SCHEMA_VERSION = "am-eval-reliability-run-v1"
VAULT_PROBE = "agent-memory-am-eval-reliability-vault-probe-v1"
TABLES = (
    "audit.events",
    "core.namespaces",
    "core.sessions",
    "core.sources",
    "core.subject_sources",
    "core.subjects",
    "core.turns",
    "evidence.events",
    "evidence.redaction_findings",
    "memory.arc_facts",
    "memory.arcs",
    "memory.artifacts",
    "memory.entities",
    "memory.entity_aliases",
    "memory.entity_mentions",
    "memory.entity_relations",
    "memory.episode_artifacts",
    "memory.episode_entities",
    "memory.episode_facts",
    "memory.episode_steps",
    "memory.episodes",
    "memory.fact_entities",
    "memory.fact_evidence",
    "memory.facts",
    "memory.preference_assertions",
    "memory.preference_evidence",
    "memory.procedure_steps",
    "memory.procedure_support",
    "memory.procedures",
    "memory.relation_facts",
    "memory.relationship_assertions",
    "memory.temporal_rules",
    "ops.job_attempts",
    "ops.jobs",
    "projection.galaxies",
    "projection.galaxy_membership_evidence",
    "projection.galaxy_memberships",
    "projection.layout_preferences",
    "reports.consolidation",
    "retrieval.documents",
    "state.continuities",
    "state.current_items",
    "state.interaction_snapshots",
    "state.settings",
    "vault.entries",
    "vault.grants",
    "vault.references",
)


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise DatasetError(message)


def validate_reliability_dataset(
    manifest: dict[str, Any], cases: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    _require(manifest.get("dataset_id") == DATASET_ID, "reliability dataset_id is invalid")
    _require(
        manifest.get("contains_production_data") is False
        and manifest.get("contains_memory_text") is False
        and manifest.get("external_data_sent") is False
        and manifest.get("visibility") == "open"
        and manifest.get("blind_cases") == 0,
        "reliability gold must be synthetic, metadata-only, open, and local-only",
    )
    _require(
        manifest.get("files")
        == [
            {
                "path": "cases.jsonl",
                "sha256": EXPECTED_CASE_FILE_SHA256,
                "case_count": 5,
                "suites": ["backup_restore", "idempotency", "worker_recovery"],
            }
        ],
        "reliability file contract differs from the frozen dataset",
    )
    suite_counts = Counter(case["suite"] for case in cases)
    split_counts = Counter(case["split"] for case in cases)
    _require(
        len(cases) == 5
        and manifest.get("case_count") == 5
        and manifest.get("suite_counts") == EXPECTED_SUITE_COUNTS
        and dict(sorted(suite_counts.items())) == EXPECTED_SUITE_COUNTS
        and dict(sorted(split_counts.items())) == EXPECTED_SPLIT_COUNTS,
        "reliability gold case coverage is incomplete",
    )
    _require(
        {case["case_id"] for case in cases}
        == {
            "backup-restore-001",
            "expired-lease-001",
            "idempotency-concurrent-001",
            "idempotency-sequential-001",
            "worker-outage-001",
        },
        "reliability case IDs differ from the frozen dataset",
    )
    return {
        "schema_version": "am-eval-reliability-validation-v1",
        "dataset_id": DATASET_ID,
        "case_count": 5,
        "suite_counts": EXPECTED_SUITE_COUNTS,
        "split_counts": EXPECTED_SPLIT_COUNTS,
        "status": "PASS",
    }


def _context(namespace: str, case_id: str) -> ProviderContext:
    return ProviderContext(
        shared_namespace=namespace,
        source_profile="am-eval",
        source_instance="reliability-runner",
        external_session_id=case_id,
        external_turn_id="turn-1",
        correlation_id=uuid4(),
    )


def _request(namespace: str, case_id: str) -> IngestTurnRequest:
    return IngestTurnRequest(
        context=_context(namespace, case_id),
        idempotency_key=f"reliability:{case_id}",
        occurred_at=datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
        events=[
            IngestEvent(
                type="environment_observation",
                sequence=1,
                content=f"service:ReliabilityProbe-{case_id} is healthy",
            )
        ],
    )


def _evidence_sha256(connection: Connection) -> str:
    digest = hashlib.sha256()
    for event_id, payload_hash in connection.execute(
        "SELECT id,payload_hash FROM evidence.events ORDER BY id"
    ).fetchall():
        digest.update(str(event_id).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(payload_hash).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _table_counts(connection: Connection) -> dict[str, int]:
    actual_tables = tuple(
        row[0]
        for row in connection.execute(
            """SELECT table_schema||'.'||table_name
               FROM information_schema.tables
               WHERE table_type='BASE TABLE'
                 AND table_schema IN (
                   'audit','core','evidence','memory','ops','projection',
                   'reports','retrieval','state','vault'
                 )
               ORDER BY 1"""
        ).fetchall()
    )
    _require(actual_tables == TABLES, "reliability business table set differs from the frozen set")
    return {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in TABLES
    }


def _migration_revision(connection: Connection) -> str:
    row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise DatasetError("reliability database has no migration revision")
    return row[0]


def _vault_ciphertext_sha256(connection: Connection, entry_id: UUID) -> str:
    row = connection.execute(
        """SELECT id,kind,ciphertext,data_nonce,wrapped_dek,wrap_nonce,key_version
           FROM vault.entries WHERE id=%s""",
        (entry_id,),
    ).fetchone()
    if row is None:
        raise DatasetError("reliability Vault probe is missing")
    digest = hashlib.sha256()
    for value in row:
        payload = (
            bytes(value)
            if isinstance(value, (bytes, bytearray, memoryview))
            else str(value).encode()
        )
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _vault_decrypts(
    connection: Connection, crypto: VaultCrypto, entry_id: UUID
) -> bool:
    row = connection.execute(
        """SELECT kind,ciphertext,data_nonce,wrapped_dek,wrap_nonce,key_version
           FROM vault.entries WHERE id=%s""",
        (entry_id,),
    ).fetchone()
    if row is None:
        return False
    return (
        crypto.decrypt(
            entry_id=entry_id,
            kind=row[0],
            ciphertext=row[1],
            data_nonce=row[2],
            wrapped_dek=row[3],
            wrap_nonce=row[4],
            key_version=row[5],
        )
        == VAULT_PROBE
    )


def _process_expected_jobs(
    connection: Connection, *, namespace: str, expected_job_ids: tuple[UUID, ...]
) -> bool:
    for _ in range(20):
        statuses = dict(
            connection.execute(
                "SELECT id,status FROM ops.jobs WHERE id=ANY(%s::uuid[])",
                (list(expected_job_ids),),
            ).fetchall()
        )
        if (
            statuses
            and set(statuses) == set(expected_job_ids)
            and set(statuses.values()) == {"done"}
        ):
            return True
        job = claim_job(connection, 30, "core", namespace)
        if job is None:
            return False
        process_one(connection, job)
        connection.commit()
    return False


def _run_worker_cases(connection: Connection, namespace: str) -> list[dict[str, Any]]:
    outage_namespace = f"{namespace}:worker-outage-001"
    event_ids, job_ids, duplicate = ingest_turn(
        connection, _request(outage_namespace, "worker-outage-001")
    )
    connection.commit()
    before = _evidence_sha256(connection)
    pending = connection.execute(
        "SELECT count(*) FROM ops.jobs WHERE id=ANY(%s::uuid[]) AND status='pending'",
        (job_ids,),
    ).fetchone()[0]
    recovered = _process_expected_jobs(
        connection, namespace=outage_namespace, expected_job_ids=tuple(job_ids)
    )
    after = _evidence_sha256(connection)
    outage = {
        "case_id": "worker-outage-001",
        "evidence_preserved": len(event_ids) == 1 and before == after,
        "jobs_before_recovery": pending,
        "jobs_expected": len(job_ids),
        "recovered": not duplicate and pending == len(job_ids) == 2 and recovered,
    }

    lease_namespace = f"{namespace}:expired-lease-001"
    lease_event_ids, lease_job_ids, lease_duplicate = ingest_turn(
        connection, _request(lease_namespace, "expired-lease-001")
    )
    extract_job = connection.execute(
        "SELECT id FROM ops.jobs WHERE id=ANY(%s::uuid[]) AND kind='extract_facts'",
        (lease_job_ids,),
    ).fetchone()[0]
    connection.execute(
        "UPDATE ops.jobs SET status='done' WHERE id=ANY(%s::uuid[]) AND id<>%s",
        (lease_job_ids, extract_job),
    )
    connection.execute(
        """UPDATE ops.jobs SET status='running',lease_until=now()-interval '1 minute',
           attempt_count=1 WHERE id=%s""",
        (extract_job,),
    )
    connection.commit()
    lease_before = _evidence_sha256(connection)
    claimed = claim_job(connection, 30, "core", lease_namespace)
    if claimed is not None:
        process_one(connection, claimed)
        connection.commit()
    lease_status = connection.execute(
        "SELECT status,attempt_count FROM ops.jobs WHERE id=%s", (extract_job,)
    ).fetchone()
    lease_after = _evidence_sha256(connection)
    lease = {
        "attempt_count_after": lease_status[1],
        "case_id": "expired-lease-001",
        "evidence_preserved": len(lease_event_ids) == 1 and lease_before == lease_after,
        "reclaimed_exact_job": claimed is not None and claimed[0] == extract_job,
        "recovered": (
            not lease_duplicate
            and claimed is not None
            and claimed[0] == extract_job
            and lease_status == ("done", 2)
        ),
    }
    return [outage, lease]


def _idempotency_counts(connection: Connection, namespace: str) -> dict[str, int]:
    namespace_id = connection.execute(
        "SELECT id FROM core.namespaces WHERE stable_key=%s", (namespace,)
    ).fetchone()[0]
    return {
        "events": connection.execute(
            "SELECT count(*) FROM evidence.events WHERE namespace_id=%s", (namespace_id,)
        ).fetchone()[0],
        "jobs": connection.execute(
            "SELECT count(*) FROM ops.jobs WHERE namespace_id=%s", (namespace_id,)
        ).fetchone()[0],
        "turns": connection.execute(
            """SELECT count(*) FROM core.turns t JOIN core.sessions s ON s.id=t.session_id
               WHERE s.namespace_id=%s""",
            (namespace_id,),
        ).fetchone()[0],
    }


def _run_concurrent_attempt(database_url: str, request: IngestTurnRequest) -> bool:
    with connect(database_url) as connection:
        _event_ids, _job_ids, duplicate = ingest_turn(connection, request)
    return duplicate


def _run_idempotency_cases(
    connection: Connection, *, database_url: str, namespace: str
) -> list[dict[str, Any]]:
    sequential_namespace = f"{namespace}:idempotency-sequential-001"
    sequential_request = _request(sequential_namespace, "idempotency-sequential-001")
    sequential_duplicates = []
    first_job_ids: list[UUID] = []
    for _ in range(2):
        _event_ids, job_ids, duplicate = ingest_turn(connection, sequential_request)
        connection.commit()
        sequential_duplicates.append(duplicate)
        if job_ids:
            first_job_ids = job_ids
    sequential_counts = _idempotency_counts(connection, sequential_namespace)
    sequential = {
        "attempts": 2,
        "case_id": "idempotency-sequential-001",
        "duplicate_attempts": sum(sequential_duplicates),
        "passed": (
            sequential_duplicates == [False, True]
            and len(first_job_ids) == 2
            and sequential_counts == {"events": 1, "jobs": 2, "turns": 1}
        ),
        **sequential_counts,
        "winner_attempts": 2 - sum(sequential_duplicates),
    }

    concurrent_namespace = f"{namespace}:idempotency-concurrent-001"
    concurrent_request = _request(concurrent_namespace, "idempotency-concurrent-001")
    with ThreadPoolExecutor(max_workers=8) as executor:
        concurrent_duplicates = list(
            executor.map(
                lambda _index: _run_concurrent_attempt(database_url, concurrent_request),
                range(8),
            )
        )
    concurrent_counts = _idempotency_counts(connection, concurrent_namespace)
    concurrent = {
        "attempts": 8,
        "case_id": "idempotency-concurrent-001",
        "duplicate_attempts": sum(concurrent_duplicates),
        "passed": (
            sum(not duplicate for duplicate in concurrent_duplicates) == 1
            and concurrent_counts == {"events": 1, "jobs": 2, "turns": 1}
        ),
        **concurrent_counts,
        "winner_attempts": sum(not duplicate for duplicate in concurrent_duplicates),
    }
    return [sequential, concurrent]


def _database_snapshot(connection: Connection, entry_id: UUID) -> dict[str, Any]:
    return {
        "evidence_sha256": _evidence_sha256(connection),
        "migration_revision": _migration_revision(connection),
        "table_counts": _table_counts(connection),
        "vault_ciphertext_sha256": _vault_ciphertext_sha256(connection, entry_id),
        "vault_entry_id": str(entry_id),
    }


def run_prepare_cases(
    connection: Connection,
    *,
    cases: tuple[dict[str, Any], ...],
    database_url: str,
    namespace: str,
    crypto: VaultCrypto,
) -> dict[str, Any]:
    _require(
        connection.execute("SELECT count(*) FROM core.namespaces").fetchone()[0] == 0,
        "reliability prepare requires a dedicated empty database",
    )
    _require(
        namespace.startswith("hermes:automated-tests:"),
        "reliability runner requires an automated namespace",
    )
    worker_ledger = _run_worker_cases(connection, namespace)
    idempotency_ledger = _run_idempotency_cases(
        connection, database_url=database_url, namespace=namespace
    )
    vault_namespace = f"{namespace}:backup-restore-001"
    entry_id = create_entry(
        connection,
        crypto,
        namespace_key=vault_namespace,
        kind="credential",
        display_label="AM-Eval reliability probe",
        redacted_hint="synthetic recovery probe",
        secret_value=VAULT_PROBE,
        actor_id="reliability-runner",
        correlation_id=uuid4(),
    )
    _require(entry_id is not None, "reliability Vault probe could not be created")
    connection.commit()
    backup_snapshot = _database_snapshot(connection, entry_id)
    vault_decrypts = _vault_decrypts(connection, crypto, entry_id)
    counts = {
        "evidence_loss_count": sum(not item["evidence_preserved"] for item in worker_ledger),
        "idempotency_cases": len(idempotency_ledger),
        "idempotency_passed": sum(item["passed"] for item in idempotency_ledger),
        "restore_cases": 1,
        "worker_cases": len(worker_ledger),
        "worker_recovered": sum(item["recovered"] for item in worker_ledger),
    }
    passed = (
        len(cases) == 5
        and counts
        == {
            "evidence_loss_count": 0,
            "idempotency_cases": 2,
            "idempotency_passed": 2,
            "restore_cases": 1,
            "worker_cases": 2,
            "worker_recovered": 2,
        }
        and vault_decrypts
    )
    return {
        "schema_version": "am-eval-reliability-prepare-ledger-v1",
        "status": "PASS" if passed else "FAIL",
        "case_count": len(cases),
        "counts": counts,
        "worker_ledger": worker_ledger,
        "idempotency_ledger": idempotency_ledger,
        "backup_snapshot": backup_snapshot,
        "vault_decrypts_before_backup": vault_decrypts,
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
        "model_called": False,
    }


def _private_file_snapshot(path: Path, *, label: str):
    expanded = path.expanduser()
    if any(candidate.is_symlink() for candidate in (expanded, *expanded.parents)):
        raise DatasetError(f"{label} cannot use a symlink")
    resolved = expanded.resolve()
    source_root = discover_runtime_source_root().resolve()
    try:
        resolved.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise DatasetError(f"{label} must be outside the source repository")
    parent_mode = stat.S_IMODE(resolved.parent.stat().st_mode)
    file_stat = resolved.stat()
    if (
        parent_mode & 0o077
        or not stat.S_ISREG(file_stat.st_mode)
        or stat.S_IMODE(file_stat.st_mode) & 0o077
        or file_stat.st_nlink != 1
    ):
        raise DatasetError(f"{label} must be a private single-link regular file")
    return read_file_snapshot(resolved)


def _identity_payload() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    identity = resolve_runtime_identity()
    environment = runtime_environment_identity()
    system = {
        "environment_sha256": environment["sha256"],
        "name": "agent-memory",
        "revision": identity.revision,
        "source_file_count": identity.source_file_count,
        "source_sha256": identity.source_sha256,
        "version": identity.version,
    }
    return (
        system,
        {
            "provenance": identity.provenance,
            "revision": identity.revision,
            "source_file_count": identity.source_file_count,
            "source_sha256": identity.source_sha256,
            "version": identity.version,
        },
        environment,
    )


def _load_crypto(path: Path) -> VaultCrypto:
    snapshot = _private_file_snapshot(path, label="reliability Vault root key")
    try:
        return VaultCrypto.from_file(str(snapshot.path))
    except ValueError as error:
        raise DatasetError(str(error)) from error


def build_prepare_output(
    *,
    manifest: Path,
    confirm_sha256: str,
    database_url: str,
    namespace: str,
    vault_root_key: Path,
) -> dict[str, Any]:
    dataset = load_dataset_snapshot(manifest)
    _require(
        confirm_sha256.casefold() == dataset.manifest_sha256 == EXPECTED_MANIFEST_SHA256,
        "reliability manifest SHA does not match the frozen dataset",
    )
    validation = validate_reliability_dataset(dataset.manifest, dataset.cases)
    validate_isolated_database_url(database_url)
    crypto = _load_crypto(vault_root_key)
    system, runtime_identity, runtime_environment = _identity_payload()
    with connect(database_url) as connection:
        result = run_prepare_cases(
            connection,
            cases=dataset.cases,
            database_url=database_url,
            namespace=namespace,
            crypto=crypto,
        )
    return {
        **result,
        "schema_version": PREPARE_SCHEMA_VERSION,
        "run_id": namespace,
        "dataset_id": DATASET_ID,
        "manifest_sha256": dataset.manifest_sha256,
        "dataset_visibility": dataset.manifest["visibility"],
        "dataset_blind": False,
        "dataset_contains_memory_text": False,
        "dataset_validation": validation["status"],
        "system": system,
        "runner_runtime_identity": runtime_identity,
        "runner_runtime_environment": runtime_environment,
    }


def _validate_prepare(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != PREPARE_SCHEMA_VERSION:
        raise DatasetError("reliability prepare receipt has an invalid schema")
    required = {
        "backup_snapshot",
        "case_count",
        "contains_memory_text",
        "contains_production_data",
        "counts",
        "dataset_blind",
        "dataset_contains_memory_text",
        "dataset_id",
        "dataset_validation",
        "dataset_visibility",
        "external_data_sent",
        "idempotency_ledger",
        "manifest_sha256",
        "model_called",
        "run_id",
        "runner_runtime_environment",
        "runner_runtime_identity",
        "schema_version",
        "status",
        "system",
        "vault_decrypts_before_backup",
        "worker_ledger",
    }
    _require(set(payload) == required, "reliability prepare receipt fields are invalid")
    _require(
        payload["status"] == "PASS"
        and payload["case_count"] == 5
        and payload["dataset_id"] == DATASET_ID
        and payload["manifest_sha256"] == EXPECTED_MANIFEST_SHA256
        and payload["dataset_validation"] == "PASS"
        and payload["dataset_visibility"] == "open"
        and payload["dataset_blind"] is False
        and payload["dataset_contains_memory_text"] is False
        and payload["contains_memory_text"] is False
        and payload["contains_production_data"] is False
        and payload["external_data_sent"] is False
        and payload["model_called"] is False
        and payload["vault_decrypts_before_backup"] is True,
        "reliability prepare receipt is not complete",
    )
    _require(
        payload["counts"]
        == {
            "evidence_loss_count": 0,
            "idempotency_cases": 2,
            "idempotency_passed": 2,
            "restore_cases": 1,
            "worker_cases": 2,
            "worker_recovered": 2,
        },
        "reliability prepare counts are invalid",
    )
    _require(
        isinstance(payload["worker_ledger"], list)
        and len(payload["worker_ledger"]) == 2
        and all(
            item.get("recovered") is True and item.get("evidence_preserved") is True
            for item in payload["worker_ledger"]
        ),
        "reliability worker ledger is incomplete",
    )
    _require(
        isinstance(payload["idempotency_ledger"], list)
        and len(payload["idempotency_ledger"]) == 2
        and all(item.get("passed") is True for item in payload["idempotency_ledger"]),
        "reliability idempotency ledger is incomplete",
    )
    backup = payload["backup_snapshot"]
    _require(
        isinstance(backup, dict)
        and set(backup)
        == {
            "evidence_sha256",
            "migration_revision",
            "table_counts",
            "vault_ciphertext_sha256",
            "vault_entry_id",
        }
        and isinstance(backup["table_counts"], dict)
        and set(backup["table_counts"]) == set(TABLES),
        "reliability backup snapshot is invalid",
    )
    return payload


def build_verify_output(
    *,
    prepare_path: Path,
    confirm_prepare_sha256: str,
    backup_artifact: Path,
    confirm_backup_sha256: str,
    source_database_url: str,
    restore_database_url: str,
    vault_root_key: Path,
) -> dict[str, Any]:
    source_values = validate_isolated_database_url(source_database_url)
    restore_values = validate_isolated_database_url(restore_database_url)
    _require(
        source_values["dbname"] != restore_values["dbname"],
        "restore database must differ from source",
    )
    prepare_snapshot = _private_file_snapshot(prepare_path, label="reliability prepare receipt")
    _require(
        prepare_snapshot.sha256 == confirm_prepare_sha256.casefold(),
        "reliability prepare SHA does not match confirmation",
    )
    try:
        prepare = _validate_prepare(json.loads(prepare_snapshot.payload.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DatasetError("reliability prepare receipt is invalid JSON") from error
    backup = _private_file_snapshot(backup_artifact, label="reliability backup artifact")
    _require(
        backup.sha256 == confirm_backup_sha256.casefold(),
        "reliability backup SHA does not match confirmation",
    )
    _require(backup.payload.startswith(b"PGDMP"), "reliability backup is not a custom pg_dump")
    crypto = _load_crypto(vault_root_key)
    system, runtime_identity, runtime_environment = _identity_payload()
    _require(
        prepare["system"] == system
        and prepare["runner_runtime_identity"] == runtime_identity
        and prepare["runner_runtime_environment"] == runtime_environment,
        "reliability prepare and verify runtime identities differ",
    )
    entry_id = UUID(prepare["backup_snapshot"]["vault_entry_id"])
    with connect(source_database_url) as source_connection, connect(
        restore_database_url
    ) as restore_connection:
        source_snapshot = _database_snapshot(source_connection, entry_id)
        restore_snapshot = _database_snapshot(restore_connection, entry_id)
        source_vault = _vault_decrypts(source_connection, crypto, entry_id)
        restore_vault = _vault_decrypts(restore_connection, crypto, entry_id)
    restore_ledger = {
        "backup_artifact_sha256": backup.sha256,
        "case_id": "backup-restore-001",
        "evidence_hash_matches": (
            source_snapshot["evidence_sha256"] == restore_snapshot["evidence_sha256"]
        ),
        "migration_revision_matches": (
            source_snapshot["migration_revision"] == restore_snapshot["migration_revision"]
        ),
        "table_counts_match": source_snapshot["table_counts"] == restore_snapshot["table_counts"],
        "table_groups_checked": len(TABLES),
        "vault_ciphertext_matches": (
            source_snapshot["vault_ciphertext_sha256"]
            == restore_snapshot["vault_ciphertext_sha256"]
        ),
        "vault_decrypts_in_source": source_vault,
        "vault_decrypts_in_restore": restore_vault,
    }
    restore_passed = (
        source_snapshot == prepare["backup_snapshot"]
        and source_snapshot == restore_snapshot
        and all(
            restore_ledger[key]
            for key in (
                "evidence_hash_matches",
                "migration_revision_matches",
                "table_counts_match",
                "vault_ciphertext_matches",
                "vault_decrypts_in_source",
                "vault_decrypts_in_restore",
            )
        )
    )
    counts = {**prepare["counts"], "restore_passed": int(restore_passed)}
    status = "PASS" if restore_passed else "FAIL"
    return {
        "schema_version": RELIABILITY_RESULT_SCHEMA_VERSION,
        "status": status,
        "run_id": prepare["run_id"],
        "case_count": 5,
        "dataset_id": DATASET_ID,
        "manifest_sha256": EXPECTED_MANIFEST_SHA256,
        "dataset_visibility": "open",
        "dataset_blind": False,
        "dataset_contains_memory_text": False,
        "dataset_validation": "PASS",
        "counts": counts,
        "worker_ledger": prepare["worker_ledger"],
        "idempotency_ledger": prepare["idempotency_ledger"],
        "restore_ledger": restore_ledger,
        "prepare_artifact_sha256": prepare_snapshot.sha256,
        "system": system,
        "runner_runtime_identity": runtime_identity,
        "runner_runtime_environment": runtime_environment,
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
        "model_called": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare or verify frozen worker, idempotency, and pg_dump restore probes."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("manifest", type=Path)
    prepare.add_argument("--confirm-sha256", required=True)
    prepare.add_argument("--database-url", default=os.getenv("AGENT_MEMORY_DATABASE_URL", ""))
    prepare.add_argument("--namespace", default="hermes:automated-tests:am-eval-reliability")
    prepare.add_argument("--vault-root-key", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--prepare", required=True, type=Path)
    verify.add_argument("--confirm-prepare-sha256", required=True)
    verify.add_argument("--backup-artifact", required=True, type=Path)
    verify.add_argument("--confirm-backup-sha256", required=True)
    verify.add_argument("--source-database-url", required=True)
    verify.add_argument("--restore-database-url", required=True)
    verify.add_argument("--vault-root-key", required=True, type=Path)
    verify.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    target = None
    try:
        target = validate_private_output(
            arguments.output, forbidden_root=discover_runtime_source_root()
        )
        if arguments.command == "prepare":
            _require(bool(arguments.database_url), "reliability prepare requires a database URL")
            output = build_prepare_output(
                manifest=arguments.manifest,
                confirm_sha256=arguments.confirm_sha256,
                database_url=arguments.database_url,
                namespace=arguments.namespace,
                vault_root_key=arguments.vault_root_key,
            )
        else:
            output = build_verify_output(
                prepare_path=arguments.prepare,
                confirm_prepare_sha256=arguments.confirm_prepare_sha256,
                backup_artifact=arguments.backup_artifact,
                confirm_backup_sha256=arguments.confirm_backup_sha256,
                source_database_url=arguments.source_database_url,
                restore_database_url=arguments.restore_database_url,
                vault_root_key=arguments.vault_root_key,
            )
        write_private_json(target, output)
    except (DatasetError, json.JSONDecodeError, OSError, PsycopgError, ValueError) as error:
        if target is not None:
            target.close()
        parser.error(str(error))
    print(
        json.dumps(
            {
                "case_count": output["case_count"],
                "counts": output["counts"],
                "output": str(arguments.output.expanduser().resolve()),
                "status": output["status"],
            },
            sort_keys=True,
        )
    )
    if output["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
