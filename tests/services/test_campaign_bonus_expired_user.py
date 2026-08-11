"""Регрессия (прод-репорт 2026-07-13): MissingGreenlet при начислении бонуса
рекламной кампании на регистрации.

Любой rollback выше по флоу регистрации (реферал, промокод, phantom-merge)
экспайрит ORM-инстанс пользователя даже при expire_on_commit=False. Дальше
``apply_campaign_bonus`` читал ``user.id`` синхронно → ленивая подгрузка
протухшего атрибута вне greenlet → ``sqlalchemy.exc.MissingGreenlet``, бонус
кампании не начислялся (у юзера падала регистрация по рекламной ссылке).

Фикс: сервис асинхронно освежает пользователя ДО первого sync-доступа к
атрибутам; сбой refresh не роняет начисление.
"""

from unittest.mock import AsyncMock, MagicMock

from app.services.campaign_service import AdvertisingCampaignService


def _service(monkeypatch) -> AdvertisingCampaignService:
    # SubscriptionService в конструкторе не нужен для этих сценариев
    monkeypatch.setattr('app.services.campaign_service.SubscriptionService', MagicMock())
    return AdvertisingCampaignService()


def _db() -> AsyncMock:
    db = AsyncMock()
    db.refresh = AsyncMock()
    return db


async def test_apply_campaign_bonus_refreshes_user_before_attribute_access(monkeypatch):
    """Пользователь перечитывается асинхронно на входе — до любых sync-чтений
    user.id (иначе expired-инстанс валит MissingGreenlet)."""
    service = _service(monkeypatch)
    db = _db()
    campaign = MagicMock(is_active=False, id=1)  # ранний выход, но refresh обязан случиться
    user = MagicMock()

    result = await service.apply_campaign_bonus(db, user, campaign)

    db.refresh.assert_awaited_once_with(user)
    assert result.success is False


async def test_apply_campaign_bonus_survives_refresh_failure(monkeypatch):
    """Сбой refresh (например, PendingRollbackError) не роняет начисление —
    логируем и продолжаем со старыми атрибутами."""
    service = _service(monkeypatch)
    db = _db()
    db.refresh = AsyncMock(side_effect=RuntimeError('session in failed state'))
    campaign = MagicMock(is_active=False, id=1)
    user = MagicMock()

    result = await service.apply_campaign_bonus(db, user, campaign)

    assert result.success is False  # дошли до обычной логики, исключение не всплыло
