"""add ondelete CASCADE/SET NULL to all FK referencing users.id

Revision ID: 0016
Revises: 0015
Create Date: 2026-03-06

Fixes backup restore errors: orphan records in child tables prevent
FK constraints from being created. This migration:
1. Cleans orphan records (rows referencing non-existent users)
2. Recreates FK constraints with ON DELETE CASCADE or SET NULL
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_actual_fk_name(connection, table: str, column: str) -> str | None:
    """Look up actual FK constraint name from pg_constraint for a given table+column."""
    result = connection.execute(
        text("""
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
            JOIN pg_attribute att ON att.attrelid = con.conrelid
                AND att.attnum = ANY(con.conkey)
            WHERE rel.relname = :table
                AND att.attname = :column
                AND con.contype = 'f'
                AND nsp.nspname = 'public'
            LIMIT 1
        """),
        {'table': table, 'column': column},
    )
    row = result.fetchone()
    return row[0] if row else None

# (table, column, ondelete, fk_name)
_FK_CHANGES: list[tuple[str, str, str, str]] = [
    ('yookassa_payments', 'user_id', 'CASCADE', 'yookassa_payments_user_id_fkey'),
    ('cryptobot_payments', 'user_id', 'CASCADE', 'cryptobot_payments_user_id_fkey'),
    ('heleket_payments', 'user_id', 'CASCADE', 'heleket_payments_user_id_fkey'),
    ('mulenpay_payments', 'user_id', 'CASCADE', 'mulenpay_payments_user_id_fkey'),
    ('pal24_payments', 'user_id', 'CASCADE', 'pal24_payments_user_id_fkey'),
    ('wata_payments', 'user_id', 'CASCADE', 'wata_payments_user_id_fkey'),
    ('platega_payments', 'user_id', 'CASCADE', 'platega_payments_user_id_fkey'),
    ('cloudpayments_payments', 'user_id', 'CASCADE', 'cloudpayments_payments_user_id_fkey'),
    ('freekassa_payments', 'user_id', 'CASCADE', 'freekassa_payments_user_id_fkey'),
    ('kassa_ai_payments', 'user_id', 'CASCADE', 'kassa_ai_payments_user_id_fkey'),
    ('users', 'referred_by_id', 'SET NULL', 'users_referred_by_id_fkey'),
    ('subscriptions', 'user_id', 'CASCADE', 'subscriptions_user_id_fkey'),
    ('transactions', 'user_id', 'CASCADE', 'transactions_user_id_fkey'),
    ('subscription_conversions', 'user_id', 'CASCADE', 'subscription_conversions_user_id_fkey'),
    ('promocodes', 'created_by', 'SET NULL', 'promocodes_created_by_fkey'),
    ('promocode_uses', 'user_id', 'CASCADE', 'promocode_uses_user_id_fkey'),
    ('referral_earnings', 'user_id', 'CASCADE', 'referral_earnings_user_id_fkey'),
    ('referral_earnings', 'referral_id', 'CASCADE', 'referral_earnings_referral_id_fkey'),
    ('withdrawal_requests', 'user_id', 'CASCADE', 'withdrawal_requests_user_id_fkey'),
    ('withdrawal_requests', 'processed_by', 'SET NULL', 'withdrawal_requests_processed_by_fkey'),
    ('broadcast_history', 'admin_id', 'SET NULL', 'broadcast_history_admin_id_fkey'),
    ('welcome_texts', 'created_by', 'SET NULL', 'welcome_texts_created_by_fkey'),
    ('advertising_campaigns', 'created_by', 'SET NULL', 'advertising_campaigns_created_by_fkey'),
    ('advertising_campaigns', 'partner_user_id', 'SET NULL', 'advertising_campaigns_partner_user_id_fkey'),
    ('admin_roles', 'created_by', 'SET NULL', 'admin_roles_created_by_fkey'),
    ('user_roles', 'user_id', 'CASCADE', 'user_roles_user_id_fkey'),
    ('user_roles', 'assigned_by', 'SET NULL', 'user_roles_assigned_by_fkey'),
    ('access_policies', 'created_by', 'SET NULL', 'access_policies_created_by_fkey'),
    ('admin_audit_log', 'user_id', 'CASCADE', 'admin_audit_log_user_id_fkey'),
    # Tables not in original list — also need ondelete in DB
    ('user_promo_groups', 'user_id', 'CASCADE', 'user_promo_groups_user_id_fkey'),
    ('partner_applications', 'user_id', 'CASCADE', 'partner_applications_user_id_fkey'),
    ('partner_applications', 'processed_by', 'SET NULL', 'partner_applications_processed_by_fkey'),
    ('referral_contest_events', 'referrer_id', 'CASCADE', 'referral_contest_events_referrer_id_fkey'),
    ('referral_contest_events', 'referral_id', 'CASCADE', 'referral_contest_events_referral_id_fkey'),
    ('contest_attempts', 'user_id', 'CASCADE', 'contest_attempts_user_id_fkey'),
    ('sent_notifications', 'user_id', 'CASCADE', 'sent_notifications_user_id_fkey'),
    ('subscription_events', 'user_id', 'CASCADE', 'subscription_events_user_id_fkey'),
    ('discount_offers', 'user_id', 'CASCADE', 'discount_offers_user_id_fkey'),
    ('polls', 'created_by', 'SET NULL', 'polls_created_by_fkey'),
    ('poll_responses', 'user_id', 'CASCADE', 'poll_responses_user_id_fkey'),
    ('advertising_campaign_registrations', 'user_id', 'CASCADE', 'advertising_campaign_registrations_user_id_fkey'),
    ('tickets', 'user_id', 'CASCADE', 'tickets_user_id_fkey'),
    ('ticket_messages', 'user_id', 'CASCADE', 'ticket_messages_user_id_fkey'),
    ('ticket_notifications', 'user_id', 'CASCADE', 'ticket_notifications_user_id_fkey'),
    ('cabinet_refresh_tokens', 'user_id', 'CASCADE', 'cabinet_refresh_tokens_user_id_fkey'),
    ('wheel_spins', 'user_id', 'CASCADE', 'wheel_spins_user_id_fkey'),
    ('button_click_logs', 'user_id', 'SET NULL', 'button_click_logs_user_id_fkey'),
    ('promo_offer_templates', 'created_by', 'SET NULL', 'promo_offer_templates_created_by_fkey'),
    ('promo_offer_logs', 'user_id', 'SET NULL', 'promo_offer_logs_user_id_fkey'),
    ('support_audit_logs', 'actor_user_id', 'SET NULL', 'support_audit_logs_actor_user_id_fkey'),
    ('support_audit_logs', 'target_user_id', 'SET NULL', 'support_audit_logs_target_user_id_fkey'),
    ('user_messages', 'created_by', 'SET NULL', 'user_messages_created_by_fkey'),
    ('pinned_messages', 'created_by', 'SET NULL', 'pinned_messages_created_by_fkey'),
    ('referral_contests', 'created_by', 'SET NULL', 'referral_contests_created_by_fkey'),
]

# ALL child tables with FK to users.id — clean orphans before constraint changes.
# (table, column, nullable) — nullable columns get SET NULL, non-nullable get DELETE.
_ALL_USER_FKS: list[tuple[str, str, bool]] = [
    ('yookassa_payments', 'user_id', False),
    ('cryptobot_payments', 'user_id', False),
    ('heleket_payments', 'user_id', False),
    ('mulenpay_payments', 'user_id', False),
    ('pal24_payments', 'user_id', False),
    ('wata_payments', 'user_id', False),
    ('platega_payments', 'user_id', False),
    ('cloudpayments_payments', 'user_id', False),
    ('freekassa_payments', 'user_id', False),
    ('kassa_ai_payments', 'user_id', False),
    ('user_promo_groups', 'user_id', False),
    ('subscriptions', 'user_id', False),
    ('transactions', 'user_id', False),
    ('subscription_conversions', 'user_id', False),
    ('promocode_uses', 'user_id', False),
    ('referral_earnings', 'user_id', False),
    ('referral_earnings', 'referral_id', False),
    ('withdrawal_requests', 'user_id', False),
    ('partner_applications', 'user_id', False),
    ('referral_contest_events', 'referrer_id', False),
    ('referral_contest_events', 'referral_id', False),
    ('contest_attempts', 'user_id', False),
    ('sent_notifications', 'user_id', False),
    ('subscription_events', 'user_id', False),
    ('discount_offers', 'user_id', False),
    ('poll_responses', 'user_id', False),
    ('advertising_campaign_registrations', 'user_id', False),
    ('tickets', 'user_id', False),
    ('ticket_messages', 'user_id', False),
    ('ticket_notifications', 'user_id', False),
    ('cabinet_refresh_tokens', 'user_id', False),
    ('wheel_spins', 'user_id', False),
    ('user_roles', 'user_id', False),
    ('admin_audit_log', 'user_id', False),
    # nullable columns — SET NULL instead of DELETE
    ('broadcast_history', 'admin_id', True),
    ('users', 'referred_by_id', True),
    ('promocodes', 'created_by', True),
    ('withdrawal_requests', 'processed_by', True),
    ('promo_offer_templates', 'created_by', True),
    ('promo_offer_logs', 'user_id', True),
    ('polls', 'created_by', True),
    ('support_audit_logs', 'actor_user_id', True),
    ('support_audit_logs', 'target_user_id', True),
    ('user_messages', 'created_by', True),
    ('welcome_texts', 'created_by', True),
    ('pinned_messages', 'created_by', True),
    ('advertising_campaigns', 'partner_user_id', True),
    ('advertising_campaigns', 'created_by', True),
    ('button_click_logs', 'user_id', True),
    ('referral_contests', 'created_by', True),
    ('admin_roles', 'created_by', True),
    ('user_roles', 'assigned_by', True),
    ('access_policies', 'created_by', True),
    ('partner_applications', 'processed_by', True),
]


def _table_exists(connection, table: str) -> bool:
    """Check if a table exists in the public schema."""
    result = connection.execute(
        text("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = :table
        """),
        {'table': table},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    connection = op.get_bind()

    # Step 1: Clean orphan records for ALL tables referencing users.id
    for table, column, nullable in _ALL_USER_FKS:
        if not _table_exists(connection, table):
            continue
        if nullable:
            op.execute(
                f'UPDATE {table} SET {column} = NULL '
                f'WHERE {column} IS NOT NULL '
                f'AND {column} NOT IN (SELECT id FROM users)'
            )
        else:
            op.execute(
                f'DELETE FROM {table} '
                f'WHERE {column} NOT IN (SELECT id FROM users)'
            )

    # Step 2: Drop old FK constraints and recreate with ON DELETE CASCADE/SET NULL
    for table, column, ondelete, fk_name in _FK_CHANGES:
        if not _table_exists(connection, table):
            continue
        # Look up the actual constraint name in the database (may differ from assumed name)
        actual_fk = _get_actual_fk_name(connection, table, column)
        if actual_fk:
            op.drop_constraint(actual_fk, table, type_='foreignkey')
        # Always create the FK with the desired ondelete behavior
        op.create_foreign_key(fk_name, table, 'users', [column], ['id'], ondelete=ondelete)


def downgrade() -> None:
    connection = op.get_bind()
    # Revert to FK constraints without ON DELETE behavior
    for table, column, _ondelete, fk_name in _FK_CHANGES:
        if not _table_exists(connection, table):
            continue
        actual_fk = _get_actual_fk_name(connection, table, column)
        if actual_fk:
            op.drop_constraint(actual_fk, table, type_='foreignkey')
        op.create_foreign_key(fk_name, table, 'users', [column], ['id'])
