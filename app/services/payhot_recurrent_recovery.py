"""Bounded canonical reads for missed late terminal-payment webhooks.

This recovery never creates invoices or charges. The seven-day polling tail is
not a declaration that later webhook receipts are invalid; acceptance remains
available for those independently.
"""

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update

from app.database.models import PayHotAgreement, PayHotChargeAttempt, PayHotPayment


logger = structlog.get_logger(__name__)


async def recover_terminal_payhot_receipts(db, *, limit=25, bot=None):
    from app.services import payhot_recurrent_service as svc

    limit = max(1, min(int(limit), 100))
    now = datetime.now(UTC)
    floor, cutoff = now - timedelta(days=7), now - timedelta(minutes=5)
    payments = list(
        (
            await db.execute(
                select(PayHotPayment.id, PayHotAgreement.id)
                .join(PayHotAgreement, PayHotAgreement.first_payment_id == PayHotPayment.id)
                .where(
                    PayHotPayment.status.in_(('failed', 'cancelled')),
                    PayHotPayment.is_paid.is_(False),
                    PayHotPayment.payhot_payment_id.is_not(None),
                    PayHotPayment.created_at >= floor,
                    PayHotPayment.updated_at <= cutoff,
                )
                .order_by(PayHotPayment.updated_at, PayHotPayment.id)
                .limit(limit)
            )
        ).all()
    )
    attempts = list(
        (
            await db.execute(
                select(PayHotChargeAttempt.id, PayHotChargeAttempt.agreement_id)
                .where(
                    PayHotChargeAttempt.status.in_(('FAILED', 'CANCELLED')),
                    PayHotChargeAttempt.provider_charge_id.is_not(None),
                    PayHotChargeAttempt.created_at >= floor,
                    PayHotChargeAttempt.updated_at <= cutoff,
                )
                .order_by(PayHotChargeAttempt.updated_at, PayHotChargeAttempt.id)
                .limit(limit)
            )
        ).all()
    )
    await db.commit()
    result = {'inspected': 0, 'errors': 0}
    for first, records in ((True, payments), (False, attempts)):
        model = PayHotPayment if first else PayHotChargeAttempt
        for row_id, agreement_id in records:
            try:
                row = await db.get(model, row_id, populate_existing=True)
                agreement = await db.get(PayHotAgreement, agreement_id, populate_existing=True)
                if row is None or agreement is None:
                    await db.rollback()
                    continue
                remote_id = row.payhot_payment_id if first else row.provider_charge_id
                terminal = row.status in (('failed', 'cancelled') if first else ('FAILED', 'CANCELLED'))
                if not terminal or not remote_id or (first and row.is_paid):
                    await db.rollback()
                    continue
                config = svc._config(agreement)
                await db.commit()
                result['inspected'] += 1
                if first:
                    canonical = await svc.payhot_service.get_payment(remote_id, config=config)
                    await svc.accept_enrollment_payment(db, row_id, canonical['payment'], bot=bot)
                else:
                    canonical = await svc.recurrent_api.get_recurring_charge(remote_id, config=config)
                    await svc.accept_recurring_charge(db, row_id, canonical['recurring_charge'], bot=bot)
            except Exception as error:
                await db.rollback()
                result['errors'] += 1
                logger.warning(
                    'HOT Pay terminal receipt check pending',
                    receipt_id=row_id,
                    first_payment=first,
                    error_type=type(error).__name__,
                )
            await db.execute(update(model).where(model.id == row_id).values(updated_at=datetime.now(UTC)))
            await db.commit()
    return result
