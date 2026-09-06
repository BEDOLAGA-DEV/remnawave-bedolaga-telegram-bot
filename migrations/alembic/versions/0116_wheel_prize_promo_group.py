"""wheel prize: promo group for generated promocodes

Revision ID: 0116
Revises: 0115
Create Date: 2026-09-06

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0116'
down_revision: Union[str, None] = '0115'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'wheel_prizes' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('wheel_prizes')}
    if 'promo_group_id' in existing:
        return

    op.add_column('wheel_prizes', sa.Column('promo_group_id', sa.Integer(), nullable=True))
    op.create_index('ix_wheel_prizes_promo_group_id', 'wheel_prizes', ['promo_group_id'])
    if 'promo_groups' in inspector.get_table_names():
        op.create_foreign_key(
            'fk_wheel_prizes_promo_group_id',
            'wheel_prizes',
            'promo_groups',
            ['promo_group_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'wheel_prizes' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('wheel_prizes')}
    if 'promo_group_id' not in existing:
        return

    fks = {fk['name'] for fk in inspector.get_foreign_keys('wheel_prizes')}
    if 'fk_wheel_prizes_promo_group_id' in fks:
        op.drop_constraint('fk_wheel_prizes_promo_group_id', 'wheel_prizes', type_='foreignkey')

    indexes = {ix['name'] for ix in inspector.get_indexes('wheel_prizes')}
    if 'ix_wheel_prizes_promo_group_id' in indexes:
        op.drop_index('ix_wheel_prizes_promo_group_id', table_name='wheel_prizes')

    op.drop_column('wheel_prizes', 'promo_group_id')
