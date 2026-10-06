"""скидка как бонус рекламной кампании

Revision ID: 0132
Revises: 0131
Create Date: 2026-10-01

Новый тип бонуса кампании «discount»: переход по ссылке выдаёт ту же
персональную скидку, что промопредложения (users.promo_offer_discount_*), —
процент и необязательный срок в часах. Процент пишется и в регистрацию, чтобы
в списке пришедших по кампании было видно, кто что получил.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0132'
down_revision: Union[str, None] = '0131'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ('advertising_campaigns', 'discount_percent'),
    ('advertising_campaigns', 'discount_duration_hours'),
    ('advertising_campaign_registrations', 'discount_percent'),
)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    for table, column in _COLUMNS:
        if not _has_column(table, column):
            op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))


def downgrade() -> None:
    for table, column in reversed(_COLUMNS):
        if _has_column(table, column):
            op.drop_column(table, column)
