"""guest_purchases: слаг рекламной кампании

Revision ID: 0106
Revises: 0105
Create Date: 2026-08-12

Добавляет ``campaign_slug`` в ``guest_purchases``. Покупатель приходит с
рекламного лендинга, а оплату подтверждает вебхук платёжки — в этот момент ни
куки, ни сессии покупателя уже нет. Поэтому слаг кампании хранится в самой
покупке: только так её можно привязать к кампании после оплаты.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0106'
down_revision: Union[str, None] = '0105'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'guest_purchases' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('guest_purchases')}
    if 'campaign_slug' not in existing:
        op.add_column(
            'guest_purchases',
            sa.Column('campaign_slug', sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'guest_purchases' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('guest_purchases')}
    if 'campaign_slug' in existing:
        op.drop_column('guest_purchases', 'campaign_slug')
