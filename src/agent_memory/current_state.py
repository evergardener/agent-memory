"""Transactional lifecycle service for current state and its projections."""

import json
from datetime import datetime
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .ids import new_uuid, stable_uuid


def _set_fact_lifecycle(
    connection: Connection,
    fact_id: UUID | None,
    *,
    state: str,
    valid_to: datetime | None = None,
) -> None:
    if fact_id is None:
        return
    if state == "active":
        connection.execute(
            """UPDATE memory.facts
               SET memory_state='active',valid_to=%s,version=version+1,updated_at=now()
               WHERE id=%s AND fact_type='current'
                 AND memory_state IN ('active','dormant')
                 AND (memory_state <> 'active' OR valid_to IS DISTINCT FROM %s)""",
            (valid_to, fact_id, valid_to),
        )
    else:
        connection.execute(
            """UPDATE memory.facts
               SET memory_state='dormant',version=version+1,updated_at=now()
               WHERE id=%s AND fact_type='current' AND memory_state='active'""",
            (fact_id,),
        )
    connection.execute(
        """UPDATE retrieval.documents SET lifecycle_state=%s,indexed_at=now()
           WHERE source_kind='fact' AND source_id=%s AND lifecycle_state IS DISTINCT FROM %s""",
        (state, fact_id, state),
    )


def _audit_transition(
    connection: Connection,
    *,
    namespace_id: UUID,
    item_id: UUID,
    action: str,
    actor_type: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID | None,
    metadata: dict | None = None,
) -> None:
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,
             correlation_id,metadata_redacted
           ) VALUES (%s,%s,%s,%s,%s,'current_state',%s,%s,%s,%s::jsonb)""",
        (
            new_uuid(),
            namespace_id,
            actor_type,
            actor_id,
            action,
            item_id,
            reason,
            correlation_id or new_uuid(),
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )


def upsert_current_item(
    connection: Connection,
    *,
    namespace_id: UUID,
    topic_key: str,
    summary: str,
    valid_from: datetime,
    expires_at: datetime,
    source_fact_id: UUID | None,
    actor_type: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID | None = None,
    expected_version: int | None = None,
    decision_reason: str = "manual_current_state_change",
    policy_version: str = "current-lifecycle-v1",
) -> dict:
    """Create or reactivate one item and keep the linked fact/index consistent."""
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,topic_key,summary,source_fact_id,status,valid_from,expires_at,
                      resolved_at,resolution_reason,version
               FROM state.current_items
               WHERE namespace_id=%s AND topic_key=%s FOR UPDATE""",
            (namespace_id, topic_key),
        )
        previous = cursor.fetchone()
        if expected_version is not None and (
            previous is None or previous["version"] != expected_version
        ):
            raise ValueError("VERSION_CONFLICT")
        if (
            previous
            and previous["status"] == "active"
            and previous["summary"] == summary
            and previous["source_fact_id"] == source_fact_id
            and previous["valid_from"] == valid_from
            and previous["expires_at"] == expires_at
        ):
            return previous
        item_id = previous["id"] if previous else stable_uuid(
            "current-item", f"{namespace_id}:{topic_key}"
        )
        cursor.execute(
            """INSERT INTO state.current_items(
                 id,namespace_id,topic_key,summary,source_fact_id,status,valid_from,expires_at,
                 resolved_at,resolution_reason,version
               ) VALUES (%s,%s,%s,%s,%s,'active',%s,%s,NULL,NULL,1)
               ON CONFLICT(namespace_id,topic_key) DO UPDATE SET
                 summary=excluded.summary,source_fact_id=excluded.source_fact_id,
                 status='active',valid_from=excluded.valid_from,expires_at=excluded.expires_at,
                 resolved_at=NULL,resolution_reason=NULL,
                 version=state.current_items.version+1,updated_at=now()
               RETURNING id,topic_key,status,source_fact_id,valid_from,expires_at,
                         resolved_at,resolution_reason,version""",
            (
                item_id,
                namespace_id,
                topic_key,
                summary,
                source_fact_id,
                valid_from,
                expires_at,
            ),
        )
        row = cursor.fetchone()
    previous_fact_id = previous["source_fact_id"] if previous else None
    if previous_fact_id and previous_fact_id != source_fact_id:
        _set_fact_lifecycle(connection, previous_fact_id, state="dormant")
    _set_fact_lifecycle(connection, source_fact_id, state="active", valid_to=expires_at)
    _audit_transition(
        connection,
        namespace_id=namespace_id,
        item_id=row["id"],
        action="state.set" if previous is None else "state.update",
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
        correlation_id=correlation_id,
        metadata={
            "source_fact_id": str(source_fact_id) if source_fact_id else None,
            "target_kind": "current",
            "decision_reason": decision_reason,
            "policy_version": policy_version,
        },
    )
    return row


def transition_current_item(
    connection: Connection,
    *,
    namespace_id: UUID,
    topic_key: str,
    target_status: str,
    actor_type: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID | None = None,
    expected_version: int | None = None,
) -> dict | None:
    """Resolve or expire one current item idempotently."""
    if target_status not in {"resolved", "expired"}:
        raise ValueError("current state transition target must be resolved or expired")
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,topic_key,status,source_fact_id,valid_from,expires_at,
                      resolved_at,resolution_reason,version
               FROM state.current_items
               WHERE namespace_id=%s AND topic_key=%s FOR UPDATE""",
            (namespace_id, topic_key),
        )
        current = cursor.fetchone()
        if current is None:
            return None
        if expected_version is not None and current["version"] != expected_version:
            raise ValueError("VERSION_CONFLICT")
        if current["status"] == target_status:
            return current
        if current["status"] != "active":
            return None
        cursor.execute(
            """UPDATE state.current_items
               SET status=%s,resolved_at=now(),resolution_reason=%s,
                   version=version+1,updated_at=now()
               WHERE id=%s AND status='active'
               RETURNING id,topic_key,status,source_fact_id,valid_from,expires_at,
                         resolved_at,resolution_reason,version""",
            (target_status, reason, current["id"]),
        )
        row = cursor.fetchone()
    _set_fact_lifecycle(connection, row["source_fact_id"], state="dormant")
    _audit_transition(
        connection,
        namespace_id=namespace_id,
        item_id=row["id"],
        action=f"state.{target_status}",
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
        correlation_id=correlation_id,
        metadata={
            "source_fact_id": str(row["source_fact_id"]) if row["source_fact_id"] else None,
            "target_kind": "current",
            "decision_reason": f"transition_to_{target_status}",
            "policy_version": "current-lifecycle-v1",
        },
    )
    return row


def expire_due_current_items(connection: Connection, namespace_id: UUID | None = None) -> int:
    """Expire due items through the same transition path used by manual resolution."""
    query = """SELECT namespace_id,topic_key FROM state.current_items
               WHERE status='active' AND expires_at <= now()"""
    params: tuple[object, ...] = ()
    if namespace_id is not None:
        query += " AND namespace_id=%s"
        params = (namespace_id,)
    query += " ORDER BY expires_at FOR UPDATE SKIP LOCKED"
    rows = connection.execute(query, params).fetchall()
    changed = 0
    for item_namespace_id, topic_key in rows:
        if transition_current_item(
            connection,
            namespace_id=item_namespace_id,
            topic_key=topic_key,
            target_status="expired",
            actor_type="system",
            actor_id="core-worker",
            reason="current validity window expired",
        ):
            changed += 1
    return changed
