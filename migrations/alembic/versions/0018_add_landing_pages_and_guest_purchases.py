"""add landing_pages and guest_purchases tables

Revision ID: 0018
Revises: 0017
Create Date: 2026-03-06

Adds public quick-purchase landing page configuration and
guest (unauthenticated) purchase records for the landing page feature.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0018'
down_revision: Union[str, None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table in inspector.get_table_names()


def _has_index(table: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in [idx['name'] for idx in inspector.get_indexes(table)]


def upgrade() -> None:
    if not _has_table('landing_pages'):
        op.create_table(
            'landing_pages',
            sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
            sa.Column('slug', sa.String(100), unique=True, nullable=False, index=True),
            sa.Column('is_active', sa.Boolean, server_default=sa.text('true'), nullable=False),
            sa.Column('title', sa.String(500), server_default='', nullable=False),
            sa.Column('subtitle', sa.Text, nullable=True),
            sa.Column('features', sa.JSON, server_default='[]', nullable=False),
            sa.Column('footer_text', sa.Text, nullable=True),
            sa.Column('allowed_tariff_ids', sa.JSON, server_default='[]', nullable=False),
            sa.Column('allowed_periods', sa.JSON, server_default='{}', nullable=False),
            sa.Column('payment_methods', sa.JSON, server_default='[]', nullable=False),
            sa.Column('gift_enabled', sa.Boolean, server_default=sa.text('true'), nullable=False),
            sa.Column('custom_css', sa.Text, nullable=True),
            sa.Column('meta_title', sa.String(200), nullable=True),
            sa.Column('meta_description', sa.Text, nullable=True),
            sa.Column('display_order', sa.Integer, server_default=sa.text('0'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not _has_table('guest_purchases'):
        op.create_table(
            'guest_purchases',
            sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
            sa.Column('token', sa.String(64), unique=True, nullable=False, index=True),
            sa.Column(
                'landing_id',
                sa.Integer,
                sa.ForeignKey('landing_pages.id', ondelete='SET NULL'),
                nullable=True,
            ),
            sa.Column('contact_type', sa.String(20), nullable=False),
            sa.Column('contact_value', sa.String(255), nullable=False),
            sa.Column('is_gift', sa.Boolean, server_default=sa.text('false'), nullable=False),
            sa.Column('gift_recipient_type', sa.String(20), nullable=True),
            sa.Column('gift_recipient_value', sa.String(255), nullable=True),
            sa.Column('gift_message', sa.Text, nullable=True),
            sa.Column(
                'tariff_id',
                sa.Integer,
                sa.ForeignKey('tariffs.id', ondelete='SET NULL'),
                nullable=True,
            ),
            sa.Column('period_days', sa.Integer, nullable=False),
            sa.Column('amount_kopeks', sa.Integer, nullable=False),
            sa.Column('currency', sa.String(3), server_default='RUB', nullable=False),
            sa.Column('payment_method', sa.String(50), nullable=True),
            sa.Column('payment_id', sa.String(255), nullable=True),
            sa.Column('status', sa.String(20), server_default='pending', nullable=False),
            sa.Column('subscription_url', sa.Text, nullable=True),
            sa.Column('subscription_crypto_link', sa.Text, nullable=True),
            sa.Column(
                'user_id',
                sa.Integer,
                sa.ForeignKey('users.id', ondelete='SET NULL'),
                nullable=True,
            ),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_index('guest_purchases', 'ix_guest_purchases_status'):
        op.create_index('ix_guest_purchases_status', 'guest_purchases', ['status'])
    if not _has_index('guest_purchases', 'ix_guest_purchases_contact'):
        op.create_index('ix_guest_purchases_contact', 'guest_purchases', ['contact_type', 'contact_value'])


def downgrade() -> None:
    op.drop_index('ix_guest_purchases_contact', table_name='guest_purchases')
    op.drop_index('ix_guest_purchases_status', table_name='guest_purchases')
    op.drop_table('guest_purchases')
    op.drop_table('landing_pages')
