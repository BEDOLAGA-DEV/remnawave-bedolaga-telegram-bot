"""Store broadcast audience conditions for history and audit.

Revision ID: 0128
Revises: 0127
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0128'
down_revision: Union[str, None] = '0127'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('broadcast_history')}
    if 'audience' not in columns:
        op.add_column('broadcast_history', sa.Column('audience', sa.JSON(), nullable=True))


def downgrade() -> None:
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('broadcast_history')}
    if 'audience' in columns:
        op.drop_column('broadcast_history', 'audience')
