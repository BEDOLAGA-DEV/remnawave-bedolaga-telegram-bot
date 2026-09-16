"""Кнопка «Сбросить триал» должна открывать триал заново — иначе она бесполезна.

Владелец нажал её в кабинете (меню «⋯» в карточке пользователя) и ничего не
получил: человек по-прежнему не мог взять триал. Помогло только удаление
аккаунта через админку бота.

Причина: доступность триала закрывают ДВЕ вещи — любая существующая подписка и
отметка «человек когда-то платил» (`users.has_had_paid_subscription`). Сброс
сносил подписки, а отметку не трогал, и у всех, кто хоть раз платил, триал
оставался закрыт навсегда. Ответ при этом всегда был «Триал успешно сброшен» —
кнопка врала.

Отметку нельзя просто снять: по ней считаются конверсия и выручка. Поэтому
сброс ставит свою дату (`users.trial_reset_at`), и она перекрывает отметку
ровно до того момента, пока человек снова не заведёт подписку.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.cabinet.routes import admin_users
from app.database.models import (
    GraceAccessSessionModel,
    PromoGroup,
    ServerSquad,
    Subscription,
    SubscriptionServer,
    SubscriptionStatus,
    Tariff,
    Transaction,
    User,
    UserPromoGroup,
    UserStatus,
    tariff_promo_groups,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    Transaction.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    GraceAccessSessionModel.__table__,
    ServerSquad.__table__,
    SubscriptionServer.__table__,
    tariff_promo_groups,
)

NOW = datetime.now(UTC)


def _user(**extra) -> User:
    return User(
        telegram_id=1,
        username='payer',
        first_name='Payer',
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
        **extra,
    )


def _subscription(user: User, *, is_trial: bool, status: str, days: int) -> Subscription:
    return Subscription(
        user_id=user.id,
        status=status,
        is_trial=is_trial,
        start_date=NOW - timedelta(days=30),
        end_date=NOW + timedelta(days=days),
        traffic_limit_gb=10,
        device_limit=1,
        remnawave_short_id=f'short{user.id}{int(is_trial)}{days}',
    )


async def _reset(db, user_id: int):
    return await admin_users.reset_user_trial(
        user_id=user_id,
        request=admin_users.ResetTrialRequest(),
        admin=_user(),
        db=db,
    )


async def test_paid_once_but_nothing_left_gets_the_trial_back(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = _user(has_had_paid_subscription=True)
        db.add(user)
        await db.commit()

        assert user.is_trial_already_used() is True

        response = await _reset(db, user.id)

        assert response.success is True
        await db.refresh(user, ['subscriptions'])
        assert user.is_trial_already_used() is False


async def test_expired_trial_of_a_former_payer_is_wiped_and_reopened(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = _user(has_had_paid_subscription=True)
        db.add(user)
        await db.flush()
        db.add(_subscription(user, is_trial=True, status=SubscriptionStatus.EXPIRED.value, days=-5))
        await db.commit()

        response = await _reset(db, user.id)

        assert response.subscription_deleted is True
        await db.refresh(user, ['subscriptions'])
        assert user.subscriptions == []
        assert user.is_trial_already_used() is False


async def test_new_trial_after_the_reset_closes_it_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сброс одноразовый: взял новый триал — снова закрыто, второй раз не выдаст."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = _user(has_had_paid_subscription=True)
        db.add(user)
        await db.commit()
        await _reset(db, user.id)

        db.add(_subscription(user, is_trial=True, status=SubscriptionStatus.TRIAL.value, days=3))
        await db.commit()
        await db.refresh(user, ['subscriptions'])

        assert user.is_trial_already_used() is True


async def test_live_paid_subscription_gets_an_honest_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Живая платная подписка сама закрывает триал — кнопка обязана сказать это, а не врать."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = _user(has_had_paid_subscription=True)
        db.add(user)
        await db.flush()
        db.add(_subscription(user, is_trial=False, status=SubscriptionStatus.ACTIVE.value, days=20))
        await db.commit()

        response = await _reset(db, user.id)

        assert response.success is False
        await db.refresh(user, ['subscriptions'])
        assert user.is_trial_already_used() is True
        assert len(user.subscriptions) == 1, 'платную подписку сброс триала сносить не должен'
