"""Add isolated PayHot invoice records.

Revision ID: 0142
Revises: 0141
"""

import sqlalchemy as sa
from alembic import op

revision = '0142'
down_revision = '0141'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('payhot_payments'):
        return
    op.create_table(
        'payhot_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('order_id', sa.String(64), nullable=False),
        sa.Column('payhot_payment_id', sa.String(128)),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(10), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('is_paid', sa.Boolean(), nullable=False),
        sa.Column('payment_url', sa.Text()),
        sa.Column('payment_method', sa.String(32)),
        sa.Column('metadata_json', sa.JSON()),
        sa.Column('request_payload', sa.JSON()),
        sa.Column('fulfillment_state', sa.JSON()),
        sa.Column('callback_payload', sa.JSON()),
        sa.Column('processed_events', sa.JSON()),
        sa.Column('paid_at', sa.DateTime(timezone=True)),
        sa.Column('expires_at', sa.DateTime(timezone=True)),
        sa.Column('last_checked_at', sa.DateTime(timezone=True)),
        sa.Column('next_check_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id')),
        sa.CheckConstraint("amount_kopeks > 0 AND currency = 'RUB'", name='ck_payhot_rub_principal'),
    )
    for column in ('id', 'user_id', 'order_id', 'payhot_payment_id'):
        op.create_index(
            f'ix_payhot_payments_{column}',
            'payhot_payments',
            [column],
            unique=column in ('order_id', 'payhot_payment_id'),
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('payhot_payments'):
        if op.get_bind().execute(sa.text('SELECT 1 FROM payhot_payments LIMIT 1')).first():
            raise RuntimeError('Archive PayHot financial records before downgrading 0142')
        op.drop_table('payhot_payments')
