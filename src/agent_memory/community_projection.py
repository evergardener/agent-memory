from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .community_evaluation import (
    EVALUATION_VERSION,
    ProjectedCommunity,
    RelationEdge,
    weighted_core_expansion,
)
from .ids import new_uuid, stable_uuid

PROJECTION_VERSION = "community-projection-v1"
FAMILY_NAMES = {
    "communication": "邮件通信星系",
    "data": "数据依赖星系",
    "observability": "日志告警星系",
    "other": "关系星系",
}


@dataclass(frozen=True)
class PersistedRelation:
    id: UUID
    edge: RelationEdge
    fact_ids: tuple[UUID, ...]
    evidence_by_fact: tuple[tuple[UUID, UUID], ...]


@dataclass(frozen=True)
class RebuildResult:
    namespace_id: UUID
    input_snapshot_hash: str
    galaxy_count: int
    membership_count: int
    evidence_link_count: int


def _relation_rows(connection: Connection, namespace_id: UUID) -> list[dict[str, Any]]:
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT relation.id,
                      COALESCE(source.canonical_entity_id,source.id) AS source_entity_id,
                      COALESCE(target.canonical_entity_id,target.id) AS target_entity_id,
                      relation.relation_type,relation.transport,relation.confidence,
                      relation.lifecycle_state
               FROM memory.entity_relations relation
               JOIN memory.entities source ON source.id=relation.source_entity_id
               JOIN memory.entities target ON target.id=relation.target_entity_id
               WHERE relation.namespace_id=%s
                 AND relation.lifecycle_state='active'
                 AND (relation.valid_from IS NULL OR relation.valid_from <= now())
                 AND (relation.valid_to IS NULL OR relation.valid_to > now())
                 AND NOT EXISTS (
                   SELECT 1 FROM core.subjects subject
                   WHERE subject.entity_id IN (
                     COALESCE(source.canonical_entity_id,source.id),
                     COALESCE(target.canonical_entity_id,target.id)
                   )
                 )
               ORDER BY relation.id""",
            (namespace_id,),
        )
        return list(cursor.fetchall())


def _relation_support(
    connection: Connection, namespace_id: UUID
) -> dict[UUID, list[dict[str, Any]]]:
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT relation.id AS relation_id,support.fact_id,fact_evidence.event_id,
                      session.id AS session_id
               FROM memory.entity_relations relation
               JOIN memory.relation_facts support
                 ON support.relation_id=relation.id AND support.support_kind='support'
               JOIN memory.facts fact
                 ON fact.id=support.fact_id
                AND fact.memory_state IN ('active','dormant')
               JOIN memory.fact_evidence fact_evidence
                 ON fact_evidence.fact_id=fact.id
                AND fact_evidence.support_kind='support'
               JOIN evidence.events event ON event.id=fact_evidence.event_id
               JOIN core.turns turn ON turn.id=event.turn_id
               JOIN core.sessions session ON session.id=turn.session_id
               WHERE relation.namespace_id=%s
                 AND relation.lifecycle_state='active'
               ORDER BY relation.id,support.fact_id,fact_evidence.event_id""",
            (namespace_id,),
        )
        grouped: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        for row in cursor.fetchall():
            grouped[row["relation_id"]].append(row)
        return grouped


def load_persisted_relations(
    connection: Connection, namespace_id: UUID
) -> tuple[PersistedRelation, ...]:
    support = _relation_support(connection, namespace_id)
    records: list[PersistedRelation] = []
    for row in _relation_rows(connection, namespace_id):
        source_id = row["source_entity_id"]
        target_id = row["target_entity_id"]
        if source_id == target_id:
            continue
        relation_support = support.get(row["id"], [])
        fact_ids = tuple(sorted({item["fact_id"] for item in relation_support}, key=str))
        evidence_by_fact = tuple(
            sorted(
                {
                    (item["fact_id"], item["event_id"])
                    for item in relation_support
                },
                key=lambda item: (str(item[0]), str(item[1])),
            )
        )
        evidence_refs = tuple(
            sorted({str(item["event_id"]) for item in relation_support})
        )
        sessions = {item["session_id"] for item in relation_support}
        records.append(
            PersistedRelation(
                id=row["id"],
                edge=RelationEdge(
                    source=str(source_id),
                    target=str(target_id),
                    relation_type=row["relation_type"],
                    transport=row["transport"],
                    evidence_refs=evidence_refs,
                    session_count=len(sessions),
                    confidence=float(row["confidence"]),
                    recency_weight=1.0,
                    eligible=bool(evidence_refs),
                ),
                fact_ids=fact_ids,
                evidence_by_fact=evidence_by_fact,
            )
        )
    return tuple(sorted(records, key=lambda item: str(item.id)))


def relation_snapshot_hash(records: tuple[PersistedRelation, ...]) -> str:
    payload = [
        {
            "id": str(record.id),
            "source": record.edge.source,
            "target": record.edge.target,
            "type": record.edge.relation_type,
            "transport": record.edge.transport,
            "confidence": record.edge.confidence,
            "facts": [str(value) for value in record.fact_ids],
            "evidence": list(record.edge.evidence_refs),
            "sessions": record.edge.session_count,
        }
        for record in records
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def enqueue_community_rebuild(
    connection: Connection,
    namespace_id: UUID,
    *,
    reason_key: str,
) -> UUID:
    snapshot_hash = relation_snapshot_hash(load_persisted_relations(connection, namespace_id))
    idempotency_key = (
        f"rebuild_communities:{PROJECTION_VERSION}:{namespace_id}:"
        f"{snapshot_hash}:{reason_key}"
    )
    job_id = stable_uuid("job", idempotency_key)
    connection.execute(
        """INSERT INTO ops.jobs(
             id,namespace_id,kind,idempotency_key,input_ref,input_version
           ) VALUES (%s,%s,'rebuild_communities',%s,%s,1)
           ON CONFLICT DO NOTHING""",
        (job_id, namespace_id, idempotency_key, namespace_id),
    )
    return job_id


def _member_scores(
    community: ProjectedCommunity,
    records_by_algorithm_id: dict[str, PersistedRelation],
) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    for algorithm_relation_id in community.relation_ids:
        record = records_by_algorithm_id[algorithm_relation_id]
        scores[record.edge.source] += record.edge.weight
        scores[record.edge.target] += record.edge.weight
    return dict(scores)


def _load_governance(connection: Connection, namespace_id: UUID) -> tuple[set, set]:
    rows = connection.execute(
        """SELECT galaxy_id,entity_id,governance_state,membership_kind
           FROM projection.galaxy_memberships WHERE namespace_id=%s
             AND governance_state IN ('fixed','excluded')""",
        (namespace_id,),
    ).fetchall()
    excluded = {
        (row[0], row[1]) for row in rows if row[2] == "excluded"
    }
    fixed_primary = {
        row[1] for row in rows if row[2] == "fixed" and row[3] == "primary"
    }
    return excluded, fixed_primary


def _upsert_galaxy(
    connection: Connection,
    namespace_id: UUID,
    community: ProjectedCommunity,
    snapshot_hash: str,
) -> UUID:
    galaxy_id = stable_uuid("galaxy", f"{namespace_id}:{community.community_id}")
    roles = dict(community.roles)
    anchor_ids = [
        UUID(member) for member in community.members if roles.get(member) == "core"
    ]
    if not anchor_ids:
        anchor_ids = [UUID(community.members[0])]
    anchor_rows = connection.execute(
        """SELECT canonical_name FROM memory.entities
           WHERE namespace_id=%s AND id=ANY(%s::uuid[])
           ORDER BY canonical_name LIMIT 2""",
        (namespace_id, anchor_ids),
    ).fetchall()
    anchors = " · ".join(row[0] for row in anchor_rows)
    suffix = FAMILY_NAMES.get(community.family, FAMILY_NAMES["other"])
    display_name = f"{anchors} {suffix}" if anchors else suffix
    connection.execute(
        """INSERT INTO projection.galaxies(
             id,namespace_id,stable_key,family,display_name,name_origin,origin,
             algorithm_version,input_snapshot_hash
           ) VALUES (%s,%s,%s,%s,%s,'automatic','automatic',%s,%s)
           ON CONFLICT(namespace_id,stable_key) DO UPDATE SET
             family=excluded.family,
             display_name=CASE
               WHEN projection.galaxies.name_origin='manual'
                 THEN projection.galaxies.display_name
               ELSE excluded.display_name
             END,
             algorithm_version=excluded.algorithm_version,
             input_snapshot_hash=excluded.input_snapshot_hash,
             lifecycle_state='active',updated_at=now()
           WHERE projection.galaxies.family IS DISTINCT FROM excluded.family
              OR projection.galaxies.algorithm_version IS DISTINCT FROM excluded.algorithm_version
              OR projection.galaxies.input_snapshot_hash
                   IS DISTINCT FROM excluded.input_snapshot_hash
              OR projection.galaxies.lifecycle_state <> 'active'
              OR (
                projection.galaxies.name_origin='automatic'
                AND projection.galaxies.display_name IS DISTINCT FROM excluded.display_name
              )""",
        (
            galaxy_id,
            namespace_id,
            community.community_id,
            community.family,
            display_name,
            EVALUATION_VERSION,
            snapshot_hash,
        ),
    )
    return galaxy_id


def rebuild_communities(
    connection: Connection,
    namespace_id: UUID,
    *,
    actor_id: str = "community-worker",
) -> RebuildResult:
    records = load_persisted_relations(connection, namespace_id)
    snapshot_hash = relation_snapshot_hash(records)
    communities = weighted_core_expansion(record.edge for record in records)
    records_by_algorithm_id = {record.edge.relation_id: record for record in records}

    projected: list[tuple[ProjectedCommunity, UUID, dict[str, float]]] = []
    for community in communities:
        galaxy_id = _upsert_galaxy(connection, namespace_id, community, snapshot_hash)
        projected.append(
            (community, galaxy_id, _member_scores(community, records_by_algorithm_id))
        )
    active_galaxy_ids = {item[1] for item in projected}

    existing_galaxies = connection.execute(
        """SELECT id FROM projection.galaxies
           WHERE namespace_id=%s AND origin='automatic' AND manual_locked=false""",
        (namespace_id,),
    ).fetchall()
    for (galaxy_id,) in existing_galaxies:
        if galaxy_id not in active_galaxy_ids:
            connection.execute(
                """UPDATE projection.galaxies
                   SET lifecycle_state='inactive',updated_at=now()
                   WHERE id=%s AND lifecycle_state <> 'inactive'""",
                (galaxy_id,),
            )

    excluded, fixed_primary = _load_governance(connection, namespace_id)
    candidates_by_entity: dict[UUID, list[tuple[float, UUID]]] = defaultdict(list)
    desired: dict[tuple[UUID, UUID], dict[str, Any]] = {}
    for community, galaxy_id, scores in projected:
        roles = dict(community.roles)
        for member in community.members:
            entity_id = UUID(member)
            if (galaxy_id, entity_id) in excluded:
                continue
            score = scores.get(member, 0.0)
            candidates_by_entity[entity_id].append((score, galaxy_id))
            desired[(galaxy_id, entity_id)] = {
                "role": roles[member],
                "weight": score,
                "kind": "secondary",
                "community": community,
            }
    for entity_id, candidates in candidates_by_entity.items():
        if entity_id in fixed_primary:
            continue
        _score, galaxy_id = max(candidates, key=lambda item: (item[0], str(item[1])))
        desired[(galaxy_id, entity_id)]["kind"] = "primary"

    existing_rows = connection.execute(
        """SELECT galaxy_id,entity_id,membership_kind
           FROM projection.galaxy_memberships
           WHERE namespace_id=%s AND governance_state='automatic'""",
        (namespace_id,),
    ).fetchall()
    existing = {(row[0], row[1]): row[2] for row in existing_rows}
    removed_memberships = sorted(
        existing.keys() - desired.keys(),
        key=lambda item: (str(item[0]), str(item[1])),
    )
    for key in removed_memberships:
        connection.execute(
            """DELETE FROM projection.galaxy_memberships
               WHERE galaxy_id=%s AND entity_id=%s AND governance_state='automatic'""",
            key,
        )
    for key, current_kind in existing.items():
        if key in desired and current_kind == "primary" and desired[key]["kind"] != "primary":
            connection.execute(
                """UPDATE projection.galaxy_memberships
                   SET membership_kind='secondary',updated_at=now()
                   WHERE galaxy_id=%s AND entity_id=%s
                     AND governance_state='automatic'""",
                key,
            )

    for (galaxy_id, entity_id), values in sorted(
        desired.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
    ):
        connection.execute(
            """INSERT INTO projection.galaxy_memberships(
                 galaxy_id,namespace_id,entity_id,role,membership_kind,weight,
                 governance_state,algorithm_version
               ) VALUES (%s,%s,%s,%s,%s,%s,'automatic',%s)
               ON CONFLICT(galaxy_id,entity_id) DO UPDATE SET
                 role=excluded.role,membership_kind=excluded.membership_kind,
                 weight=excluded.weight,algorithm_version=excluded.algorithm_version,
                 updated_at=now()
               WHERE projection.galaxy_memberships.governance_state='automatic'
                 AND (
                   projection.galaxy_memberships.role IS DISTINCT FROM excluded.role
                   OR projection.galaxy_memberships.membership_kind
                        IS DISTINCT FROM excluded.membership_kind
                   OR projection.galaxy_memberships.weight IS DISTINCT FROM excluded.weight
                   OR projection.galaxy_memberships.algorithm_version
                        IS DISTINCT FROM excluded.algorithm_version
                 )""",
            (
                galaxy_id,
                namespace_id,
                entity_id,
                values["role"],
                values["kind"],
                values["weight"],
                EVALUATION_VERSION,
            ),
        )

    desired_evidence: set[tuple[UUID, UUID, UUID, UUID, UUID]] = set()
    for (galaxy_id, entity_id), values in desired.items():
        community: ProjectedCommunity = values["community"]
        for algorithm_relation_id in community.relation_ids:
            record = records_by_algorithm_id[algorithm_relation_id]
            if str(entity_id) not in (record.edge.source, record.edge.target):
                continue
            for fact_id, event_id in record.evidence_by_fact:
                desired_evidence.add(
                    (galaxy_id, entity_id, record.id, fact_id, event_id)
                )
    existing_evidence_rows = connection.execute(
        """SELECT evidence.galaxy_id,evidence.entity_id,evidence.relation_id,
                  evidence.fact_id,evidence.event_id
           FROM projection.galaxy_membership_evidence evidence
           JOIN projection.galaxy_memberships membership
             ON membership.galaxy_id=evidence.galaxy_id
            AND membership.entity_id=evidence.entity_id
           WHERE membership.namespace_id=%s
             AND evidence.origin='automatic'
             AND membership.governance_state='automatic'""",
        (namespace_id,),
    ).fetchall()
    existing_evidence = {tuple(row) for row in existing_evidence_rows}
    for item in sorted(
        existing_evidence - desired_evidence,
        key=lambda value: tuple(str(part) for part in value),
    ):
        connection.execute(
            """DELETE FROM projection.galaxy_membership_evidence
               WHERE galaxy_id=%s AND entity_id=%s AND relation_id=%s
                 AND fact_id=%s AND event_id=%s AND origin='automatic'""",
            item,
        )
    for item in sorted(
        desired_evidence - existing_evidence,
        key=lambda value: tuple(str(part) for part in value),
    ):
        galaxy_id, entity_id, relation_id, fact_id, event_id = item
        evidence_id = stable_uuid(
            "galaxy-membership-evidence",
            ":".join(str(value) for value in item),
        )
        connection.execute(
            """INSERT INTO projection.galaxy_membership_evidence(
                 id,galaxy_id,entity_id,relation_id,fact_id,event_id,origin
               ) VALUES (%s,%s,%s,%s,%s,%s,'automatic')
               ON CONFLICT(id) DO NOTHING""",
            (
                evidence_id,
                galaxy_id,
                entity_id,
                relation_id,
                fact_id,
                event_id,
            ),
        )

    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             correlation_id,metadata_redacted
           ) VALUES (
             %s,%s,'worker',%s,'projection.communities.rebuild','namespace',%s,%s,%s::jsonb
           )""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            namespace_id,
            new_uuid(),
            json.dumps(
                {
                    "algorithm_version": EVALUATION_VERSION,
                    "projection_version": PROJECTION_VERSION,
                    "input_snapshot_hash": snapshot_hash,
                    "galaxy_count": len(communities),
                    "membership_count": len(desired),
                    "evidence_link_count": len(desired_evidence),
                },
                sort_keys=True,
            ),
        ),
    )
    return RebuildResult(
        namespace_id=namespace_id,
        input_snapshot_hash=snapshot_hash,
        galaxy_count=len(communities),
        membership_count=len(desired),
        evidence_link_count=len(desired_evidence),
    )
