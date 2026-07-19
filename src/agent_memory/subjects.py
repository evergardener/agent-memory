"""Canonical user/profile Subjects and their Hermes source mappings."""

import hashlib
import json
import re
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .ids import new_uuid, stable_uuid
from .redaction import redact_text

SUBJECT_KINDS = {"user", "profile_persona"}
SUBJECT_STATUSES = {"active", "hidden"}
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
PROFILE_COLORS = (
    "#91cfb2",
    "#9db9ee",
    "#c2a5e8",
    "#e0a6bd",
    "#8fd1d1",
    "#d8b783",
)


def normalize_profile(source_profile: str) -> str:
    return " ".join(source_profile.split()).strip().casefold()


def _profile_color(profile_key: str) -> str:
    digest = hashlib.sha256(profile_key.encode()).digest()
    return PROFILE_COLORS[digest[0] % len(PROFILE_COLORS)]


def _ensure_subject(
    connection: Connection,
    namespace_id: UUID,
    *,
    kind: str,
    stable_key: str,
    display_name: str,
    entity_type: str,
    color: str,
) -> UUID:
    normalized_name = f"__subject__:{stable_key}"
    entity_id = stable_uuid("subject-entity", f"{namespace_id}:{stable_key}")
    entity_row = connection.execute(
        """INSERT INTO memory.entities(
             id,namespace_id,entity_type,canonical_name,normalized_name
           ) VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT(namespace_id,normalized_name) DO UPDATE
             SET updated_at=memory.entities.updated_at
           RETURNING id""",
        (entity_id, namespace_id, entity_type, display_name, normalized_name),
    ).fetchone()
    canonical_entity_id = entity_row[0]
    subject_id = stable_uuid("subject", f"{namespace_id}:{stable_key}")
    row = connection.execute(
        """INSERT INTO core.subjects(
             id,namespace_id,entity_id,kind,stable_key,display_name,color
           ) VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(namespace_id,stable_key) DO UPDATE
             SET updated_at=core.subjects.updated_at
           RETURNING id""",
        (
            subject_id,
            namespace_id,
            canonical_entity_id,
            kind,
            stable_key,
            display_name,
            color,
        ),
    ).fetchone()
    return row[0]


def ensure_user_subject(connection: Connection, namespace_id: UUID) -> UUID:
    return _ensure_subject(
        connection,
        namespace_id,
        kind="user",
        stable_key="user",
        display_name="User",
        entity_type="person",
        color="#efd095",
    )


def ensure_profile_subject(
    connection: Connection, namespace_id: UUID, source_profile: str
) -> UUID:
    profile_key = normalize_profile(source_profile)
    if not profile_key:
        raise ValueError("SOURCE_PROFILE_INVALID")
    display_profile = " ".join(source_profile.split()).strip()
    return _ensure_subject(
        connection,
        namespace_id,
        kind="profile_persona",
        stable_key=f"profile:{profile_key}",
        display_name=f"Hermes · {display_profile}",
        entity_type="agent",
        color=_profile_color(profile_key),
    )


def ensure_source_subject_mapping(
    connection: Connection,
    namespace_id: UUID,
    source_id: UUID,
    source_profile: str,
) -> UUID:
    """Create stable stars and bind a new source without overriding manual mapping."""
    ensure_user_subject(connection, namespace_id)
    subject_id = ensure_profile_subject(connection, namespace_id, source_profile)
    existing = connection.execute(
        "SELECT subject_id,mapping_origin FROM core.subject_sources WHERE source_id=%s",
        (source_id,),
    ).fetchone()
    if existing and existing[1] == "manual":
        connection.execute(
            "UPDATE core.sources SET subject_id=%s WHERE id=%s",
            (existing[0], source_id),
        )
        return existing[0]
    connection.execute(
        """INSERT INTO core.subject_sources(source_id,subject_id,mapping_origin)
           VALUES (%s,%s,'automatic')
           ON CONFLICT(source_id) DO UPDATE SET
             subject_id=excluded.subject_id,mapping_origin='automatic',updated_at=now()""",
        (source_id, subject_id),
    )
    connection.execute(
        "UPDATE core.sources SET subject_id=%s WHERE id=%s", (subject_id, source_id)
    )
    return subject_id


def list_subjects(connection: Connection, namespace_key: str) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT subject.id,subject.entity_id,subject.kind,subject.stable_key,
                      subject.display_name,subject.color,subject.status,
                      subject.created_at,subject.updated_at,
                      COALESCE(jsonb_agg(jsonb_build_object(
                        'source_id',source.id,
                        'source_profile',source.source_profile,
                        'source_instance',source.source_instance,
                        'mapping_origin',mapping.mapping_origin
                      ) ORDER BY source.source_profile,source.source_instance)
                        FILTER (WHERE source.id IS NOT NULL),'[]'::jsonb) AS sources
               FROM core.subjects subject
               LEFT JOIN core.subject_sources mapping ON mapping.subject_id=subject.id
               LEFT JOIN core.sources source ON source.id=mapping.source_id
               WHERE subject.namespace_id=%s
               GROUP BY subject.id
               ORDER BY CASE subject.kind WHEN 'user' THEN 0 ELSE 1 END,
                        subject.display_name,subject.id""",
            (namespace_id,),
        )
        return list(cursor.fetchall())


def update_subject(
    connection: Connection,
    *,
    namespace_key: str,
    subject_id: UUID,
    display_name: str | None,
    color: str | None,
    status: str | None,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    current = connection.execute(
        """SELECT kind,display_name,color,status FROM core.subjects
           WHERE id=%s AND namespace_id=%s FOR UPDATE""",
        (subject_id, namespace_id),
    ).fetchone()
    if current is None:
        return None
    next_name = current[1]
    if display_name is not None:
        next_name = redact_text(display_name).text.strip()
        if not next_name or next_name in {"[REDACTED]", "«REDACTED_SECRET»"}:
            raise ValueError("SUBJECT_NAME_INVALID")
    next_color = color or current[2]
    if not COLOR_PATTERN.fullmatch(next_color):
        raise ValueError("SUBJECT_COLOR_INVALID")
    next_status = status or current[3]
    if next_status not in SUBJECT_STATUSES:
        raise ValueError("SUBJECT_STATUS_INVALID")
    if current[0] == "user" and next_status != "active":
        raise ValueError("USER_SUBJECT_CANNOT_BE_HIDDEN")
    connection.execute(
        """UPDATE core.subjects SET display_name=%s,color=%s,status=%s,updated_at=now()
           WHERE id=%s""",
        (next_name, next_color.lower(), next_status, subject_id),
    )
    _audit(
        connection,
        namespace_id=namespace_id,
        subject_id=subject_id,
        actor_id=actor_id,
        action="subject.update",
        reason=reason,
        correlation_id=correlation_id,
        metadata={
            "previous": {
                "display_name": current[1],
                "color": current[2],
                "status": current[3],
            },
            "current": {
                "display_name": next_name,
                "color": next_color.lower(),
                "status": next_status,
            },
        },
    )
    return next(
        item for item in list_subjects(connection, namespace_key)
        if item["id"] == subject_id
    )


def assign_source_to_subject(
    connection: Connection,
    *,
    namespace_key: str,
    subject_id: UUID,
    source_id: UUID,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    target = connection.execute(
        """SELECT kind FROM core.subjects
           WHERE id=%s AND namespace_id=%s AND status='active' FOR UPDATE""",
        (subject_id, namespace_id),
    ).fetchone()
    source = connection.execute(
        """SELECT subject_id,source_profile FROM core.sources
           WHERE id=%s AND namespace_id=%s FOR UPDATE""",
        (source_id, namespace_id),
    ).fetchone()
    if target is None or source is None:
        return None
    if target[0] != "profile_persona":
        raise ValueError("SUBJECT_SOURCE_TARGET_INVALID")
    previous_subject_id = source[0]
    connection.execute(
        """INSERT INTO core.subject_sources(source_id,subject_id,mapping_origin)
           VALUES (%s,%s,'manual') ON CONFLICT(source_id) DO UPDATE SET
             subject_id=excluded.subject_id,mapping_origin='manual',updated_at=now()""",
        (source_id, subject_id),
    )
    connection.execute(
        "UPDATE core.sources SET subject_id=%s WHERE id=%s", (subject_id, source_id)
    )
    _audit(
        connection,
        namespace_id=namespace_id,
        subject_id=subject_id,
        actor_id=actor_id,
        action="subject.source.assign",
        reason=reason,
        correlation_id=correlation_id,
        metadata={
            "source_id": str(source_id),
            "source_profile": source[1],
            "previous_subject_id": str(previous_subject_id) if previous_subject_id else None,
        },
    )
    return next(
        item for item in list_subjects(connection, namespace_key)
        if item["id"] == subject_id
    )


def reset_source_subject_mapping(
    connection: Connection,
    *,
    namespace_key: str,
    subject_id: UUID,
    source_id: UUID,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    source = connection.execute(
        """SELECT source_profile,subject_id FROM core.sources
           WHERE id=%s AND namespace_id=%s FOR UPDATE""",
        (source_id, namespace_id),
    ).fetchone()
    if source is None or source[1] != subject_id:
        return None
    automatic_subject_id = ensure_profile_subject(connection, namespace_id, source[0])
    connection.execute(
        """INSERT INTO core.subject_sources(source_id,subject_id,mapping_origin)
           VALUES (%s,%s,'automatic') ON CONFLICT(source_id) DO UPDATE SET
             subject_id=excluded.subject_id,mapping_origin='automatic',updated_at=now()""",
        (source_id, automatic_subject_id),
    )
    connection.execute(
        "UPDATE core.sources SET subject_id=%s WHERE id=%s",
        (automatic_subject_id, source_id),
    )
    _audit(
        connection,
        namespace_id=namespace_id,
        subject_id=automatic_subject_id,
        actor_id=actor_id,
        action="subject.source.reset",
        reason=reason,
        correlation_id=correlation_id,
        metadata={"source_id": str(source_id), "previous_subject_id": str(subject_id)},
    )
    return next(
        item for item in list_subjects(connection, namespace_key)
        if item["id"] == automatic_subject_id
    )


def _audit(
    connection: Connection,
    *,
    namespace_id: UUID,
    subject_id: UUID,
    actor_id: str,
    action: str,
    reason: str,
    correlation_id: UUID,
    metadata: dict,
) -> None:
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,reason,
             correlation_id,metadata_redacted
           ) VALUES (%s,%s,'user',%s,%s,'subject',%s,%s,%s,%s::jsonb)""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            action,
            subject_id,
            reason,
            correlation_id,
            json.dumps(metadata, ensure_ascii=False),
        ),
    )
