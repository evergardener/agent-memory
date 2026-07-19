"""Add user/profile subjects and bind Hermes sources to them."""

from alembic import op

revision = "0012_subject_identity"
down_revision = "0011_entity_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        CREATE TABLE core.subjects (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          entity_id uuid NOT NULL REFERENCES memory.entities(id) ON DELETE RESTRICT,
          kind text NOT NULL CHECK (kind IN ('user','profile_persona')),
          stable_key text NOT NULL,
          display_name text NOT NULL,
          color text NOT NULL,
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','hidden')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id,stable_key),
          UNIQUE(namespace_id,entity_id)
        );

        ALTER TABLE core.sources
          ADD COLUMN subject_id uuid REFERENCES core.subjects(id) ON DELETE RESTRICT;
        CREATE INDEX core_sources_subject ON core.sources(namespace_id,subject_id);

        CREATE TABLE core.subject_sources (
          source_id uuid PRIMARY KEY REFERENCES core.sources(id) ON DELETE CASCADE,
          subject_id uuid NOT NULL REFERENCES core.subjects(id) ON DELETE RESTRICT,
          mapping_origin text NOT NULL DEFAULT 'automatic'
            CHECK (mapping_origin IN ('automatic','manual')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX subject_sources_subject ON core.subject_sources(subject_id);

        INSERT INTO memory.entities(
          id,namespace_id,entity_type,canonical_name,normalized_name
        )
        SELECT (
          substr(md5('agent-memory:subject-entity:' || n.id::text || ':user'),1,8) || '-' ||
          substr(md5('agent-memory:subject-entity:' || n.id::text || ':user'),9,4) || '-' ||
          substr(md5('agent-memory:subject-entity:' || n.id::text || ':user'),13,4) || '-' ||
          substr(md5('agent-memory:subject-entity:' || n.id::text || ':user'),17,4) || '-' ||
          substr(md5('agent-memory:subject-entity:' || n.id::text || ':user'),21,12)
        )::uuid,n.id,'person','User','__subject__:user'
        FROM core.namespaces n
        ON CONFLICT(namespace_id,normalized_name) DO NOTHING;

        INSERT INTO memory.entities(
          id,namespace_id,entity_type,canonical_name,normalized_name
        )
        SELECT (
          substr(digest.value,1,8) || '-' || substr(digest.value,9,4) || '-' ||
          substr(digest.value,13,4) || '-' || substr(digest.value,17,4) || '-' ||
          substr(digest.value,21,12)
        )::uuid,p.namespace_id,'agent','Hermes · ' || p.display_profile,
        '__subject__:profile:' || p.profile_key
        FROM (
          SELECT namespace_id,lower(source_profile) AS profile_key,
                 min(source_profile) AS display_profile
          FROM core.sources GROUP BY namespace_id,lower(source_profile)
        ) p
        CROSS JOIN LATERAL (
          SELECT md5(
            'agent-memory:subject-entity:' || p.namespace_id::text ||
            ':profile:' || p.profile_key
          ) AS value
        ) digest
        ON CONFLICT(namespace_id,normalized_name) DO NOTHING;

        INSERT INTO core.subjects(
          id,namespace_id,entity_id,kind,stable_key,display_name,color
        )
        SELECT (
          substr(md5('agent-memory:subject:' || n.id::text || ':user'),1,8) || '-' ||
          substr(md5('agent-memory:subject:' || n.id::text || ':user'),9,4) || '-' ||
          substr(md5('agent-memory:subject:' || n.id::text || ':user'),13,4) || '-' ||
          substr(md5('agent-memory:subject:' || n.id::text || ':user'),17,4) || '-' ||
          substr(md5('agent-memory:subject:' || n.id::text || ':user'),21,12)
        )::uuid,n.id,e.id,'user','user','User','#efd095'
        FROM core.namespaces n
        JOIN memory.entities e
          ON e.namespace_id=n.id AND e.normalized_name='__subject__:user'
        ON CONFLICT(namespace_id,stable_key) DO NOTHING;

        INSERT INTO core.subjects(
          id,namespace_id,entity_id,kind,stable_key,display_name,color
        )
        SELECT (
          substr(digest.value,1,8) || '-' || substr(digest.value,9,4) || '-' ||
          substr(digest.value,13,4) || '-' || substr(digest.value,17,4) || '-' ||
          substr(digest.value,21,12)
        )::uuid,p.namespace_id,e.id,'profile_persona','profile:' || p.profile_key,
        'Hermes · ' || p.display_profile,'#91cfb2'
        FROM (
          SELECT namespace_id,lower(source_profile) AS profile_key,
                 min(source_profile) AS display_profile
          FROM core.sources GROUP BY namespace_id,lower(source_profile)
        ) p
        CROSS JOIN LATERAL (
          SELECT md5(
            'agent-memory:subject:' || p.namespace_id::text ||
            ':profile:' || p.profile_key
          ) AS value
        ) digest
        JOIN memory.entities e ON e.namespace_id=p.namespace_id
          AND e.normalized_name='__subject__:profile:' || p.profile_key
        ON CONFLICT(namespace_id,stable_key) DO NOTHING;

        UPDATE core.sources source
        SET subject_id=subject.id
        FROM core.subjects subject
        WHERE subject.namespace_id=source.namespace_id
          AND subject.stable_key='profile:' || lower(source.source_profile);

        INSERT INTO core.subject_sources(source_id,subject_id,mapping_origin)
        SELECT id,subject_id,'automatic' FROM core.sources WHERE subject_id IS NOT NULL
        ON CONFLICT(source_id) DO UPDATE SET
          subject_id=excluded.subject_id,mapping_origin='automatic',updated_at=now();
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DROP TABLE core.subject_sources;
        DROP INDEX core.core_sources_subject;
        ALTER TABLE core.sources DROP COLUMN subject_id;
        DROP TABLE core.subjects;
        DELETE FROM memory.entities
        WHERE (
          normalized_name='__subject__:user'
          OR normalized_name LIKE '__subject__:profile:%%'
        )
          AND NOT EXISTS (
            SELECT 1 FROM memory.fact_entities link WHERE link.entity_id=memory.entities.id
          )
          AND NOT EXISTS (
            SELECT 1 FROM memory.entity_mentions mention
            WHERE mention.entity_id=memory.entities.id
          )
          AND NOT EXISTS (
            SELECT 1 FROM memory.episodes episode WHERE episode.entity_id=memory.entities.id
          )
          AND NOT EXISTS (
            SELECT 1 FROM memory.arcs arc WHERE arc.entity_id=memory.entities.id
          );
        """
    )
