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
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import ALIVE_SUBSCRIPTION_STATUSES, decrement_subscription_server_counts
from app.database.models import Subscription, User


logger = structlog.get_logger(__name__)


async def _panel_account_is_referenced(db: AsyncSession, subscription: Subscription, panel_user_id: int) -> bool:
    """A unique numeric ID does not exclude another row linked by shortUuid."""
    foreign_owner = await db.scalar(
        select(User.id).where(User.remnawave_id == panel_user_id, User.id != subscription.user_id).limit(1)
    )
    if foreign_owner is not None:
        return True

    references = [Subscription.id != subscription.id]
    if not settings.is_multi_tariff_enabled():
        # Single-tariff rows intentionally share the owner's account. Dead
        # history must not prevent revoking their last live subscription.
        references.append(
            or_(
                Subscription.user_id != subscription.user_id,
                Subscription.status.in_(tuple(ALIVE_SUBSCRIPTION_STATUSES)),
            )
        )
    identities = [Subscription.remnawave_id == panel_user_id]
    # Single-tariff targets the owner's ID, which may differ from a historical
    # subscription ID. That row's shortUuid cannot identify the owner's account.
    short_uuid = subscription.remnawave_short_uuid if subscription.remnawave_id == panel_user_id else None
    if short_uuid:
        identities.append(Subscription.remnawave_short_uuid == short_uuid)
    reference = await db.scalar(select(Subscription.id).where(*references, or_(*identities)).limit(1))
    if reference is not None:
        return True
    if short_uuid:
        return False

    # Old duplicates may only record a numeric ID. Resolve just this account,
    # only when other shortUuid references exist; never guess by Telegram/email.
    possible_reference = await db.scalar(
        select(Subscription.id)
        .where(
            *references,
            Subscription.remnawave_short_uuid.is_not(None),
            Subscription.remnawave_short_uuid != '',
        )
        .limit(1)
    )
    if possible_reference is None:
        return False
    from app.services.subscription_service import SubscriptionService

    try:
        async with SubscriptionService().get_api_client() as api:
            panel_user = await api.get_user_by_id(panel_user_id)
        if panel_user is None:
            return False  # Already absent, so an idempotent delete cannot cut off a sibling.
        short_uuid = panel_user.short_uuid
        if not short_uuid:
            return True
    except Exception as error:
        logger.warning(
            'Cannot verify shared panel identity; preserving account', panel_user_id=panel_user_id, error=error
        )
        return True
    reference = await db.scalar(
        select(Subscription.id).where(*references, Subscription.remnawave_short_uuid == short_uuid).limit(1)
    )
    return reference is not None


async def _resolve_panel_target(db: AsyncSession, subscription: Subscription) -> tuple[int | None, bool]:
    """Панельный аккаунт этой подписки и можно ли его удалять.

    Возвращает ``(panel_user_id, deletable)``.

    В мультитарифе аккаунт принадлежит подписке, но старый дубль может
    ссылаться на тот же аккаунт по shortUuid. Такую панель не трогаем.

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
        panel_user_id, deletable = subscription.remnawave_id, True
    else:
        panel_user_id = await db.scalar(select(User.remnawave_id).where(User.id == subscription.user_id))
        deletable = not bool(panel_user_id)
        if panel_user_id:
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
        else:
            # Historical rows can retain the identity only on the subscription.
            panel_user_id = subscription.remnawave_id

    if panel_user_id and await _panel_account_is_referenced(db, subscription, panel_user_id):
        logger.info(
            'Preserving panel account referenced by another record',
            subscription_id=subscription.id,
            panel_user_id=panel_user_id,
        )
        return None, False
    return panel_user_id, deletable


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

    from app.services.cashera_recurring_cancel import cancel_cashera_recurring_for_subscription_safe
    from app.services.payment.lava import cancel_lava_recurring_for_subscription_safe
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    await cancel_platega_recurring_for_subscription_safe(db, subscription.id)
    await cancel_lava_recurring_for_subscription_safe(db, subscription.id)
    await cancel_cashera_recurring_for_subscription_safe(db, subscription.id)

    # Автоплатёжки закоммитили своё — advisory-lock отпущен, берём заново.
    await ensure_no_open_grace_for_subscriptions(db, (subscription.id,))
    # A concurrent delete can have finished while we waited for the guard.
    # The request's identity-map copy is not proof that the row still exists.
    subscription = await db.get(Subscription, subscription.id, populate_existing=True)
    if subscription is None:
        return

    panel_user_id, panel_deletable = await _resolve_panel_target(db, subscription)
    panel_account_deleted = False
    if panel_user_id:
        try:
            from app.services.remnawave_webhook_service import RemnaWaveWebhookService
            from app.services.subscription_service import SubscriptionService

            service = SubscriptionService()
            # ``REMNAWAVE_USER_DELETE_MODE=disable`` — аккаунт в панели не удаляем
            # никогда, даже когда он принадлежит только этой подписке.
            if panel_deletable and settings.get_remnawave_user_delete_mode() == 'delete':
                RemnaWaveWebhookService.mark_intentional_panel_deletion(panel_user_ids=[panel_user_id])
                panel_account_deleted = await service.delete_remnawave_user(panel_user_id)
            else:
                # Общий аккаунт однотарифного режима (или режим disable): доступ
                # снимаем, но сам аккаунт оставляем — на него ещё смотрит
                # users.remnawave_id, и следующая покупка переиспользует его же.
                await service.disable_remnawave_user(panel_user_id, db=db)
        except Exception as error:
            logger.warning('Failed to delete RemnaWave user on subscription delete', error=error)

    await decrement_subscription_server_counts(db, subscription)

    subscription_id = subscription.id
    tariff_id = subscription.tariff_id
    user_id = subscription.user_id

    if panel_account_deleted:
        # Первый аккаунт мультитарифа записан и человеку (users.remnawave_id). Оставить
        # там id удалённого аккаунта — значит отдать его следующей покупке: правило
        # should_create_panel_account привяжет «свободный аккаунт человека» к новой
        # строке, и каждый запрос в панель по ней ответит «User not found».
        await db.execute(
            update(User).where(User.id == user_id, User.remnawave_id == panel_user_id).values(remnawave_id=None)
        )

    await db.delete(subscription)
    await db.commit()

    logger.info(
        'Subscription deleted',
        subscription_id=subscription_id,
        user_id=user_id,
        tariff_id=tariff_id,
        deleted_by=deleted_by,
    )
