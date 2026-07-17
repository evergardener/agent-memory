"""Add reversible entity merge and split governance."""

from alembic import op

revision = "0011_entity_governance"
down_revision = "0010_review_queue_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory.entities
          ADD COLUMN canonical_entity_id uuid
            REFERENCES memory.entities(id) ON DELETE RESTRICT,
          ADD CONSTRAINT entities_not_self_merged
            CHECK (canonical_entity_id IS NULL OR canonical_entity_id <> id),
          ADD CONSTRAINT entities_merge_state_valid
            CHECK (merge_state IN ('active','merged'));
        CREATE INDEX entities_canonical
          ON memory.entities(namespace_id,canonical_entity_id)
          WHERE canonical_entity_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX memory.entities_canonical;
        ALTER TABLE memory.entities
          DROP CONSTRAINT entities_merge_state_valid,
          DROP CONSTRAINT entities_not_self_merged,
          DROP COLUMN canonical_entity_id;
        """
    )
