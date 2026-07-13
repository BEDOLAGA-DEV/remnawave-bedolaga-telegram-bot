"""add recurrent_payments table

The cabinet «recurrent payments» legal/info document (per-language content
plus an enable flag) was added to the models AFTER the 0001 initial-schema
snapshot. 0001 bootstraps a database with ``Base.metadata.create_all`` against
the models of its time, so a fresh install already has this table — but an
existing database that ran 0001 before the feature existed never got it, and
no incremental migration created it. The cabinet endpoint
``GET /cabinet/info/recurrent-payments`` then 500s with
``UndefinedTableError: relation "recurrent_payments" does not exist``.

Idempotent: guarded by the inspector so it is a no-op where the table is
already present (fresh installs via create_all).

Revision ID: 0096
Revises: 0095
Create Date: 2026-07-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0096'
down_revision: Union[str, None] = '0095'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'recurrent_payments' not in inspector.get_table_names():
        op.create_table(
            'recurrent_payments',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('language', sa.String(length=10), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('language', name='uq_recurrent_payments_language'),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'recurrent_payments' in inspector.get_table_names():
        op.drop_table('recurrent_payments')
