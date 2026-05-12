"""Listener for payment.completed event → sends S2S postback if subid present.

Listens for any Transaction(type=DEPOSIT) event:
- cabinet/bot top-ups, recurrent autopayments, admin manual top-ups.

Landing-purchase postbacks are NOT routed through this listener because
guest_purchase_service.fulfill_purchase creates a SUBSCRIPTION_PAYMENT
transaction (which emits 'transaction.created', not 'payment.completed').
That path keeps its inline send_postback() call.
"""

from __future__ import annotations

import asyncio

import structlog

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.event_emitter import event_emitter
from app.services.s2s_postback_service import send_postback


logger = structlog.get_logger(__name__)

_registered = False


async def _fire_postback(
    *,
    user_id: int,
    amount_rubles: float,
    transaction_id: int | None,
) -> None:
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


async def _on_payment_completed(event_data: dict) -> None:
    """Schedule a postback for every completed DEPOSIT transaction.

    Wrapped in asyncio.create_task so we do NOT block the emit caller
    (which is awaited from inside create_transaction right after db.commit()).
    """
    # Honor the global feature flag — avoids a DB roundtrip when disabled.
    if not getattr(settings, 'S2S_POSTBACK_ENABLED', False):
        return

    payload = event_data.get('payload', {}) if isinstance(event_data, dict) else {}
    user_id = payload.get('user_id')
    amount_rubles = payload.get('amount_rubles') or 0
    transaction_id = payload.get('transaction_id')
    is_completed = payload.get('is_completed', True)

    if not user_id or amount_rubles <= 0:
        return
    # Pending/failed DEPOSITs must not generate postbacks.
    if not is_completed:
        return

    # Fire-and-forget so the emit caller is not blocked by the partner-tracker HTTP roundtrip.
    asyncio.create_task(
        _fire_postback(
            user_id=user_id,
            amount_rubles=amount_rubles,
            transaction_id=transaction_id,
        )
    )


def register_postback_listeners() -> None:
    """Register S2S postback listener on the global event emitter (idempotent)."""
    global _registered
    if _registered:
        return
    event_emitter.on('payment.completed', _on_payment_completed)
    _registered = True
    logger.info('s2s_postback_listener_registered', event_type='payment.completed')
