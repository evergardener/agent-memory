import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from agent_memory.current_state import transition_current_item, upsert_current_item
from agent_memory.governance_debt import (
    APPLY_CONFIRMATION as GOVERNANCE_CONFIRMATION,
)
from agent_memory.governance_debt import (
    apply_governance_manifest,
    build_governance_manifest,
)
from agent_memory.ids import new_uuid, stable_uuid
from agent_memory.model_replay import (
    APPLY_CONFIRMATION as REPLAY_CONFIRMATION,
)
from agent_memory.model_replay import (
    replay_failed_model_jobs,
)
from agent_memory.repository import browse_memories, trace_memory
from agent_memory.unified_memory import PreferenceCandidate, _store_preference
from agent_memory.worker import process_purge

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AGENT_MEMORY_INTEGRATION") != "1",
        reason="set AGENT_MEMORY_INTEGRATION=1 against an isolated database",
    ),
]

DATABASE_URL = os.getenv("AGENT_MEMORY_DATABASE_URL", "")
RUN_ID = uuid4().hex
NAMESPACE = f"hermes:automated-tests:governance:{RUN_ID}"
NAMESPACE_ID = stable_uuid("namespace", NAMESPACE)


def create_namespace(connection) -> None:
    connection.execute(
        "INSERT INTO core.namespaces(id,stable_key) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (NAMESPACE_ID, NAMESPACE),
    )


def create_event(connection, suffix: str) -> UUID:
    source_id = stable_uuid("source", f"{NAMESPACE_ID}:test:{RUN_ID}")
    session_id = stable_uuid("session", f"{source_id}:{RUN_ID}")
    turn_id = stable_uuid("turn", f"{session_id}:{suffix}")
    event_id = stable_uuid("event", f"{turn_id}:{suffix}")
    connection.execute(
        """INSERT INTO core.sources(id,namespace_id,source_profile,source_instance)
           VALUES (%s,%s,'test','governance-integration') ON CONFLICT DO NOTHING""",
        (source_id, NAMESPACE_ID),
    )
    connection.execute(
        """INSERT INTO core.sessions(id,namespace_id,source_id,external_session_id,started_at)
           VALUES (%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING""",
        (session_id, NAMESPACE_ID, source_id, RUN_ID),
    )
    connection.execute(
        """INSERT INTO core.turns(id,session_id,external_turn_id,occurred_at)
           VALUES (%s,%s,%s,now()) ON CONFLICT DO NOTHING""",
        (turn_id, session_id, suffix),
    )
    connection.execute(
        """INSERT INTO evidence.events(
             id,namespace_id,turn_id,event_type,sequence_no,redacted_payload,payload_hash,
             ingest_key,occurred_at
           ) VALUES (%s,%s,%s,'user_message',1,'{"content":"test"}',%s,%s,now())
           ON CONFLICT DO NOTHING""",
        (event_id, NAMESPACE_ID, turn_id, f"hash-{suffix}", f"{RUN_ID}:{suffix}"),
    )
    return event_id


def create_current_fact(connection, statement: str) -> UUID:
    fact_id = stable_uuid("fact", f"{NAMESPACE_ID}:{statement}")
    connection.execute(
        """INSERT INTO memory.facts(
             id,namespace_id,statement,fact_type,confidence,memory_state,source_profile,
             valid_from,valid_to
           ) VALUES (%s,%s,%s,'current',0.9,'active','test',now(),now()+interval '1 day')""",
        (fact_id, NAMESPACE_ID, statement),
    )
    connection.execute(
        """INSERT INTO retrieval.documents(
             id,namespace_id,source_kind,source_id,text_redacted,lifecycle_state
           ) VALUES (%s,%s,'fact',%s,%s,'active')""",
        (stable_uuid("document", str(fact_id)), NAMESPACE_ID, fact_id, statement),
    )
    return fact_id


def ensure_user_subject(connection) -> UUID:
    subject_id = stable_uuid("subject", f"{NAMESPACE_ID}:user")
    entity_id = stable_uuid("subject-entity", f"{NAMESPACE_ID}:user")
    connection.execute(
        """INSERT INTO memory.entities(
             id,namespace_id,entity_type,canonical_name,normalized_name
           ) VALUES (%s,%s,'person','User',%s) ON CONFLICT DO NOTHING""",
        (entity_id, NAMESPACE_ID, f"user-{RUN_ID}"),
    )
    connection.execute(
        """INSERT INTO core.subjects(
             id,namespace_id,entity_id,kind,stable_key,display_name,color
           ) VALUES (%s,%s,%s,'user','user','User','#f0c36b')
           ON CONFLICT DO NOTHING""",
        (subject_id, NAMESPACE_ID, entity_id),
    )
    return subject_id


def test_current_transition_hides_fact_and_is_idempotent() -> None:
    statement = f"当前邮件提醒任务暂停，后续继续处理 {RUN_ID}"
    topic_key = f"mail-reminder-{RUN_ID}"
    with psycopg.connect(DATABASE_URL) as connection:
        create_namespace(connection)
        fact_id = create_current_fact(connection, statement)
        item = upsert_current_item(
            connection,
            namespace_id=NAMESPACE_ID,
            topic_key=topic_key,
            summary=statement,
            source_fact_id=fact_id,
            valid_from=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            actor_type="worker",
            actor_id="integration-test",
            reason="test current admission",
        )
        resolved = transition_current_item(
            connection,
            namespace_id=NAMESPACE_ID,
            topic_key=topic_key,
            target_status="resolved",
            actor_type="provider",
            actor_id="integration-test",
            reason="task completed",
        )
        repeated = transition_current_item(
            connection,
            namespace_id=NAMESPACE_ID,
            topic_key=topic_key,
            target_status="resolved",
            actor_type="provider",
            actor_id="integration-test",
            reason="task completed",
        )
        with pytest.raises(ValueError, match="VERSION_CONFLICT"):
            transition_current_item(
                connection,
                namespace_id=NAMESPACE_ID,
                topic_key=topic_key,
                target_status="resolved",
                actor_type="provider",
                actor_id="integration-test",
                reason="stale update",
                expected_version=item["version"],
            )
        assert resolved["version"] == item["version"] + 1
        assert repeated["version"] == resolved["version"]
        fact_state, document_state = connection.execute(
            """SELECT fact.memory_state,document.lifecycle_state
               FROM memory.facts fact JOIN retrieval.documents document
                 ON document.source_id=fact.id AND document.source_kind='fact'
               WHERE fact.id=%s""",
            (fact_id,),
        ).fetchone()
        assert (fact_state, document_state) == ("dormant", "dormant")
        assert browse_memories(
            connection,
            namespace_key=NAMESPACE,
            source_profile=None,
            fact_type="current",
            state=None,
            updated_after=None,
            limit=10,
        ).items == []
        history = browse_memories(
            connection,
            namespace_key=NAMESPACE,
            source_profile=None,
            fact_type="current",
            state=None,
            updated_after=None,
            include_resolved=True,
            limit=10,
        )
        assert history.items[0].current_state_status == "resolved"
        assert history.items[0].current_state_resolution_reason == "task completed"
        trace = trace_memory(connection, NAMESPACE, fact_id)
        assert trace.current_state_status == "resolved"
        assert trace.current_state_resolution_reason == "task completed"


def test_preference_evidence_deduplicates_and_conflicts_supersede() -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        create_namespace(connection)
        subject_id = ensure_user_subject(connection)
        first_event = create_event(connection, "pref-1")
        second_event = create_event(connection, "pref-2")
        third_event = create_event(connection, "pref-3")
        first = _store_preference(
            connection,
            namespace_id=NAMESPACE_ID,
            user_subject_id=subject_id,
            event_id=first_event,
            occurred_at=datetime.now(UTC),
            candidate=PreferenceCandidate("称呼", "require", "公子", 0.95),
            fact_id=None,
        )
        repeated = _store_preference(
            connection,
            namespace_id=NAMESPACE_ID,
            user_subject_id=subject_id,
            event_id=second_event,
            occurred_at=datetime.now(UTC),
            candidate=PreferenceCandidate("称呼", "require", "公子", 0.95),
            fact_id=None,
        )
        replacement = _store_preference(
            connection,
            namespace_id=NAMESPACE_ID,
            user_subject_id=subject_id,
            event_id=third_event,
            occurred_at=datetime.now(UTC),
            candidate=PreferenceCandidate("称呼", "require", "Evergarden", 0.95),
            fact_id=None,
        )
        assert repeated == first
        assert replacement != first
        assert connection.execute(
            "SELECT count(*) FROM memory.preference_evidence WHERE preference_id=%s",
            (first,),
        ).fetchone()[0] == 2
        states = connection.execute(
            """SELECT id,state,supersedes_id FROM memory.preference_assertions
               WHERE namespace_id=%s AND aspect='称呼' ORDER BY created_at""",
            (NAMESPACE_ID,),
        ).fetchall()
        assert sum(state == "active" for _, state, _ in states) == 1
        active = next(item for item in states if item[1] == "active")
        assert active[0] == replacement
        assert active[2] == first


def test_purge_preserves_evidence_referenced_by_preference() -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        create_namespace(connection)
        event_id = create_event(connection, "purge")
        fact_id = stable_uuid("fact", f"{NAMESPACE_ID}:purge")
        connection.execute(
            """INSERT INTO memory.facts(
                 id,namespace_id,statement,fact_type,confidence,memory_state,source_profile
               ) VALUES (%s,%s,'purge test','long_term',0.9,'purge_requested','test')""",
            (fact_id, NAMESPACE_ID),
        )
        connection.execute(
            "INSERT INTO memory.fact_evidence(fact_id,event_id) VALUES (%s,%s)",
            (fact_id, event_id),
        )
        subject_id = ensure_user_subject(connection)
        preference_id = _store_preference(
            connection,
            namespace_id=NAMESPACE_ID,
            user_subject_id=subject_id,
            event_id=event_id,
            occurred_at=datetime.now(UTC),
            candidate=PreferenceCandidate("测试", "prefer", "保留证据", 0.9),
            fact_id=fact_id,
        )
        process_purge(connection, (new_uuid(), NAMESPACE_ID, "purge_memory", fact_id, 1))
        assert connection.execute(
            "SELECT 1 FROM memory.facts WHERE id=%s", (fact_id,)
        ).fetchone() is None
        assert connection.execute(
            "SELECT fact_id FROM memory.preference_assertions WHERE id=%s", (preference_id,)
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT 1 FROM evidence.events WHERE id=%s", (event_id,)
        ).fetchone() is not None


def test_targeted_model_replay_and_governance_manifest_are_fail_closed() -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        create_namespace(connection)
        job_id = new_uuid()
        connection.execute(
            """INSERT INTO ops.jobs(
                 id,namespace_id,kind,idempotency_key,input_ref,status,attempt_count,last_error_code
               ) VALUES (%s,%s,'extract_atomic_turn',%s,%s,'failed',5,'Timeout')""",
            (job_id, NAMESPACE_ID, f"failed:{job_id}", new_uuid()),
        )
        preview = replay_failed_model_jobs(
            connection, namespace_key=NAMESPACE, job_ids=(job_id,)
        )
        assert preview["mode"] == "dry-run" and preview["selected_count"] == 1
        with pytest.raises(ValueError, match="requires --confirm"):
            replay_failed_model_jobs(
                connection,
                namespace_key=NAMESPACE,
                job_ids=(job_id,),
                apply=True,
                confirmation="WRONG",
            )
        replay_failed_model_jobs(
            connection,
            namespace_key=NAMESPACE,
            job_ids=(job_id,),
            apply=True,
            confirmation=REPLAY_CONFIRMATION,
        )
        assert replay_failed_model_jobs(
            connection, namespace_key=NAMESPACE, job_ids=(job_id,)
        )["selected_count"] == 0

        drift_fact = create_current_fact(connection, f"expired drift {RUN_ID}")
        connection.execute(
            "UPDATE memory.facts SET valid_to=now()-interval '1 day' WHERE id=%s",
            (drift_fact,),
        )
        manifest = build_governance_manifest(connection, NAMESPACE)
        assert manifest["write_count"] == 0
        with pytest.raises(ValueError, match="manifest changed"):
            apply_governance_manifest(
                connection,
                namespace_key=NAMESPACE,
                expected_manifest_sha256="0" * 64,
                confirmation=GOVERNANCE_CONFIRMATION,
                reason="integration test",
            )
        applied = apply_governance_manifest(
            connection,
            namespace_key=NAMESPACE,
            expected_manifest_sha256=manifest["manifest_sha256"],
            confirmation=GOVERNANCE_CONFIRMATION,
            reason="integration test",
        )
        assert applied["write_count"] >= 1
        assert applied["integrity_before"]["evidence_hash"] == applied["integrity_after"][
            "evidence_hash"
        ]
