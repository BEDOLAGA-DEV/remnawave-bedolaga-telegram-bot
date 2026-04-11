"""add tariff_id to promocodes

Revision ID: 0052
Revises: 0051
Create Date: 2026-03-26

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0052'
down_revision: Union[str, None] = '0051'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_cols = {c['name'] for c in inspector.get_columns('promocodes')}
    existing_indexes = {ix['name'] for ix in inspector.get_indexes('promocodes')}
    existing_fks = {fk['name'] for fk in inspector.get_foreign_keys('promocodes')}

    if 'tariff_id' not in existing_cols:
        op.add_column('promocodes', sa.Column('tariff_id', sa.Integer(), nullable=True))

    if 'ix_promocodes_tariff_id' not in existing_indexes:
        op.create_index('ix_promocodes_tariff_id', 'promocodes', ['tariff_id'])

    if 'fk_promocodes_tariff_id' not in existing_fks:
        op.create_foreign_key(
            'fk_promocodes_tariff_id',
            'promocodes',
            'tariffs',
            ['tariff_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    op.drop_constraint('fk_promocodes_tariff_id', 'promocodes', type_='foreignkey')
    op.drop_index('ix_promocodes_tariff_id', table_name='promocodes')
    op.drop_column('promocodes', 'tariff_id')
