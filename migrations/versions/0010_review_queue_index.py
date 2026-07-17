"""Add the review queue ordering index."""

from alembic import op

revision = "0010_review_queue_index"
down_revision = "0009_atomic_extraction_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE INDEX facts_review_queue
           ON memory.facts(namespace_id,memory_state,updated_at DESC,id DESC)
           WHERE memory_state <> 'purge_requested'"""
    )


def downgrade() -> None:
    op.execute("DROP INDEX memory.facts_review_queue")
