from __future__ import annotations

from datetime import UTC, datetime

from psycopg import Connection

from .classification import is_recallable_memory_content
from .ids import stable_uuid
from .model_adapter import is_graph_entity_candidate
from .redaction import redact_text


def build_quality_report(
    connection: Connection, *, namespace_key: str, trusted_tools: frozenset[str]
) -> dict:
    """Return aggregate-only quality evidence without exposing memory text."""
    namespace_id = stable_uuid("namespace", namespace_key)
    total_facts, traceable_facts, model_atomic_facts = connection.execute(
        """SELECT count(*),
                  count(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM memory.fact_evidence fe WHERE fe.fact_id=f.id
                  )),
                  count(*) FILTER (WHERE f.extraction_method='model-verbatim')
           FROM memory.facts f
           WHERE f.namespace_id=%s AND f.memory_state <> 'purge_requested'""",
        (namespace_id,),
    ).fetchone()
    valid_atomic_spans = connection.execute(
        """SELECT count(DISTINCT f.id)
           FROM memory.facts f
           JOIN memory.fact_evidence fe ON fe.fact_id=f.id
           JOIN evidence.events e ON e.id=fe.event_id
           WHERE f.namespace_id=%s AND f.extraction_method='model-verbatim'
             AND f.evidence_span_start IS NOT NULL AND f.evidence_span_end IS NOT NULL
             AND f.evidence_span_start >= 0 AND f.evidence_span_end > f.evidence_span_start
             AND substring(e.redacted_payload->>'content'
                   FROM f.evidence_span_start + 1
                   FOR f.evidence_span_end - f.evidence_span_start)=f.statement""",
        (namespace_id,),
    ).fetchone()[0]
    mention_rows = connection.execute(
        """SELECT m.mention_text,en.entity_type,
             (m.span_start >= 0 AND m.span_end > m.span_start AND
              substring(e.redacted_payload->>'content'
                FROM m.span_start + 1 FOR m.span_end - m.span_start)=m.mention_text)
           FROM memory.entity_mentions m
           JOIN memory.entities en ON en.id=m.entity_id
           JOIN evidence.events e ON e.id=m.event_id
           WHERE m.namespace_id=%s""",
        (namespace_id,),
    ).fetchall()
    total_mentions = len(mention_rows)
    valid_mentions = sum(bool(row[2]) for row in mention_rows)
    disallowed_entity_mentions = sum(
        not is_graph_entity_candidate(str(row[0]), str(row[1]))
        for row in mention_rows
    )
    duplicate_fact_support = connection.execute(
        """SELECT count(*) FROM (
             SELECT fe.event_id,lower(trim(f.statement))
             FROM memory.facts f JOIN memory.fact_evidence fe ON fe.fact_id=f.id
             WHERE f.namespace_id=%s AND f.memory_state <> 'purge_requested'
             GROUP BY fe.event_id,lower(trim(f.statement)) HAVING count(*) > 1
           ) duplicates""",
        (namespace_id,),
    ).fetchone()[0]
    trusted = list(trusted_tools)
    untrusted_tool_facts = connection.execute(
        """SELECT count(*) FROM (
             SELECT f.id
             FROM memory.facts f
             JOIN memory.fact_evidence fe ON fe.fact_id=f.id
             JOIN evidence.events e ON e.id=fe.event_id
             WHERE f.namespace_id=%s AND f.memory_state <> 'purge_requested'
             GROUP BY f.id
             HAVING bool_or(e.event_type='tool_result' AND
                     NOT (lower(COALESCE(e.redacted_payload->>'tool_name',''))=ANY(%s)))
                AND NOT bool_or(
                     e.event_type IN ('user_message','environment_observation') OR
                     (e.event_type='tool_result' AND
                      lower(COALESCE(e.redacted_payload->>'tool_name',''))=ANY(%s)))
           ) suspect""",
        (namespace_id, trusted, trusted),
    ).fetchone()[0]
    statements = connection.execute(
        """SELECT statement FROM memory.facts
           WHERE namespace_id=%s AND memory_state <> 'purge_requested'""",
        (namespace_id,),
    ).fetchall()
    raw_sensitive_facts = sum(
        redact_text(row[0]).text != row[0] for row in statements
    )
    model_rows = connection.execute(
        """SELECT f.statement,bool_or(
                 e.event_type='user_message'
                 AND se.external_session_id LIKE 'hermes-export:cron_%%'
               ) AS automated_user_fact
           FROM memory.facts f
           JOIN memory.fact_evidence fe ON fe.fact_id=f.id
           JOIN evidence.events e ON e.id=fe.event_id
           JOIN core.turns t ON t.id=e.turn_id
           JOIN core.sessions se ON se.id=t.session_id
           WHERE f.namespace_id=%s AND f.extraction_method='model-verbatim'
             AND f.memory_state <> 'purge_requested'
           GROUP BY f.id,f.statement""",
        (namespace_id,),
    ).fetchall()
    disallowed_model_facts = sum(
        not is_recallable_memory_content(str(row[0])) for row in model_rows
    )
    automated_user_facts = sum(bool(row[1]) for row in model_rows)
    classifications = {
        f"{row[0]}:{row[1]}": int(row[2])
        for row in connection.execute(
            """SELECT fact_type,memory_state,count(*) FROM memory.facts
               WHERE namespace_id=%s AND memory_state <> 'purge_requested'
               GROUP BY fact_type,memory_state ORDER BY fact_type,memory_state""",
            (namespace_id,),
        ).fetchall()
    }

    def rate(value: int, total: int) -> float | None:
        return round(value / total, 6) if total else None

    gates = {
        "evidence_traceability": total_facts > 0 and traceable_facts == total_facts,
        "raw_sensitive_fact_leakage": raw_sensitive_facts == 0,
        "duplicate_fact_support": duplicate_fact_support == 0,
        "model_atomic_coverage": model_atomic_facts > 0,
        "atomic_span_integrity": (
            model_atomic_facts > 0 and valid_atomic_spans == model_atomic_facts
        ),
        "entity_mention_span_integrity": (
            total_mentions == 0 or valid_mentions == total_mentions
        ),
        "model_declarative_shape": disallowed_model_facts == 0,
        "automated_prompt_isolation": automated_user_facts == 0,
        "graph_entity_policy": disallowed_entity_mentions == 0,
    }
    automatic_ready = all(gates.values())
    return {
        "namespace": namespace_key,
        "generated_at": datetime.now(UTC),
        "automatic_ready": automatic_ready,
        "promotion_ready": False,
        "manual_review_required": True,
        "gates": gates,
        "metrics": {
            "facts": int(total_facts),
            "traceable_facts": int(traceable_facts),
            "traceability_rate": rate(traceable_facts, total_facts),
            "model_atomic_facts": int(model_atomic_facts),
            "valid_atomic_spans": int(valid_atomic_spans),
            "atomic_span_rate": rate(valid_atomic_spans, model_atomic_facts),
            "entity_mentions": int(total_mentions),
            "valid_entity_mentions": int(valid_mentions),
            "entity_mention_span_rate": rate(valid_mentions, total_mentions),
            "duplicate_fact_support": int(duplicate_fact_support),
            "raw_sensitive_facts": int(raw_sensitive_facts),
            "untrusted_tool_facts": int(untrusted_tool_facts),
            "disallowed_model_facts": int(disallowed_model_facts),
            "automated_user_facts": int(automated_user_facts),
            "disallowed_entity_mentions": int(disallowed_entity_mentions),
        },
        "classifications": classifications,
        "decision": (
            "MANUAL_REVIEW_REQUIRED" if automatic_ready else "AUTOMATIC_GATES_FAILED"
        ),
    }
