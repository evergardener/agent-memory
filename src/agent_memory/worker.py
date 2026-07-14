import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta

from psycopg import Connection
from psycopg.rows import dict_row

from .classification import classify_event
from .config import get_settings
from .db import Database
from .embeddings import EMBEDDING_VERSION, deterministic_embedding, vector_literal
from .ids import new_uuid, stable_uuid
from .interaction_state import advance_state
from .state_views import effective_state_config

logger = logging.getLogger(__name__)
ENTITY_PATTERN = re.compile(r"(?:project|service|项目|服务)[:： ]+([\w\-]+)", re.IGNORECASE)


def claim_job(connection: Connection, lease_seconds: int):
    return connection.execute(
        """UPDATE ops.jobs SET status='running',
             lease_until=now() + make_interval(secs => %s),
             attempt_count=attempt_count+1,updated_at=now()
           WHERE id=(
             SELECT id FROM ops.jobs
             WHERE (status IN ('pending','retry') AND run_after <= now())
                OR (status='running' AND lease_until < now())
             ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
           ) RETURNING id,namespace_id,kind,input_ref,input_version""",
        (lease_seconds,),
    ).fetchone()


def process_extract(connection: Connection, job) -> None:
    job_id, namespace_id, _kind, event_id, _version = job
    row = connection.execute(
        """SELECT e.redacted_payload->>'content',s.source_profile,e.event_type,e.occurred_at
           FROM evidence.events e JOIN core.turns t ON t.id=e.turn_id
           JOIN core.sessions se ON se.id=t.session_id JOIN core.sources s ON s.id=se.source_id
           WHERE e.id=%s""",
        (event_id,),
    ).fetchone()
    if row is None:
        raise ValueError("INPUT_NOT_FOUND")
    content, source_profile, event_type, occurred_at = row
    if not content or not content.strip():
        return
    settings = get_settings()
    classification = classify_event(
        event_type,
        content,
        occurred_at,
        current_days=settings.current_state_days,
        weather_hours=settings.weather_state_hours,
    )
    _update_continuity_and_state(
        connection,
        namespace_id,
        event_id,
        source_profile,
        event_type,
        content,
        occurred_at,
    )
    _ensure_weekly_report_job(connection, namespace_id, event_id, occurred_at)
    if not classification.create_fact:
        return
    fact_id = stable_uuid("fact", f"{event_id}:{content}")
    connection.execute(
        """INSERT INTO memory.facts(
             id,namespace_id,statement,fact_type,confidence,memory_state,source_profile,
             valid_from,valid_to
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (
            fact_id,
            namespace_id,
            content,
            classification.fact_type,
            classification.confidence,
            classification.memory_state,
            source_profile,
            occurred_at,
            classification.valid_to,
        ),
    )
    connection.execute(
        """INSERT INTO memory.fact_evidence(fact_id,event_id)
           VALUES (%s,%s) ON CONFLICT DO NOTHING""",
        (fact_id, event_id),
    )
    for entity_name in ENTITY_PATTERN.findall(content):
        normalized = entity_name.casefold()
        entity_id = stable_uuid("entity", f"{namespace_id}:{normalized}")
        connection.execute(
            """INSERT INTO memory.entities(id,namespace_id,canonical_name,normalized_name)
               VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (entity_id, namespace_id, entity_name, normalized),
        )
        connection.execute(
            """INSERT INTO memory.fact_entities(fact_id,entity_id)
               VALUES (%s,%s) ON CONFLICT DO NOTHING""",
            (fact_id, entity_id),
        )
    connection.execute(
        """INSERT INTO retrieval.documents(
             id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state,
             embedding,embedding_model_version
           ) VALUES (%s,%s,'fact',%s,%s,%s,%s::vector,%s)
           ON CONFLICT (source_kind,source_id) DO NOTHING""",
        (
            stable_uuid("document", str(fact_id)),
            namespace_id,
            fact_id,
            content,
            classification.memory_state,
            vector_literal(deterministic_embedding(content)),
            EMBEDDING_VERSION,
        ),
    )
    if classification.fact_type == "current" and classification.valid_to is not None:
        connection.execute(
            """INSERT INTO state.current_items(
                 id,namespace_id,topic_key,summary,source_fact_id,valid_from,expires_at
               ) VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(namespace_id,topic_key) DO UPDATE SET
                 summary=excluded.summary,source_fact_id=excluded.source_fact_id,
                 status='active',valid_from=excluded.valid_from,expires_at=excluded.expires_at,
                 updated_at=now()""",
            (
                stable_uuid("current-item", f"{namespace_id}:{content.casefold()}"),
                namespace_id,
                content.casefold()[:256],
                content,
                fact_id,
                occurred_at,
                classification.valid_to,
            ),
        )
    if classification.fact_type in {"stage", "long_term"}:
        rebuild_id = stable_uuid("job", f"rebuild_derived:extract:{event_id}")
        connection.execute(
            """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
               VALUES (%s,%s,'rebuild_derived',%s,%s) ON CONFLICT DO NOTHING""",
            (rebuild_id, namespace_id, f"rebuild_derived:extract:{event_id}", fact_id),
        )


def _update_continuity_and_state(
    connection: Connection,
    namespace_id,
    event_id,
    source_profile: str,
    event_type: str,
    content: str,
    occurred_at: datetime,
) -> None:
    if event_type in {"user_message", "tool_result", "environment_observation"}:
        connection.execute(
            """INSERT INTO state.continuities(
                 id,namespace_id,topic_key,summary,source_event_id,last_active_at,expires_at
               ) VALUES (%s,%s,'recent',%s,%s,%s,%s)
               ON CONFLICT(namespace_id,topic_key) DO UPDATE SET
                 summary=excluded.summary,source_event_id=excluded.source_event_id,
                 last_active_at=excluded.last_active_at,expires_at=excluded.expires_at,
                 updated_at=now()""",
            (
                stable_uuid("continuity", f"{namespace_id}:recent"),
                namespace_id,
                content[:1000],
                event_id,
                occurred_at,
                occurred_at + timedelta(days=get_settings().continuity_days),
            ),
        )
    config = effective_state_config(connection, namespace_id, source_profile)
    if not config["enabled"]:
        return
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT axes,calculated_at FROM state.interaction_snapshots
               WHERE namespace_id=%s AND calculated_at <= %s
               ORDER BY calculated_at DESC LIMIT 1""",
            (namespace_id, occurred_at),
        )
        previous = cursor.fetchone()
    result = advance_state(
        previous["axes"] if previous else None,
        previous["calculated_at"] if previous else None,
        event_type,
        content,
        occurred_at,
        axes_initial=config["axes_initial"],
        axis_ranges=config["axis_ranges"],
        axis_enabled=config["axis_enabled"],
        drift_hours=config["drift_hours"],
        thresholds=config["thresholds"],
    )
    connection.execute(
        """INSERT INTO state.interaction_snapshots(
             id,namespace_id,source_event_id,axes,summary,suggestions,calculated_at,algorithm_version
           ) VALUES (%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,'jiwen-neutral-v1')
           ON CONFLICT DO NOTHING""",
        (
            stable_uuid("interaction-state", f"{event_id}:jiwen-neutral-v1"),
            namespace_id,
            event_id,
            json.dumps(result.axes),
            result.summary,
            json.dumps(result.suggestions, ensure_ascii=False),
            occurred_at,
        ),
    )


def _ensure_weekly_report_job(
    connection: Connection, namespace_id, event_id, occurred_at: datetime
) -> None:
    week_start = (occurred_at - timedelta(days=occurred_at.weekday())).date().isoformat()
    job_id = stable_uuid("job", f"generate_report:{namespace_id}:{week_start}:{event_id}")
    idempotency_key = f"generate_report:{namespace_id}:{week_start}:{event_id}"
    connection.execute(
        """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
           VALUES (%s,%s,'generate_report',%s,%s) ON CONFLICT DO NOTHING""",
        (job_id, namespace_id, idempotency_key, namespace_id),
    )


def process_report(connection: Connection, job) -> None:
    _job_id, namespace_id, _kind, _input_ref, _version = job
    now = datetime.now(UTC)
    period_start = datetime.combine(
        (now - timedelta(days=now.weekday())).date(), datetime.min.time(), UTC
    )
    period_end = period_start + timedelta(days=7)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT
                 count(*) FILTER (WHERE e.created_at >= %s) AS evidence_added,
                 count(*) FILTER (
                   WHERE e.event_type='tool_result' AND e.created_at >= %s
                 ) AS tool_results
               FROM evidence.events e WHERE e.namespace_id=%s""",
            (period_start, period_start, namespace_id),
        )
        events = cursor.fetchone()
        cursor.execute(
            """SELECT fact_type,memory_state,count(*) AS count FROM memory.facts
               WHERE namespace_id=%s AND created_at >= %s
               GROUP BY fact_type,memory_state ORDER BY fact_type,memory_state""",
            (namespace_id, period_start),
        )
        facts = cursor.fetchall()
        cursor.execute(
            """SELECT count(*) AS redactions FROM evidence.redaction_findings rf
               JOIN evidence.events e ON e.id=rf.event_id
               WHERE e.namespace_id=%s AND rf.created_at >= %s""",
            (namespace_id, period_start),
        )
        redactions = cursor.fetchone()["redactions"]
    summary = {
        "evidence_added": events["evidence_added"],
        "tool_results": events["tool_results"],
        "facts": [dict(item) for item in facts],
        "redactions": redactions,
        "conflicts": [],
        "pending_confirmation": sum(
            item["count"] for item in facts if item["memory_state"] == "candidate"
        ),
    }
    connection.execute(
        """INSERT INTO reports.consolidation(
             id,namespace_id,period_start,period_end,summary
           ) VALUES (%s,%s,%s,%s,%s::jsonb)
           ON CONFLICT(namespace_id,period_start,period_end) DO UPDATE SET
             summary=excluded.summary,created_at=now()""",
        (
            stable_uuid("report", f"{namespace_id}:{period_start.isoformat()}"),
            namespace_id,
            period_start,
            period_end,
            json.dumps(summary),
        ),
    )


def process_purge(connection: Connection, job) -> None:
    _job_id, namespace_id, _kind, memory_id, _version = job
    row = connection.execute(
        """SELECT id FROM memory.facts
           WHERE id=%s AND namespace_id=%s AND memory_state='purge_requested' FOR UPDATE""",
        (memory_id, namespace_id),
    ).fetchone()
    if row is None:
        return
    evidence_ids = [
        item[0]
        for item in connection.execute(
            "SELECT event_id FROM memory.fact_evidence WHERE fact_id=%s", (memory_id,)
        ).fetchall()
    ]
    connection.execute(
        "DELETE FROM vault.references WHERE target_type='fact' AND target_id=%s", (memory_id,)
    )
    connection.execute("DELETE FROM state.current_items WHERE source_fact_id=%s", (memory_id,))
    connection.execute(
        "DELETE FROM retrieval.documents WHERE source_kind='fact' AND source_id=%s", (memory_id,)
    )
    connection.execute("DELETE FROM memory.fact_entities WHERE fact_id=%s", (memory_id,))
    connection.execute("DELETE FROM memory.fact_evidence WHERE fact_id=%s", (memory_id,))
    connection.execute(
        "UPDATE memory.facts SET supersedes_fact_id=NULL WHERE supersedes_fact_id=%s", (memory_id,)
    )
    connection.execute("DELETE FROM memory.facts WHERE id=%s", (memory_id,))
    removed_evidence = 0
    for event_id in evidence_ids:
        still_used = connection.execute(
            "SELECT 1 FROM memory.fact_evidence WHERE event_id=%s LIMIT 1", (event_id,)
        ).fetchone()
        if still_used is not None:
            continue
        connection.execute("DELETE FROM state.continuities WHERE source_event_id=%s", (event_id,))
        connection.execute(
            "DELETE FROM state.interaction_snapshots WHERE source_event_id=%s", (event_id,)
        )
        connection.execute("DELETE FROM evidence.redaction_findings WHERE event_id=%s", (event_id,))
        connection.execute("DELETE FROM evidence.events WHERE id=%s", (event_id,))
        removed_evidence += 1
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,correlation_id,
             metadata_redacted
           ) VALUES (
             %s,%s,'worker','purge-worker','memory.purge.complete','fact',%s,%s,%s::jsonb
           )""",
        (
            new_uuid(),
            namespace_id,
            memory_id,
            new_uuid(),
            json.dumps({"orphan_evidence_removed": removed_evidence}),
        ),
    )


def process_rebuild_derived(connection: Connection, job) -> None:
    _job_id, namespace_id, _kind, _input_ref, _version = job
    connection.execute(
        """DELETE FROM retrieval.documents
           WHERE namespace_id=%s AND source_kind IN ('episode','arc')""",
        (namespace_id,),
    )
    connection.execute("DELETE FROM memory.episodes WHERE namespace_id=%s", (namespace_id,))
    connection.execute("DELETE FROM memory.arcs WHERE namespace_id=%s", (namespace_id,))
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT e.id AS entity_id,e.canonical_name,
                      array_agg(f.id ORDER BY f.created_at) AS ids,
                      array_agg(f.statement ORDER BY f.created_at) AS statements
               FROM memory.entities e JOIN memory.fact_entities fe ON fe.entity_id=e.id
               JOIN memory.facts f ON f.id=fe.fact_id
               WHERE e.namespace_id=%s AND f.fact_type='stage' AND f.memory_state='active'
               GROUP BY e.id,e.canonical_name HAVING count(*) >= 2""",
            (namespace_id,),
        )
        episodes = cursor.fetchall()
        cursor.execute(
            """SELECT e.id AS entity_id,e.canonical_name,
                      array_agg(f.id ORDER BY f.created_at) AS ids,
                      array_agg(f.statement ORDER BY f.created_at) AS statements
               FROM memory.entities e JOIN memory.fact_entities fe ON fe.entity_id=e.id
               JOIN memory.facts f ON f.id=fe.fact_id
               WHERE e.namespace_id=%s AND f.fact_type='long_term' AND f.memory_state='active'
               GROUP BY e.id,e.canonical_name HAVING count(*) >= 2""",
            (namespace_id,),
        )
        arcs = cursor.fetchall()
    for kind, groups in (("episode", episodes), ("arc", arcs)):
        for group in groups:
            derived_id = stable_uuid(kind, f"{namespace_id}:{group['entity_id']}")
            summary = " · ".join(group["statements"][:8])
            table = "episodes" if kind == "episode" else "arcs"
            link_table = "episode_facts" if kind == "episode" else "arc_facts"
            owner_column = "episode_id" if kind == "episode" else "arc_id"
            title_prefix = "阶段情节" if kind == "episode" else "长期脉络"
            connection.execute(
                f"""INSERT INTO memory.{table}(
                       id,namespace_id,entity_id,title,summary
                     ) VALUES (%s,%s,%s,%s,%s)""",
                (
                    derived_id,
                    namespace_id,
                    group["entity_id"],
                    f"{title_prefix} · {group['canonical_name']}",
                    summary,
                ),
            )
            for fact_id in group["ids"]:
                connection.execute(
                    f"INSERT INTO memory.{link_table}({owner_column},fact_id) VALUES (%s,%s)",
                    (derived_id, fact_id),
                )
            connection.execute(
                """INSERT INTO retrieval.documents(
                     id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state,
                     embedding,embedding_model_version
                   ) VALUES (%s,%s,%s,%s,%s,'active',%s::vector,%s)""",
                (
                    stable_uuid("document", str(derived_id)),
                    namespace_id,
                    kind,
                    derived_id,
                    summary,
                    vector_literal(deterministic_embedding(summary)),
                    EMBEDDING_VERSION,
                ),
            )


def run_maintenance(connection: Connection) -> None:
    settings = get_settings()
    rows_to_embed = connection.execute(
        """SELECT id,text_redacted FROM retrieval.documents
           WHERE embedding IS NULL ORDER BY indexed_at LIMIT 100"""
    ).fetchall()
    for document_id, text_redacted in rows_to_embed:
        connection.execute(
            """UPDATE retrieval.documents SET embedding=%s::vector,
                 embedding_model_version=%s,indexed_at=now() WHERE id=%s""",
            (
                vector_literal(deterministic_embedding(text_redacted)),
                EMBEDDING_VERSION,
                document_id,
            ),
        )
    connection.execute(
        """UPDATE state.current_items SET status='expired',updated_at=now()
           WHERE status='active' AND expires_at <= now()"""
    )
    connection.execute(
        """UPDATE memory.facts SET memory_state='dormant',updated_at=now()
           WHERE fact_type='stage' AND memory_state='active'
             AND updated_at < now() - make_interval(days => %s)""",
        (settings.stage_dormant_days,),
    )
    connection.execute(
        """UPDATE retrieval.documents d SET lifecycle_state='dormant',indexed_at=now()
           FROM memory.facts f WHERE d.source_kind='fact' AND d.source_id=f.id
             AND f.memory_state='dormant' AND d.lifecycle_state <> 'dormant'"""
    )
    connection.execute(
        """UPDATE memory.facts SET memory_state='forgotten',updated_at=now()
           WHERE fact_type='stage' AND memory_state='dormant'
             AND created_at < now() - make_interval(days => %s)""",
        (settings.stage_forget_days,),
    )
    connection.execute(
        """UPDATE retrieval.documents d SET lifecycle_state='forgotten',indexed_at=now()
           FROM memory.facts f WHERE d.source_kind='fact' AND d.source_id=f.id
             AND f.memory_state='forgotten' AND d.lifecycle_state <> 'forgotten'"""
    )
    connection.execute(
        """UPDATE memory.facts SET memory_state='dormant',updated_at=now()
           WHERE memory_state='candidate'
             AND created_at < now() - make_interval(days => %s)""",
        (settings.candidate_retention_days,),
    )
    rows = connection.execute(
        """SELECT n.id FROM core.namespaces n
           WHERE NOT EXISTS (
             SELECT 1 FROM reports.consolidation r WHERE r.namespace_id=n.id
               AND r.created_at > now() - make_interval(days => %s)
           )""",
        (settings.report_interval_days,),
    ).fetchall()
    today = datetime.now(UTC).date().isoformat()
    for (namespace_id,) in rows:
        job_id = stable_uuid("job", f"generate_report:{namespace_id}:{today}")
        connection.execute(
            """INSERT INTO ops.jobs(id,namespace_id,kind,idempotency_key,input_ref)
               VALUES (%s,%s,'generate_report',%s,%s) ON CONFLICT DO NOTHING""",
            (job_id, namespace_id, f"generate_report:{namespace_id}:{today}", namespace_id),
        )


def process_one(connection: Connection, job) -> None:
    job_id = job[0]
    attempt_id = new_uuid()
    correlation_id = new_uuid()
    connection.execute(
        "INSERT INTO ops.job_attempts(id,job_id,started_at,correlation_id) VALUES (%s,%s,%s,%s)",
        (attempt_id, job_id, datetime.now(UTC), correlation_id),
    )
    try:
        with connection.transaction():
            if job[2] == "extract_facts":
                process_extract(connection, job)
            elif job[2] == "generate_report":
                process_report(connection, job)
            elif job[2] == "purge_memory":
                process_purge(connection, job)
            elif job[2] == "rebuild_derived":
                process_rebuild_derived(connection, job)
            else:
                raise ValueError("UNKNOWN_JOB_KIND")
        connection.execute(
            "UPDATE ops.jobs SET status='done',lease_until=NULL,updated_at=now() WHERE id=%s",
            (job_id,),
        )
        connection.execute(
            "UPDATE ops.job_attempts SET ended_at=now(),result='done' WHERE id=%s", (attempt_id,)
        )
    except Exception as error:
        logger.exception("job failed", extra={"job_id": str(job_id)})
        connection.execute(
            """UPDATE ops.jobs
               SET status=CASE WHEN attempt_count < 5 THEN 'retry' ELSE 'failed' END,
               run_after=now() + make_interval(secs => LEAST(300, attempt_count * attempt_count)),
               lease_until=NULL,last_error_code=%s,updated_at=now() WHERE id=%s""",
            (type(error).__name__, job_id),
        )
        connection.execute(
            "UPDATE ops.job_attempts SET ended_at=now(),result='error',error_code=%s WHERE id=%s",
            (type(error).__name__, attempt_id),
        )


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    database = Database(settings)
    database.open()
    logger.info("worker started")
    last_maintenance = 0.0
    try:
        while True:
            with database.connection() as connection:
                if time.monotonic() - last_maintenance >= 60:
                    run_maintenance(connection)
                    last_maintenance = time.monotonic()
                job = claim_job(connection, settings.worker_lease_seconds)
                if job:
                    process_one(connection, job)
            time.sleep(settings.worker_poll_seconds if not job else 0.05)
    finally:
        database.close()
