"""Add governed deterministic interaction-state configuration."""

from alembic import op

revision = "0006_state_governance"
down_revision = "0005_episodes_arcs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE state.settings (
          namespace_id uuid PRIMARY KEY REFERENCES core.namespaces(id),
          enabled boolean NOT NULL DEFAULT true,
          drift_hours integer NOT NULL DEFAULT 72 CHECK (drift_hours BETWEEN 1 AND 720),
          axes_initial jsonb NOT NULL,
          thresholds jsonb NOT NULL,
          profile_overrides jsonb NOT NULL DEFAULT '{}'::jsonb,
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS state.settings")
