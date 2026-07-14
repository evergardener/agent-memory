"""Create evidence-first core tables."""

from alembic import op

revision = "0001_evidence_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for schema in ("core", "evidence", "memory", "retrieval", "ops", "audit"):
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    op.execute(
        """
        CREATE TABLE core.namespaces (
          id uuid PRIMARY KEY,
          stable_key text NOT NULL UNIQUE,
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE core.sources (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          source_profile text NOT NULL,
          source_instance text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id, source_profile, source_instance)
        );
        CREATE TABLE core.sessions (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          source_id uuid NOT NULL REFERENCES core.sources(id),
          external_session_id text NOT NULL,
          started_at timestamptz NOT NULL,
          ended_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id, source_id, external_session_id)
        );
        CREATE TABLE core.turns (
          id uuid PRIMARY KEY,
          session_id uuid NOT NULL REFERENCES core.sessions(id),
          external_turn_id text NOT NULL,
          status text NOT NULL DEFAULT 'completed',
          occurred_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(session_id, external_turn_id)
        );
        CREATE TABLE evidence.events (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          turn_id uuid NOT NULL REFERENCES core.turns(id),
          event_type text NOT NULL CHECK (event_type IN (
            'user_message','assistant_message','tool_call','tool_result',
            'environment_observation','session_boundary'
          )),
          sequence_no integer NOT NULL,
          redacted_payload jsonb NOT NULL,
          payload_hash text NOT NULL,
          ingest_key text NOT NULL UNIQUE,
          occurred_at timestamptz NOT NULL,
          retention_class text NOT NULL DEFAULT 'standard',
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(turn_id, sequence_no, event_type)
        );
        CREATE TABLE evidence.redaction_findings (
          id uuid PRIMARY KEY,
          event_id uuid NOT NULL REFERENCES evidence.events(id),
          kind text NOT NULL,
          span_hash text NOT NULL,
          action text NOT NULL,
          rule_version text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE memory.facts (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          statement text NOT NULL,
          fact_type text NOT NULL DEFAULT 'candidate',
          confidence double precision NOT NULL DEFAULT 0 CHECK (confidence BETWEEN 0 AND 1),
          memory_state text NOT NULL DEFAULT 'candidate' CHECK (memory_state IN (
            'candidate','active','dormant','forgotten','isolated','superseded','purge_requested'
          )),
          source_profile text NOT NULL,
          valid_from timestamptz,
          valid_to timestamptz,
          supersedes_fact_id uuid REFERENCES memory.facts(id),
          version integer NOT NULL DEFAULT 1,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE memory.fact_evidence (
          fact_id uuid NOT NULL REFERENCES memory.facts(id) ON DELETE CASCADE,
          event_id uuid NOT NULL REFERENCES evidence.events(id),
          support_kind text NOT NULL DEFAULT 'support',
          weight double precision NOT NULL DEFAULT 1,
          PRIMARY KEY(fact_id, event_id)
        );
        CREATE TABLE memory.entities (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          entity_type text NOT NULL DEFAULT 'unknown',
          canonical_name text NOT NULL,
          normalized_name text NOT NULL,
          merge_state text NOT NULL DEFAULT 'active',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id, normalized_name)
        );
        CREATE TABLE memory.fact_entities (
          fact_id uuid NOT NULL REFERENCES memory.facts(id) ON DELETE CASCADE,
          entity_id uuid NOT NULL REFERENCES memory.entities(id) ON DELETE CASCADE,
          PRIMARY KEY(fact_id, entity_id)
        );
        CREATE TABLE retrieval.documents (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          source_kind text NOT NULL,
          source_id uuid NOT NULL,
          text_redacted text NOT NULL,
          search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', text_redacted)) STORED,
          embedding vector,
          embedding_model_version text,
          lifecycle_state text NOT NULL DEFAULT 'candidate',
          indexed_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(source_kind, source_id)
        );
        CREATE INDEX retrieval_documents_fts ON retrieval.documents USING gin(search_vector);
        CREATE TABLE ops.jobs (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          kind text NOT NULL,
          idempotency_key text NOT NULL UNIQUE,
          input_ref uuid NOT NULL,
          input_version integer NOT NULL DEFAULT 1,
          status text NOT NULL DEFAULT 'pending' CHECK (status IN (
            'pending','running','done','retry','failed','cancelled'
          )),
          run_after timestamptz NOT NULL DEFAULT now(),
          lease_until timestamptz,
          attempt_count integer NOT NULL DEFAULT 0,
          last_error_code text,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ops_jobs_claim ON ops.jobs(status, run_after, created_at);
        CREATE TABLE ops.job_attempts (
          id uuid PRIMARY KEY,
          job_id uuid NOT NULL REFERENCES ops.jobs(id),
          started_at timestamptz NOT NULL,
          ended_at timestamptz,
          result text,
          error_code text,
          correlation_id uuid NOT NULL
        );
        CREATE TABLE audit.events (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          actor_type text NOT NULL,
          actor_id text NOT NULL,
          action text NOT NULL,
          target_type text NOT NULL,
          target_id uuid,
          reason text,
          correlation_id uuid NOT NULL,
          metadata_redacted jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    for schema in ("audit", "ops", "retrieval", "memory", "evidence", "core"):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
