import os
import unittest
from datetime import UTC, datetime
from uuid import uuid4

import psycopg

from agent_memory.community_projection import rebuild_communities
from agent_memory.galaxies import (
    create_manual_galaxy,
    list_galaxies,
    list_layout_preferences,
    request_rebuild,
    save_layout_preference,
    undo_last_galaxy_change,
    update_galaxy,
    update_membership,
)
from agent_memory.ids import stable_uuid
from agent_memory.schemas import (
    GalaxyCreateRequest,
    GalaxyMembershipRequest,
    GalaxyUndoRequest,
    GalaxyUpdateRequest,
    LayoutPreferenceRequest,
    ProviderContext,
)
from agent_memory.worker import claim_job, process_one


def _seed_relation_fixture(connection):
    suffix = uuid4().hex
    namespace_id = stable_uuid("namespace", f"community-projection-test:{suffix}")
    connection.execute(
        "INSERT INTO core.namespaces(id,stable_key) VALUES (%s,%s)",
        (namespace_id, f"community-projection-test:{suffix}"),
    )
    source_id = stable_uuid("source", suffix)
    connection.execute(
        """INSERT INTO core.sources(
             id,namespace_id,source_profile,source_instance
           ) VALUES (%s,%s,'test','community-projection-test')""",
        (source_id, namespace_id),
    )

    entities = {}
    for name, entity_type in (
        ("PostgreSQL", "service"),
        ("Hindsight", "service"),
        ("Honcho", "service"),
        ("Alloy", "service"),
        ("Loki", "service"),
    ):
        entity_id = stable_uuid("entity", f"{namespace_id}:{name.casefold()}")
        entities[name] = entity_id
        connection.execute(
            """INSERT INTO memory.entities(
                 id,namespace_id,entity_type,canonical_name,normalized_name
               ) VALUES (%s,%s,%s,%s,%s)""",
            (entity_id, namespace_id, entity_type, name, name.casefold()),
        )

    relation_specs = (
        ("Hindsight", "PostgreSQL", "uses_database"),
        ("Honcho", "PostgreSQL", "uses_database"),
        ("Alloy", "Loki", "pushes_logs_to"),
        ("Alloy", "Hindsight", "collects_logs_from"),
    )
    event_hashes = []
    for index, (source_name, target_name, relation_type) in enumerate(relation_specs):
        session_id = stable_uuid("session", f"{suffix}:{index}")
        turn_id = stable_uuid("turn", f"{suffix}:{index}")
        event_id = stable_uuid("event", f"{suffix}:{index}")
        fact_id = stable_uuid("fact", f"{suffix}:{index}")
        relation_id = stable_uuid("entity-relation", f"{suffix}:{index}")
        payload_hash = f"fixture-hash-{suffix}-{index}"
        event_hashes.append((event_id, payload_hash))
        connection.execute(
            """INSERT INTO core.sessions(
                 id,namespace_id,source_id,external_session_id,started_at
               ) VALUES (%s,%s,%s,%s,%s)""",
            (session_id, namespace_id, source_id, f"session-{index}", datetime.now(UTC)),
        )
        connection.execute(
            """INSERT INTO core.turns(
                 id,session_id,external_turn_id,occurred_at
               ) VALUES (%s,%s,%s,%s)""",
            (turn_id, session_id, f"turn-{index}", datetime.now(UTC)),
        )
        connection.execute(
            """INSERT INTO evidence.events(
                 id,namespace_id,turn_id,event_type,sequence_no,redacted_payload,
                 payload_hash,ingest_key,occurred_at
               ) VALUES (%s,%s,%s,'environment_observation',1,%s::jsonb,%s,%s,%s)""",
            (
                event_id,
                namespace_id,
                turn_id,
                '{"content":"typed relation fixture"}',
                payload_hash,
                f"fixture-{suffix}-{index}",
                datetime.now(UTC),
            ),
        )
        connection.execute(
            """INSERT INTO memory.facts(
                 id,namespace_id,statement,fact_type,confidence,memory_state,source_profile
               ) VALUES (%s,%s,%s,'observed',1,'active','test')""",
            (fact_id, namespace_id, f"{source_name} {relation_type} {target_name}"),
        )
        connection.execute(
            "INSERT INTO memory.fact_evidence(fact_id,event_id) VALUES (%s,%s)",
            (fact_id, event_id),
        )
        connection.execute(
            """INSERT INTO memory.entity_relations(
                 id,namespace_id,source_entity_id,target_entity_id,relation_type,
                 transport,confidence,lifecycle_state,origin,extractor_version
               ) VALUES (%s,%s,%s,%s,%s,'lan_direct',1,'active','manual','fixture-v1')""",
            (
                relation_id,
                namespace_id,
                entities[source_name],
                entities[target_name],
                relation_type,
            ),
        )
        connection.execute(
            "INSERT INTO memory.relation_facts(relation_id,fact_id) VALUES (%s,%s)",
            (relation_id, fact_id),
        )
    return namespace_id, entities, tuple(event_hashes)


def _context(namespace_key: str) -> ProviderContext:
    marker = uuid4().hex
    return ProviderContext(
        shared_namespace=namespace_key,
        source_profile="test-user",
        source_instance="community-projection-test",
        external_session_id=f"session-{marker}",
        external_turn_id=f"turn-{marker}",
        correlation_id=uuid4(),
    )


@unittest.skipUnless(
    os.getenv("AGENT_MEMORY_DATABASE_URL"),
    "set AGENT_MEMORY_DATABASE_URL to an isolated migrated database",
)
def test_rebuild_is_idempotent_overlapping_governed_and_evidence_immutable():
    with psycopg.connect(os.environ["AGENT_MEMORY_DATABASE_URL"]) as connection:
        namespace_id, entities, event_hashes = _seed_relation_fixture(connection)
        first = rebuild_communities(connection, namespace_id)
        assert first.galaxy_count == 2
        assert first.membership_count == 6
        assert first.evidence_link_count == 8

        overlap_count = connection.execute(
            """SELECT count(*) FROM projection.galaxy_memberships
               WHERE namespace_id=%s AND entity_id=%s
                 AND governance_state <> 'excluded'""",
            (namespace_id, entities["Hindsight"]),
        ).fetchone()[0]
        assert overlap_count == 2

        first_counts = connection.execute(
            """SELECT
                 (SELECT count(*) FROM projection.galaxies WHERE namespace_id=%s),
                 (SELECT count(*) FROM projection.galaxy_memberships WHERE namespace_id=%s),
                 (SELECT count(*) FROM projection.galaxy_membership_evidence evidence
                    JOIN projection.galaxy_memberships membership
                      ON membership.galaxy_id=evidence.galaxy_id
                     AND membership.entity_id=evidence.entity_id
                    WHERE membership.namespace_id=%s),
                 (SELECT max(updated_at) FROM projection.galaxy_memberships
                    WHERE namespace_id=%s)""",
            (namespace_id, namespace_id, namespace_id, namespace_id),
        ).fetchone()
        second = rebuild_communities(connection, namespace_id)
        second_counts = connection.execute(
            """SELECT
                 (SELECT count(*) FROM projection.galaxies WHERE namespace_id=%s),
                 (SELECT count(*) FROM projection.galaxy_memberships WHERE namespace_id=%s),
                 (SELECT count(*) FROM projection.galaxy_membership_evidence evidence
                    JOIN projection.galaxy_memberships membership
                      ON membership.galaxy_id=evidence.galaxy_id
                     AND membership.entity_id=evidence.entity_id
                    WHERE membership.namespace_id=%s),
                 (SELECT max(updated_at) FROM projection.galaxy_memberships
                    WHERE namespace_id=%s)""",
            (namespace_id, namespace_id, namespace_id, namespace_id),
        ).fetchone()
        assert second == first
        assert second_counts == first_counts

        data_galaxy_id = connection.execute(
            """SELECT id FROM projection.galaxies
               WHERE namespace_id=%s AND family='data'""",
            (namespace_id,),
        ).fetchone()[0]
        connection.execute(
            """UPDATE projection.galaxies
               SET display_name='我的数据基础设施',name_origin='manual',manual_locked=true
               WHERE id=%s""",
            (data_galaxy_id,),
        )
        connection.execute(
            """UPDATE projection.galaxy_memberships
               SET governance_state='fixed'
               WHERE galaxy_id=%s AND entity_id=%s""",
            (data_galaxy_id, entities["PostgreSQL"]),
        )
        excluded_entity_id = entities["Honcho"]
        connection.execute(
            """UPDATE projection.galaxy_memberships
               SET governance_state='excluded',membership_kind='secondary'
               WHERE galaxy_id=%s AND entity_id=%s""",
            (data_galaxy_id, excluded_entity_id),
        )
        layout_id = stable_uuid("layout", f"{namespace_id}:{data_galaxy_id}")
        connection.execute(
            """INSERT INTO projection.layout_preferences(
                 id,namespace_id,scope_kind,scope_id,target_kind,target_id,
                 position,motion_enabled,pinned
               ) VALUES (%s,%s,'universe',%s,'galaxy',%s,%s::jsonb,false,true)""",
            (
                layout_id,
                namespace_id,
                namespace_id,
                data_galaxy_id,
                '{"x":12,"y":24}',
            ),
        )

        governed = rebuild_communities(connection, namespace_id)
        assert governed.input_snapshot_hash == first.input_snapshot_hash
        galaxy_governance = connection.execute(
            """SELECT display_name,name_origin,manual_locked
               FROM projection.galaxies WHERE id=%s""",
            (data_galaxy_id,),
        ).fetchone()
        assert galaxy_governance == ("我的数据基础设施", "manual", True)
        membership_states = dict(
            connection.execute(
                """SELECT entity_id,governance_state
                   FROM projection.galaxy_memberships WHERE galaxy_id=%s""",
                (data_galaxy_id,),
            ).fetchall()
        )
        assert membership_states[entities["PostgreSQL"]] == "fixed"
        assert membership_states[excluded_entity_id] == "excluded"
        assert connection.execute(
            "SELECT count(*) FROM projection.layout_preferences WHERE id=%s",
            (layout_id,),
        ).fetchone()[0] == 1

        after_hashes = tuple(
            connection.execute(
                """SELECT id,payload_hash FROM evidence.events
                   WHERE namespace_id=%s ORDER BY id""",
                (namespace_id,),
            ).fetchall()
        )
        assert after_hashes == tuple(sorted(event_hashes, key=lambda item: str(item[0])))


@unittest.skipUnless(
    os.getenv("AGENT_MEMORY_DATABASE_URL"),
    "set AGENT_MEMORY_DATABASE_URL to an isolated migrated database",
)
def test_expired_community_job_lease_is_reclaimed_and_completed():
    with psycopg.connect(os.environ["AGENT_MEMORY_DATABASE_URL"]) as connection:
        namespace_id, _entities, _event_hashes = _seed_relation_fixture(connection)
        namespace_key = connection.execute(
            "SELECT stable_key FROM core.namespaces WHERE id=%s",
            (namespace_id,),
        ).fetchone()[0]
        job_id = stable_uuid("job", f"expired-community-job:{namespace_id}")
        connection.execute(
            """INSERT INTO ops.jobs(
                 id,namespace_id,kind,idempotency_key,input_ref,status,lease_until,
                 attempt_count
               ) VALUES (
                 %s,%s,'rebuild_communities',%s,%s,'running',now() - interval '1 minute',1
               )""",
            (job_id, namespace_id, f"expired-community-job:{namespace_id}", namespace_id),
        )
        connection.commit()

        job = claim_job(connection, 60, "core", namespace_key)
        assert job is not None
        assert job[0] == job_id
        assert job[2] == "rebuild_communities"
        connection.commit()
        process_one(connection, job)

        status, attempts = connection.execute(
            """SELECT status,attempt_count FROM ops.jobs WHERE id=%s""",
            (job_id,),
        ).fetchone()
        assert status == "done"
        assert attempts == 2
        assert connection.execute(
            "SELECT count(*) FROM ops.job_attempts WHERE job_id=%s AND result='done'",
            (job_id,),
        ).fetchone()[0] == 1


@unittest.skipUnless(
    os.getenv("AGENT_MEMORY_DATABASE_URL"),
    "set AGENT_MEMORY_DATABASE_URL to an isolated migrated database",
)
def test_governance_uses_versions_audit_namespace_and_preserves_layout():
    with psycopg.connect(os.environ["AGENT_MEMORY_DATABASE_URL"]) as connection:
        namespace_id, entities, _event_hashes = _seed_relation_fixture(connection)
        namespace_key = connection.execute(
            "SELECT stable_key FROM core.namespaces WHERE id=%s",
            (namespace_id,),
        ).fetchone()[0]
        rebuild_communities(connection, namespace_id)
        galaxies = list_galaxies(connection, namespace_key)
        assert len(galaxies) == 2
        assert all("secret_value" not in repr(item) for item in galaxies)

        context = _context(namespace_key)
        manual = create_manual_galaxy(
            connection,
            GalaxyCreateRequest(
                context=context,
                display_name="人工基础设施",
                family="infrastructure",
                entity_ids=[entities["PostgreSQL"], entities["Hindsight"], entities["Loki"]],
                reason="integration governance fixture",
            ),
        )
        assert manual["origin"] == "manual"
        assert {member["role"] for member in manual["members"]} == {"member"}

        updated = update_galaxy(
            connection,
            manual["id"],
            GalaxyUpdateRequest(
                context=context,
                expected_version=manual["version"],
                display_name="人工基础设施 · 已确认",
                visibility="hidden",
                reason="verify optimistic update",
            ),
        )
        assert updated["version"] == manual["version"] + 1
        assert updated["display_name"] == "人工基础设施 · 已确认"
        with unittest.TestCase().assertRaisesRegex(ValueError, "VERSION_CONFLICT"):
            update_galaxy(
                connection,
                manual["id"],
                GalaxyUpdateRequest(
                    context=context,
                    expected_version=manual["version"],
                    visibility="visible",
                    reason="stale write must fail",
                ),
            )

        excluded = update_membership(
            connection,
            manual["id"],
            entities["Loki"],
            GalaxyMembershipRequest(
                context=context,
                expected_version=updated["version"],
                action="excluded",
                role="member",
                membership_kind="secondary",
                reason="verify exclusion",
            ),
        )
        assert next(
            item
            for item in excluded["members"]
            if item["entity_id"] == entities["Loki"]
        )["governance_state"] == "excluded"
        undone = undo_last_galaxy_change(
            connection,
            manual["id"],
            GalaxyUndoRequest(
                context=context,
                expected_version=excluded["version"],
                reason="verify undo",
            ),
        )
        assert next(
            item
            for item in undone["members"]
            if item["entity_id"] == entities["Loki"]
        )["governance_state"] == "fixed"

        layout = save_layout_preference(
            connection,
            LayoutPreferenceRequest(
                context=context,
                scope_kind="universe",
                scope_id=namespace_id,
                target_kind="galaxy",
                target_id=manual["id"],
                position={"x": 10.0, "y": 20.0},
                motion_enabled=False,
                pinned=True,
                reason="verify layout persistence",
            ),
        )
        assert layout["version"] == 1
        assert layout["id"] in {
            item["id"] for item in list_layout_preferences(connection, namespace_key)
        }
        with unittest.TestCase().assertRaisesRegex(ValueError, "EXPECTED_VERSION_REQUIRED"):
            save_layout_preference(
                connection,
                LayoutPreferenceRequest(
                    context=context,
                    scope_kind="universe",
                    scope_id=namespace_id,
                    target_kind="galaxy",
                    target_id=manual["id"],
                    position={"x": 30.0, "y": 40.0},
                    reason="missing optimistic version",
                ),
            )

        job_id = request_rebuild(
            connection,
            namespace_key=namespace_key,
            actor_id=context.source_profile,
            reason="verify audited rebuild request",
            correlation_id=context.correlation_id,
        )
        assert connection.execute(
            """SELECT count(*) FROM ops.jobs
               WHERE id=%s AND kind='rebuild_communities'""",
            (job_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            """SELECT count(*) FROM audit.events
               WHERE namespace_id=%s AND action LIKE 'projection.%%'""",
            (namespace_id,),
        ).fetchone()[0] >= 6

        other_key = f"other-{uuid4().hex}"
        other_id = stable_uuid("namespace", other_key)
        connection.execute(
            "INSERT INTO core.namespaces(id,stable_key) VALUES (%s,%s)",
            (other_id, other_key),
        )
        assert list_galaxies(connection, other_key) == []


if __name__ == "__main__":
    test_rebuild_is_idempotent_overlapping_governed_and_evidence_immutable()
    test_expired_community_job_lease_is_reclaimed_and_completed()
    test_governance_uses_versions_audit_namespace_and_preserves_layout()
    print("community projection database verification: PASS")
