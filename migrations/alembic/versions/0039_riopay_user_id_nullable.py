"""make riopay_payments.user_id nullable for guest purchases

Revision ID: 0039
Revises: 0038
Create Date: 2026-03-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0039'
down_revision: Union[str, None] = '0038'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('riopay_payments', 'user_id', existing_type=sa.Integer(), nullable=True)
    op.drop_constraint('riopay_payments_user_id_fkey', 'riopay_payments', type_='foreignkey')
    op.create_foreign_key(None, 'riopay_payments', 'users', ['user_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint(None, 'riopay_payments', type_='foreignkey')
    op.create_foreign_key('riopay_payments_user_id_fkey', 'riopay_payments', 'users', ['user_id'], ['id'])
    op.alter_column('riopay_payments', 'user_id', existing_type=sa.Integer(), nullable=False)
