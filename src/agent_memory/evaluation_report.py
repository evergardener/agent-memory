from __future__ import annotations

import argparse
import json
from collections import Counter
from uuid import UUID

from .classification import is_recallable_memory_content
from .config import get_settings
from .db import Database
from .evaluation_plan import build_report, load_turns
from .ids import stable_uuid
from .model_adapter import is_graph_entity_candidate
from .redaction import redact_text
from .worker import ATOMIC_EXTRACTION_VERSION


def assemble_report(*, plan: dict, metrics: dict) -> dict:
    selected_count = int(plan["selected_turn_count"])
    model_fact_count = int(metrics["model_fact_count"])
    gates = {
        "plan_has_no_redaction_findings": plan["selected_redaction_findings"] == 0,
        "selected_jobs_complete": (
            metrics["done_job_count"] == selected_count
            and metrics["unfinished_job_count"] == 0
        ),
        "selected_turns_audited": metrics["audited_turn_count"] == selected_count,
        "nonselected_model_facts": metrics["outside_plan_model_fact_count"] == 0,
        "atomic_span_integrity": (
            model_fact_count == 0
            or metrics["valid_atomic_span_count"] == model_fact_count
        ),
        "entity_span_integrity": (
            metrics["entity_mention_count"] == metrics["valid_entity_mention_count"]
        ),
        "raw_sensitive_fact_leakage": metrics["raw_sensitive_fact_count"] == 0,
        "declarative_fact_shape": metrics["disallowed_statement_count"] == 0,
        "graph_entity_policy": metrics["disallowed_entity_mention_count"] == 0,
        "automated_prompt_isolation": metrics["automated_user_fact_count"] == 0,
    }
    automatic_ready = all(gates.values())
    return {
        "plan_version": plan["plan_version"],
        "namespace": plan["namespace"],
        "confirm_sha256": plan["confirm_sha256"],
        "selected_turn_count": selected_count,
        "automatic_ready": automatic_ready,
        "promotion_ready": False,
        "manual_semantic_review_required": True,
        "gates": gates,
        "metrics": metrics,
        "contains_memory_text": False,
        "model_called_by_report": False,
        "external_data_sent_by_report": False,
        "decision": (
            "MANUAL_SEMANTIC_REVIEW_REQUIRED"
            if automatic_ready
            else "AUTOMATIC_GATES_FAILED"
        ),
    }


def collect_metrics(connection, *, namespace: str, turn_ids: tuple[UUID, ...]) -> dict:
    namespace_id = stable_uuid("namespace", namespace)
    ids = list(turn_ids)
    job_rows = connection.execute(
        """SELECT status,count(*) FROM ops.jobs
           WHERE namespace_id=%s AND kind='extract_atomic_turn'
             AND idempotency_key LIKE %s
             AND input_ref=ANY(%s::uuid[])
           GROUP BY status""",
        (namespace_id, f"extract_atomic_turn:{ATOMIC_EXTRACTION_VERSION}:%", ids),
    ).fetchall()
    jobs = Counter({str(status): int(count) for status, count in job_rows})
    audit_rows = connection.execute(
        """SELECT action,count(DISTINCT target_id) FROM audit.events
           WHERE namespace_id=%s AND target_type='turn'
             AND target_id=ANY(%s::uuid[])
             AND action LIKE 'memory.model.atomic.%%'
             AND metadata_redacted->>'extractor_version'=%s
           GROUP BY action""",
        (namespace_id, ids, ATOMIC_EXTRACTION_VERSION),
    ).fetchall()
    outcomes = {
        str(action).removeprefix("memory.model.atomic."): int(count)
        for action, count in audit_rows
    }
    audited_turn_count = connection.execute(
        """SELECT count(DISTINCT target_id) FROM audit.events
           WHERE namespace_id=%s AND target_type='turn'
             AND target_id=ANY(%s::uuid[])
             AND action LIKE 'memory.model.atomic.%%'
             AND metadata_redacted->>'extractor_version'=%s""",
        (namespace_id, ids, ATOMIC_EXTRACTION_VERSION),
    ).fetchone()[0]
    facts = connection.execute(
        """SELECT f.id,f.statement,
                  bool_or(
                    f.evidence_span_start IS NOT NULL
                    AND f.evidence_span_end > f.evidence_span_start
                    AND substring(e.redacted_payload->>'content'
                      FROM f.evidence_span_start + 1
                      FOR f.evidence_span_end - f.evidence_span_start)=f.statement
                  ) AS valid_span,
                  bool_or(
                    e.event_type='user_message'
                    AND se.external_session_id LIKE 'hermes-export:cron_%%'
                  ) AS automated_user_fact
           FROM memory.facts f
           JOIN memory.fact_evidence fe ON fe.fact_id=f.id
           JOIN evidence.events e ON e.id=fe.event_id
           JOIN core.turns t ON t.id=e.turn_id
           JOIN core.sessions se ON se.id=t.session_id
           WHERE f.namespace_id=%s AND f.extraction_method='model-verbatim'
             AND f.extraction_version=%s AND f.memory_state <> 'isolated'
             AND e.turn_id=ANY(%s::uuid[])
           GROUP BY f.id,f.statement""",
        (namespace_id, ATOMIC_EXTRACTION_VERSION, ids),
    ).fetchall()
    fact_ids = [row[0] for row in facts]
    mention_rows = []
    if fact_ids:
        mention_rows = connection.execute(
            """SELECT m.fact_id,m.mention_text,en.entity_type,
                  (m.span_start >= 0 AND m.span_end > m.span_start AND
                   substring(e.redacted_payload->>'content'
                     FROM m.span_start + 1 FOR m.span_end - m.span_start)=m.mention_text)
               FROM memory.entity_mentions m
               JOIN memory.entities en ON en.id=m.entity_id
               JOIN evidence.events e ON e.id=m.event_id
               WHERE m.namespace_id=%s AND m.fact_id=ANY(%s::uuid[])
                 AND m.extraction_version=%s""",
            (namespace_id, fact_ids, ATOMIC_EXTRACTION_VERSION),
        ).fetchall()
    outside_plan_model_fact_count = connection.execute(
        """SELECT count(DISTINCT f.id)
           FROM memory.facts f
           JOIN memory.fact_evidence fe ON fe.fact_id=f.id
           JOIN evidence.events e ON e.id=fe.event_id
           WHERE f.namespace_id=%s AND f.extraction_method='model-verbatim'
             AND f.extraction_version=%s AND f.memory_state <> 'isolated'
             AND NOT (e.turn_id=ANY(%s::uuid[]))""",
        (namespace_id, ATOMIC_EXTRACTION_VERSION, ids),
    ).fetchone()[0]
    policy_eligible_fact_ids = {
        row[0]
        for row in facts
        if is_recallable_memory_content(str(row[1])) and not bool(row[3])
    }
    return {
        "job_statuses": dict(sorted(jobs.items())),
        "extraction_version": ATOMIC_EXTRACTION_VERSION,
        "done_job_count": jobs["done"],
        "unfinished_job_count": sum(
            count for status, count in jobs.items() if status != "done"
        ),
        "audited_turn_count": int(audited_turn_count),
        "outcomes": dict(sorted(outcomes.items())),
        "model_fact_count": len(facts),
        "valid_atomic_span_count": sum(bool(row[2]) for row in facts),
        "entity_mention_count": len(mention_rows),
        "valid_entity_mention_count": sum(bool(row[3]) for row in mention_rows),
        "disallowed_statement_count": sum(
            not is_recallable_memory_content(str(row[1])) for row in facts
        ),
        "disallowed_entity_mention_count": sum(
            not is_graph_entity_candidate(str(row[1]), str(row[2]))
            for row in mention_rows
        ),
        "automated_user_fact_count": sum(bool(row[3]) for row in facts),
        "policy_eligible_fact_count": len(policy_eligible_fact_ids),
        "policy_eligible_entity_mention_count": sum(
            row[0] in policy_eligible_fact_ids
            and is_graph_entity_candidate(str(row[1]), str(row[2]))
            for row in mention_rows
        ),
        "raw_sensitive_fact_count": sum(
            bool(redact_text(str(row[1])).findings) for row in facts
        ),
        "outside_plan_model_fact_count": int(outside_plan_model_fact_count),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report aggregate-only results for a confirmed real-history evaluation plan."
    )
    parser.add_argument("--namespace")
    parser.add_argument("--sample-size", type=int, default=24)
    parser.add_argument("--seed", default="v1-real-history-pilot")
    parser.add_argument("--confirm-sha256", required=True)
    arguments = parser.parse_args()
    settings = get_settings()
    namespace = arguments.namespace or settings.namespace
    if namespace != settings.namespace:
        parser.error("namespace must match AGENT_MEMORY_NAMESPACE for this runtime")
    if namespace == "hermes:user-primary":
        parser.error("evaluation reporting is forbidden for the primary namespace")

    database = Database(settings)
    database.open()
    try:
        with database.connection() as connection:
            plan = build_report(
                namespace=namespace,
                seed=arguments.seed,
                turns=load_turns(connection, namespace=namespace),
                sample_size=arguments.sample_size,
            )
            if arguments.confirm_sha256 != plan["confirm_sha256"]:
                parser.error("--confirm-sha256 does not match the current evaluation plan")
            turn_ids = tuple(UUID(item["turn_id"]) for item in plan["selected_turns"])
            metrics = collect_metrics(connection, namespace=namespace, turn_ids=turn_ids)
            report = assemble_report(plan=plan, metrics=metrics)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    finally:
        database.close()


if __name__ == "__main__":
    main()
