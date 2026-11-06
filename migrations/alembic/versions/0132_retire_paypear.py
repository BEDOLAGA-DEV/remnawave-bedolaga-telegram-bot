"""Retire PayPear without rewriting balances or completed transactions.

Revision ID: 0132
Revises: 0131

Provider-specific invoices and credentials are removed. Financial transactions
keep their original method and amounts. Downgrade restores the empty invoice
schema only; deleted provider data requires a pre-upgrade backup.
"""

from pathlib import Path
from runpy import run_path

import sqlalchemy as sa
from alembic import op


revision = '0132'
down_revision = '0131'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    configs = sa.table('payment_method_configs', sa.column('id'), sa.column('method_id'))
    if 'payment_method_configs' in tables:
        if 'payment_method_promo_groups' in tables:
            groups = sa.table('payment_method_promo_groups', sa.column('payment_method_config_id'))
            op.execute(
                groups.delete().where(
                    groups.c.payment_method_config_id.in_(
                        sa.select(configs.c.id).where(configs.c.method_id == 'paypear')
                    )
                )
            )
        op.execute(configs.delete().where(configs.c.method_id == 'paypear'))
    if 'system_settings' in tables:
        settings = sa.table('system_settings', sa.column('key'))
        op.execute(settings.delete().where(settings.c.key.startswith('PAYPEAR_', autoescape=True)))
    if 'paypear_payments' in tables:
        op.drop_table('paypear_payments')


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table('paypear_payments'):
        original = run_path(str(Path(__file__).with_name('0058_create_paypear_payments.py')))
        original['upgrade']()
