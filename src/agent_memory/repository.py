import hashlib
import json
import re
from collections import defaultdict
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .classification import is_recallable_memory_content
from .community_projection import enqueue_community_rebuild
from .embeddings import EMBEDDING_VERSION, deterministic_embedding, vector_literal
from .ids import new_uuid, stable_uuid
from .model_adapter import is_graph_entity_candidate
from .redaction import redact_structure, redact_structure_with_findings, redact_text
from .schemas import (
    CorrectionRequest,
    EntityMergeRequest,
    EntitySplitRequest,
    EvidenceTraceItem,
    GovernanceTraceItem,
    IngestTurnRequest,
    MemoryTraceResponse,
    RecallItem,
    RecallRequest,
    ReviewQueueItem,
    ReviewQueueResponse,
)
from .subjects import ensure_source_subject_mapping

ENTITY_TYPES = {
    "person",
    "agent",
    "project",
    "service",
    "location",
    "organization",
    "tool",
    "technology",
    "device",
    "concept",
    "event",
    "other",
}


def _redact_event_payload(event) -> tuple[dict, tuple]:
    content_redaction = redact_text(event.content)
    findings = list(content_redaction.findings)
    payload = {"content": content_redaction.text}
    if event.tool_name:
        payload["tool_name"] = event.tool_name
    if event.arguments is not None:
        arguments_redaction = redact_structure_with_findings(event.arguments)
        payload["arguments_redacted"] = json.dumps(
            arguments_redaction.value, ensure_ascii=False, sort_keys=True
        )
        findings.extend(arguments_redaction.findings)
    return payload, tuple(findings)


def ingest_turn(
    connection: Connection, request: IngestTurnRequest
) -> tuple[list[UUID], list[UUID], bool]:
    context = request.context
    namespace_id = stable_uuid("namespace", context.shared_namespace)
    source_id = stable_uuid(
        "source", f"{namespace_id}:{context.source_profile}:{context.source_instance}"
    )
    session_id = stable_uuid("session", f"{source_id}:{context.external_session_id}")
    turn_id = stable_uuid("turn", f"{session_id}:{context.external_turn_id}")
    connection.execute(
        "INSERT INTO core.namespaces(id,stable_key) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (namespace_id, context.shared_namespace),
    )
    connection.execute(
        """INSERT INTO core.sources(id,namespace_id,source_profile,source_instance)
           VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (source_id, namespace_id, context.source_profile, context.source_instance),
    )
    ensure_source_subject_mapping(
        connection, namespace_id, source_id, context.source_profile
    )
    connection.execute(
        """INSERT INTO core.sessions(id,namespace_id,source_id,external_session_id,started_at)
           VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (session_id, namespace_id, source_id, context.external_session_id, request.occurred_at),
    )
    connection.execute(
        """INSERT INTO core.turns(id,session_id,external_turn_id,occurred_at)
           VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (turn_id, session_id, context.external_turn_id, request.occurred_at),
    )

    event_ids: list[UUID] = []
    job_ids: list[UUID] = []
    inserted_any = False
    for event in request.events:
        payload, redaction_findings = _redact_event_payload(event)
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        ingest_key = f"{request.idempotency_key}:{event.sequence}:{event.type}"
        event_id = stable_uuid("event", ingest_key)
        row = connection.execute(
            """INSERT INTO evidence.events(
                 id,namespace_id,turn_id,event_type,sequence_no,redacted_payload,payload_hash,
                 ingest_key,occurred_at
               ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
               ON CONFLICT (ingest_key) DO NOTHING RETURNING id""",
            (
                event_id,
                namespace_id,
                turn_id,
                event.type,
                event.sequence,
                serialized,
                hashlib.sha256(serialized.encode()).hexdigest(),
                ingest_key,
                request.occurred_at,
            ),
        ).fetchone()
        event_ids.append(event_id)
        if row is None:
            continue
        inserted_any = True
        for index, finding in enumerate(redaction_findings):
            finding_id = stable_uuid("finding", f"{event_id}:{index}:{finding.span_hash}")
            connection.execute(
                """INSERT INTO evidence.redaction_findings(
                     id,event_id,kind,span_hash,action,rule_version
                   ) VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    finding_id,
                    event_id,
                    finding.kind,
                    finding.span_hash,
                    finding.action,
                    finding.rule_version,
                ),
            )
        job_id = stable_uuid("job", f"extract_facts:{event_id}")
        connection.execute(
            """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
               VALUES (%s,%s,'extract_facts',%s,%s) ON CONFLICT DO NOTHING""",
            (job_id, namespace_id, f"extract_facts:{event_id}", event_id),
        )
        job_ids.append(job_id)
        connection.execute(
            """INSERT INTO audit.events(
                 id,namespace_id,actor_type,actor_id,action,target_type,target_id,correlation_id
               ) VALUES (%s,%s,'provider',%s,'evidence.ingest','evidence_event',%s,%s)""",
            (new_uuid(), namespace_id, context.source_profile, event_id, context.correlation_id),
        )
    return event_ids, job_ids, not inserted_any


def enqueue_derived_rebuild(
    connection: Connection, namespace_id: UUID, input_ref: UUID, reason_key: str
) -> UUID:
    job_id = stable_uuid("job", f"rebuild_derived:{reason_key}")
    connection.execute(
        """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
           VALUES (%s,%s,'rebuild_derived',%s,%s) ON CONFLICT DO NOTHING""",
        (job_id, namespace_id, f"rebuild_derived:{reason_key}", input_ref),
    )
    enqueue_community_rebuild(connection, namespace_id, reason_key=reason_key)
    return job_id


def _overlapping_deterministic_fact_ids(records: dict[UUID, dict]) -> set[UUID]:
    """Hide a whole-message fallback when a recalled atomic quote covers the same evidence."""
    atomic: list[dict] = [
        row
        for row in records.values()
        if row.get("kind") == "fact" and row.get("extraction_method") == "model-verbatim"
    ]
    suppressed: set[UUID] = set()
    for memory_id, row in records.items():
        if row.get("kind") != "fact" or row.get("extraction_method") != "deterministic-v1":
            continue
        parent_text = row.get("text_redacted") or ""
        parent_sources = set(row.get("source_ids") or ())
        if not parent_sources:
            continue
        for atomic_row in atomic:
            atomic_text = atomic_row.get("text_redacted") or ""
            if (
                atomic_text
                and atomic_text != parent_text
                and atomic_text in parent_text
                and parent_sources.intersection(atomic_row.get("source_ids") or ())
            ):
                suppressed.add(memory_id)
                break
    return suppressed


def recall(connection: Connection, request: RecallRequest) -> tuple[list[RecallItem], bool]:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    query_embedding = vector_literal(deterministic_embedding(request.query))
    explicit_recall = request.intent == "explicit"
    lexical_states = (
        "('candidate','active','forgotten')" if explicit_recall else "('candidate','active')"
    )
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            f"""SELECT d.source_id AS memory_id,'fact' AS kind,d.text_redacted,f.source_profile,
                      f.extraction_method,
                      array_remove(array_agg(fe.event_id),NULL) AS source_ids
               FROM retrieval.documents d
               JOIN memory.facts f ON f.id=d.source_id AND d.source_kind='fact'
               LEFT JOIN memory.fact_evidence fe ON fe.fact_id=f.id
               WHERE d.namespace_id=%s AND d.lifecycle_state IN {lexical_states}
                 AND (f.valid_to IS NULL OR f.valid_to > now())
                 AND d.search_vector @@ plainto_tsquery('simple',%s)
               GROUP BY d.source_id,d.text_redacted,f.source_profile,f.extraction_method,
                        d.search_vector,d.lifecycle_state
               ORDER BY ts_rank(d.search_vector,plainto_tsquery('simple',%s)) DESC LIMIT 25""",
            (namespace_id, request.query, request.query),
        )
        lexical = cursor.fetchall()
        cursor.execute(
            f"""SELECT DISTINCT d.source_id AS memory_id,'fact' AS kind,d.text_redacted,
                      f.source_profile,f.extraction_method,
                      CASE WHEN fev.event_id IS NULL THEN '{{}}'::uuid[]
                           ELSE array[fev.event_id] END AS source_ids
               FROM retrieval.documents d
               JOIN memory.facts f ON f.id=d.source_id AND d.source_kind='fact'
               LEFT JOIN memory.fact_evidence fev ON fev.fact_id=f.id
               JOIN memory.fact_entities mfe ON mfe.fact_id=f.id
               JOIN memory.entities e ON e.id=mfe.entity_id
               LEFT JOIN memory.entities canonical ON canonical.id=e.canonical_entity_id
               WHERE d.namespace_id=%s AND d.lifecycle_state IN {lexical_states}
                 AND (f.valid_to IS NULL OR f.valid_to > now())
                 AND (position(e.normalized_name in lower(%s)) > 0 OR
                      position(canonical.normalized_name in lower(%s)) > 0) LIMIT 25""",
            (namespace_id, request.query, request.query),
        )
        entity = cursor.fetchall()
        cursor.execute(
            """SELECT d.source_id AS memory_id,'fact' AS kind,d.text_redacted,f.source_profile,
                      f.extraction_method,
                      array_remove(array_agg(fe.event_id),NULL) AS source_ids
               FROM retrieval.documents d
               JOIN memory.facts f ON f.id=d.source_id AND d.source_kind='fact'
               LEFT JOIN memory.fact_evidence fe ON fe.fact_id=f.id
               WHERE d.namespace_id=%s AND d.lifecycle_state IN ('candidate','active')
                 AND (f.valid_to IS NULL OR f.valid_to > now())
                 AND d.embedding_model_version=%s AND d.embedding IS NOT NULL
                 AND (d.embedding <=> %s::vector) < 0.5
               GROUP BY d.source_id,d.text_redacted,f.source_profile,f.extraction_method,d.embedding
               ORDER BY d.embedding <=> %s::vector LIMIT 25""",
            (namespace_id, EMBEDDING_VERSION, query_embedding, query_embedding),
        )
        semantic = cursor.fetchall()

        derived_cte = """
            WITH derived AS (
              SELECT id,entity_id,'episode'::text AS kind FROM memory.episodes
               WHERE namespace_id=%s AND state='active'
              UNION ALL
              SELECT id,entity_id,'arc'::text AS kind FROM memory.arcs
               WHERE namespace_id=%s AND state='active'
            ), evidence_links AS (
              SELECT ef.episode_id AS derived_id,
                     array_remove(array_agg(DISTINCT fe.event_id),NULL) AS source_ids
                FROM memory.episode_facts ef
                LEFT JOIN memory.fact_evidence fe ON fe.fact_id=ef.fact_id
               GROUP BY ef.episode_id
              UNION ALL
              SELECT af.arc_id AS derived_id,
                     array_remove(array_agg(DISTINCT fe.event_id),NULL) AS source_ids
                FROM memory.arc_facts af
                LEFT JOIN memory.fact_evidence fe ON fe.fact_id=af.fact_id
               GROUP BY af.arc_id
            )
        """
        cursor.execute(
            derived_cte
            + """SELECT d.source_id AS memory_id,derived.kind,d.text_redacted,
                         'derived'::text AS source_profile,'derived'::text AS extraction_method,
                         COALESCE(el.source_ids,'{}'::uuid[]) AS source_ids
                    FROM retrieval.documents d JOIN derived ON derived.id=d.source_id
                    LEFT JOIN evidence_links el ON el.derived_id=derived.id
                   WHERE d.namespace_id=%s AND d.lifecycle_state='active'
                     AND d.search_vector @@ plainto_tsquery('simple',%s)
                   ORDER BY ts_rank(d.search_vector,plainto_tsquery('simple',%s)) DESC
                   LIMIT 25""",
            (namespace_id, namespace_id, namespace_id, request.query, request.query),
        )
        derived_lexical = cursor.fetchall()
        cursor.execute(
            derived_cte
            + """SELECT d.source_id AS memory_id,derived.kind,d.text_redacted,
                         'derived'::text AS source_profile,'derived'::text AS extraction_method,
                         COALESCE(el.source_ids,'{}'::uuid[]) AS source_ids
                    FROM retrieval.documents d JOIN derived ON derived.id=d.source_id
                    JOIN memory.entities e ON e.id=derived.entity_id
                    LEFT JOIN memory.entities canonical ON canonical.id=e.canonical_entity_id
                    LEFT JOIN evidence_links el ON el.derived_id=derived.id
                   WHERE d.namespace_id=%s AND d.lifecycle_state='active'
                     AND (position(e.normalized_name in lower(%s)) > 0 OR
                          position(canonical.normalized_name in lower(%s)) > 0) LIMIT 25""",
            (namespace_id, namespace_id, namespace_id, request.query, request.query),
        )
        derived_entity = cursor.fetchall()
        cursor.execute(
            derived_cte
            + """SELECT d.source_id AS memory_id,derived.kind,d.text_redacted,
                         'derived'::text AS source_profile,'derived'::text AS extraction_method,
                         COALESCE(el.source_ids,'{}'::uuid[]) AS source_ids
                    FROM retrieval.documents d JOIN derived ON derived.id=d.source_id
                    LEFT JOIN evidence_links el ON el.derived_id=derived.id
                   WHERE d.namespace_id=%s AND d.lifecycle_state='active'
                     AND d.embedding_model_version=%s AND d.embedding IS NOT NULL
                     AND (d.embedding <=> %s::vector) < 0.5
                   ORDER BY d.embedding <=> %s::vector LIMIT 25""",
            (
                namespace_id,
                namespace_id,
                namespace_id,
                EMBEDDING_VERSION,
                query_embedding,
                query_embedding,
            ),
        )
        derived_semantic = cursor.fetchall()

    lexical.extend(derived_lexical)
    entity.extend(derived_entity)
    semantic.extend(derived_semantic)

    scores: dict[UUID, float] = defaultdict(float)
    records: dict[UUID, dict] = {}
    channels: dict[UUID, list[str]] = defaultdict(list)
    for channel, rows in (("lexical", lexical), ("semantic", semantic), ("entity", entity)):
        seen_in_channel: set[UUID] = set()
        unique_rank = 0
        for row in rows:
            if row["kind"] == "fact" and not is_recallable_memory_content(
                row["text_redacted"]
            ):
                continue
            memory_id = row["memory_id"]
            if memory_id in seen_in_channel:
                merged_sources = {
                    *records[memory_id].get("source_ids", ()),
                    *row.get("source_ids", ()),
                }
                records[memory_id]["source_ids"] = sorted(merged_sources, key=str)
                continue
            seen_in_channel.add(memory_id)
            unique_rank += 1
            scores[memory_id] += 1 / (60 + unique_rank)
            if memory_id in records:
                merged_sources = {
                    *records[memory_id].get("source_ids", ()),
                    *row.get("source_ids", ()),
                }
                records[memory_id]["source_ids"] = sorted(merged_sources, key=str)
            else:
                records[memory_id] = dict(row)
            channels[memory_id].append(channel)

    items: list[RecallItem] = []
    used_chars = 0
    truncated = False
    suppressed = _overlapping_deterministic_fact_ids(records)
    for memory_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
        if memory_id in suppressed:
            continue
        row = records[memory_id]
        text = redact_text(row["text_redacted"]).text
        if (
            len(items) >= request.budget.max_items
            or used_chars + len(text) > request.budget.max_chars
        ):
            truncated = True
            break
        items.append(
            RecallItem(
                memory_id=memory_id,
                kind=row["kind"],
                text=text,
                source_ids=row["source_ids"],
                source_profile=row["source_profile"],
                channels=channels[memory_id],
                rrf_score=round(score, 8),
                why_recalled="+".join(channels[memory_id]),
            )
        )
        used_chars += len(text)
    return items, truncated


def trace_memory(
    connection: Connection, namespace_key: str, memory_id: UUID
) -> MemoryTraceResponse | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,statement,memory_state,version,supersedes_fact_id,
                      extraction_method,extraction_version,model_name,
                      evidence_span_start,evidence_span_end
               FROM memory.facts WHERE id=%s AND namespace_id=%s""",
            (memory_id, namespace_id),
        )
        fact = cursor.fetchone()
        if fact is None:
            cursor.execute(
                """SELECT id,summary AS statement,state AS memory_state,version
                   FROM memory.episodes WHERE id=%s AND namespace_id=%s
                   UNION ALL
                   SELECT id,summary AS statement,state AS memory_state,version
                   FROM memory.arcs WHERE id=%s AND namespace_id=%s""",
                (memory_id, namespace_id, memory_id, namespace_id),
            )
            fact = cursor.fetchone()
            if fact is None:
                return None
            fact["supersedes_fact_id"] = None
            fact["extraction_method"] = "derived"
            fact["extraction_version"] = None
            fact["model_name"] = None
            fact["evidence_span_start"] = None
            fact["evidence_span_end"] = None
            cursor.execute(
                """WITH linked_facts AS (
                     SELECT fact_id FROM memory.episode_facts WHERE episode_id=%s
                     UNION SELECT fact_id FROM memory.arc_facts WHERE arc_id=%s
                   )
                   SELECT DISTINCT e.id AS evidence_id,e.event_type,e.occurred_at,
                          s.source_profile,s.source_instance,s.id AS source_id,
                          se.id AS internal_session_id,se.external_session_id,
                          t.external_turn_id,fe.support_kind,fe.weight,e.redacted_payload
                   FROM linked_facts lf JOIN memory.fact_evidence fe ON fe.fact_id=lf.fact_id
                   JOIN evidence.events e ON e.id=fe.event_id
                   JOIN core.turns t ON t.id=e.turn_id
                   JOIN core.sessions se ON se.id=t.session_id
                   JOIN core.sources s ON s.id=se.source_id
                   ORDER BY e.occurred_at""",
                (memory_id, memory_id),
            )
        else:
            cursor.execute(
                """SELECT e.id AS evidence_id,e.event_type,e.occurred_at,s.source_profile,
                          s.source_instance,s.id AS source_id,se.id AS internal_session_id,
                          se.external_session_id,t.external_turn_id,fe.support_kind,fe.weight,
                          e.redacted_payload
                   FROM memory.fact_evidence fe JOIN evidence.events e ON e.id=fe.event_id
               JOIN core.turns t ON t.id=e.turn_id
               JOIN core.sessions se ON se.id=t.session_id
               JOIN core.sources s ON s.id=se.source_id
               WHERE fe.fact_id=%s ORDER BY e.occurred_at""",
                (memory_id,),
            )
        evidence_rows = cursor.fetchall()
        for row in evidence_rows:
            row["redacted_payload"] = redact_structure(row["redacted_payload"])
        evidence = [EvidenceTraceItem.model_validate(row) for row in evidence_rows]
        cursor.execute(
            """SELECT action,actor_type,actor_id,reason,correlation_id,
                      metadata_redacted,created_at
               FROM audit.events
               WHERE namespace_id=%s AND target_id=%s
                 AND action IN ('memory.correct','memory.forgotten','memory.isolated',
                                'memory.purge.requested')
               ORDER BY created_at""",
            (namespace_id, memory_id),
        )
        governance_rows = cursor.fetchall()
        for row in governance_rows:
            row["reason"] = redact_text(row["reason"]).text
            row["metadata_redacted"] = redact_structure(row["metadata_redacted"])
        governance = [GovernanceTraceItem.model_validate(row) for row in governance_rows]
    return MemoryTraceResponse(
        memory_id=fact["id"],
        statement=redact_text(fact["statement"]).text,
        state=fact["memory_state"],
        version=fact["version"],
        supersedes_memory_id=fact["supersedes_fact_id"],
        extraction_method=fact["extraction_method"],
        extraction_version=fact["extraction_version"],
        model_name=fact["model_name"],
        evidence_span_start=fact["evidence_span_start"],
        evidence_span_end=fact["evidence_span_end"],
        evidence=evidence,
        governance=governance,
    )


def list_review_queue(
    connection: Connection,
    *,
    namespace_key: str,
    trusted_tools: frozenset[str],
    reason: str,
    source_profile: str | None,
    limit: int,
    offset: int,
) -> ReviewQueueResponse:
    namespace_id = stable_uuid("namespace", namespace_key)
    trusted = list(trusted_tools)
    filters = ["f.namespace_id=%s", "f.memory_state <> 'purge_requested'"]
    parameters: list[object] = [namespace_id]
    if source_profile:
        filters.append("f.source_profile=%s")
        parameters.append(source_profile)
    base_query = f"""
        WITH review_facts AS (
          SELECT f.id,f.statement,f.fact_type,f.memory_state,f.source_profile,
                 f.confidence,f.updated_at,f.extraction_method,
                 count(DISTINCT fe.event_id) AS evidence_count,
                 array_remove(array_agg(DISTINCT CASE
                   WHEN e.event_type='tool_result'
                   THEN lower(COALESCE(e.redacted_payload->>'tool_name',''))
                 END),NULL) AS tool_names,
                 bool_or(
                   e.event_type='tool_result' AND
                   NOT (lower(COALESCE(e.redacted_payload->>'tool_name','')) = ANY(%s))
                 ) AS has_untrusted_tool,
                 bool_or(
                   e.event_type IN ('user_message','environment_observation') OR
                   (e.event_type='tool_result' AND
                    lower(COALESCE(e.redacted_payload->>'tool_name','')) = ANY(%s))
                 ) AS has_trusted_support
          FROM memory.facts f
          LEFT JOIN memory.fact_evidence fe ON fe.fact_id=f.id
          LEFT JOIN evidence.events e ON e.id=fe.event_id
          WHERE {' AND '.join(filters)}
          GROUP BY f.id
        )
    """
    parameters = [trusted, trusted, *parameters]
    review_condition = (
        "(memory_state='candidate' OR (has_untrusted_tool AND NOT has_trusted_support))"
    )
    if reason == "candidate":
        review_condition = "memory_state='candidate'"
    elif reason == "untrusted_tool":
        review_condition = "has_untrusted_tool AND NOT has_trusted_support"
    total = connection.execute(
        base_query + f"SELECT count(*) FROM review_facts WHERE {review_condition}",
        parameters,
    ).fetchone()[0]
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            base_query
            + f"""SELECT * FROM review_facts WHERE {review_condition}
                   ORDER BY (has_untrusted_tool AND NOT has_trusted_support) DESC,
                            updated_at DESC,id DESC
                   LIMIT %s OFFSET %s""",
            [*parameters, limit, offset],
        )
        rows = cursor.fetchall()
    items: list[ReviewQueueItem] = []
    for row in rows:
        reasons = []
        if row["has_untrusted_tool"] and not row["has_trusted_support"]:
            reasons.append("untrusted_tool")
        if row["memory_state"] == "candidate":
            reasons.append("candidate")
        items.append(
            ReviewQueueItem(
                memory_id=row["id"],
                statement=redact_text(row["statement"]).text,
                fact_type=row["fact_type"],
                state=row["memory_state"],
                source_profile=row["source_profile"],
                confidence=float(row["confidence"]),
                evidence_count=int(row["evidence_count"]),
                updated_at=row["updated_at"],
                extraction_method=row["extraction_method"],
                review_reasons=reasons,
                tool_names=[name for name in row["tool_names"] if name],
            )
        )
    profiles = [
        row[0]
        for row in connection.execute(
            """SELECT DISTINCT source_profile FROM core.sources
               WHERE namespace_id=%s ORDER BY source_profile""",
            (namespace_id,),
        ).fetchall()
    ]
    return ReviewQueueResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        profiles=profiles,
    )


def _entity_audit(
    connection: Connection,
    *,
    namespace_id: UUID,
    actor_id: str,
    action: str,
    entity_id: UUID,
    reason: str,
    correlation_id: UUID,
    metadata: dict,
) -> None:
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,
             correlation_id,metadata_redacted
           ) VALUES (%s,%s,'user',%s,%s,'entity',%s,%s,%s,%s::jsonb)""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            action,
            entity_id,
            redact_text(reason).text,
            correlation_id,
            json.dumps(redact_structure(metadata), ensure_ascii=False),
        ),
    )


def merge_entity(
    connection: Connection, source_entity_id: UUID, request: EntityMergeRequest
) -> dict | None:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    if source_entity_id == request.target_entity_id:
        raise ValueError("ENTITY_SELF_MERGE")
    rows = connection.execute(
        """SELECT id,canonical_entity_id,merge_state FROM memory.entities
           WHERE namespace_id=%s AND id=ANY(%s) FOR UPDATE""",
        (namespace_id, [source_entity_id, request.target_entity_id]),
    ).fetchall()
    entities = {row[0]: row for row in rows}
    if source_entity_id not in entities or request.target_entity_id not in entities:
        return None
    source = entities[source_entity_id]
    target = entities[request.target_entity_id]
    if source[1] is not None or source[2] != "active":
        raise ValueError("ENTITY_ALREADY_MERGED")
    target_id = target[1] or target[0]
    if target_id == source_entity_id:
        raise ValueError("ENTITY_MERGE_CYCLE")
    affected = connection.execute(
        """SELECT count(DISTINCT fact_id) FROM memory.fact_entities
           WHERE entity_id=%s OR entity_id=%s""",
        (source_entity_id, target_id),
    ).fetchone()[0]
    connection.execute(
        """UPDATE memory.entities SET canonical_entity_id=%s,updated_at=now()
           WHERE namespace_id=%s AND canonical_entity_id=%s""",
        (target_id, namespace_id, source_entity_id),
    )
    connection.execute(
        """UPDATE memory.entities
           SET canonical_entity_id=%s,merge_state='merged',updated_at=now()
           WHERE id=%s AND namespace_id=%s""",
        (target_id, source_entity_id, namespace_id),
    )
    _entity_audit(
        connection,
        namespace_id=namespace_id,
        actor_id=request.context.source_profile,
        action="entity.merge",
        entity_id=source_entity_id,
        reason=request.reason,
        correlation_id=request.context.correlation_id,
        metadata={"canonical_entity_id": str(target_id)},
    )
    enqueue_derived_rebuild(
        connection,
        namespace_id,
        source_entity_id,
        f"entity-merge:{source_entity_id}:{request.context.correlation_id}",
    )
    return {
        "entity_id": source_entity_id,
        "state": "merged",
        "canonical_entity_id": target_id,
        "affected_fact_count": int(affected),
        "correlation_id": request.context.correlation_id,
    }


def unmerge_entity(
    connection: Connection,
    *,
    namespace_key: str,
    entity_id: UUID,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    row = connection.execute(
        """SELECT canonical_entity_id FROM memory.entities
           WHERE id=%s AND namespace_id=%s AND merge_state='merged' FOR UPDATE""",
        (entity_id, namespace_id),
    ).fetchone()
    if row is None:
        return None
    previous_target = row[0]
    affected = connection.execute(
        "SELECT count(*) FROM memory.fact_entities WHERE entity_id=%s",
        (entity_id,),
    ).fetchone()[0]
    connection.execute(
        """UPDATE memory.entities
           SET canonical_entity_id=NULL,merge_state='active',updated_at=now()
           WHERE id=%s""",
        (entity_id,),
    )
    _entity_audit(
        connection,
        namespace_id=namespace_id,
        actor_id=actor_id,
        action="entity.unmerge",
        entity_id=entity_id,
        reason=reason,
        correlation_id=correlation_id,
        metadata={"previous_canonical_entity_id": str(previous_target)},
    )
    enqueue_derived_rebuild(
        connection,
        namespace_id,
        entity_id,
        f"entity-unmerge:{entity_id}:{correlation_id}",
    )
    return {
        "entity_id": entity_id,
        "state": "active",
        "canonical_entity_id": None,
        "affected_fact_count": int(affected),
        "correlation_id": correlation_id,
    }


def split_entity(
    connection: Connection, source_entity_id: UUID, request: EntitySplitRequest
) -> dict | None:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    source = connection.execute(
        """SELECT id FROM memory.entities
           WHERE id=%s AND namespace_id=%s AND merge_state='active'
             AND canonical_entity_id IS NULL FOR UPDATE""",
        (source_entity_id, namespace_id),
    ).fetchone()
    if source is None:
        return None
    canonical_name = redact_text(request.canonical_name).text.strip()
    normalized_name = re.sub(r"\s+", " ", canonical_name).casefold()
    if not normalized_name or normalized_name in {"[redacted]", "«redacted-secret»"}:
        raise ValueError("ENTITY_NAME_INVALID")
    entity_type = request.entity_type.strip().lower()
    if entity_type not in ENTITY_TYPES:
        raise ValueError("ENTITY_TYPE_INVALID")
    if not is_graph_entity_candidate(canonical_name, entity_type):
        raise ValueError("ENTITY_NAME_INVALID")
    if connection.execute(
        """SELECT 1 FROM memory.entities
           WHERE namespace_id=%s AND normalized_name=%s""",
        (namespace_id, normalized_name),
    ).fetchone():
        raise ValueError("ENTITY_NAME_CONFLICT")
    requested_fact_ids = list(dict.fromkeys(request.fact_ids))
    rows = connection.execute(
        """SELECT DISTINCT fe.fact_id
           FROM memory.fact_entities fe
           JOIN memory.entities e ON e.id=fe.entity_id
           JOIN memory.facts f ON f.id=fe.fact_id
           WHERE f.namespace_id=%s AND fe.fact_id=ANY(%s)
             AND (e.id=%s OR e.canonical_entity_id=%s)""",
        (namespace_id, requested_fact_ids, source_entity_id, source_entity_id),
    ).fetchall()
    selected_fact_ids = [row[0] for row in rows]
    if len(selected_fact_ids) != len(requested_fact_ids):
        raise ValueError("ENTITY_FACT_SELECTION_INVALID")
    created_entity_id = new_uuid()
    connection.execute(
        """INSERT INTO memory.entities(
             id,namespace_id,entity_type,canonical_name,normalized_name
           ) VALUES (%s,%s,%s,%s,%s)""",
        (created_entity_id, namespace_id, entity_type, canonical_name, normalized_name),
    )
    connection.execute(
        """INSERT INTO memory.fact_entities(fact_id,entity_id)
           SELECT unnest(%s::uuid[]),%s ON CONFLICT DO NOTHING""",
        (selected_fact_ids, created_entity_id),
    )
    connection.execute(
        """DELETE FROM memory.fact_entities fe USING memory.entities e
           WHERE fe.entity_id=e.id AND fe.fact_id=ANY(%s)
             AND e.namespace_id=%s AND (e.id=%s OR e.canonical_entity_id=%s)""",
        (selected_fact_ids, namespace_id, source_entity_id, source_entity_id),
    )
    connection.execute(
        """UPDATE memory.entity_mentions m SET entity_id=%s
           FROM memory.entities e
           WHERE m.entity_id=e.id AND m.fact_id=ANY(%s)
             AND e.namespace_id=%s AND (e.id=%s OR e.canonical_entity_id=%s)""",
        (
            created_entity_id,
            selected_fact_ids,
            namespace_id,
            source_entity_id,
            source_entity_id,
        ),
    )
    _entity_audit(
        connection,
        namespace_id=namespace_id,
        actor_id=request.context.source_profile,
        action="entity.split",
        entity_id=source_entity_id,
        reason=request.reason,
        correlation_id=request.context.correlation_id,
        metadata={
            "created_entity_id": str(created_entity_id),
            "fact_ids": [str(value) for value in selected_fact_ids],
        },
    )
    enqueue_derived_rebuild(
        connection,
        namespace_id,
        source_entity_id,
        f"entity-split:{source_entity_id}:{request.context.correlation_id}",
    )
    return {
        "entity_id": source_entity_id,
        "state": "active",
        "canonical_entity_id": None,
        "created_entity_id": created_entity_id,
        "affected_fact_count": len(selected_fact_ids),
        "correlation_id": request.context.correlation_id,
    }


def change_entity_fact_relation(
    connection: Connection,
    *,
    namespace_key: str,
    entity_id: UUID,
    fact_id: UUID,
    action: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    entity = connection.execute(
        """SELECT 1 FROM memory.entities
           WHERE id=%s AND namespace_id=%s AND canonical_entity_id IS NULL
             AND merge_state='active'""",
        (entity_id, namespace_id),
    ).fetchone()
    fact = connection.execute(
        """SELECT 1 FROM memory.facts
           WHERE id=%s AND namespace_id=%s AND memory_state <> 'purge_requested'""",
        (fact_id, namespace_id),
    ).fetchone()
    if entity is None or fact is None:
        return None
    if action == "attach":
        changed = connection.execute(
            """INSERT INTO memory.fact_entities(fact_id,entity_id) VALUES (%s,%s)
               ON CONFLICT DO NOTHING RETURNING fact_id""",
            (fact_id, entity_id),
        ).fetchone()
        state = "attached"
        error = "ENTITY_RELATION_ALREADY_ATTACHED"
    elif action == "detach":
        changed = connection.execute(
            """DELETE FROM memory.fact_entities WHERE fact_id=%s AND entity_id=%s
               RETURNING fact_id""",
            (fact_id, entity_id),
        ).fetchone()
        state = "detached"
        error = "ENTITY_RELATION_NOT_ATTACHED"
        if changed is not None:
            connection.execute(
                "DELETE FROM memory.entity_mentions WHERE fact_id=%s AND entity_id=%s",
                (fact_id, entity_id),
            )
    else:
        raise ValueError("ENTITY_RELATION_ACTION_INVALID")
    if changed is None:
        raise ValueError(error)
    _entity_audit(
        connection,
        namespace_id=namespace_id,
        actor_id=actor_id,
        action=f"entity.relation.{action}",
        entity_id=entity_id,
        reason=reason,
        correlation_id=correlation_id,
        metadata={"fact_id": str(fact_id)},
    )
    enqueue_derived_rebuild(
        connection,
        namespace_id,
        entity_id,
        f"entity-relation-{action}:{entity_id}:{fact_id}:{correlation_id}",
    )
    return {
        "entity_id": entity_id,
        "fact_id": fact_id,
        "state": state,
        "correlation_id": correlation_id,
    }


def correct_memory(
    connection: Connection, memory_id: UUID, request: CorrectionRequest
) -> UUID | None:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    row = connection.execute(
        """SELECT version FROM memory.facts
           WHERE id=%s AND namespace_id=%s AND memory_state <> 'purge_requested'
           FOR UPDATE""",
        (memory_id, namespace_id),
    ).fetchone()
    if row is None:
        return None
    replacement_id = new_uuid()
    corrected_statement = redact_text(request.corrected_statement).text
    connection.execute(
        """UPDATE memory.facts SET memory_state='superseded',updated_at=now()
           WHERE id=%s""",
        (memory_id,),
    )
    connection.execute(
        """UPDATE retrieval.documents SET lifecycle_state='superseded',indexed_at=now()
           WHERE source_kind='fact' AND source_id=%s""",
        (memory_id,),
    )
    connection.execute(
        """INSERT INTO memory.facts(
             id,namespace_id,statement,fact_type,confidence,memory_state,source_profile,
             supersedes_fact_id,version,extraction_method,extraction_version
           ) VALUES (%s,%s,%s,'corrected',1,'active',%s,%s,%s,'user-correction','manual-v1')""",
        (
            replacement_id,
            namespace_id,
            corrected_statement,
            request.context.source_profile,
            memory_id,
            row[0] + 1,
        ),
    )
    connection.execute(
        """INSERT INTO retrieval.documents(
             id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state,
             embedding,embedding_model_version
           ) VALUES (%s,%s,'fact',%s,%s,'active',%s::vector,%s)""",
        (
            stable_uuid("document", str(replacement_id)),
            namespace_id,
            replacement_id,
            corrected_statement,
            vector_literal(deterministic_embedding(corrected_statement)),
            EMBEDDING_VERSION,
        ),
    )
    connection.execute(
        """INSERT INTO memory.fact_entities(fact_id,entity_id)
           SELECT %s,entity_id FROM memory.fact_entities WHERE fact_id=%s
           ON CONFLICT DO NOTHING""",
        (replacement_id, memory_id),
    )
    connection.execute(
        """INSERT INTO memory.fact_evidence(fact_id,event_id,support_kind,weight)
           SELECT %s,event_id,'historical_context',weight
           FROM memory.fact_evidence WHERE fact_id=%s
           ON CONFLICT DO NOTHING""",
        (replacement_id, memory_id),
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,
             correlation_id,metadata_redacted
           ) VALUES (%s,%s,'user',%s,'memory.correct','fact',%s,%s,%s,%s::jsonb)""",
        (
            new_uuid(),
            namespace_id,
            request.context.source_profile,
            replacement_id,
            request.reason,
            request.context.correlation_id,
            json.dumps(
                {
                    "supersedes": str(memory_id),
                    "external_session_id": request.context.external_session_id,
                    "external_turn_id": request.context.external_turn_id,
                    "source_instance": request.context.source_instance,
                }
            ),
        ),
    )
    enqueue_derived_rebuild(
        connection, namespace_id, replacement_id, f"correction:{replacement_id}"
    )
    return replacement_id


def set_memory_state(
    connection: Connection,
    *,
    namespace_key: str,
    memory_id: UUID,
    state: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> bool:
    namespace_id = stable_uuid("namespace", namespace_key)
    result = connection.execute(
        """UPDATE memory.facts SET memory_state=%s,updated_at=now()
           WHERE id=%s AND namespace_id=%s AND memory_state <> 'purge_requested'
           RETURNING id""",
        (state, memory_id, namespace_id),
    ).fetchone()
    if result is None:
        return False
    connection.execute(
        """UPDATE retrieval.documents SET lifecycle_state=%s,indexed_at=now()
           WHERE source_kind='fact' AND source_id=%s""",
        (state, memory_id),
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,correlation_id
           ) VALUES (%s,%s,'user',%s,%s,'fact',%s,%s,%s)""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            f"memory.{state}",
            memory_id,
            reason,
            correlation_id,
        ),
    )
    enqueue_derived_rebuild(
        connection, namespace_id, memory_id, f"state:{memory_id}:{correlation_id}"
    )
    return True


def request_memory_purge(
    connection: Connection,
    *,
    namespace_key: str,
    memory_id: UUID,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> UUID | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    result = connection.execute(
        """UPDATE memory.facts SET memory_state='purge_requested',updated_at=now()
           WHERE id=%s AND namespace_id=%s AND memory_state <> 'purge_requested'
           RETURNING id""",
        (memory_id, namespace_id),
    ).fetchone()
    if result is None:
        return None
    connection.execute(
        """UPDATE retrieval.documents SET lifecycle_state='purge_requested',indexed_at=now()
           WHERE source_kind='fact' AND source_id=%s""",
        (memory_id,),
    )
    job_id = stable_uuid("job", f"purge_memory:{memory_id}")
    connection.execute(
        """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
           VALUES (%s,%s,'purge_memory',%s,%s) ON CONFLICT DO NOTHING""",
        (job_id, namespace_id, f"purge_memory:{memory_id}", memory_id),
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,correlation_id
           ) VALUES (%s,%s,'user',%s,'memory.purge.request','fact',%s,%s,%s)""",
        (new_uuid(), namespace_id, actor_id, memory_id, reason, correlation_id),
    )
    enqueue_derived_rebuild(connection, namespace_id, memory_id, f"purge:{memory_id}")
    return job_id
