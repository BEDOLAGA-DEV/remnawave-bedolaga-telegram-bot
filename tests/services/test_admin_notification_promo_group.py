"""Загрузка промогруппы для админ-уведомлений не должна падать MissingGreenlet.

Прод-репорт: уведомление о регистрации по рекламной кампании падало на
`getattr(user, 'promo_group', None)`. Дефолт у getattr срабатывает только на
AttributeError, а незагруженная связь в async-сессии лезет в базу — и падает.

Условие возникает штатно: apply_campaign_bonus перечитывает пользователя через
`db.refresh(user)` (это фикс прошлого MissingGreenlet), а refresh сбрасывает ранее
загруженные связи, включая promo_group.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import MissingGreenlet

from app.database.models import (
    PromoGroup,
    ServerSquad,
    Subscription,
    Tariff,
    User,
    UserPromoGroup,
    UserStatus,
    server_squad_promo_groups,
)
from app.services.admin_notification_service import AdminNotificationService, _loaded_relationship
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    # PromoGroup.server_squads объявлена lazy='selectin' — грузится вместе с группой
    ServerSquad.__table__,
    server_squad_promo_groups,
)


async def _seed(db) -> tuple[User, PromoGroup]:
    group = PromoGroup(name='База', server_discount_percent=0, traffic_discount_percent=0, device_discount_percent=0)
    db.add(group)
    await db.commit()
    await db.refresh(group)

    user = User(
        telegram_id=8107525854,
        username='SMMXTG',
        first_name='Кампанейский',
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
        promo_group_id=group.id,
    )
    user.promo_group = group
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user, group


async def test_promo_group_resolved_after_refresh_dropped_the_relationship(monkeypatch) -> None:
    """Точное воспроизведение прода: refresh сбросил связь, уведомление не должно упасть."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, group = await _seed(db)
        group_id = group.id

        # ровно то, что делает apply_campaign_bonus перед отправкой уведомления
        await db.refresh(user)
        assert 'promo_group' in sa_inspect(user).unloaded, 'refresh обязан сбросить связь'

        # Без этого many-to-one резолвится из identity map БЕЗ запроса и баг не
        # воспроизводится: в проде группы в карте сессии не было.
        db.expunge(group)
        with pytest.raises(MissingGreenlet):
            getattr(user, 'promo_group', None)  # ← ровно то, на чём падало

        service = AdminNotificationService(SimpleNamespace())
        resolved = await service._get_user_promo_group(db, user)

        assert resolved is not None
        assert resolved.id == group_id
        assert resolved.name == 'База'


async def test_promo_group_returned_when_already_loaded(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user, group = await _seed(db)

        service = AdminNotificationService(SimpleNamespace())
        resolved = await service._get_user_promo_group(db, user)

        assert resolved is not None and resolved.id == group.id


async def test_user_without_promo_group_returns_none(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            telegram_id=999001,
            username='nogroup',
            first_name='Без группы',
            status=UserStatus.ACTIVE.value,
            language='ru',
            balance_kopeks=0,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        service = AdminNotificationService(SimpleNamespace())

        assert await service._get_user_promo_group(db, user) is None


def test_loaded_relationship_never_triggers_io_for_unloaded() -> None:
    """Хелпер обязан отдавать None, а не лезть в базу."""
    user = User(telegram_id=1, username='u', first_name='U', promo_group_id=5)
    # Свежесозданный инстанс: связь не загружена
    assert _loaded_relationship(user, 'promo_group') is None


def test_loaded_relationship_falls_back_for_non_orm_objects() -> None:
    """Тестовые фейки не инспектируются SQLAlchemy — для них работает обычный getattr."""
    fake = SimpleNamespace(promo_group=SimpleNamespace(name='Фейк'))

    assert _loaded_relationship(fake, 'promo_group').name == 'Фейк'
    assert _loaded_relationship(SimpleNamespace(), 'promo_group') is None
