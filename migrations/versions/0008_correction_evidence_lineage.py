"""Backfill evidence lineage for corrected facts."""

from alembic import op

revision = "0008_correction_evidence_lineage"
down_revision = "0007_state_axis_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO memory.fact_evidence(fact_id,event_id,support_kind,weight)
        SELECT corrected.id,fe.event_id,'historical_context',fe.weight
          FROM memory.facts corrected
          JOIN memory.fact_evidence fe ON fe.fact_id=corrected.supersedes_fact_id
         WHERE corrected.supersedes_fact_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM memory.fact_evidence fe
         USING memory.facts corrected
         WHERE fe.fact_id=corrected.id
           AND corrected.supersedes_fact_id IS NOT NULL
           AND fe.support_kind='historical_context'
        """
    )
