"""Listener for payment.completed event → sends S2S postback if subid present.

Catches deposit-class transactions that go through the event-emitter side
effects in app/database/crud/transaction.py:

- Direct create_transaction(..., commit=True) paths — emits inline.
- Two-phase create_transaction(commit=False) + emit_transaction_side_effects()
  paths — caller is responsible for the second call after its own db.commit().
  Providers that skip the second call do NOT trigger this listener.

Landing-purchase postbacks are NOT routed through this listener because
guest_purchase_service.fulfill_purchase creates a SUBSCRIPTION_PAYMENT
transaction (which emits 'transaction.created', not 'payment.completed').
That path keeps its inline send_postback() call.
"""

from __future__ import annotations

import structlog

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.event_emitter import event_emitter
from app.services.s2s_postback_service import _mask_subid, send_postback
from app.utils.async_tasks import spawn_bg


logger = structlog.get_logger(__name__)

_registered = False

# Payment methods we know correspond to real money inflow. DEPOSITs with a
# value outside this set are typically admin manual adjustments, refund
# corrections or QA flows — they must NOT trigger partner commission.
_REVENUE_PAYMENT_METHODS = frozenset(
    {
        'yookassa',
        'cryptobot',
        'tribute',
        'mulenpay',
        'pal24',
        'freekassa',
        'kassa_ai',
        'unitpay',
        'riopay',
        'severpay',
        'antilopay',
        'aurapay',
        'jupiter',
        'donut',
        'lava',
        'etoplatezhi',
        'telegram_stars',
        'apple_iap',
        'youkassa',
    }
)


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
                tx_id=f'tx-{transaction_id}' if transaction_id is not None else None,
            )
    except Exception as error:
        logger.exception(
            'postback_listener_failed',
            user_id=user_id,
            transaction_id=transaction_id,
            error=str(error),
        )


def _is_revenue_deposit(payload: dict) -> bool:
    """True only for DEPOSITs that correspond to real partner-attributable revenue.

    Filters out admin manual top-ups (no payment_method), refunds, and QA flows
    so partners do not receive commission for non-revenue events.
    """
    method = (payload.get('payment_method') or '').lower()
    if not method or method not in _REVENUE_PAYMENT_METHODS:
        return False
    description = (payload.get('description') or '').lower()
    if description.startswith('refund') or 'возврат' in description:
        return False
    return True


async def _on_payment_completed(event_data: dict) -> None:
    """Schedule a postback for every completed real-revenue DEPOSIT.

    Wrapped in spawn_bg (fire-and-forget with strong refs) so we do NOT block
    the emit caller (which is awaited from inside create_transaction right
    after db.commit()).
    """
    if not getattr(settings, 'S2S_POSTBACK_ENABLED', False):
        return
    if not getattr(settings, 'S2S_POSTBACK_PURCHASE_URL', ''):
        return

    payload = event_data.get('payload', {}) if isinstance(event_data, dict) else {}
    user_id = payload.get('user_id')
    amount_rubles = payload.get('amount_rubles') or 0
    transaction_id = payload.get('transaction_id')
    is_completed = payload.get('is_completed', True)

    if not user_id or amount_rubles <= 0:
        return
    if not is_completed:
        return
    if not _is_revenue_deposit(payload):
        logger.debug(
            'postback_skipped_non_revenue_deposit',
            user_id=user_id,
            transaction_id=transaction_id,
            payment_method=payload.get('payment_method'),
        )
        return

    spawn_bg(
        _fire_postback(
            user_id=user_id,
            amount_rubles=amount_rubles,
            transaction_id=transaction_id,
        )
    )


# Backwards-compat alias so external callers (guest_purchase_service) keep working
# until they switch to app.utils.async_tasks.spawn_bg directly.
_spawn_bg = spawn_bg
__all__ = ['_mask_subid', 'register_postback_listeners']


def register_postback_listeners() -> None:
    """Register S2S postback listener on the global event emitter (idempotent)."""
    global _registered
    if _registered:
        return
    event_emitter.on('payment.completed', _on_payment_completed)
    _registered = True
    logger.info('s2s_postback_listener_registered', event_type='payment.completed')
