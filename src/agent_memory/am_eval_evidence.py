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
from .ids import stable_uuid
from .quality import build_quality_report
from .redaction import redact_text
from .repository import ingest_turn, trace_memory
from .schemas import IngestEvent, IngestTurnRequest, ProviderContext

DATASET_ID = "agent-memory-evidence-integrity-gold-v1"
EXPECTED_MANIFEST_SHA256 = "3734605eb7ad5cc3a491d46b97c96a63862594e24e15b487832cc59072310c18"
EXPECTED_CASE_FILE_SHA256 = "30e2fd9536cc5bf3211d55cb118dbed2672deece3ee807254d1b41a49387d2f5"
EXPECTED_CASE_COUNT = 9
EXPECTED_SPLIT_COUNTS = {"development": 5, "validation": 4}
EVIDENCE_RESULT_SCHEMA_VERSION = "am-eval-evidence-integrity-run-v1"


def validate_evidence_dataset(
    manifest: dict[str, Any], cases: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    if manifest.get("dataset_id") != DATASET_ID:
        raise DatasetError(f"evidence dataset_id must be {DATASET_ID}")
    if (
        manifest.get("contains_production_data") is not False
        or manifest.get("contains_memory_text") is not True
        or manifest.get("external_data_sent") is not False
        or manifest.get("visibility") != "open"
        or manifest.get("blind_cases") != 0
    ):
        raise DatasetError("evidence gold must be synthetic, open, local-only, and non-blind")
    expected_file = {
        "path": "evidence.jsonl",
        "sha256": EXPECTED_CASE_FILE_SHA256,
        "case_count": EXPECTED_CASE_COUNT,
        "suites": ["evidence_integrity"],
    }
    if manifest.get("files") != [expected_file]:
        raise DatasetError("evidence file contract differs from the official frozen dataset")
    if (
        len(cases) != EXPECTED_CASE_COUNT
        or manifest.get("case_count") != EXPECTED_CASE_COUNT
        or manifest.get("suite_counts") != {"evidence_integrity": EXPECTED_CASE_COUNT}
        or any(case.get("suite") != "evidence_integrity" for case in cases)
    ):
        raise DatasetError("evidence gold case coverage is incomplete")
    expected_ids = {f"evidence-redaction-{index:03d}" for index in range(1, 10)}
    if {case.get("case_id") for case in cases} != expected_ids:
        raise DatasetError("evidence gold case IDs differ from the frozen dataset")
    split_counts = Counter(case["split"] for case in cases)
    if dict(sorted(split_counts.items())) != EXPECTED_SPLIT_COUNTS:
        raise DatasetError("evidence gold split coverage is invalid")
    return {
        "schema_version": "am-eval-evidence-integrity-validation-v1",
        "dataset_id": DATASET_ID,
        "case_count": EXPECTED_CASE_COUNT,
        "persisted_surface_count": EXPECTED_CASE_COUNT * 3,
        "split_counts": dict(sorted(split_counts.items())),
        "status": "PASS",
        "contains_production_data": False,
        "external_data_sent": False,
    }


def _insert_evidence_fact(
    connection: Connection,
    *,
    namespace: str,
    case: dict[str, Any],
) -> tuple[UUID, UUID, str]:
    case_id = case["case_id"]
    request = IngestTurnRequest(
        context=ProviderContext(
            shared_namespace=namespace,
            source_profile="am-eval",
            source_instance="evidence-integrity-runner",
            external_session_id="evidence-integrity-gate",
            external_turn_id=case_id,
            correlation_id=uuid4(),
        ),
        idempotency_key=f"evidence-integrity:{case_id}",
        occurred_at=datetime.now(UTC),
        events=[IngestEvent(type="user_message", sequence=1, content=case["input"]["text"])],
    )
    event_ids, _job_ids, duplicate = ingest_turn(connection, request)
    if duplicate or len(event_ids) != 1:
        raise DatasetError(f"evidence case {case_id} did not create exactly one event")
    event_id = event_ids[0]
    redacted = redact_text(case["input"]["text"])
    actual_kinds = [finding.kind for finding in redacted.findings]
    if actual_kinds != case["expected"]["finding_kinds"]:
        raise DatasetError(f"evidence case {case_id} redaction findings differ from gold")
    fact_id = stable_uuid("fact", f"{namespace}:{case_id}")
    connection.execute(
        """INSERT INTO memory.facts(
             id,namespace_id,statement,fact_type,confidence,memory_state,source_profile,
             extraction_method
           ) VALUES (%s,%s,%s,'long_term',0.95,'active','am-eval','evidence-integrity-v1')""",
        (fact_id, stable_uuid("namespace", namespace), redacted.text),
    )
    connection.execute(
        "INSERT INTO memory.fact_evidence(fact_id,event_id) VALUES (%s,%s)",
        (fact_id, event_id),
    )
    connection.execute(
        """INSERT INTO retrieval.documents(
             id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state
           ) VALUES (%s,%s,'fact',%s,%s,'active')""",
        (
            stable_uuid("document", str(fact_id)),
            stable_uuid("namespace", namespace),
            fact_id,
            redacted.text,
        ),
    )
    return fact_id, event_id, redacted.text


def run_evidence_cases(
    connection: Connection,
    *,
    cases: tuple[dict[str, Any], ...],
    namespace: str,
) -> dict[str, Any]:
    if not namespace.startswith("hermes:automated-tests:"):
        raise DatasetError("evidence namespace must be automated")
    if connection.execute("SELECT count(*) FROM core.namespaces").fetchone()[0] != 0:
        raise DatasetError("evidence runner requires a dedicated empty database")
    ledger: list[dict[str, Any]] = []
    for case in cases:
        fact_id, event_id, redacted_text = _insert_evidence_fact(
            connection, namespace=namespace, case=case
        )
        event_content = connection.execute(
            "SELECT redacted_payload->>'content' FROM evidence.events WHERE id=%s", (event_id,)
        ).fetchone()[0]
        fact_text = connection.execute(
            "SELECT statement FROM memory.facts WHERE id=%s", (fact_id,)
        ).fetchone()[0]
        document_text = connection.execute(
            "SELECT text_redacted FROM retrieval.documents WHERE source_id=%s", (fact_id,)
        ).fetchone()[0]
        surfaces = (event_content, fact_text, document_text)
        if any(value != redacted_text for value in surfaces):
            raise DatasetError(f"evidence case {case['case_id']} persisted surfaces differ")
        remaining_findings = sum(len(redact_text(value).findings) for value in surfaces)
        forbidden_occurrences = sum(
            value.count(fragment)
            for value in surfaces
            for fragment in case["input"]["forbidden_fragments"]
        )
        sensitive_leak_surfaces = sum(
            bool(redact_text(value).findings)
            or any(fragment in value for fragment in case["input"]["forbidden_fragments"])
            for value in surfaces
        )
        finding_counts = dict(
            sorted(
                Counter(
                    row[0]
                    for row in connection.execute(
                        "SELECT kind FROM evidence.redaction_findings WHERE event_id=%s",
                        (event_id,),
                    ).fetchall()
                ).items()
            )
        )
        expected_finding_counts = dict(sorted(Counter(case["expected"]["finding_kinds"]).items()))
        trace = trace_memory(connection, namespace, fact_id)
        trace_complete = (
            trace is not None
            and len(trace.evidence) == 1
            and trace.evidence[0].evidence_id == event_id
        )
        active_fact_has_evidence = bool(
            connection.execute(
                "SELECT 1 FROM memory.fact_evidence WHERE fact_id=%s", (fact_id,)
            ).fetchone()
        )
        ledger.append(
            {
                "case_id": case["case_id"],
                "finding_counts": finding_counts,
                "expected_finding_counts": expected_finding_counts,
                "persisted_surface_count": len(surfaces),
                "remaining_sensitive_finding_count": remaining_findings,
                "forbidden_fragment_occurrences": forbidden_occurrences,
                "sensitive_leak_surface_count": sensitive_leak_surfaces,
                "active_fact_has_evidence": active_fact_has_evidence,
                "trace_complete": trace_complete,
            }
        )
    connection.commit()

    namespace_id = stable_uuid("namespace", namespace)
    active_facts, active_without_evidence = connection.execute(
        """SELECT count(*),count(*) FILTER (WHERE NOT EXISTS (
             SELECT 1 FROM memory.fact_evidence evidence WHERE evidence.fact_id=fact.id
           )) FROM memory.facts fact
           WHERE fact.namespace_id=%s AND fact.memory_state='active'""",
        (namespace_id,),
    ).fetchone()
    report = build_quality_report(
        connection,
        namespace_key=namespace,
        trusted_tools=frozenset({"terminal", "health_probe"}),
    )
    counts = {
        "cases": len(ledger),
        "persisted_surfaces": sum(item["persisted_surface_count"] for item in ledger),
        "sensitive_leak_surfaces": sum(
            item["sensitive_leak_surface_count"] for item in ledger
        ),
        "active_facts": int(active_facts),
        "active_facts_without_evidence": int(active_without_evidence),
        "traceable_facts": sum(item["trace_complete"] is True for item in ledger),
        "trace_failures": sum(item["trace_complete"] is not True for item in ledger),
    }
    passed = (
        counts["cases"] == EXPECTED_CASE_COUNT
        and counts["persisted_surfaces"] == EXPECTED_CASE_COUNT * 3
        and counts["sensitive_leak_surfaces"] == 0
        and counts["active_facts"] == EXPECTED_CASE_COUNT
        and counts["active_facts_without_evidence"] == 0
        and counts["traceable_facts"] == EXPECTED_CASE_COUNT
        and counts["trace_failures"] == 0
        and report["gates"]["evidence_traceability"] is True
        and report["gates"]["raw_sensitive_fact_leakage"] is True
        and report["metrics"]["facts"] == EXPECTED_CASE_COUNT
        and report["metrics"]["traceable_facts"] == EXPECTED_CASE_COUNT
        and report["metrics"]["raw_sensitive_facts"] == 0
    )
    return {
        "schema_version": "am-eval-evidence-integrity-ledger-v1",
        "status": "PASS" if passed else "FAIL",
        "case_count": len(ledger),
        "cases": ledger,
        "counts": counts,
        "quality_report_snapshot": {
            "evidence_traceability": report["gates"]["evidence_traceability"],
            "raw_sensitive_fact_leakage": report["gates"]["raw_sensitive_fact_leakage"],
            "facts": report["metrics"]["facts"],
            "traceable_facts": report["metrics"]["traceable_facts"],
            "raw_sensitive_facts": report["metrics"]["raw_sensitive_facts"],
        },
        "contains_memory_text": False,
        "contains_production_data": False,
        "external_data_sent": False,
        "model_called": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run frozen persisted-redaction and evidence-trace probes in an isolated database."
        )
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", default="hermes:automated-tests:am-eval-evidence")
    parser.add_argument("--confirm-sha256", required=True)
    arguments = parser.parse_args()
    output_target = None
    try:
        dataset = load_dataset_snapshot(arguments.manifest)
        if arguments.confirm_sha256.casefold() != dataset.manifest_sha256:
            raise DatasetError("--confirm-sha256 does not match the frozen manifest")
        if dataset.manifest_sha256 != EXPECTED_MANIFEST_SHA256:
            raise DatasetError("evidence manifest differs from the official frozen dataset")
        validation = validate_evidence_dataset(dataset.manifest, dataset.cases)
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
            result = run_evidence_cases(
                connection,
                cases=dataset.cases,
                namespace=arguments.namespace,
            )
        output = {
            **result,
            "schema_version": EVIDENCE_RESULT_SCHEMA_VERSION,
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
    except (DatasetError, json.JSONDecodeError, OSError) as error:
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
