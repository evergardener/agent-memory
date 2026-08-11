"""Add bounded review queue indexes for unified memory candidates."""

from alembic import op

revision = "0019_review_queue_indexes"
down_revision = "0018_preference_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE INDEX episodes_review_queue
           ON memory.episodes(namespace_id,updated_at DESC,id DESC)
           WHERE origin <> 'legacy_derived'
             AND (state='candidate' OR review_state='candidate')"""
    )
    op.execute(
        """CREATE INDEX preferences_review_queue
           ON memory.preference_assertions(namespace_id,updated_at DESC,id DESC)
           WHERE state='candidate'"""
    )
    op.execute(
        """CREATE INDEX relationships_review_queue
           ON memory.relationship_assertions(namespace_id,updated_at DESC,id DESC)
           WHERE state='candidate'"""
    )
    op.execute(
        """CREATE INDEX temporal_rules_review_queue
           ON memory.temporal_rules(namespace_id,updated_at DESC,id DESC)
           WHERE review_state='candidate'"""
    )
    op.execute(
        """CREATE INDEX procedures_review_queue
           ON memory.procedures(namespace_id,updated_at DESC,id DESC)
           WHERE state='candidate' OR review_state='candidate'"""
    )


def downgrade() -> None:
    op.execute("DROP INDEX memory.procedures_review_queue")
    op.execute("DROP INDEX memory.temporal_rules_review_queue")
    op.execute("DROP INDEX memory.relationships_review_queue")
    op.execute("DROP INDEX memory.preferences_review_queue")
    op.execute("DROP INDEX memory.episodes_review_queue")
