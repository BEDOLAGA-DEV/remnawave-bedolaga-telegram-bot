"""ensure columns exist after duplicate 0090 collision

Revision ID: 0095
Revises: 0094
Create Date: 2026-06-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0095'
down_revision: Union[str, None] = '0094'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return False
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    # 1. Ensure quick_amounts in payment_method_configs
    if not _column_exists('payment_method_configs', 'quick_amounts'):
        op.add_column('payment_method_configs', sa.Column('quick_amounts', sa.JSON(), nullable=True))

    # 2. Ensure display_mode in info_pages
    if not _column_exists('info_pages', 'display_mode'):
        op.add_column(
            'info_pages',
            sa.Column('display_mode', sa.String(length=10), nullable=False, server_default='both'),
        )

    # 3. Ensure yclid in yandex_client_id_map
    if not _column_exists('yandex_client_id_map', 'yclid'):
        op.add_column(
            'yandex_client_id_map',
            sa.Column('yclid', sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    if _column_exists('payment_method_configs', 'quick_amounts'):
        op.drop_column('payment_method_configs', 'quick_amounts')

    if _column_exists('info_pages', 'display_mode'):
        op.drop_column('info_pages', 'display_mode')

    if _column_exists('yandex_client_id_map', 'yclid'):
        op.drop_column('yandex_client_id_map', 'yclid')
