"""initial

Revision ID: 0001
Revises: 
Create Date: 2026-04-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sellers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('username', sa.String(length=255), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('telegram_id'),
    )
    op.create_index(op.f('ix_sellers_telegram_id'), 'sellers', ['telegram_id'], unique=True)

    op.create_table(
        'products',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('seller_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('sku', sa.String(length=128), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('photo_file_id', sa.String(length=255), nullable=True),
        sa.Column('avito_url', sa.Text(), nullable=True),
        sa.Column('avito_item_id', sa.String(length=128), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['seller_id'], ['sellers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_products_seller_id'), 'products', ['seller_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_products_seller_id'), table_name='products')
    op.drop_table('products')
    op.drop_index(op.f('ix_sellers_telegram_id'), table_name='sellers')
    op.drop_table('sellers')
