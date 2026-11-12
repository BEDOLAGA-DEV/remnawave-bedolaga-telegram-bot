"""A completed promo check must not block a separate notification transaction."""

import pytest
from sqlalchemy import select, text

from app.database.models import Base, PromoGroup, SubscriptionEvent, Transaction, User, UserPromoGroup
from app.services.promo_group_assignment import maybe_assign_promo_group_by_total_spent
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('scenario', ['no-spending', 'no-group', 'below-threshold', 'assign', 'already-assigned'])
async def test_notification_insert_after_promo_check_does_not_wait_for_webhook_close(postgres_database, scenario):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (webhook, notification):
        user = User(id=1, telegram_id=100001, balance_kopeks=5000, language='ru')
        webhook.add(user)
        await webhook.flush()
        if scenario != 'no-spending':
            webhook.add(
                Transaction(
                    id=1,
                    user_id=1,
                    type='subscription_payment',
                    amount_kopeks=-1000,
                    is_completed=True,
                )
            )
        if scenario in ('below-threshold', 'assign', 'already-assigned'):
            webhook.add(
                PromoGroup(
                    id=1,
                    name='Test group',
                    auto_assign_total_spent_kopeks=2000 if scenario == 'below-threshold' else 500,
                )
            )
            await webhook.flush()
        if scenario == 'already-assigned':
            user.auto_promo_group_assigned = True
            user.auto_promo_group_threshold_kopeks = 500
            webhook.add(UserPromoGroup(user_id=1, promo_group_id=1, assigned_by='auto'))
        await webhook.commit()

        result = await maybe_assign_promo_group_by_total_spent(webhook, 1, notify_admins=False, notify_user=False)
        assert (result is not None) == (scenario in ('assign', 'already-assigned'))

        # Keep the webhook session OPEN. A separate connection checks the real
        # FK lock acquired when admin notifications insert subscription events.
        await notification.execute(text("SET LOCAL lock_timeout = '500ms'"))
        notification.add(SubscriptionEvent(user_id=1, event_type='test', message='Test notification'))
        await notification.commit()
        assert await notification.scalar(select(User.balance_kopeks).where(User.id == 1)) == 5000
        if scenario != 'no-spending':
            assert await notification.scalar(select(Transaction.is_completed).where(Transaction.id == 1)) is True
        if result is not None:
            assert (
                await notification.scalar(select(UserPromoGroup.promo_group_id).where(UserPromoGroup.user_id == 1)) == 1
            )


async def test_deferred_transaction_does_not_commit_payment_or_run_promo_check(postgres_database, monkeypatch):
    from unittest.mock import AsyncMock

    from app.database.crud.transaction import create_transaction
    from app.database.models import TransactionType

    assignment = AsyncMock()
    monkeypatch.setattr('app.services.promo_group_assignment.maybe_assign_promo_group_by_total_spent', assignment)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (payment, observer):
        user = User(id=1, balance_kopeks=5000, language='ru')
        payment.add(user)
        await payment.commit()
        user.balance_kopeks = 4000
        await create_transaction(payment, 1, TransactionType.SUBSCRIPTION_PAYMENT, 1000, 'Test', commit=False)
        assignment.assert_not_awaited()
        assert await observer.scalar(select(User.balance_kopeks).where(User.id == 1)) == 5000
        assert await observer.scalar(select(Transaction.id)) is None
        await payment.rollback()
        await observer.rollback()
        assert await observer.scalar(select(User.balance_kopeks).where(User.id == 1)) == 5000
        assert await observer.scalar(select(Transaction.id)) is None
