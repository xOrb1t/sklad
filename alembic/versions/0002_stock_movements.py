"""stock movements, per-seller low-stock alerts

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sellers') as batch:
        batch.add_column(
            sa.Column('notify_low_stock', sa.Boolean(), nullable=False, server_default=sa.text('true'))
        )
        batch.add_column(
            sa.Column('low_stock_threshold', sa.Integer(), nullable=False, server_default=sa.text('2'))
        )

    op.create_table(
        'stock_movements',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('delta', sa.Integer(), nullable=False),
        sa.Column('quantity_after', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_stock_movements_product_id'), 'stock_movements', ['product_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_stock_movements_product_id'), table_name='stock_movements')
    op.drop_table('stock_movements')
    with op.batch_alter_table('sellers') as batch:
        batch.drop_column('low_stock_threshold')
        batch.drop_column('notify_low_stock')
