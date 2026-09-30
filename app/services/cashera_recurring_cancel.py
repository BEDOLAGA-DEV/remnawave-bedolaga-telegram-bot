"""Отмена, состояние и уведомления автопродления Cashera — без тяжёлых зависимостей.

Хуки отмены стоят на всех путях удаления/замены подписки (crud.subscription,
subscription_service, кабинет, админка). Модуль нарочно зависит только от
CRUD своей таблицы и HTTP-клиента: импорт ``payment.cashera`` отсюда замкнул бы
кольцо crud.subscription → payment.cashera → monitoring_service → crud.subscription
(CodeQL py/cyclic-import на PR #3292).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import cashera_subscription as sub_crud
from app.database.models import User
from app.services.cashera_service import cashera_service
from app.utils.payment_logger import payment_logger as logger


async def cancel_cashera_recurrent_subscription(db: AsyncSession, *, local_id: int, commit: bool = True) -> bool:
    """Отменяет одну подписку Cashera по локальному id. Идемпотентна.

    Удалённая отмена — best-effort: сбой не мешает пометить запись CANCELLED
    (иначе недоступность Cashera блокировала бы отмену навсегда); недошедшую
    отмену добьёт reconciler по свипу CANCELLED-записей.
    """
    record = await sub_crud.get_cashera_subscription_by_id(db, local_id)
    if not record:
        return False
    if record.status == 'CANCELLED':
        return True

    if record.cashera_subscription_uuid:
        try:
            await cashera_service.cancel_subscription(record.cashera_subscription_uuid)
        except Exception as error:  # pragma: no cover - network errors
            logger.warning(
                'Cashera: не удалось отменить подписку на стороне провайдера',
                cashera_uuid=record.cashera_subscription_uuid,
                error=str(error),
            )

    record.status = 'CANCELLED'
    if commit:
        await db.commit()
    else:
        # Вызывающий держит свою транзакцию — CANCELLED войдёт в неё.
        await db.flush()
    return True


async def cancel_cashera_recurring_for_subscription(
    db: AsyncSession, subscription_id: int, *, commit: bool = True
) -> None:
    """Best-effort отмена живой подписки Cashera по subscription_id; не бросает."""
    try:
        record = await sub_crud.get_active_cashera_subscription_by_subscription(db, subscription_id)
        if not record:
            return
        await cancel_cashera_recurrent_subscription(db, local_id=record.id, commit=commit)
    except Exception as error:  # pragma: no cover - best-effort cleanup
        logger.warning(
            'Cashera: не удалось отменить автопродление по подписке',
            subscription_id=subscription_id,
            error=str(error),
        )


async def cancel_cashera_recurring_for_subscription_safe(
    db: AsyncSession,
    subscription_id: int,
    *,
    commit: bool = True,
) -> None:
    """Отмена автопродления Cashera на путях удаления/замены подписки. Никогда не бросает.

    НЕ гейтится флагом рекуррента намеренно: отмена — операция безопасности.
    Выключение CASHERA_RECURRENT_ENABLED не останавливает списания у Cashera.
    """
    try:
        await cancel_cashera_recurring_for_subscription(db, subscription_id, commit=commit)
    except Exception as error:  # pragma: no cover - defensive
        logger.warning('Cashera: не удалось отменить автопродление', error=str(error), subscription_id=subscription_id)


async def cancel_cashera_recurring_by_local_id(db: AsyncSession, local_id: int) -> bool:
    """Отмена привязки по локальному id (кабинет/бот). Идемпотентна."""
    return await cancel_cashera_recurrent_subscription(db, local_id=local_id)


async def get_cashera_recurring_status(db: AsyncSession, subscription_id: int) -> dict[str, Any] | None:
    """Состояние живой привязки для UI (бот/кабинет) либо None."""
    record = await sub_crud.get_active_cashera_subscription_by_subscription(db, subscription_id)
    if not record:
        return None
    return {
        'local_id': record.id,
        'cashera_subscription_uuid': record.cashera_subscription_uuid,
        'status': record.status,
        'amount_kopeks': record.amount_kopeks,
        'charge_days': record.charge_days,
        'interval': record.interval,
        'redirect_url': record.redirect_url,
        'next_charge_at': record.next_charge_at,
        'last_charge_at': record.last_charge_at,
        'charges_success': record.charges_success,
        'charges_failed': record.charges_failed,
    }


async def notify_cashera_recurring(db: AsyncSession, record: Any, kind: str, *, bot: Any = None) -> None:
    """Best-effort уведомление о событии автопродления; никогда не бросает.

    kind: activated (подтверждена привязка), confirmed (списание прошло),
    failed (списание не прошло), cancelled.
    """
    if not settings.is_notifications_enabled():
        return
    try:
        from app.cabinet.ws_manager import cabinet_ws_manager

        await cabinet_ws_manager.send_to_user(
            record.user_id,
            {
                'type': f'cashera_recurring.{kind}',
                'status': record.status,
                'amount_kopeks': record.amount_kopeks,
                'amount_rubles': record.amount_kopeks / 100,
                'next_charge_at': record.next_charge_at.isoformat() if record.next_charge_at else None,
                'subscription_id': record.subscription_id,
            },
        )
    except Exception as ws_error:  # pragma: no cover — best-effort
        logger.warning('Cashera: не удалось отправить WS-событие автопродления', error=str(ws_error), kind=kind)

    if not bot:
        return
    try:
        from app.localization.texts import get_texts

        user = await db.get(User, record.user_id)
        if not user or not user.telegram_id:
            return
        texts = get_texts(user.language)
        messages = {
            'activated': texts.t('CASHERA_RECURRING_NOTIFY_ACTIVATED', '✅ Автопродление через Cashera подключено.'),
            'confirmed': texts.t('CASHERA_RECURRING_NOTIFY_CONFIRMED', '✅ Подписка продлена автосписанием Cashera.'),
            'failed': texts.t(
                'CASHERA_RECURRING_NOTIFY_FAILED', '⚠️ Не удалось списать оплату по автопродлению Cashera.'
            ),
            'cancelled': texts.t('CASHERA_RECURRING_NOTIFY_CANCELLED', 'ℹ️ Автопродление Cashera отменено.'),
        }
        text = messages.get(kind)
        if text:
            await bot.send_message(chat_id=user.telegram_id, text=text)
    except Exception as error:  # pragma: no cover - best-effort notify
        logger.warning('Cashera: не удалось отправить уведомление об автопродлении', error=str(error), kind=kind)
