"""Link redacted Vault entries to graph memories."""

from alembic import op

revision = "0003_vault_references"
down_revision = "0002_vault"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE vault.references (
          entry_id uuid NOT NULL REFERENCES vault.entries(id) ON DELETE CASCADE,
          target_type text NOT NULL CHECK (target_type IN ('fact','entity')),
          target_id uuid NOT NULL,
          display_relation text NOT NULL DEFAULT 'protected_resource',
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(entry_id,target_type,target_id)
        );
        CREATE INDEX vault_references_target ON vault.references(target_type,target_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS vault.references")
