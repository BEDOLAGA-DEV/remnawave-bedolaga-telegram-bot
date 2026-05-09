"""add subscriptions.is_bio_reward column

Marks a subscription as the free bio-reward sub so that:
- Remnawave user_tag can be set to BIO_REWARD_USER_TAG (e.g. "FREE")
- UI displays "Бесплатная (за bio)" instead of "Тестовая"

Revision ID: 0078
Revises: 0077
Create Date: 2026-05-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0078'
down_revision: str | None = '0077'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column(
            'is_bio_reward',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )


def downgrade() -> None:
    op.drop_column('subscriptions', 'is_bio_reward')
