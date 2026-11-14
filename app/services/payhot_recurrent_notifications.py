"""Post-commit effects for a newly accepted recurring receipt, never a retry.

The financial acceptance caller owns deduplication. Notification outages must
not roll back an accepted receipt or replay its grant. This module uses existing
notification transports and never creates a Telegram Bot/session.
"""

import structlog

from app.database.models import PaymentMethod, TransactionType


logger = structlog.get_logger(__name__)


async def emit_payhot_receipt(db, tx, user, target, agreement, *, applied, bot=None):
    if tx is None or user is None or user.id != agreement.owner_id or tx.user_id != user.id:
        return
    amount_kopeks = abs(tx.amount_kopeks)
    from app.database.crud.transaction import emit_transaction_side_effects

    try:
        await emit_transaction_side_effects(
            db,
            tx,
            amount_kopeks=amount_kopeks,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT if applied else TransactionType.DEPOSIT,
            payment_method=PaymentMethod.PAYHOT,
            external_id=tx.external_id,
            description=tx.description,
        )
    except Exception as error:
        logger.warning(
            'HOT Pay recurring receipt event unavailable', transaction_id=tx.id, error_type=type(error).__name__
        )
    try:
        from app.cabinet.routes.websocket import cabinet_ws_manager

        await cabinet_ws_manager.send_to_user(
            user.id,
            {
                'type': 'payhot_recurring.updated',
                'subscription_id': agreement.subscription_id,
                'status': agreement.status,
            },
        )
    except Exception as error:
        logger.warning('HOT Pay recurring update unavailable', transaction_id=tx.id, error_type=type(error).__name__)
    try:
        from app.services.notification_delivery_service import notification_delivery_service

        if applied and target is not None:
            await notification_delivery_service.notify_autopay_success(user, amount_kopeks, target.end_date, bot=bot)
        elif not applied:
            await notification_delivery_service.notify_balance_topup(user, amount_kopeks, user.balance_kopeks, bot=bot)
    except Exception as error:
        logger.warning(
            'HOT Pay recurring notification unavailable', transaction_id=tx.id, error_type=type(error).__name__
        )
