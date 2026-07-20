"""Add typed entity relations and governed galaxy projections."""

from alembic import op

revision = "0013_relation_galaxies"
down_revision = "0012_subject_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS projection;

        CREATE TABLE memory.entity_aliases (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          entity_id uuid NOT NULL REFERENCES memory.entities(id) ON DELETE CASCADE,
          alias text NOT NULL,
          normalized_alias text NOT NULL,
          origin text NOT NULL DEFAULT 'automatic'
            CHECK (origin IN ('automatic','manual','imported')),
          confidence double precision NOT NULL DEFAULT 1
            CHECK (confidence BETWEEN 0 AND 1),
          review_state text NOT NULL DEFAULT 'accepted'
            CHECK (review_state IN ('candidate','accepted','rejected')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id,normalized_alias)
        );
        CREATE INDEX entity_aliases_entity
          ON memory.entity_aliases(namespace_id,entity_id);

        CREATE TABLE memory.entity_relations (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          source_entity_id uuid NOT NULL REFERENCES memory.entities(id) ON DELETE RESTRICT,
          target_entity_id uuid NOT NULL REFERENCES memory.entities(id) ON DELETE RESTRICT,
          relation_type text NOT NULL,
          transport text NOT NULL DEFAULT 'direct',
          confidence double precision NOT NULL DEFAULT 0
            CHECK (confidence BETWEEN 0 AND 1),
          lifecycle_state text NOT NULL DEFAULT 'candidate'
            CHECK (lifecycle_state IN (
              'candidate','active','dormant','forgotten','isolated','superseded'
            )),
          origin text NOT NULL DEFAULT 'automatic'
            CHECK (origin IN ('automatic','manual','imported')),
          extractor_version text,
          version integer NOT NULL DEFAULT 1 CHECK (version > 0),
          valid_from timestamptz,
          valid_to timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK (source_entity_id <> target_entity_id),
          UNIQUE(
            namespace_id,source_entity_id,target_entity_id,relation_type,transport
          )
        );
        CREATE INDEX entity_relations_active
          ON memory.entity_relations(namespace_id,lifecycle_state,relation_type);
        CREATE INDEX entity_relations_source
          ON memory.entity_relations(namespace_id,source_entity_id);
        CREATE INDEX entity_relations_target
          ON memory.entity_relations(namespace_id,target_entity_id);

        CREATE TABLE memory.relation_facts (
          relation_id uuid NOT NULL
            REFERENCES memory.entity_relations(id) ON DELETE CASCADE,
          fact_id uuid NOT NULL REFERENCES memory.facts(id) ON DELETE CASCADE,
          support_kind text NOT NULL DEFAULT 'support'
            CHECK (support_kind IN ('support','contradiction','context')),
          weight double precision NOT NULL DEFAULT 1 CHECK (weight BETWEEN 0 AND 1),
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(relation_id,fact_id)
        );
        CREATE INDEX relation_facts_fact ON memory.relation_facts(fact_id);

        CREATE TABLE projection.galaxies (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          stable_key text NOT NULL,
          family text NOT NULL,
          display_name text NOT NULL,
          name_origin text NOT NULL DEFAULT 'automatic'
            CHECK (name_origin IN ('automatic','manual')),
          origin text NOT NULL DEFAULT 'automatic'
            CHECK (origin IN ('automatic','manual')),
          algorithm_version text NOT NULL,
          input_snapshot_hash text NOT NULL CHECK (length(input_snapshot_hash) = 64),
          lifecycle_state text NOT NULL DEFAULT 'active'
            CHECK (lifecycle_state IN ('active','inactive')),
          visibility text NOT NULL DEFAULT 'visible'
            CHECK (visibility IN ('visible','hidden')),
          manual_locked boolean NOT NULL DEFAULT false,
          version integer NOT NULL DEFAULT 1 CHECK (version > 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id,stable_key)
        );
        CREATE INDEX galaxies_active
          ON projection.galaxies(namespace_id,lifecycle_state,visibility);

        CREATE TABLE projection.galaxy_memberships (
          galaxy_id uuid NOT NULL
            REFERENCES projection.galaxies(id) ON DELETE CASCADE,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          entity_id uuid NOT NULL REFERENCES memory.entities(id) ON DELETE RESTRICT,
          role text NOT NULL CHECK (role IN ('core','bridge','satellite','member')),
          membership_kind text NOT NULL DEFAULT 'secondary'
            CHECK (membership_kind IN ('primary','secondary')),
          weight double precision NOT NULL DEFAULT 0 CHECK (weight >= 0),
          governance_state text NOT NULL DEFAULT 'automatic'
            CHECK (governance_state IN ('automatic','fixed','excluded')),
          algorithm_version text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(galaxy_id,entity_id)
        );
        CREATE INDEX galaxy_memberships_entity
          ON projection.galaxy_memberships(namespace_id,entity_id);
        CREATE UNIQUE INDEX galaxy_memberships_one_primary
          ON projection.galaxy_memberships(namespace_id,entity_id)
          WHERE membership_kind='primary' AND governance_state <> 'excluded';

        CREATE TABLE projection.galaxy_membership_evidence (
          id uuid PRIMARY KEY,
          galaxy_id uuid NOT NULL,
          entity_id uuid NOT NULL,
          relation_id uuid NOT NULL
            REFERENCES memory.entity_relations(id) ON DELETE CASCADE,
          fact_id uuid REFERENCES memory.facts(id) ON DELETE CASCADE,
          event_id uuid REFERENCES evidence.events(id) ON DELETE RESTRICT,
          origin text NOT NULL DEFAULT 'automatic'
            CHECK (origin IN ('automatic','manual')),
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY(galaxy_id,entity_id)
            REFERENCES projection.galaxy_memberships(galaxy_id,entity_id)
            ON DELETE CASCADE,
          UNIQUE(galaxy_id,entity_id,relation_id,fact_id,event_id)
        );
        CREATE INDEX galaxy_membership_evidence_relation
          ON projection.galaxy_membership_evidence(relation_id);
        CREATE INDEX galaxy_membership_evidence_event
          ON projection.galaxy_membership_evidence(event_id);

        CREATE TABLE projection.layout_preferences (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          scope_kind text NOT NULL CHECK (scope_kind IN ('universe','galaxy')),
          scope_id uuid NOT NULL,
          target_kind text NOT NULL CHECK (target_kind IN ('camera','entity','galaxy')),
          target_id uuid NOT NULL,
          position jsonb NOT NULL DEFAULT '{}'::jsonb,
          zoom double precision,
          motion_enabled boolean,
          pinned boolean NOT NULL DEFAULT false,
          version integer NOT NULL DEFAULT 1 CHECK (version > 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id,scope_kind,scope_id,target_kind,target_id)
        );
        CREATE INDEX layout_preferences_scope
          ON projection.layout_preferences(namespace_id,scope_kind,scope_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS projection.layout_preferences;
        DROP TABLE IF EXISTS projection.galaxy_membership_evidence;
        DROP TABLE IF EXISTS projection.galaxy_memberships;
        DROP TABLE IF EXISTS projection.galaxies;
        DROP TABLE IF EXISTS memory.relation_facts;
        DROP TABLE IF EXISTS memory.entity_relations;
        DROP TABLE IF EXISTS memory.entity_aliases;
        DROP SCHEMA IF EXISTS projection;
        """
    )
