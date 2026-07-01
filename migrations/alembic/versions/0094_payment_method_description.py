"""payment_method_configs: per-method description override

Revision ID: 0094
Revises: 0093
Create Date: 2026-06-26

Adds ``description`` to ``payment_method_configs`` so admins can set a custom
per-method description shown on the cabinet balance page. When empty, the
cabinet falls back to its default localized description.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0094'
down_revision: Union[str, None] = '0093'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'payment_method_configs' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('payment_method_configs')}
    if 'description' not in existing:
        op.add_column(
            'payment_method_configs',
            sa.Column('description', sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'payment_method_configs' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('payment_method_configs')}
    if 'description' in existing:
        op.drop_column('payment_method_configs', 'description')
