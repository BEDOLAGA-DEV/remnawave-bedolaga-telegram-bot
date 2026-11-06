"""Add isolated Anore invoice records.

Revision ID: 0135
Revises: 0134
"""

import sqlalchemy as sa
from alembic import op

revision = '0135'
down_revision = '0134'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('anore_payments'):
        return
    op.create_table(
        'anore_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('order_id', sa.String(64), nullable=False),
        sa.Column('anore_payment_id', sa.String(128)),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(10), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('is_paid', sa.Boolean(), nullable=False),
        sa.Column('is_test', sa.Boolean(), nullable=False),
        sa.Column('payment_url', sa.Text()),
        sa.Column('payment_method', sa.String(32)),
        sa.Column('metadata_json', sa.JSON()),
        sa.Column('callback_payload', sa.JSON()),
        sa.Column('processed_events', sa.JSON()),
        sa.Column('paid_at', sa.DateTime(timezone=True)),
        sa.Column('expires_at', sa.DateTime(timezone=True)),
        sa.Column('last_checked_at', sa.DateTime(timezone=True)),
        sa.Column('next_check_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id')),
    )
    for column in ('id', 'user_id', 'order_id', 'anore_payment_id'):
        op.create_index(
            f'ix_anore_payments_{column}', 'anore_payments', [column], unique=column in ('order_id', 'anore_payment_id')
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('anore_payments'):
        if op.get_bind().execute(sa.text('SELECT 1 FROM anore_payments LIMIT 1')).first():
            raise RuntimeError('Archive Anore financial records before downgrading 0135')
        op.drop_table('anore_payments')
