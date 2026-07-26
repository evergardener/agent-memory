"""Add unified episodic, temporal, preference, artifact, and procedural memory."""

from alembic import op

revision = "0016_unified_memory"
down_revision = "0015_subject_display_name_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE memory.episodes
          DROP CONSTRAINT episodes_namespace_id_entity_id_key,
          ALTER COLUMN entity_id DROP NOT NULL,
          ADD COLUMN episode_type text NOT NULL DEFAULT 'legacy_derived',
          ADD COLUMN started_at timestamptz,
          ADD COLUMN ended_at timestamptz,
          ADD COLUMN time_precision text NOT NULL DEFAULT 'unknown'
            CHECK (time_precision IN ('instant','day','range','month','year','unknown')),
          ADD COLUMN timezone text,
          ADD COLUMN time_resolution jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN importance double precision NOT NULL DEFAULT 0.5
            CHECK (importance BETWEEN 0 AND 1),
          ADD COLUMN review_state text NOT NULL DEFAULT 'accepted'
            CHECK (review_state IN ('candidate','accepted','rejected')),
          ADD COLUMN origin text NOT NULL DEFAULT 'legacy_derived'
            CHECK (origin IN ('automatic','manual','imported','legacy_derived')),
          ADD COLUMN extractor_version text,
          ADD COLUMN confidence double precision NOT NULL DEFAULT 1
            CHECK (confidence BETWEEN 0 AND 1),
          ADD CONSTRAINT episodes_time_order
            CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at);

        ALTER TABLE projection.layout_preferences
          DROP CONSTRAINT layout_preferences_target_kind_check,
          ADD CONSTRAINT layout_preferences_target_kind_check
            CHECK (target_kind IN ('camera','entity','galaxy','subject'));

        CREATE INDEX episodes_time_type
          ON memory.episodes(namespace_id,episode_type,state,started_at,ended_at);
        CREATE INDEX episodes_anchor ON memory.episodes(namespace_id,entity_id);

        CREATE TABLE memory.episode_entities (
          episode_id uuid NOT NULL REFERENCES memory.episodes(id) ON DELETE CASCADE,
          entity_id uuid REFERENCES memory.entities(id) ON DELETE RESTRICT,
          subject_id uuid REFERENCES core.subjects(id) ON DELETE RESTRICT,
          role text NOT NULL CHECK (role IN (
            'actor','experiencer','participant','subject','location','object',
            'affected','artifact'
          )),
          fact_id uuid REFERENCES memory.facts(id) ON DELETE SET NULL,
          confidence double precision NOT NULL DEFAULT 1
            CHECK (confidence BETWEEN 0 AND 1),
          origin text NOT NULL DEFAULT 'automatic'
            CHECK (origin IN ('automatic','manual','imported')),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK ((entity_id IS NULL) <> (subject_id IS NULL))
        );
        CREATE UNIQUE INDEX episode_entities_entity_unique
          ON memory.episode_entities(episode_id,entity_id,role)
          WHERE entity_id IS NOT NULL;
        CREATE UNIQUE INDEX episode_entities_subject_unique
          ON memory.episode_entities(episode_id,subject_id,role)
          WHERE subject_id IS NOT NULL;
        CREATE INDEX episode_entities_entity ON memory.episode_entities(entity_id,role);
        CREATE INDEX episode_entities_subject ON memory.episode_entities(subject_id,role);

        CREATE TABLE memory.episode_steps (
          id uuid PRIMARY KEY,
          episode_id uuid NOT NULL REFERENCES memory.episodes(id) ON DELETE CASCADE,
          sequence_no integer NOT NULL CHECK (sequence_no >= 0),
          parent_step_id uuid REFERENCES memory.episode_steps(id) ON DELETE CASCADE,
          branch_key text,
          step_kind text NOT NULL CHECK (step_kind IN (
            'observation','action','encounter','decision','hypothesis','result',
            'cause','resolution','verification','milestone'
          )),
          summary text NOT NULL,
          occurred_at timestamptz,
          status text NOT NULL DEFAULT 'observed'
            CHECK (status IN ('candidate','observed','confirmed','rejected')),
          fact_id uuid REFERENCES memory.facts(id) ON DELETE SET NULL,
          evidence_event_id uuid REFERENCES evidence.events(id) ON DELETE RESTRICT,
          confidence double precision NOT NULL DEFAULT 1
            CHECK (confidence BETWEEN 0 AND 1),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(episode_id,sequence_no)
        );

        CREATE TABLE memory.temporal_rules (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          owner_subject_id uuid REFERENCES core.subjects(id) ON DELETE RESTRICT,
          owner_entity_id uuid REFERENCES memory.entities(id) ON DELETE RESTRICT,
          rule_type text NOT NULL CHECK (rule_type IN ('birthday','anniversary','recurring')),
          label text NOT NULL,
          month smallint NOT NULL CHECK (month BETWEEN 1 AND 12),
          day smallint NOT NULL CHECK (day BETWEEN 1 AND 31),
          year integer,
          timezone text,
          recurrence text NOT NULL DEFAULT 'yearly'
            CHECK (recurrence IN ('yearly','none')),
          sensitivity text NOT NULL DEFAULT 'personal'
            CHECK (sensitivity IN ('normal','personal','protected')),
          reminder_policy jsonb NOT NULL DEFAULT '{"enabled":false}'::jsonb,
          fact_id uuid REFERENCES memory.facts(id) ON DELETE SET NULL,
          review_state text NOT NULL DEFAULT 'candidate'
            CHECK (review_state IN ('candidate','accepted','rejected')),
          state text NOT NULL DEFAULT 'active'
            CHECK (state IN ('active','disabled','superseded','forgotten','isolated')),
          supersedes_id uuid REFERENCES memory.temporal_rules(id),
          version integer NOT NULL DEFAULT 1 CHECK (version > 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK ((owner_subject_id IS NULL) <> (owner_entity_id IS NULL))
        );
        CREATE INDEX temporal_rules_calendar
          ON memory.temporal_rules(namespace_id,month,day,state);

        CREATE TABLE memory.preference_assertions (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          subject_id uuid NOT NULL REFERENCES core.subjects(id) ON DELETE RESTRICT,
          topic_entity_id uuid REFERENCES memory.entities(id) ON DELETE RESTRICT,
          aspect text NOT NULL,
          polarity text NOT NULL CHECK (polarity IN ('like','dislike','prefer','avoid','require')),
          strength double precision NOT NULL DEFAULT 0.5
            CHECK (strength BETWEEN 0 AND 1),
          explicitness text NOT NULL CHECK (explicitness IN ('explicit','inferred')),
          valid_from timestamptz,
          valid_to timestamptz,
          fact_id uuid REFERENCES memory.facts(id) ON DELETE SET NULL,
          state text NOT NULL DEFAULT 'candidate'
            CHECK (state IN ('candidate','active','dormant','forgotten','isolated','superseded')),
          supersedes_id uuid REFERENCES memory.preference_assertions(id),
          version integer NOT NULL DEFAULT 1 CHECK (version > 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX preference_subject_current
          ON memory.preference_assertions(namespace_id,subject_id,state,updated_at);

        CREATE TABLE memory.relationship_assertions (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          subject_id uuid NOT NULL REFERENCES core.subjects(id) ON DELETE RESTRICT,
          related_entity_id uuid NOT NULL REFERENCES memory.entities(id) ON DELETE RESTRICT,
          relation_type text NOT NULL,
          label text NOT NULL,
          valid_from timestamptz,
          valid_to timestamptz,
          fact_id uuid REFERENCES memory.facts(id) ON DELETE SET NULL,
          episode_id uuid REFERENCES memory.episodes(id) ON DELETE SET NULL,
          state text NOT NULL DEFAULT 'candidate'
            CHECK (state IN (
              'candidate','active','dormant','forgotten','isolated','superseded'
            )),
          supersedes_id uuid REFERENCES memory.relationship_assertions(id),
          version integer NOT NULL DEFAULT 1 CHECK (version > 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
        );
        CREATE INDEX relationship_subject_current
          ON memory.relationship_assertions(
            namespace_id,subject_id,related_entity_id,state,updated_at
          );

        CREATE TABLE memory.artifacts (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          artifact_type text NOT NULL CHECK (artifact_type IN (
            'change_report','knowledge_record','photo_note','document','other'
          )),
          title text NOT NULL,
          reference_uri text,
          content_hash text,
          summary_redacted text NOT NULL DEFAULT '',
          sensitivity text NOT NULL DEFAULT 'normal'
            CHECK (sensitivity IN ('normal','personal','protected')),
          state text NOT NULL DEFAULT 'active'
            CHECK (state IN ('candidate','active','disabled','forgotten','isolated')),
          version integer NOT NULL DEFAULT 1 CHECK (version > 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE memory.episode_artifacts (
          episode_id uuid NOT NULL REFERENCES memory.episodes(id) ON DELETE CASCADE,
          artifact_id uuid NOT NULL REFERENCES memory.artifacts(id) ON DELETE CASCADE,
          role text NOT NULL DEFAULT 'documentation'
            CHECK (role IN ('documentation','evidence','result','context')),
          fact_id uuid REFERENCES memory.facts(id) ON DELETE SET NULL,
          PRIMARY KEY(episode_id,artifact_id,role)
        );

        CREATE TABLE memory.procedures (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          title text NOT NULL,
          goal text NOT NULL,
          scope jsonb NOT NULL DEFAULT '{}'::jsonb,
          preconditions jsonb NOT NULL DEFAULT '[]'::jsonb,
          environment_fingerprint jsonb NOT NULL DEFAULT '{}'::jsonb,
          risk_level text NOT NULL DEFAULT 'medium'
            CHECK (risk_level IN ('low','medium','high','critical')),
          state text NOT NULL DEFAULT 'candidate'
            CHECK (state IN ('candidate','active','dormant','disabled','superseded')),
          review_state text NOT NULL DEFAULT 'candidate'
            CHECK (review_state IN ('candidate','accepted','rejected')),
          valid_from timestamptz,
          valid_to timestamptz,
          supersedes_id uuid REFERENCES memory.procedures(id),
          version integer NOT NULL DEFAULT 1 CHECK (version > 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
        );
        CREATE INDEX procedures_current
          ON memory.procedures(namespace_id,state,updated_at);

        CREATE TABLE memory.procedure_steps (
          id uuid PRIMARY KEY,
          procedure_id uuid NOT NULL REFERENCES memory.procedures(id) ON DELETE CASCADE,
          sequence_no integer NOT NULL CHECK (sequence_no >= 0),
          parent_step_id uuid REFERENCES memory.procedure_steps(id) ON DELETE CASCADE,
          branch_key text,
          instruction text NOT NULL,
          expected_observation text,
          success_condition text,
          failure_condition text,
          stop_condition text NOT NULL,
          required_permission text NOT NULL DEFAULT 'none',
          risk_level text NOT NULL DEFAULT 'medium'
            CHECK (risk_level IN ('low','medium','high','critical')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(procedure_id,sequence_no)
        );

        CREATE TABLE memory.procedure_support (
          procedure_id uuid NOT NULL REFERENCES memory.procedures(id) ON DELETE CASCADE,
          episode_id uuid REFERENCES memory.episodes(id) ON DELETE CASCADE,
          fact_id uuid REFERENCES memory.facts(id) ON DELETE CASCADE,
          artifact_id uuid REFERENCES memory.artifacts(id) ON DELETE CASCADE,
          support_kind text NOT NULL DEFAULT 'success'
            CHECK (support_kind IN ('success','counterexample','context')),
          weight double precision NOT NULL DEFAULT 1 CHECK (weight BETWEEN 0 AND 1),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK (num_nonnulls(episode_id,fact_id,artifact_id) = 1)
        );
        CREATE UNIQUE INDEX procedure_support_episode_unique
          ON memory.procedure_support(procedure_id,episode_id,support_kind)
          WHERE episode_id IS NOT NULL;
        CREATE UNIQUE INDEX procedure_support_fact_unique
          ON memory.procedure_support(procedure_id,fact_id,support_kind)
          WHERE fact_id IS NOT NULL;
        CREATE UNIQUE INDEX procedure_support_artifact_unique
          ON memory.procedure_support(procedure_id,artifact_id,support_kind)
          WHERE artifact_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DROP TABLE IF EXISTS memory.procedure_support;
        DROP TABLE IF EXISTS memory.procedure_steps;
        DROP TABLE IF EXISTS memory.procedures;
        DROP TABLE IF EXISTS memory.episode_artifacts;
        DROP TABLE IF EXISTS memory.artifacts;
        DROP TABLE IF EXISTS memory.relationship_assertions;
        DROP TABLE IF EXISTS memory.preference_assertions;
        DROP TABLE IF EXISTS memory.temporal_rules;
        DROP TABLE IF EXISTS memory.episode_steps;
        DROP TABLE IF EXISTS memory.episode_entities;
        DELETE FROM projection.layout_preferences WHERE target_kind='subject';
        ALTER TABLE projection.layout_preferences
          DROP CONSTRAINT layout_preferences_target_kind_check,
          ADD CONSTRAINT layout_preferences_target_kind_check
            CHECK (target_kind IN ('camera','entity','galaxy'));
        DROP INDEX IF EXISTS memory.episodes_time_type;
        DROP INDEX IF EXISTS memory.episodes_anchor;
        ALTER TABLE memory.episodes
          DROP CONSTRAINT IF EXISTS episodes_time_order,
          DROP COLUMN IF EXISTS confidence,
          DROP COLUMN IF EXISTS extractor_version,
          DROP COLUMN IF EXISTS origin,
          DROP COLUMN IF EXISTS review_state,
          DROP COLUMN IF EXISTS importance,
          DROP COLUMN IF EXISTS timezone,
          DROP COLUMN IF EXISTS time_resolution,
          DROP COLUMN IF EXISTS time_precision,
          DROP COLUMN IF EXISTS ended_at,
          DROP COLUMN IF EXISTS started_at,
          DROP COLUMN IF EXISTS episode_type;
        DELETE FROM memory.episodes duplicate
        USING memory.episodes retained
        WHERE duplicate.namespace_id=retained.namespace_id
          AND duplicate.entity_id=retained.entity_id
          AND duplicate.entity_id IS NOT NULL
          AND duplicate.id>retained.id;
        DELETE FROM memory.episodes WHERE entity_id IS NULL;
        ALTER TABLE memory.episodes
          ALTER COLUMN entity_id SET NOT NULL,
          ADD CONSTRAINT episodes_namespace_id_entity_id_key
            UNIQUE(namespace_id,entity_id);
        """
    )
