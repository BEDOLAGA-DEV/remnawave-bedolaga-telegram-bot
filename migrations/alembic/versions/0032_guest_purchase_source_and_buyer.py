"""Add source and buyer_user_id columns to guest_purchases

Supports cabinet gift purchases by tracking purchase origin
and linking to the authenticated buyer.

Revision ID: 0032
Revises: 0031
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0032'
down_revision: Union[str, None] = '0031'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('guest_purchases', 'source'):
        op.add_column(
            'guest_purchases',
            sa.Column('source', sa.String(20), nullable=False, server_default='landing'),
        )
        op.create_index('ix_guest_purchases_source', 'guest_purchases', ['source'])

    if not _has_column('guest_purchases', 'buyer_user_id'):
        op.add_column(
            'guest_purchases',
            sa.Column('buyer_user_id', sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            'fk_guest_purchases_buyer_user_id',
            'guest_purchases',
            'users',
            ['buyer_user_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    if _has_column('guest_purchases', 'buyer_user_id'):
        op.drop_constraint('fk_guest_purchases_buyer_user_id', 'guest_purchases', type_='foreignkey')
        op.drop_column('guest_purchases', 'buyer_user_id')

    if _has_column('guest_purchases', 'source'):
        op.drop_index('ix_guest_purchases_source', table_name='guest_purchases')
        op.drop_column('guest_purchases', 'source')
