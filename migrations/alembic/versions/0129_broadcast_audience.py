"""Store broadcast audience conditions for history and audit.

Revision ID: 0129
Revises: 0128
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0129'
down_revision: Union[str, None] = '0128'
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
