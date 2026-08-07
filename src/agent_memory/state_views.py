import json
from datetime import UTC, datetime

from psycopg import Connection
from psycopg.rows import dict_row

from .current_state import expire_due_current_items, transition_current_item, upsert_current_item
from .ids import new_uuid, stable_uuid
from .interaction_state import (
    DEFAULT_AXES,
    DEFAULT_AXIS_ENABLED,
    DEFAULT_AXIS_LABELS,
    DEFAULT_AXIS_RANGES,
    DEFAULT_THRESHOLDS,
    advance_state,
)

STATE_CONFIG_COLUMNS = """enabled,drift_hours,axes_initial,axis_labels,axis_ranges,
                          axis_enabled,thresholds,profile_overrides,updated_at"""


def default_state_config() -> dict:
    return {
        "enabled": True,
        "drift_hours": 72,
        "axes_initial": dict(DEFAULT_AXES),
        "axis_labels": dict(DEFAULT_AXIS_LABELS),
        "axis_ranges": dict(DEFAULT_AXIS_RANGES),
        "axis_enabled": dict(DEFAULT_AXIS_ENABLED),
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "profile_overrides": {},
    }


def get_state_config(connection: Connection, namespace_key: str) -> dict:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            f"SELECT {STATE_CONFIG_COLUMNS} FROM state.settings WHERE namespace_id=%s",
            (namespace_id,),
        )
        row = cursor.fetchone()
    return row or default_state_config()


def effective_state_config(connection: Connection, namespace_id, source_profile: str) -> dict:
    config = get_state_config_by_id(connection, namespace_id)
    override = config["profile_overrides"].get(source_profile, {})
    for key in (
        "enabled",
        "drift_hours",
        "axes_initial",
        "axis_labels",
        "axis_ranges",
        "axis_enabled",
        "thresholds",
    ):
        if key in override:
            config[key] = override[key]
    return config


def get_state_config_by_id(connection: Connection, namespace_id) -> dict:
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            f"SELECT {STATE_CONFIG_COLUMNS} FROM state.settings WHERE namespace_id=%s",
            (namespace_id,),
        )
        row = cursor.fetchone()
    return row or default_state_config()


def update_state_config(connection: Connection, request) -> dict:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    connection.execute(
        """INSERT INTO state.settings(
             namespace_id,enabled,drift_hours,axes_initial,axis_labels,axis_ranges,
             axis_enabled,thresholds,profile_overrides
           ) VALUES (
             %s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb
           )
           ON CONFLICT(namespace_id) DO UPDATE SET enabled=excluded.enabled,
             drift_hours=excluded.drift_hours,axes_initial=excluded.axes_initial,
             axis_labels=excluded.axis_labels,axis_ranges=excluded.axis_ranges,
             axis_enabled=excluded.axis_enabled,
             thresholds=excluded.thresholds,profile_overrides=excluded.profile_overrides,
             updated_at=now()""",
        (
            namespace_id,
            request.enabled,
            request.drift_hours,
            json.dumps(request.axes_initial),
            json.dumps(request.axis_labels, ensure_ascii=False),
            json.dumps(request.axis_ranges),
            json.dumps(request.axis_enabled),
            json.dumps(request.thresholds),
            json.dumps(request.profile_overrides),
        ),
    )
    _audit_state_change(connection, request, "state.config.update", namespace_id)
    return get_state_config_by_id(connection, namespace_id)


def reset_interaction_state(connection: Connection, request) -> dict:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    config = get_state_config_by_id(connection, namespace_id)
    connection.execute(
        "DELETE FROM state.interaction_snapshots WHERE namespace_id=%s", (namespace_id,)
    )
    now = datetime.now(UTC)
    snapshot_id = stable_uuid("interaction-state-reset", f"{namespace_id}:{now.isoformat()}")
    connection.execute(
        """INSERT INTO state.interaction_snapshots(
             id,namespace_id,axes,summary,suggestions,calculated_at,algorithm_version
           ) VALUES (%s,%s,%s::jsonb,'状态已由用户重置','[]'::jsonb,%s,'jiwen-neutral-v1')""",
        (snapshot_id, namespace_id, json.dumps(config["axes_initial"]), now),
    )
    _audit_state_change(connection, request, "state.reset", snapshot_id)
    return latest_state(connection, request.context.shared_namespace)


def simulate_interaction_state(connection: Connection, request) -> dict:
    config = effective_state_config(
        connection,
        stable_uuid("namespace", request.context.shared_namespace),
        request.context.source_profile,
    )
    current = latest_state(connection, request.context.shared_namespace)
    result = advance_state(
        current["axes"] if current else None,
        current["calculated_at"] if current else None,
        request.event_type,
        request.content,
        request.occurred_at,
        axes_initial=config["axes_initial"],
        axis_ranges=config["axis_ranges"],
        axis_enabled=config["axis_enabled"],
        drift_hours=config["drift_hours"],
        thresholds=config["thresholds"],
    )
    return {"axes": result.axes, "summary": result.summary, "suggestions": result.suggestions}


def _audit_state_change(connection: Connection, request, action: str, target_id) -> None:
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,
             correlation_id
           ) VALUES (%s,%s,'user',%s,%s,'interaction_state',%s,%s,%s)""",
        (
            new_uuid(),
            stable_uuid("namespace", request.context.shared_namespace),
            request.context.source_profile,
            action,
            target_id,
            request.reason,
            request.context.correlation_id,
        ),
    )


def change_current_item(connection: Connection, request) -> dict | None:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    if request.action == "resolve":
        row = transition_current_item(
            connection,
            namespace_id=namespace_id,
            topic_key=request.topic_key,
            target_status="resolved",
            actor_type="provider",
            actor_id=request.context.source_profile,
            reason=request.reason,
            correlation_id=request.context.correlation_id,
            expected_version=request.expected_version,
        )
    else:
        row = upsert_current_item(
            connection,
            namespace_id=namespace_id,
            topic_key=request.topic_key,
            summary=request.summary,
            source_fact_id=None,
            valid_from=datetime.now(UTC),
            expires_at=request.expires_at,
            actor_type="provider",
            actor_id=request.context.source_profile,
            reason=request.reason,
            correlation_id=request.context.correlation_id,
            expected_version=request.expected_version,
        )
    if row is None:
        return None
    return {
        "id": row["id"],
        "status": row["status"],
        "topic_key": row["topic_key"],
        "version": row["version"],
        "resolved_at": row["resolved_at"],
        "resolution_reason": row["resolution_reason"],
    }


def latest_state(connection: Connection, namespace_key: str) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT axes,summary,suggestions,calculated_at,algorithm_version
               FROM state.interaction_snapshots WHERE namespace_id=%s
               ORDER BY calculated_at DESC LIMIT 1""",
            (namespace_id,),
        )
        return cursor.fetchone()


def active_continuity(connection: Connection, namespace_key: str) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT topic_key,summary,last_active_at,expires_at
               FROM state.continuities
               WHERE namespace_id=%s AND expires_at > now()
               ORDER BY last_active_at DESC""",
            (namespace_id,),
        )
        return cursor.fetchall()


def active_current_items(connection: Connection, namespace_key: str) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    expire_due_current_items(connection, namespace_id)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,topic_key,summary,source_fact_id,valid_from,expires_at,status,
                      resolved_at,resolution_reason,version
               FROM state.current_items
               WHERE namespace_id=%s AND status='active' ORDER BY expires_at""",
            (namespace_id,),
        )
        return cursor.fetchall()


def list_reports(connection: Connection, namespace_key: str, limit: int = 12) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,period_start,period_end,summary,created_at
               FROM reports.consolidation WHERE namespace_id=%s
               ORDER BY period_end DESC LIMIT %s""",
            (namespace_id, limit),
        )
        return cursor.fetchall()
