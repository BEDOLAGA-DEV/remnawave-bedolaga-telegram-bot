"""Version daily device billing without repricing existing subscriptions.

Revision ID: 0138
Revises: 0137
"""

import sqlalchemy as sa
from alembic import op


revision = '0138'
down_revision = '0137'
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('subscriptions')}
    if 'daily_devices_billing_version' not in columns:
        op.add_column(
            'subscriptions',
            sa.Column('daily_devices_billing_version', sa.Integer(), nullable=False, server_default='0'),
        )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text('SELECT 1 FROM subscriptions WHERE daily_devices_billing_version <> 0 LIMIT 1'))
        .first()
    ):
        raise RuntimeError('Cannot discard active daily device billing policy')
    op.drop_column('subscriptions', 'daily_devices_billing_version')
