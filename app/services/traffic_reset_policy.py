"""Правила обнуления израсходованного трафика при суточной оплате.

Суточный тариф выпадал из общего правила проекта. Покупка, продление, смена
тарифа и ручное продление админом спрашивают выключатель
``RESET_TRAFFIC_ON_PAYMENT``, а суточное списание обнуление не делало никогда —
в каждой копии стояла жёсткая константа. Для человека это выглядело так: он
платит каждые сутки, а израсходованный трафик копится с самой первой покупки и
однажды упирается в лимит, хотя ни одного пропущенного платежа не было. Обойти
это можно было только тарифной настройкой ``traffic_reset_mode``, то есть
переложив сброс на панель.

Здесь правило одно на все суточные списания: планировщик бота, возобновление
из кабинета, из Mini App и из меню бота, авто-возобновление после пополнения.
Места суточной оплаты находит по коду сторож
``tests/services/test_daily_charge_reset_policy_guard.py`` — новый поток
обязан спрашивать это правило, а не подставлять константу.

Вторая половина того же правила — подписка в статусе «трафик исчерпан».
Оплата новых суток обнуляет счётчик, значит и лимит должен сняться, но PATCH
сам по себе статус ``LIMITED`` не снимает: после оплаты со сбросом аккаунт в
панели включается явно (``lift_panel_traffic_limit``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.external.remnawave_api import TrafficLimitStrategy
from app.services.panel_sync.traffic_strategy import get_traffic_reset_strategy


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import Subscription
    from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


def should_reset_traffic_on_daily_charge(tariff: object | None) -> bool:
    """Обнулять ли израсходованный трафик после успешного суточного списания.

    Суточное списание — обычная оплата, поэтому решает общий выключатель
    ``RESET_TRAFFIC_ON_PAYMENT``: выключен — счётчик не трогаем (прежнее
    поведение и значение по умолчанию).

    Исключение — тариф, которому панель сама обнуляет счётчик раз в сутки
    (``traffic_reset_mode='DAY'``, либо та же стратегия в общей настройке).
    Наш сброс пришёлся бы на другое время суток, и за календарные сутки человек
    получал бы две квоты вместо одной. Более редкие стратегии панели
    (``WEEK``/``MONTH``/``MONTH_ROLLING``) суточную квоту не покрывают, поэтому
    там обнуляем сами.
    """
    if not settings.RESET_TRAFFIC_ON_PAYMENT:
        return False

    return get_traffic_reset_strategy(tariff) is not TrafficLimitStrategy.DAY


async def lift_panel_traffic_limit(
    db: AsyncSession,
    subscription: Subscription,
    *,
    service: SubscriptionService,
) -> None:
    """Снять с аккаунта в панели статус «трафик исчерпан» после оплаты новых суток.

    Обнуления счётчика обычно достаточно, но PATCH сам по себе статус LIMITED
    не снимает (та же оговорка стоит во всех админских реактивациях). Явное
    включение делает возврат независимым от того, как панель обработала сброс.

    ``service`` — тот же экземпляр, которым вызывающий только что синхронизировал
    подписку: так решение «куда писать» остаётся у него, а не у этого помощника.
    Ошибка панели здесь не роняет оплату: деньги уже взяты, подписка активна,
    аккаунт включит ближайший проход мониторинга.
    """
    if settings.is_multi_tariff_enabled():
        panel_user_id = getattr(subscription, 'remnawave_id', None)
    else:
        panel_user_id = getattr(await get_user_by_id(db, subscription.user_id), 'remnawave_id', None)
    if not panel_user_id:
        return

    try:
        await service.enable_remnawave_user(panel_user_id, db=db)
    except Exception as exc:
        logger.warning(
            'Не удалось снять лимит трафика в панели после оплаты суток',
            subscription_id=subscription.id,
            error=exc,
        )
