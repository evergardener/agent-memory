"""Add governed human-facing Subject display names."""

from alembic import op

revision = "0015_subject_display_name_origin"
down_revision = "0014_audit_event_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE core.subjects
          ADD COLUMN display_name_origin text NOT NULL DEFAULT 'default'
          CHECK (display_name_origin IN ('default','source','manual'));

        UPDATE core.subjects subject
        SET display_name_origin='manual'
        WHERE EXISTS (
          SELECT 1
          FROM audit.events event
          WHERE event.target_type='subject'
            AND event.target_id=subject.id
            AND event.action='subject.update'
            AND event.metadata_redacted->'previous'->>'display_name'
                IS DISTINCT FROM
                event.metadata_redacted->'current'->>'display_name'
        );

        UPDATE core.subjects
        SET display_name=CASE
              WHEN display_name ~ '^Hermes[[:space:]]*·[[:space:]]*'
                THEN regexp_replace(
                  display_name,
                  '^Hermes[[:space:]]*·[[:space:]]*',
                  ''
                )
              ELSE display_name
            END,
            display_name_origin='source',
            updated_at=now()
        WHERE kind='profile_persona'
          AND display_name_origin<>'manual';
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE core.subjects DROP COLUMN display_name_origin;
        """
    )
