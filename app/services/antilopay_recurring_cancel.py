"""Отмена рекуррентов Antilopay без зависимости от общего PaymentService."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.antilopay_service import antilopay_service
from app.utils.payment_logger import payment_logger as logger


async def cancel_user_antilopay_recurrents(db: AsyncSession, user_id: int) -> int:
    if not settings.ANTILOPAY_RECURRENT_ENABLED or not settings.is_antilopay_enabled():
        return 0

    from app.database.crud.antilopay_recurrent import (
        deactivate_antilopay_recurrent,
        get_active_antilopay_recurrents_by_user,
    )

    recurrents = await get_active_antilopay_recurrents_by_user(db, user_id)
    cancelled = 0
    for recurrent in recurrents:
        try:
            if recurrent.recurrent_id:
                await antilopay_service.cancel_recurrent_payment(recurrent_id=recurrent.recurrent_id)
            elif recurrent.initial_payment_id:
                await antilopay_service.cancel_recurrent_payment(transaction_id=recurrent.initial_payment_id)
        except Exception as error:
            if recurrent.recurrent_id and recurrent.initial_payment_id:
                try:
                    await antilopay_service.cancel_recurrent_payment(transaction_id=recurrent.initial_payment_id)
                except Exception as fallback_error:
                    logger.warning('Antilopay: fallback cancellation error', error=fallback_error)
            logger.warning(
                'Antilopay: не удалось отменить рекуррент через API',
                recurrent_id=recurrent.recurrent_id,
                user_id=user_id,
                error=error,
            )
        await deactivate_antilopay_recurrent(db, recurrent)
        cancelled += 1
    return cancelled
