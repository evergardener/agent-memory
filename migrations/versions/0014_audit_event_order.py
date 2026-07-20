"""Give audit events a deterministic database order."""

from alembic import op

revision = "0014_audit_event_order"
down_revision = "0013_relation_galaxies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE audit.events
          ADD COLUMN event_sequence bigint GENERATED ALWAYS AS IDENTITY;
        CREATE UNIQUE INDEX audit_events_sequence
          ON audit.events(event_sequence);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX audit.audit_events_sequence;
        ALTER TABLE audit.events DROP COLUMN event_sequence;
        """
    )
