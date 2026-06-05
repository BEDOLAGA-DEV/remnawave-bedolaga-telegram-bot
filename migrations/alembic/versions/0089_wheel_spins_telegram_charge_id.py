"""wheel_spins.telegram_charge_id idempotency guard

Real Telegram Stars wheel spins are granted in ``_handle_wheel_spin_payment`` when
the ``successful_payment`` update arrives. Telegram delivers that update
*at-least-once* (webhook retry on a non-2xx / full worker queue, polling-offset
replay, or a crash after ``db.commit()`` but before the offset ack), and the
handler had no idempotency key — so a redelivery re-granted the prize to a
legitimate payer, unbounded.

This adds a nullable ``telegram_charge_id`` with a UNIQUE index so a second
processing of the same Telegram charge id is refused at the database level
(NULLs stay distinct, so the pre-existing rows and all non-Stars spins are
unaffected).

Revision ID: 0089
Revises: 0088
Create Date: 2026-06-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0089'
down_revision: Union[str, None] = '0088'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('wheel_spins', sa.Column('telegram_charge_id', sa.String(length=255), nullable=True))
    op.create_index(
        'uq_wheel_spins_telegram_charge_id',
        'wheel_spins',
        ['telegram_charge_id'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('uq_wheel_spins_telegram_charge_id', table_name='wheel_spins')
    op.drop_column('wheel_spins', 'telegram_charge_id')
