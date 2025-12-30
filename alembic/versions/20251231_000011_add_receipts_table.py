"""add receipts table

Revision ID: 20251231_000011
Revises: 20250903_000001
Create Date: 2025-12-31 00:00:11

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20251231_000011'
down_revision = '20250903_000001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'receipts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.Column('type', sa.String(length=16), nullable=False),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id', ondelete='SET NULL'), nullable=True),
        sa.Column('buyer_tg_id', sa.String(length=64), nullable=True),
        sa.Column('title', sa.String(length=256), nullable=False),
        sa.Column('amount_minor', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False, server_default='RUB'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('fns_id', sa.String(length=64), nullable=True),
        sa.Column('error_text', sa.String(length=2048), nullable=True),
        sa.Column('attempts_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_receipts_status', 'receipts', ['status'])
    op.create_index('ix_receipts_order_id', 'receipts', ['order_id'])


def downgrade() -> None:
    op.drop_index('ix_receipts_order_id', table_name='receipts')
    op.drop_index('ix_receipts_status', table_name='receipts')
    op.drop_table('receipts')


