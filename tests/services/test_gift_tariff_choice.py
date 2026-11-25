"""Мультитариф: тариф подарка у получателя уже жив — сначала спросить.

Без ответа подарок остаётся привязанным и ждущим активации, а получатель видит
выбор: добавить дни к одной из живых подписок тарифа или активировать подарок
отдельной копией. В одиночном режиме вопроса нет.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.landing import generate_purchase_token
from app.database.models import Base, GuestPurchase, GuestPurchaseStatus, Subscription, Tariff, User
from app.services.gift_claim_service import claim_bound_gift_for_user, claim_gift_for_user
from app.services.guest_purchase_service import TariffChoiceRequiredError
from tests.fixtures.sqlite_memory import memory_session


# Активация проходит через продление со всеми его сверками (автоплатежи, грейс),
# поэтому схема — целиком, как у боевой базы.
_TABLES = list(Base.metadata.sorted_tables)


async def _seed(db, *, owned: bool = True):
    now = datetime.now(UTC)
    db.add_all(
        [
            Tariff(
                id=1, name='Premium', is_active=True, device_limit=2, traffic_limit_gb=50, period_prices={'30': 300}
            ),
            User(id=20, telegram_id=22222, username='recipient', balance_kopeks=0),
        ]
    )
    if owned:
        db.add(
            Subscription(
                id=5,
                user_id=20,
                tariff_id=1,
                status='active',
                is_trial=False,
                start_date=now - timedelta(days=1),
                end_date=now + timedelta(days=10),
                device_limit=2,
                remnawave_short_id='own-5',
            )
        )
    token = generate_purchase_token()
    purchase = GuestPurchase(
        token=token,
        contact_type='email',
        contact_value='gift@example.com',
        is_gift=True,
        status=GuestPurchaseStatus.PAID.value,
        tariff_id=1,
        period_days=30,
        amount_kopeks=300,
    )
    db.add(purchase)
    await db.commit()
    return token, purchase


async def _subscriptions(db):
    return (await db.execute(select(Subscription).where(Subscription.user_id == 20))).scalars().all()


@pytest.fixture
def multi_tariff(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')


async def test_owned_tariff_asks_and_keeps_the_gift_bound(monkeypatch, multi_tariff):
    async with memory_session(monkeypatch, _TABLES) as db:
        token, purchase = await _seed(db)

        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()) as prov:
            with pytest.raises(TariffChoiceRequiredError) as asked:
                await claim_gift_for_user(db, claimant_user_id=20, claim_input=token)

        prov.assert_not_awaited()
        assert [subscription.id for subscription in asked.value.subscriptions] == [5]
        await db.refresh(purchase)
        assert purchase.user_id == 20
        assert purchase.status == GuestPurchaseStatus.PENDING_ACTIVATION.value
        assert asked.value.as_detail()['code'] == 'tariff_choice_required'


async def test_answer_separate_copy_creates_a_second_subscription(monkeypatch, multi_tariff):
    async with memory_session(monkeypatch, _TABLES) as db:
        token, purchase = await _seed(db)
        with pytest.raises(TariffChoiceRequiredError):
            await claim_gift_for_user(db, claimant_user_id=20, claim_input=token)

        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            result = await claim_bound_gift_for_user(db, claimant_user_id=20, purchase_id=purchase.id, as_copy=True)

        assert result.status == GuestPurchaseStatus.DELIVERED.value
        rows = await _subscriptions(db)
        assert len(rows) == 2
        new_copy = next(row for row in rows if row.id != 5)
        assert new_copy.copy_key is not None
        assert new_copy.tariff_id == 1


async def test_answer_extend_adds_the_days_to_the_chosen_subscription(monkeypatch, multi_tariff):
    async with memory_session(monkeypatch, _TABLES) as db:
        token, purchase = await _seed(db)
        before = (await db.get(Subscription, 5)).end_date

        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            result = await claim_gift_for_user(db, claimant_user_id=20, claim_input=token, target_subscription_id=5)

        assert result.status == GuestPurchaseStatus.DELIVERED.value
        rows = await _subscriptions(db)
        assert [row.id for row in rows] == [5]
        extended = rows[0].end_date if rows[0].end_date.tzinfo else rows[0].end_date.replace(tzinfo=UTC)
        before = before if before.tzinfo else before.replace(tzinfo=UTC)
        assert extended - before >= timedelta(days=29)


async def test_no_question_without_the_tariff(monkeypatch, multi_tariff):
    async with memory_session(monkeypatch, _TABLES) as db:
        token, _ = await _seed(db, owned=False)

        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            result = await claim_gift_for_user(db, claimant_user_id=20, claim_input=token)

        assert result.status == GuestPurchaseStatus.DELIVERED.value


async def test_single_tariff_mode_never_asks(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    async with memory_session(monkeypatch, _TABLES) as db:
        token, _ = await _seed(db)

        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            result = await claim_gift_for_user(db, claimant_user_id=20, claim_input=token)

        assert result.status == GuestPurchaseStatus.DELIVERED.value
