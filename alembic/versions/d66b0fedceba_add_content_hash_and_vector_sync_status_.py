"""add content_hash and vector_sync_status to products

Revision ID: d66b0fedceba
Revises: 831bda8cf752
Create Date: 2026-08-02 00:53:37.852059

SQLite can't add a CHECK constraint via plain ALTER TABLE, so this uses
Alembic's batch mode — on SQLite that transparently rebuilds the table
(copy data, swap in the new schema); on Postgres/other backends batch
mode just emits the equivalent plain ALTER TABLE statements. Same
migration file works correctly against both, per Stage 0's "avoid
DB-specific SQL" principle.

`server_default='pending'` on vector_sync_status matters here
specifically because this ALTER runs against a table that may already
have rows (any product created under Stage 3) — a NOT NULL column
without a default would fail to backfill those rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd66b0fedceba'
down_revision: Union[str, Sequence[str], None] = '831bda8cf752'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('products') as batch_op:
        batch_op.add_column(sa.Column('content_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column('vector_sync_status', sa.String(length=20), nullable=False, server_default='pending')
        )
        batch_op.create_index('ix_products_vector_sync_status', ['vector_sync_status'], unique=False)
        batch_op.create_check_constraint(
            'ck_products_vector_sync_status_valid',
            "vector_sync_status IN ('pending', 'synced', 'failed')",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('products') as batch_op:
        batch_op.drop_constraint('ck_products_vector_sync_status_valid', type_='check')
        batch_op.drop_index('ix_products_vector_sync_status')
        batch_op.drop_column('vector_sync_status')
        batch_op.drop_column('content_hash')
