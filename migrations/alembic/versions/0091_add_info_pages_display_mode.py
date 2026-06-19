from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0091'
down_revision: Union[str, None] = '0090'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'info_pages',
        sa.Column('display_mode', sa.String(length=10), nullable=False, server_default='both'),
    )


def downgrade() -> None:
    op.drop_column('info_pages', 'display_mode')
