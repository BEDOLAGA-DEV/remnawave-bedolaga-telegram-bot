"""add autopay_period_days to subscriptions

Lets users (and admins) pick which renewal period the autopay cron
charges for, instead of always using the tariff's cheapest period.

NULL means "use the legacy default" — settings.DEFAULT_AUTOPAY_PERIOD_DAYS
if set, otherwise tariff.get_shortest_period().

Revision ID: 0086
Revises: 0085
Create Date: 2026-05-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0086'
down_revision: Union[str, None] = '0085'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('autopay_period_days', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('subscriptions', 'autopay_period_days')
