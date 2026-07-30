"""Rollback внутри активации промокода не должен ломать ORM-объекты вызывающего.

Регрессия (MissingGreenlet при триал-промокоде): ветки отказа в
``activate_promocode`` делают ``db.rollback()``, который экспирирует все объекты
сессии — включая ``db_user`` хендлера (та же сессия → identity map → тот же
инстанс). Последующий ``db_user.language`` в error-ветке хендлера запускал
ленивую догрузку без greenlet-моста и падал ``MissingGreenlet`` вместо
человеческого сообщения об ошибке.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect as sa_inspect

from app.database.models import (
    PromoCode,
    PromoCodeType,
    PromoCodeUse,
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    UserPromoGroup,
    UserStatus,
)
from app.services.promocode_service import PromoCodeService
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    PromoCode.__table__,
    PromoCodeUse.__table__,
    UserPromoGroup.__table__,
)


async def test_failed_trial_activation_keeps_user_attributes_loaded(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        user = User(
            telegram_id=111,
            username='user111',
            first_name='User',
            status=UserStatus.ACTIVE.value,
            language='ru',
            balance_kopeks=0,
        )
        db.add(user)
        await db.commit()

        # Существующая подписка блокирует триал-промокод → ValueError → rollback
        db.add(
            Subscription(
                user_id=user.id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now - timedelta(days=5),
                end_date=now + timedelta(days=25),
                remnawave_short_id='short1',
            )
        )
        db.add(
            PromoCode(
                code='TRIAL1',
                type=PromoCodeType.TRIAL_SUBSCRIPTION.value,
                subscription_days=7,
                max_uses=10,
                current_uses=0,
                is_active=True,
                valid_from=now - timedelta(days=1),
                valid_until=now + timedelta(days=1),
            )
        )
        await db.commit()

        result = await PromoCodeService().activate_promocode(db, user.id, 'TRIAL1')

        assert result == {'success': False, 'error': 'trial_subscription_exists'}
        # После отказа объект юзера обязан остаться пригодным: хендлер сразу
        # читает db_user.language для рендера сообщения об ошибке.
        assert not sa_inspect(user).expired
        assert user.language == 'ru'
