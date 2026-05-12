"""Listener for payment.completed event → sends S2S postback if subid present."""

from __future__ import annotations

import structlog

from app.database.database import AsyncSessionLocal
from app.services.event_emitter import event_emitter
from app.services.s2s_postback_service import send_postback


logger = structlog.get_logger(__name__)


async def _on_payment_completed(event_data: dict) -> None:
    """Handle every DEPOSIT-class transaction: send purchase postback if user has subid."""
    payload = event_data.get('payload', {}) if isinstance(event_data, dict) else {}
    user_id = payload.get('user_id')
    amount_rubles = payload.get('amount_rubles') or 0
    transaction_id = payload.get('transaction_id')

    if not user_id or amount_rubles <= 0:
        return

    try:
        from app.database.crud.yandex_client_id import get_subid

        async with AsyncSessionLocal() as db:
            subid = await get_subid(db, user_id)
            if not subid:
                return
            await send_postback(
                'purchase',
                subid,
                amount=float(amount_rubles),
                user_id=user_id,
                tx_id=str(transaction_id) if transaction_id is not None else None,
            )
    except Exception as error:
        logger.exception(
            'postback_listener_failed',
            user_id=user_id,
            transaction_id=transaction_id,
            error=str(error),
        )


def register_postback_listeners() -> None:
    """Register S2S postback listeners on global event emitter."""
    event_emitter.on('payment.completed', _on_payment_completed)
    logger.info('s2s_postback_listener_registered', event_type='payment.completed')
