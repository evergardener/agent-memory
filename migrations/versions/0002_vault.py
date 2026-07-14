"""Create encrypted Vault tables."""

from alembic import op

revision = "0002_vault"
down_revision = "0001_evidence_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS vault")
    op.execute(
        """
        CREATE TABLE vault.entries (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          kind text NOT NULL,
          display_label text NOT NULL,
          redacted_hint text NOT NULL,
          ciphertext bytea NOT NULL,
          data_nonce bytea NOT NULL,
          wrapped_dek bytea NOT NULL,
          wrap_nonce bytea NOT NULL,
          key_version integer NOT NULL DEFAULT 1,
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','deleted')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX vault_entries_namespace_status
          ON vault.entries(namespace_id,status,updated_at);
        CREATE TABLE vault.grants (
          id uuid PRIMARY KEY,
          namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
          entry_id uuid NOT NULL REFERENCES vault.entries(id),
          grantee text NOT NULL,
          operation text NOT NULL,
          target_constraint text NOT NULL,
          expires_at timestamptz NOT NULL,
          revoked_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX vault_grants_lookup
          ON vault.grants(entry_id,operation,target_constraint,expires_at)
          WHERE revoked_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS vault CASCADE")
