"""Add configurable names, ranges and enable flags for state axes."""

from alembic import op

revision = "0007_state_axis_metadata"
down_revision = "0006_state_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE state.settings
          ADD COLUMN axis_labels jsonb NOT NULL DEFAULT jsonb_build_object(
            'interaction_need','互动需求','restraint','表达克制','valence','情感效价',
            'arousal','激活度','immersion','任务沉浸'
          ),
          ADD COLUMN axis_ranges jsonb NOT NULL DEFAULT jsonb_build_object(
            'interaction_need',jsonb_build_object('min',0,'max',1),
            'restraint',jsonb_build_object('min',0,'max',1),
            'valence',jsonb_build_object('min',0,'max',1),
            'arousal',jsonb_build_object('min',0,'max',1),
            'immersion',jsonb_build_object('min',0,'max',1)
          ),
          ADD COLUMN axis_enabled jsonb NOT NULL DEFAULT jsonb_build_object(
            'interaction_need',true,'restraint',true,'valence',true,
            'arousal',true,'immersion',true
          );
        """
    )


def downgrade() -> None:
    op.execute(
        """ALTER TABLE state.settings DROP COLUMN IF EXISTS axis_enabled,
             DROP COLUMN IF EXISTS axis_ranges,DROP COLUMN IF EXISTS axis_labels"""
    )
