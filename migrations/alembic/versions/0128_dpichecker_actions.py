"""DPI//CHECKER: своя запись действий из кабинета

Revision ID: 0128
Revises: 0127
Create Date: 2026-09-24

Строка на каждую проверку, Зонд, скан «Шумные соседи» и монитор, запущенные из кабинета:
кто запустил, что проверяли (источник в панели, имена ключей), сколько списано и вернули.
Результаты проверок живут у сервиса и сюда не копируются.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0128'
down_revision: Union[str, None] = '0127'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if 'dpichecker_actions' in _tables():
        return
    op.create_table(
        'dpichecker_actions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('check_type', sa.String(16), nullable=True),
        sa.Column('remote_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False, server_default='submitting'),
        sa.Column('admin_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('location', sa.String(16), nullable=True),
        sa.Column('pop_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('resource_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('source', sa.String(24), nullable=False, server_default='paste'),
        sa.Column('source_ref', sa.String(128), nullable=True),
        sa.Column('label', sa.String(255), nullable=False, server_default=''),
        sa.Column('targets', sa.JSON(), nullable=False),
        sa.Column('request', sa.JSON(), nullable=False),
        sa.Column('idempotency_key', sa.String(64), nullable=False, unique=True),
        sa.Column('cost_usd', sa.Numeric(12, 4), nullable=True),
        sa.Column('refunded_usd', sa.Numeric(12, 4), nullable=True),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('delivery_ids', sa.JSON(), nullable=False),
        sa.Column('last_run_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('kind', 'remote_id', name='uq_dpichecker_actions_kind_remote'),
    )
    op.create_index('ix_dpichecker_actions_id', 'dpichecker_actions', ['id'])
    op.create_index('ix_dpichecker_actions_admin_user_id', 'dpichecker_actions', ['admin_user_id'])
    op.create_index('ix_dpichecker_actions_kind_created', 'dpichecker_actions', ['kind', 'created_at'])


def downgrade() -> None:
    if 'dpichecker_actions' in _tables():
        op.drop_table('dpichecker_actions')
