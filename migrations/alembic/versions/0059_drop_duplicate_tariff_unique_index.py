"""drop unique active subscription per tariff index

Revision ID: 0059
Revises: 0058
Create Date: 2026-04-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0059'
down_revision: Union[str, None] = '0058'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text('DROP INDEX IF EXISTS uq_subscriptions_user_tariff_active'))


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX uq_subscriptions_user_tariff_active
            ON subscriptions (user_id, tariff_id)
            WHERE tariff_id IS NOT NULL AND status IN ('active', 'trial', 'limited')
            """
        )
    )
