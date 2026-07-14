"""Add lifecycle classification, continuity, deterministic state and reports."""

from alembic import op

revision = "0004_lifecycle_state_reports"
down_revision = "0003_vault_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS state")
    op.execute("CREATE SCHEMA IF NOT EXISTS reports")
    op.execute(
        """
        CREATE TABLE state.current_items (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          topic_key text NOT NULL,
          summary text NOT NULL,
          source_fact_id uuid REFERENCES memory.facts(id),
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','resolved','expired')),
          valid_from timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id,topic_key)
        );
        CREATE INDEX state_current_active ON state.current_items(namespace_id,expires_at)
          WHERE status='active';

        CREATE TABLE state.continuities (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          topic_key text NOT NULL,
          summary text NOT NULL,
          source_event_id uuid NOT NULL REFERENCES evidence.events(id),
          last_active_at timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id,topic_key)
        );

        CREATE TABLE state.interaction_snapshots (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          source_event_id uuid REFERENCES evidence.events(id),
          axes jsonb NOT NULL,
          summary text NOT NULL,
          suggestions jsonb NOT NULL DEFAULT '[]'::jsonb,
          calculated_at timestamptz NOT NULL,
          algorithm_version text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX state_snapshots_latest
          ON state.interaction_snapshots(namespace_id,calculated_at DESC);

        CREATE TABLE reports.consolidation (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          period_start timestamptz NOT NULL,
          period_end timestamptz NOT NULL,
          summary jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id,period_start,period_end)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS reports CASCADE")
    op.execute("DROP SCHEMA IF EXISTS state CASCADE")
