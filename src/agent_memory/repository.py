import hashlib
import json
from collections import defaultdict
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .embeddings import EMBEDDING_VERSION, deterministic_embedding, vector_literal
from .ids import new_uuid, stable_uuid
from .redaction import redact_text
from .schemas import (
    CorrectionRequest,
    EvidenceTraceItem,
    IngestTurnRequest,
    MemoryTraceResponse,
    RecallItem,
    RecallRequest,
)


def _json_payload(event, redacted_content: str) -> dict:
    payload = {"content": redacted_content}
    if event.tool_name:
        payload["tool_name"] = event.tool_name
    if event.arguments is not None:
        arguments = redact_text(json.dumps(event.arguments, ensure_ascii=False)).text
        payload["arguments_redacted"] = arguments
    return payload


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
        redaction = redact_text(event.content)
        payload = _json_payload(event, redaction.text)
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
        for index, finding in enumerate(redaction.findings):
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
    return job_id


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
                      array_remove(array_agg(fe.event_id),NULL) AS source_ids
               FROM retrieval.documents d
               JOIN memory.facts f ON f.id=d.source_id AND d.source_kind='fact'
               LEFT JOIN memory.fact_evidence fe ON fe.fact_id=f.id
               WHERE d.namespace_id=%s AND d.lifecycle_state IN {lexical_states}
                 AND (f.valid_to IS NULL OR f.valid_to > now())
                 AND d.search_vector @@ plainto_tsquery('simple',%s)
               GROUP BY d.source_id,d.text_redacted,f.source_profile,d.search_vector,
                        d.lifecycle_state
               ORDER BY ts_rank(d.search_vector,plainto_tsquery('simple',%s)) DESC LIMIT 25""",
            (namespace_id, request.query, request.query),
        )
        lexical = cursor.fetchall()
        cursor.execute(
            f"""SELECT DISTINCT d.source_id AS memory_id,'fact' AS kind,d.text_redacted,
                      f.source_profile,
                      CASE WHEN fev.event_id IS NULL THEN '{{}}'::uuid[]
                           ELSE array[fev.event_id] END AS source_ids
               FROM retrieval.documents d
               JOIN memory.facts f ON f.id=d.source_id AND d.source_kind='fact'
               LEFT JOIN memory.fact_evidence fev ON fev.fact_id=f.id
               JOIN memory.fact_entities mfe ON mfe.fact_id=f.id
               JOIN memory.entities e ON e.id=mfe.entity_id
               WHERE d.namespace_id=%s AND d.lifecycle_state IN {lexical_states}
                 AND (f.valid_to IS NULL OR f.valid_to > now())
                 AND position(e.normalized_name in lower(%s)) > 0 LIMIT 25""",
            (namespace_id, request.query),
        )
        entity = cursor.fetchall()
        cursor.execute(
            """SELECT d.source_id AS memory_id,'fact' AS kind,d.text_redacted,f.source_profile,
                      array_remove(array_agg(fe.event_id),NULL) AS source_ids
               FROM retrieval.documents d
               JOIN memory.facts f ON f.id=d.source_id AND d.source_kind='fact'
               LEFT JOIN memory.fact_evidence fe ON fe.fact_id=f.id
               WHERE d.namespace_id=%s AND d.lifecycle_state IN ('candidate','active')
                 AND (f.valid_to IS NULL OR f.valid_to > now())
                 AND d.embedding_model_version=%s AND d.embedding IS NOT NULL
                 AND (d.embedding <=> %s::vector) < 0.5
               GROUP BY d.source_id,d.text_redacted,f.source_profile,d.embedding
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
                         'derived'::text AS source_profile,
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
                         'derived'::text AS source_profile,
                         COALESCE(el.source_ids,'{}'::uuid[]) AS source_ids
                    FROM retrieval.documents d JOIN derived ON derived.id=d.source_id
                    JOIN memory.entities e ON e.id=derived.entity_id
                    LEFT JOIN evidence_links el ON el.derived_id=derived.id
                   WHERE d.namespace_id=%s AND d.lifecycle_state='active'
                     AND position(e.normalized_name in lower(%s)) > 0 LIMIT 25""",
            (namespace_id, namespace_id, namespace_id, request.query),
        )
        derived_entity = cursor.fetchall()
        cursor.execute(
            derived_cte
            + """SELECT d.source_id AS memory_id,derived.kind,d.text_redacted,
                         'derived'::text AS source_profile,
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
        for rank, row in enumerate(rows, start=1):
            memory_id = row["memory_id"]
            scores[memory_id] += 1 / (60 + rank)
            records[memory_id] = row
            channels[memory_id].append(channel)

    items: list[RecallItem] = []
    used_chars = 0
    truncated = False
    for memory_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
        row = records[memory_id]
        text = row["text_redacted"]
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
            """SELECT id,statement,memory_state,version,supersedes_fact_id
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
            cursor.execute(
                """WITH linked_facts AS (
                     SELECT fact_id FROM memory.episode_facts WHERE episode_id=%s
                     UNION SELECT fact_id FROM memory.arc_facts WHERE arc_id=%s
                   )
                   SELECT DISTINCT e.id AS evidence_id,e.event_type,e.occurred_at,
                          s.source_profile,e.redacted_payload
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
                          e.redacted_payload
                   FROM memory.fact_evidence fe JOIN evidence.events e ON e.id=fe.event_id
               JOIN core.turns t ON t.id=e.turn_id
               JOIN core.sessions se ON se.id=t.session_id
               JOIN core.sources s ON s.id=se.source_id
               WHERE fe.fact_id=%s ORDER BY e.occurred_at""",
                (memory_id,),
            )
        evidence = [EvidenceTraceItem.model_validate(row) for row in cursor.fetchall()]
    return MemoryTraceResponse(
        memory_id=fact["id"],
        statement=fact["statement"],
        state=fact["memory_state"],
        version=fact["version"],
        supersedes_memory_id=fact["supersedes_fact_id"],
        evidence=evidence,
    )


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
             supersedes_fact_id,version
           ) VALUES (%s,%s,%s,'corrected',1,'active',%s,%s,%s)""",
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
            json.dumps({"supersedes": str(memory_id)}),
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
