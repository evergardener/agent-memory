"""Add atomic extraction and entity mention lineage."""

from alembic import op

revision = "0009_atomic_extraction_lineage"
down_revision = "0008_correction_evidence_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory.facts
          ADD COLUMN extraction_method text NOT NULL DEFAULT 'deterministic-v1',
          ADD COLUMN extraction_version text NOT NULL DEFAULT 'deterministic-v1',
          ADD COLUMN model_name text,
          ADD COLUMN evidence_span_start integer,
          ADD COLUMN evidence_span_end integer,
          ADD CONSTRAINT facts_evidence_span_valid CHECK (
            (evidence_span_start IS NULL AND evidence_span_end IS NULL)
            OR (evidence_span_start >= 0 AND evidence_span_end > evidence_span_start)
          );

        CREATE TABLE memory.entity_mentions (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          entity_id uuid NOT NULL REFERENCES memory.entities(id) ON DELETE CASCADE,
          fact_id uuid NOT NULL REFERENCES memory.facts(id) ON DELETE CASCADE,
          event_id uuid NOT NULL REFERENCES evidence.events(id),
          mention_text text NOT NULL,
          span_start integer NOT NULL CHECK (span_start >= 0),
          span_end integer NOT NULL CHECK (span_end > span_start),
          extraction_version text NOT NULL,
          confidence double precision NOT NULL DEFAULT 0.7
            CHECK (confidence BETWEEN 0 AND 1),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(fact_id,entity_id,event_id,span_start,span_end)
        );
        CREATE INDEX entity_mentions_event ON memory.entity_mentions(namespace_id,event_id);
        CREATE INDEX entity_mentions_entity ON memory.entity_mentions(namespace_id,entity_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE memory.entity_mentions;
        ALTER TABLE memory.facts
          DROP CONSTRAINT facts_evidence_span_valid,
          DROP COLUMN evidence_span_end,
          DROP COLUMN evidence_span_start,
          DROP COLUMN model_name,
          DROP COLUMN extraction_version,
          DROP COLUMN extraction_method;
        """
    )
