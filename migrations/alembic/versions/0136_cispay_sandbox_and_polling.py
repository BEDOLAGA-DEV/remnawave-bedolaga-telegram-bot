"""Persist CisPay sandbox isolation and shared polling reservation.

Revision ID: 0136
Revises: 0135
"""

import sqlalchemy as sa
from alembic import op

revision = '0136'
down_revision = '0135'
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('cispay_payments')}
    if 'is_sandbox' not in columns:
        op.add_column(
            'cispay_payments', sa.Column('is_sandbox', sa.Boolean(), nullable=False, server_default=sa.false())
        )
    if 'next_check_at' not in columns:
        op.add_column('cispay_payments', sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if op.get_bind().execute(sa.text('SELECT 1 FROM cispay_payments WHERE is_sandbox LIMIT 1')).first():
        raise RuntimeError('Cannot discard sandbox provenance from existing payments')
    op.drop_column('cispay_payments', 'next_check_at')
    op.drop_column('cispay_payments', 'is_sandbox')
