"""make platega_subscriptions user_id nullable

Revision ID: 5651d7089c66
Revises: 0108
Create Date: 2026-08-18 00:15:46.324878

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5651d7089c66'
down_revision: Union[str, None] = '0108'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('platega_subscriptions', 'user_id', existing_type=sa.Integer(), nullable=True)
    op.alter_column('platega_subscriptions', 'subscription_id', existing_type=sa.Integer(), nullable=True)

def downgrade() -> None:
    # Note: If there are existing records with NULL user_id or subscription_id, 
    # downgrading will fail. You'd need to set a default or delete them first.
    op.alter_column('platega_subscriptions', 'subscription_id', existing_type=sa.Integer(), nullable=False)
    op.alter_column('platega_subscriptions', 'user_id', existing_type=sa.Integer(), nullable=False)
