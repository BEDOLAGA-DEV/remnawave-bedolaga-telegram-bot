"""Add AuraPay card subscriptions.

Revision ID: 0132
Revises: 0131
"""

from alembic import op
import sqlalchemy as sa

revision = '0132'
down_revision = '0131'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'aurapay_subscriptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('merchant_id', sa.String(64), nullable=False, unique=True),
        sa.Column('provider_id', sa.String(128), nullable=True, unique=True),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('charge_days', sa.Integer(), nullable=False),
        sa.Column('period', sa.Integer(), nullable=False),
        sa.Column('interval', sa.String(10), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('redirect_url', sa.Text(), nullable=True),
        sa.Column('next_charge_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_aurapay_subscriptions_user_id', 'aurapay_subscriptions', ['user_id'])
    op.create_index('ix_aurapay_subscriptions_subscription_id', 'aurapay_subscriptions', ['subscription_id'])
    op.create_index(
        'uq_aurapay_subscriptions_alive',
        'aurapay_subscriptions',
        ['subscription_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('NEW', 'WAITING_PAYMENT', 'ACTIVE')"),
        sqlite_where=sa.text("status IN ('NEW', 'WAITING_PAYMENT', 'ACTIVE')"),
    )
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("""
            CREATE FUNCTION guard_aurapay_subscription_delete() RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM aurapay_subscriptions
                    WHERE subscription_id = OLD.id
                      AND status IN ('NEW', 'WAITING_PAYMENT', 'ACTIVE')
                ) THEN
                    RAISE EXCEPTION 'cancel AuraPay renewal before deleting subscription'
                        USING ERRCODE = '23503';
                END IF;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute("""
            CREATE TRIGGER trg_guard_aurapay_subscription_delete
            BEFORE DELETE ON subscriptions FOR EACH ROW
            EXECUTE FUNCTION guard_aurapay_subscription_delete()
        """)
    elif op.get_bind().dialect.name == 'sqlite':
        op.execute("""
            CREATE TRIGGER trg_guard_aurapay_subscription_delete
            BEFORE DELETE ON subscriptions FOR EACH ROW
            WHEN EXISTS (
                SELECT 1 FROM aurapay_subscriptions
                WHERE subscription_id = OLD.id
                  AND status IN ('NEW', 'WAITING_PAYMENT', 'ACTIVE')
            )
            BEGIN
                SELECT RAISE(ABORT, 'cancel AuraPay renewal before deleting subscription');
            END
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP TRIGGER trg_guard_aurapay_subscription_delete ON subscriptions')
        op.execute('DROP FUNCTION guard_aurapay_subscription_delete()')
    elif op.get_bind().dialect.name == 'sqlite':
        op.execute('DROP TRIGGER trg_guard_aurapay_subscription_delete')
    op.drop_table('aurapay_subscriptions')
