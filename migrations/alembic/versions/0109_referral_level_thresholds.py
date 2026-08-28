"""Порог открытия уровня: сколько рефералов его открывают

Revision ID: 0109
Revises: 0108
Create Date: 2026-08-27

Номер уровня отвечал только на вопрос «чьё пополнение приносит награду», и со
стороны админки было неясно, за что уровни вообще получают. Порог отвечает на
второй вопрос: с какого момента партнёр начинает получать доход с этого звена.

Считаются по умолчанию только рефералы с пополнением — иначе порог берётся
накруткой пустых регистраций, и уровень открывается, не принеся ничего.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0109'
down_revision: Union[str, None] = '0108'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    ('required_referrals', sa.Column('required_referrals', sa.Integer(), nullable=False, server_default='0')),
    (
        'required_referrals_active_only',
        sa.Column('required_referrals_active_only', sa.Boolean(), nullable=False, server_default='true'),
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'referral_reward_levels' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('referral_reward_levels')}
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column('referral_reward_levels', column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'referral_reward_levels' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('referral_reward_levels')}
    for name, _column in reversed(_COLUMNS):
        if name in existing:
            op.drop_column('referral_reward_levels', name)
