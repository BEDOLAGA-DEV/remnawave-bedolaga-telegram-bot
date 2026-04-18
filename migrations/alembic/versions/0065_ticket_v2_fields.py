"""ticket v2 fields and quick replies

Adds category, assigned_to, first_response_at to tickets table.
Creates ticket_quick_replies table for template responses.

Revision ID: 0065
Revises: 0064
Create Date: 2026-04-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0065'
down_revision: Union[str, None] = '0064'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    ticket_cols = {c['name'] for c in inspector.get_columns('tickets')}

    if 'category' not in ticket_cols:
        op.add_column(
            'tickets',
            sa.Column('category', sa.String(50), nullable=True),
        )
    if 'assigned_to' not in ticket_cols:
        op.add_column(
            'tickets',
            sa.Column(
                'assigned_to',
                sa.Integer(),
                sa.ForeignKey('users.id', ondelete='SET NULL'),
                nullable=True,
            ),
        )
    if 'first_response_at' not in ticket_cols:
        op.add_column(
            'tickets',
            sa.Column('first_response_at', sa.DateTime(timezone=True), nullable=True),
        )

    if 'ticket_quick_replies' not in inspector.get_table_names():
        op.create_table(
            'ticket_quick_replies',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('title', sa.String(255), nullable=False),
            sa.Column('text', sa.Text(), nullable=False),
            sa.Column('category', sa.String(50), nullable=True),
            sa.Column(
                'created_by',
                sa.Integer(),
                sa.ForeignKey('users.id', ondelete='SET NULL'),
                nullable=True,
            ),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table('ticket_quick_replies')
    op.drop_column('tickets', 'first_response_at')
    op.drop_column('tickets', 'assigned_to')
    op.drop_column('tickets', 'category')
