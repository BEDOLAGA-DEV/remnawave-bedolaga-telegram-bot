"""Add isolated LirPay invoice records.

Revision ID: 0141
Revises: 0140
"""

import sqlalchemy as sa
from alembic import op

revision = '0141'
down_revision = '0140'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('lirpay_payments'):
        return
    op.create_table(
        'lirpay_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('order_id', sa.String(64), nullable=False),
        sa.Column('lirpay_payment_id', sa.String(64)),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(10), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('is_paid', sa.Boolean(), nullable=False),
        sa.Column('payment_url', sa.Text()),
        sa.Column('payment_method', sa.String(32)),
        sa.Column('metadata_json', sa.JSON()),
        sa.Column('callback_payload', sa.JSON()),
        sa.Column('paid_at', sa.DateTime(timezone=True)),
        sa.Column('expires_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id')),
    )
    # Ключи поиска по нашим идентификаторам: order_id — Idempotency-Key,
    # lirpay_payment_id — public_id ссылки провайдера.
    for column in ('id', 'user_id', 'order_id', 'lirpay_payment_id'):
        op.create_index(
            f'ix_lirpay_payments_{column}',
            'lirpay_payments',
            [column],
            unique=column in ('order_id', 'lirpay_payment_id'),
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('lirpay_payments'):
        if op.get_bind().execute(sa.text('SELECT 1 FROM lirpay_payments LIMIT 1')).first():
            raise RuntimeError('Archive LirPay financial records before downgrading 0141')
        op.drop_table('lirpay_payments')
