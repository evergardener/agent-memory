"""Add governed lifecycle metadata to current state items."""

from alembic import op

revision = "0017_current_governance"
down_revision = "0016_unified_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE state.current_items
          ADD COLUMN resolved_at timestamptz,
          ADD COLUMN resolution_reason text,
          ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK (version > 0);
        CREATE INDEX state_current_source_fact
          ON state.current_items(namespace_id,source_fact_id)
          WHERE source_fact_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS state.state_current_source_fact;
        ALTER TABLE state.current_items
          DROP COLUMN IF EXISTS version,
          DROP COLUMN IF EXISTS resolution_reason,
          DROP COLUMN IF EXISTS resolved_at;
        """
    )
