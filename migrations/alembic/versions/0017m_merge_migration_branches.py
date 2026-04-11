"""merge custom and upstream migration branches at revision 0017

Revision ID: 0017m
Revises: 0017, 0017u
Create Date: 2026-03-27

Merges two parallel migration branches:
  - 0017 (user custom: add_wl_tariff_traffic_fields)
  - 0017u (upstream: add_unique_constraint_transaction_external_id)
Both branches originate from 0014 and must be applied before proceeding.
"""

from typing import Sequence, Union

from alembic import op

revision: str = '0017m'
down_revision: Union[str, Sequence[str], None] = ('0017', '0017u')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
