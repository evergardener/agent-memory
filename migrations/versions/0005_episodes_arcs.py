"""Add evidence-linked episodes and long-term arcs."""

from alembic import op

revision = "0005_episodes_arcs"
down_revision = "0004_lifecycle_state_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE memory.episodes (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          entity_id uuid NOT NULL REFERENCES memory.entities(id),
          title text NOT NULL,
          summary text NOT NULL,
          state text NOT NULL DEFAULT 'active',
          version integer NOT NULL DEFAULT 1,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id,entity_id)
        );
        CREATE TABLE memory.episode_facts (
          episode_id uuid NOT NULL REFERENCES memory.episodes(id) ON DELETE CASCADE,
          fact_id uuid NOT NULL REFERENCES memory.facts(id) ON DELETE CASCADE,
          PRIMARY KEY(episode_id,fact_id)
        );
        CREATE TABLE memory.arcs (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          entity_id uuid NOT NULL REFERENCES memory.entities(id),
          title text NOT NULL,
          summary text NOT NULL,
          state text NOT NULL DEFAULT 'active',
          version integer NOT NULL DEFAULT 1,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(namespace_id,entity_id)
        );
        CREATE TABLE memory.arc_facts (
          arc_id uuid NOT NULL REFERENCES memory.arcs(id) ON DELETE CASCADE,
          fact_id uuid NOT NULL REFERENCES memory.facts(id) ON DELETE CASCADE,
          PRIMARY KEY(arc_id,fact_id)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """DROP TABLE IF EXISTS memory.arc_facts;
           DROP TABLE IF EXISTS memory.arcs;
           DROP TABLE IF EXISTS memory.episode_facts;
           DROP TABLE IF EXISTS memory.episodes;"""
    )
