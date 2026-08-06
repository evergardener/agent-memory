"""Add multi-evidence lineage and confirmation metadata for preferences."""

from alembic import op

revision = "0018_preference_evidence"
down_revision = "0017_current_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory.preference_assertions
          ADD COLUMN extraction_method text NOT NULL DEFAULT 'deterministic-v1',
          ADD COLUMN confirmed_at timestamptz;

        CREATE TABLE memory.preference_evidence (
          preference_id uuid NOT NULL
            REFERENCES memory.preference_assertions(id) ON DELETE CASCADE,
          event_id uuid NOT NULL REFERENCES evidence.events(id) ON DELETE RESTRICT,
          support_kind text NOT NULL DEFAULT 'support'
            CHECK (support_kind IN ('support','contradiction','context')),
          weight double precision NOT NULL DEFAULT 1 CHECK (weight BETWEEN 0 AND 1),
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(preference_id,event_id)
        );
        CREATE INDEX preference_evidence_event ON memory.preference_evidence(event_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS memory.preference_evidence;
        ALTER TABLE memory.preference_assertions
          DROP COLUMN IF EXISTS confirmed_at,
          DROP COLUMN IF EXISTS extraction_method;
        """
    )
