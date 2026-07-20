from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .community_evaluation import EVALUATION_VERSION
from .community_projection import PROJECTION_VERSION, enqueue_community_rebuild
from .ids import new_uuid, stable_uuid
from .redaction import redact_text
from .schemas import (
    GalaxyCreateRequest,
    GalaxyMembershipRequest,
    GalaxyUndoRequest,
    GalaxyUpdateRequest,
    LayoutPreferenceRequest,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _galaxy_base_rows(
    connection: Connection,
    namespace_id: UUID,
    *,
    galaxy_id: UUID | None = None,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    filters = ["galaxy.namespace_id=%s"]
    parameters: list[Any] = [namespace_id]
    if galaxy_id is not None:
        filters.append("galaxy.id=%s")
        parameters.append(galaxy_id)
    if not include_inactive:
        filters.append("galaxy.lifecycle_state='active'")
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            f"""SELECT galaxy.id,galaxy.stable_key,galaxy.family,galaxy.display_name,
                       galaxy.name_origin,galaxy.origin,galaxy.algorithm_version,
                       galaxy.input_snapshot_hash,galaxy.lifecycle_state,
                       galaxy.visibility,galaxy.manual_locked,galaxy.version,
                       galaxy.created_at,galaxy.updated_at
                FROM projection.galaxies galaxy
                WHERE {' AND '.join(filters)}
                ORDER BY galaxy.display_name,galaxy.id""",
            tuple(parameters),
        )
        return list(cursor.fetchall())


def _galaxy_members(
    connection: Connection, namespace_id: UUID, galaxy_ids: list[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    if not galaxy_ids:
        return {}
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT membership.galaxy_id,membership.entity_id,
                      entity.canonical_name,entity.entity_type,membership.role,
                      membership.membership_kind,membership.weight,
                      membership.governance_state,membership.algorithm_version,
                      membership.created_at,membership.updated_at,
                      count(DISTINCT evidence.event_id) AS evidence_count,
                      count(DISTINCT evidence.relation_id) AS relation_count
               FROM projection.galaxy_memberships membership
               JOIN memory.entities entity ON entity.id=membership.entity_id
               LEFT JOIN projection.galaxy_membership_evidence evidence
                 ON evidence.galaxy_id=membership.galaxy_id
                AND evidence.entity_id=membership.entity_id
               WHERE membership.namespace_id=%s
                 AND membership.galaxy_id=ANY(%s::uuid[])
               GROUP BY membership.galaxy_id,membership.entity_id,
                        entity.canonical_name,entity.entity_type,membership.role,
                        membership.membership_kind,membership.weight,
                        membership.governance_state,membership.algorithm_version,
                        membership.created_at,membership.updated_at
               ORDER BY membership.galaxy_id,membership.membership_kind,
                        membership.weight DESC,entity.canonical_name""",
            (namespace_id, galaxy_ids),
        )
        grouped: dict[UUID, list[dict[str, Any]]] = {}
        for row in cursor.fetchall():
            item = dict(row)
            item["canonical_name"] = redact_text(item["canonical_name"]).text
            item["weight"] = float(item["weight"])
            item["evidence_count"] = int(item["evidence_count"])
            item["relation_count"] = int(item["relation_count"])
            grouped.setdefault(item.pop("galaxy_id"), []).append(item)
        return grouped


def _galaxy_relations(
    connection: Connection, namespace_id: UUID, galaxy_ids: list[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    if not galaxy_ids:
        return {}
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT evidence.galaxy_id,relation.id,
                      COALESCE(source.canonical_entity_id,source.id) AS source_entity_id,
                      COALESCE(target.canonical_entity_id,target.id) AS target_entity_id,
                      relation.relation_type,
                      relation.transport,relation.confidence,relation.lifecycle_state,
                      array_agg(DISTINCT evidence.fact_id)
                        FILTER (WHERE evidence.fact_id IS NOT NULL) AS fact_ids,
                      array_agg(DISTINCT evidence.event_id)
                        FILTER (WHERE evidence.event_id IS NOT NULL) AS evidence_ids
               FROM projection.galaxy_membership_evidence evidence
               JOIN memory.entity_relations relation ON relation.id=evidence.relation_id
               JOIN memory.entities source ON source.id=relation.source_entity_id
               JOIN memory.entities target ON target.id=relation.target_entity_id
               JOIN projection.galaxies galaxy ON galaxy.id=evidence.galaxy_id
               WHERE galaxy.namespace_id=%s
                 AND evidence.galaxy_id=ANY(%s::uuid[])
               GROUP BY evidence.galaxy_id,relation.id,
                        COALESCE(source.canonical_entity_id,source.id),
                        COALESCE(target.canonical_entity_id,target.id)
               ORDER BY evidence.galaxy_id,relation.relation_type,relation.id""",
            (namespace_id, galaxy_ids),
        )
        grouped: dict[UUID, list[dict[str, Any]]] = {}
        for row in cursor.fetchall():
            item = dict(row)
            item["confidence"] = float(item["confidence"])
            item["fact_ids"] = item["fact_ids"] or []
            item["evidence_ids"] = item["evidence_ids"] or []
            item["evidence_count"] = len(item["evidence_ids"])
            grouped.setdefault(item.pop("galaxy_id"), []).append(item)
        return grouped


def list_galaxies(
    connection: Connection,
    namespace_key: str,
    *,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    namespace_id = stable_uuid("namespace", namespace_key)
    rows = _galaxy_base_rows(
        connection, namespace_id, include_inactive=include_inactive
    )
    galaxy_ids = [row["id"] for row in rows]
    members = _galaxy_members(connection, namespace_id, galaxy_ids)
    relations = _galaxy_relations(connection, namespace_id, galaxy_ids)
    result = []
    for row in rows:
        item = dict(row)
        item["display_name"] = redact_text(item["display_name"]).text
        item["members"] = members.get(item["id"], [])
        item["relations"] = relations.get(item["id"], [])
        item["member_count"] = sum(
            member["governance_state"] != "excluded" for member in item["members"]
        )
        item["relation_count"] = len(item["relations"])
        item["evidence_count"] = len(
            {
                evidence_id
                for relation in item["relations"]
                for evidence_id in relation["evidence_ids"]
            }
        )
        result.append(item)
    return result


def get_galaxy(
    connection: Connection,
    namespace_key: str,
    galaxy_id: UUID,
    *,
    include_inactive: bool = False,
) -> dict[str, Any] | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    rows = _galaxy_base_rows(
        connection,
        namespace_id,
        galaxy_id=galaxy_id,
        include_inactive=include_inactive,
    )
    if not rows:
        return None
    members = _galaxy_members(connection, namespace_id, [galaxy_id])
    relations = _galaxy_relations(connection, namespace_id, [galaxy_id])
    item = dict(rows[0])
    item["display_name"] = redact_text(item["display_name"]).text
    item["members"] = members.get(galaxy_id, [])
    item["relations"] = relations.get(galaxy_id, [])
    item["member_count"] = sum(
        member["governance_state"] != "excluded" for member in item["members"]
    )
    item["relation_count"] = len(item["relations"])
    item["evidence_count"] = len(
        {
            evidence_id
            for relation in item["relations"]
            for evidence_id in relation["evidence_ids"]
        }
    )
    return item


def list_layout_preferences(connection: Connection, namespace_key: str) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,scope_kind,scope_id,target_kind,target_id,position,zoom,
                      motion_enabled,pinned,version,created_at,updated_at
               FROM projection.layout_preferences
               WHERE namespace_id=%s
               ORDER BY scope_kind,scope_id,target_kind,target_id""",
            (namespace_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


def _audit(
    connection: Connection,
    *,
    namespace_id: UUID,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: UUID,
    reason: str,
    correlation_id: UUID,
    before: Any,
    after: Any,
) -> UUID:
    event_id = new_uuid()
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id,metadata_redacted
           ) VALUES (%s,%s,'user',%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (
            event_id,
            namespace_id,
            actor_id,
            action,
            target_type,
            target_id,
            reason,
            correlation_id,
            _json({"before": before, "after": after}),
        ),
    )
    return event_id


def _galaxy_for_update(
    connection: Connection, namespace_id: UUID, galaxy_id: UUID, expected_version: int
) -> dict[str, Any]:
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,display_name,name_origin,visibility,manual_locked,version
               FROM projection.galaxies
               WHERE id=%s AND namespace_id=%s FOR UPDATE""",
            (galaxy_id, namespace_id),
        )
        row = cursor.fetchone()
    if row is None:
        raise ValueError("GALAXY_NOT_FOUND")
    if row["version"] != expected_version:
        raise ValueError("VERSION_CONFLICT")
    return dict(row)


def create_manual_galaxy(
    connection: Connection, request: GalaxyCreateRequest
) -> dict[str, Any]:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    entity_rows = connection.execute(
        """SELECT entity.id FROM memory.entities entity
           WHERE entity.namespace_id=%s AND entity.id=ANY(%s::uuid[])
             AND entity.canonical_entity_id IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM core.subjects subject WHERE subject.entity_id=entity.id
             )""",
        (namespace_id, request.entity_ids),
    ).fetchall()
    if {row[0] for row in entity_rows} != set(request.entity_ids):
        raise ValueError("GALAXY_ENTITY_NOT_FOUND_OR_NOT_CANONICAL")
    galaxy_id = new_uuid()
    stable_key = f"manual:{galaxy_id}"
    snapshot_hash = hashlib.sha256(stable_key.encode()).hexdigest()
    connection.execute(
        """INSERT INTO projection.galaxies(
             id,namespace_id,stable_key,family,display_name,name_origin,origin,
             algorithm_version,input_snapshot_hash,manual_locked
           ) VALUES (%s,%s,%s,%s,%s,'manual','manual','manual-v1',%s,true)""",
        (
            galaxy_id,
            namespace_id,
            stable_key,
            request.family,
            request.display_name,
            snapshot_hash,
        ),
    )
    for entity_id in request.entity_ids:
        connection.execute(
            """INSERT INTO projection.galaxy_memberships(
                 galaxy_id,namespace_id,entity_id,role,membership_kind,weight,
                 governance_state,algorithm_version
               ) VALUES (%s,%s,%s,'member','secondary',1,'fixed','manual-v1')""",
            (galaxy_id, namespace_id, entity_id),
        )
    _audit(
        connection,
        namespace_id=namespace_id,
        actor_id=request.context.source_profile,
        action="projection.galaxy.create",
        target_type="galaxy",
        target_id=galaxy_id,
        reason=request.reason,
        correlation_id=request.context.correlation_id,
        before=None,
        after={
            "display_name": request.display_name,
            "family": request.family,
            "entity_ids": request.entity_ids,
        },
    )
    return get_galaxy(
        connection, request.context.shared_namespace, galaxy_id, include_inactive=True
    )


def update_galaxy(
    connection: Connection, galaxy_id: UUID, request: GalaxyUpdateRequest
) -> dict[str, Any]:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    before = _galaxy_for_update(
        connection, namespace_id, galaxy_id, request.expected_version
    )
    display_name = request.display_name or before["display_name"]
    visibility = request.visibility or before["visibility"]
    manual_locked = (
        request.manual_locked
        if request.manual_locked is not None
        else before["manual_locked"]
    )
    name_origin = "manual" if request.display_name is not None else before["name_origin"]
    connection.execute(
        """UPDATE projection.galaxies SET
             display_name=%s,name_origin=%s,visibility=%s,manual_locked=%s,
             version=version+1,updated_at=now()
           WHERE id=%s""",
        (display_name, name_origin, visibility, manual_locked, galaxy_id),
    )
    after = {
        **before,
        "display_name": display_name,
        "name_origin": name_origin,
        "visibility": visibility,
        "manual_locked": manual_locked,
        "version": before["version"] + 1,
    }
    _audit(
        connection,
        namespace_id=namespace_id,
        actor_id=request.context.source_profile,
        action="projection.galaxy.update",
        target_type="galaxy",
        target_id=galaxy_id,
        reason=request.reason,
        correlation_id=request.context.correlation_id,
        before=before,
        after=after,
    )
    return get_galaxy(
        connection, request.context.shared_namespace, galaxy_id, include_inactive=True
    )


def update_membership(
    connection: Connection,
    galaxy_id: UUID,
    entity_id: UUID,
    request: GalaxyMembershipRequest,
) -> dict[str, Any]:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    galaxy = _galaxy_for_update(
        connection, namespace_id, galaxy_id, request.expected_version
    )
    entity = connection.execute(
        """SELECT id FROM memory.entities
           WHERE id=%s AND namespace_id=%s AND canonical_entity_id IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM core.subjects subject
               WHERE subject.entity_id=memory.entities.id
             )""",
        (entity_id, namespace_id),
    ).fetchone()
    if entity is None:
        raise ValueError("ENTITY_NOT_FOUND_OR_NOT_CANONICAL")
    before_row = connection.execute(
        """SELECT role,membership_kind,weight,governance_state,algorithm_version
           FROM projection.galaxy_memberships
           WHERE galaxy_id=%s AND entity_id=%s""",
        (galaxy_id, entity_id),
    ).fetchone()
    before = list(before_row) if before_row is not None else None
    if request.action == "fixed" and request.membership_kind == "primary":
        fixed_conflict = connection.execute(
            """SELECT 1 FROM projection.galaxy_memberships
               WHERE namespace_id=%s AND entity_id=%s AND galaxy_id<>%s
                 AND membership_kind='primary' AND governance_state='fixed'""",
            (namespace_id, entity_id, galaxy_id),
        ).fetchone()
        if fixed_conflict:
            raise ValueError("PRIMARY_MEMBERSHIP_CONFLICT")
        connection.execute(
            """UPDATE projection.galaxy_memberships
               SET membership_kind='secondary',updated_at=now()
               WHERE namespace_id=%s AND entity_id=%s AND galaxy_id<>%s
                 AND membership_kind='primary' AND governance_state='automatic'""",
            (namespace_id, entity_id, galaxy_id),
        )
    connection.execute(
        """INSERT INTO projection.galaxy_memberships(
             galaxy_id,namespace_id,entity_id,role,membership_kind,weight,
             governance_state,algorithm_version
           ) VALUES (%s,%s,%s,%s,%s,1,%s,%s)
           ON CONFLICT(galaxy_id,entity_id) DO UPDATE SET
             role=excluded.role,membership_kind=excluded.membership_kind,
             governance_state=excluded.governance_state,updated_at=now()""",
        (
            galaxy_id,
            namespace_id,
            entity_id,
            request.role,
            request.membership_kind,
            request.action,
            EVALUATION_VERSION,
        ),
    )
    connection.execute(
        "UPDATE projection.galaxies SET version=version+1,updated_at=now() WHERE id=%s",
        (galaxy_id,),
    )
    after = {
        "role": request.role,
        "membership_kind": request.membership_kind,
        "weight": 1,
        "governance_state": request.action,
        "algorithm_version": EVALUATION_VERSION,
    }
    _audit(
        connection,
        namespace_id=namespace_id,
        actor_id=request.context.source_profile,
        action="projection.galaxy.membership.update",
        target_type="galaxy",
        target_id=galaxy_id,
        reason=request.reason,
        correlation_id=request.context.correlation_id,
        before={"entity_id": entity_id, "membership": before},
        after={"entity_id": entity_id, "membership": after},
    )
    if request.action == "automatic":
        enqueue_community_rebuild(
            connection,
            namespace_id,
            reason_key=f"governance-reset:{request.context.correlation_id}",
        )
    assert galaxy["version"] == request.expected_version
    return get_galaxy(
        connection, request.context.shared_namespace, galaxy_id, include_inactive=True
    )


def save_layout_preference(
    connection: Connection, request: LayoutPreferenceRequest
) -> dict[str, Any]:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,position,zoom,motion_enabled,pinned,version
               FROM projection.layout_preferences
               WHERE namespace_id=%s AND scope_kind=%s AND scope_id=%s
                 AND target_kind=%s AND target_id=%s FOR UPDATE""",
            (
                namespace_id,
                request.scope_kind,
                request.scope_id,
                request.target_kind,
                request.target_id,
            ),
        )
        before_row = cursor.fetchone()
    if before_row is not None:
        if request.expected_version is None:
            raise ValueError("EXPECTED_VERSION_REQUIRED")
        if before_row["version"] != request.expected_version:
            raise ValueError("VERSION_CONFLICT")
        layout_id = before_row["id"]
        connection.execute(
            """UPDATE projection.layout_preferences SET
                 position=%s::jsonb,zoom=%s,motion_enabled=%s,pinned=%s,
                 version=version+1,updated_at=now()
               WHERE id=%s""",
            (
                _json(request.position),
                request.zoom,
                request.motion_enabled,
                request.pinned,
                layout_id,
            ),
        )
        version = before_row["version"] + 1
    else:
        if request.expected_version is not None:
            raise ValueError("LAYOUT_NOT_FOUND")
        layout_id = stable_uuid(
            "layout",
            f"{namespace_id}:{request.scope_kind}:{request.scope_id}:"
            f"{request.target_kind}:{request.target_id}",
        )
        connection.execute(
            """INSERT INTO projection.layout_preferences(
                 id,namespace_id,scope_kind,scope_id,target_kind,target_id,
                 position,zoom,motion_enabled,pinned
               ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
            (
                layout_id,
                namespace_id,
                request.scope_kind,
                request.scope_id,
                request.target_kind,
                request.target_id,
                _json(request.position),
                request.zoom,
                request.motion_enabled,
                request.pinned,
            ),
        )
        version = 1
    after = {
        "position": request.position,
        "zoom": request.zoom,
        "motion_enabled": request.motion_enabled,
        "pinned": request.pinned,
        "version": version,
    }
    _audit(
        connection,
        namespace_id=namespace_id,
        actor_id=request.context.source_profile,
        action="projection.layout.update",
        target_type="layout",
        target_id=layout_id,
        reason=request.reason,
        correlation_id=request.context.correlation_id,
        before=dict(before_row) if before_row else None,
        after=after,
    )
    return next(
        item
        for item in list_layout_preferences(connection, request.context.shared_namespace)
        if item["id"] == layout_id
    )


def request_rebuild(
    connection: Connection,
    *,
    namespace_key: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> UUID:
    namespace_id = stable_uuid("namespace", namespace_key)
    job_id = enqueue_community_rebuild(
        connection,
        namespace_id,
        reason_key=f"manual:{correlation_id}",
    )
    _audit(
        connection,
        namespace_id=namespace_id,
        actor_id=actor_id,
        action="projection.communities.rebuild.request",
        target_type="job",
        target_id=job_id,
        reason=reason,
        correlation_id=correlation_id,
        before=None,
        after={"job_id": job_id, "projection_version": PROJECTION_VERSION},
    )
    return job_id


def undo_last_galaxy_change(
    connection: Connection, galaxy_id: UUID, request: GalaxyUndoRequest
) -> dict[str, Any]:
    namespace_id = stable_uuid("namespace", request.context.shared_namespace)
    _galaxy_for_update(connection, namespace_id, galaxy_id, request.expected_version)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT event.id,event.action,event.metadata_redacted
               FROM audit.events event
               WHERE event.namespace_id=%s AND event.target_type='galaxy'
                 AND event.target_id=%s
                 AND event.action IN (
                   'projection.galaxy.update','projection.galaxy.membership.update'
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM audit.events undo
                   WHERE undo.namespace_id=event.namespace_id
                     AND undo.action='projection.galaxy.undo'
                     AND undo.metadata_redacted->>'undone_event_id'=event.id::text
                 )
               ORDER BY event.created_at DESC,event.id DESC LIMIT 1
               FOR UPDATE""",
            (namespace_id, galaxy_id),
        )
        event = cursor.fetchone()
    if event is None:
        raise ValueError("NO_UNDOABLE_CHANGE")
    metadata = event["metadata_redacted"]
    if event["action"] == "projection.galaxy.update":
        before = metadata["before"]
        connection.execute(
            """UPDATE projection.galaxies SET
                 display_name=%s,name_origin=%s,visibility=%s,manual_locked=%s,
                 version=version+1,updated_at=now()
               WHERE id=%s""",
            (
                before["display_name"],
                before["name_origin"],
                before["visibility"],
                before["manual_locked"],
                galaxy_id,
            ),
        )
    else:
        before = metadata["before"]
        entity_id = UUID(before["entity_id"])
        membership = before["membership"]
        if membership is None:
            connection.execute(
                """DELETE FROM projection.galaxy_memberships
                   WHERE galaxy_id=%s AND entity_id=%s""",
                (galaxy_id, entity_id),
            )
        else:
            connection.execute(
                """UPDATE projection.galaxy_memberships SET
                     role=%s,membership_kind=%s,weight=%s,governance_state=%s,
                     algorithm_version=%s,updated_at=now()
                   WHERE galaxy_id=%s AND entity_id=%s""",
                (*membership, galaxy_id, entity_id),
            )
        connection.execute(
            "UPDATE projection.galaxies SET version=version+1,updated_at=now() WHERE id=%s",
            (galaxy_id,),
        )
    undo_event_id = new_uuid()
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id,metadata_redacted
           ) VALUES (%s,%s,'user',%s,'projection.galaxy.undo','galaxy',%s,%s,%s,%s::jsonb)""",
        (
            undo_event_id,
            namespace_id,
            request.context.source_profile,
            galaxy_id,
            request.reason,
            request.context.correlation_id,
            _json({"undone_event_id": event["id"], "undone_action": event["action"]}),
        ),
    )
    return get_galaxy(
        connection, request.context.shared_namespace, galaxy_id, include_inactive=True
    )
