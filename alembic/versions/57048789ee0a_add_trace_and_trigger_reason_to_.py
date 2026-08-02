"""add trace and trigger_reason to recommendation_snapshots

Revision ID: 57048789ee0a
Revises: 66a6b0cf91b9
Create Date: 2026-08-02 19:15:51.415475

Batch mode + server_default, same reasoning as d66b0fedceba
(products.vector_sync_status): this table may already have rows (any
user who's visited /dashboard since Stage 12), and a NOT NULL column
added without a default would fail to backfill them. `trigger_reason`
defaults to 'no_snapshot' — a lie for rows that already existed before
this migration, but a harmless one: it's overwritten the very next time
that user's recommendation regenerates, and there is no correct
historical value to backfill it with (this table was never designed to
answer "what was the trigger reason before Stage 14 could even record
one"). `trace` defaults to an empty JSON object for the same reason.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '57048789ee0a'
down_revision: Union[str, Sequence[str], None] = '66a6b0cf91b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('recommendation_snapshots') as batch_op:
        batch_op.add_column(
            sa.Column('trigger_reason', sa.String(length=40), nullable=False, server_default='no_snapshot')
        )
        batch_op.add_column(
            sa.Column('trace', sa.JSON(), nullable=False, server_default='{}')
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('recommendation_snapshots') as batch_op:
        batch_op.drop_column('trace')
        batch_op.drop_column('trigger_reason')
