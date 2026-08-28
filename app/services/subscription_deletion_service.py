"""Единый порядок удаления подписки: грейс-гард, автоплатежи, панель, строка.

Порядок шагов здесь не косметический, а выстраданный:

* грейс-гард берёт advisory-lock, поэтому проверка идёт первой — удалять
  подписку с открытым временным доступом нельзя;
* отмена автоплатежей идёт ДО удаления строки: записи платёжек уходят по
  CASCADE вместе с ней, и отменять потом было бы уже нечего. Отмена
  коммитит свою транзакцию и тем самым отпускает advisory-lock — поэтому
  сразу за ней гард берётся повторно, закрывая окно перед необратимыми
  шагами;
* удаление юзера в панели помечается как намеренное, иначе прилетевший
  ``user.deleted`` вебхук своей чисткой заденет соседние живые подписки
  того же владельца.

Панельный аккаунт адресуется по-разному в двух режимах, см.
``_resolve_panel_target``.

Вызывающий сам решает, как отвечать на ``GraceAccessDeletionBlocked``:
кабинет отдаёт 409, массовые действия — пропуск с пояснением.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import ALIVE_SUBSCRIPTION_STATUSES, decrement_subscription_server_counts
from app.database.models import Subscription, User


logger = structlog.get_logger(__name__)


async def _resolve_panel_target(db: AsyncSession, subscription: Subscription) -> tuple[int | None, bool]:
    """Панельный аккаунт этой подписки и можно ли его удалять.

    Возвращает ``(panel_user_id, deletable)``.

    В мультитарифе аккаунт принадлежит подписке: у каждой строки свой
    ``remnawave_id``, удаление строки честно уносит и аккаунт.

    В однотарифном режиме аккаунт принадлежит ПОЛЬЗОВАТЕЛЮ. Колонка подписки
    там пуста у всех строк, кроме одной: ``uq_subscriptions_remnawave_id``
    частично уникален, поэтому второй строке id не пишется, а адресация
    остаётся через ``users.remnawave_id`` (см. subscription_service). Читать
    только ``subscription.remnawave_id`` значит для таких строк молча
    пропустить панель — админ снимает доступ, а VPN у человека продолжает
    работать. Тот же режим у синхронизации кабинета по email/OAuth, которая
    создаёт строку с ``remnawave_id=None`` намеренно.

    Обратная сторона общего аккаунта: удалять его нельзя, пока у человека
    осталась ЖИВАЯ соседняя подписка — она живёт на этом же аккаунте, и её
    доступ умер бы заодно. В этом случае аккаунт только отключается, если
    удаляемая строка была последней живой, и не трогается вовсе, если живая
    соседка есть.
    """
    if settings.is_multi_tariff_enabled():
        return subscription.remnawave_id, True

    panel_user_id = await db.scalar(select(User.remnawave_id).where(User.id == subscription.user_id))
    if not panel_user_id:
        # Историческая строка, у которой id остался только на подписке.
        return subscription.remnawave_id, True

    alive_sibling = await db.scalar(
        select(Subscription.id)
        .where(
            Subscription.user_id == subscription.user_id,
            Subscription.id != subscription.id,
            Subscription.status.in_(tuple(ALIVE_SUBSCRIPTION_STATUSES)),
        )
        .limit(1)
    )
    if alive_sibling is not None:
        return None, False
    return panel_user_id, False


async def delete_subscription_record(
    db: AsyncSession,
    subscription: Subscription,
    *,
    deleted_by: str,
) -> None:
    """Удалить подписку целиком. Грейс-гард пробрасывается наружу.

    ``deleted_by`` попадает в лог — по нему видно, кто инициировал:
    сам владелец из кабинета или администратор из карточки.
    """
    from app.services.grace_access_runtime import ensure_no_open_grace_for_subscriptions

    await ensure_no_open_grace_for_subscriptions(db, (subscription.id,))

    from app.services.payment.lava import cancel_lava_recurring_for_subscription_safe
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    await cancel_platega_recurring_for_subscription_safe(db, subscription.id)
    await cancel_lava_recurring_for_subscription_safe(db, subscription.id)

    # Автоплатёжки закоммитили своё — advisory-lock отпущен, берём заново.
    await ensure_no_open_grace_for_subscriptions(db, (subscription.id,))

    panel_user_id, panel_deletable = await _resolve_panel_target(db, subscription)
    if panel_user_id:
        try:
            from app.services.remnawave_webhook_service import RemnaWaveWebhookService
            from app.services.subscription_service import SubscriptionService

            service = SubscriptionService()
            if panel_deletable:
                RemnaWaveWebhookService.mark_intentional_panel_deletion(panel_user_ids=[panel_user_id])
                await service.delete_remnawave_user(panel_user_id)
            else:
                # Общий аккаунт однотарифного режима: доступ снимаем, но сам
                # аккаунт оставляем — на него ещё смотрит users.remnawave_id,
                # и следующая покупка переиспользует его же.
                await service.disable_remnawave_user(panel_user_id, db=db)
        except Exception as error:
            logger.warning('Failed to delete RemnaWave user on subscription delete', error=error)

    await decrement_subscription_server_counts(db, subscription)

    subscription_id = subscription.id
    tariff_id = subscription.tariff_id
    user_id = subscription.user_id

    await db.delete(subscription)
    await db.commit()

    logger.info(
        'Subscription deleted',
        subscription_id=subscription_id,
        user_id=user_id,
        tariff_id=tariff_id,
        deleted_by=deleted_by,
    )
