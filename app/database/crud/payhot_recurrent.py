"""Durable PayHot recurring reservations; callers own commit and network work."""

import json
import re
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select

from app.database.models import PayHotAgreement, PayHotChargeAttempt, Subscription, User
from app.services.payhot_recurrent_api import validate_charge_payload
from app.services.payhot_recurrent_terms import MAX_AMOUNT_KOPEKS, next_charge_due_at


UNRESOLVED_CHARGE_STATUSES = ('RESERVED', 'UNKNOWN', 'PROCESSING', 'REVIEW', 'REFUND_REVIEW')
PAID_AWAITING_DELIVERY_STATUS = 'SUCCEEDED'


def _identifier(value, *, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value) <= 128 or not re.fullmatch(r'[!-~]+', value):
        raise ValueError('Invalid PayHot journal identifier')
    return value


def _payload(value):
    if type(value) is not dict:
        raise ValueError('Invalid PayHot recurring request')
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError('Invalid PayHot recurring request') from exc
    if len(encoded.encode()) > 65536:
        raise ValueError('PayHot recurring request too large')
    return encoded


async def reserve_charge(
    db, *, agreement_id, cycle_key, expected_end_date, request_payload, idempotency_key, merchant_reference
):
    """Reserve a cycle under agreement lock. Never commit or call the provider.

    A cancelled agreement cannot create/replay a reservation; recovery readers
    should read its existing attempt directly instead of reserving another one.
    """
    cycle_key = _identifier(cycle_key)
    idempotency_key = _identifier(idempotency_key, minimum=16)
    merchant_reference = _identifier(merchant_reference)
    expected_end_date = next_charge_due_at(expected_end_date)
    validate_charge_payload(request_payload)
    encoded = _payload(request_payload)
    amount = request_payload.get('amount_minor')
    if (
        not isinstance(amount, str)
        or not re.fullmatch(r'[1-9][0-9]{0,9}', amount)
        or int(amount) > MAX_AMOUNT_KOPEKS
        or request_payload.get('currency') != 'RUB'
    ):
        raise ValueError('Invalid PayHot recurring principal')
    if request_payload.get('merchant_recurring_charge_reference') != merchant_reference:
        raise ValueError('PayHot merchant reference mismatch')
    binding = (
        await db.execute(
            select(PayHotAgreement.user_id, PayHotAgreement.subscription_id).where(PayHotAgreement.id == agreement_id)
        )
    ).one_or_none()
    if binding is None or binding.user_id is None or binding.subscription_id is None:
        raise ValueError('PayHot agreement binding missing')
    user = await db.scalar(
        select(User).where(User.id == binding.user_id).with_for_update().execution_options(populate_existing=True)
    )
    subscription = await db.scalar(
        select(Subscription)
        .where(Subscription.id == binding.subscription_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    agreement = await db.scalar(
        select(PayHotAgreement)
        .where(PayHotAgreement.id == agreement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if agreement is None or agreement.status != 'ACTIVE' or agreement.cancel_requested_at is not None:
        raise ValueError('PayHot agreement is not active')
    if (
        agreement.user_id != binding.user_id
        or agreement.subscription_id != binding.subscription_id
        or agreement.owner_id != binding.user_id
        or agreement.mandate_id is None
    ):
        raise ValueError('PayHot agreement ownership or binding changed')
    if (
        user is None
        or user.status != 'active'
        or user.restriction_subscription
        or user.restriction_topup
        or subscription is None
        or subscription.user_id != agreement.user_id
        or subscription.end_date != expected_end_date
    ):
        raise ValueError('PayHot recurring target changed')
    if request_payload['mandate_id'] != agreement.mandate_id:
        raise ValueError('PayHot mandate mismatch')
    consent = agreement.consent_snapshot or {}
    if (
        type(consent.get('amount_kopeks')) is not int
        or consent['amount_kopeks'] != int(amount)
        or consent.get('currency') != 'RUB'
        or consent.get('payment_method') != request_payload['payment_method']
    ):
        raise ValueError('PayHot charge differs from consent')
    previous = await db.scalar(
        select(PayHotChargeAttempt).where(
            PayHotChargeAttempt.agreement_id == agreement_id, PayHotChargeAttempt.cycle_key == cycle_key
        )
    )
    if previous is not None:
        if (
            previous.expected_end_date != expected_end_date
            or _payload(previous.request_payload) != encoded
            or previous.idempotency_key != idempotency_key
            or previous.merchant_reference != merchant_reference
        ):
            raise ValueError('PayHot recurring cycle request changed')
        return previous
    existing_boundary = await db.scalar(
        select(PayHotChargeAttempt.id)
        .where(
            PayHotChargeAttempt.agreement_id == agreement_id,
            PayHotChargeAttempt.expected_end_date == expected_end_date,
        )
        .limit(1)
    )
    if existing_boundary is not None:
        raise ValueError('PayHot billing boundary already reserved')
    unresolved = await db.scalar(
        select(PayHotChargeAttempt.id)
        .where(
            PayHotChargeAttempt.agreement_id == agreement_id,
            or_(
                PayHotChargeAttempt.status.in_(UNRESOLVED_CHARGE_STATUSES),
                and_(
                    PayHotChargeAttempt.status == PAID_AWAITING_DELIVERY_STATUS,
                    PayHotChargeAttempt.delivery_completed_at.is_(None),
                ),
            ),
        )
        .limit(1)
    )
    if unresolved is not None:
        raise ValueError('PayHot previous cycle is unresolved')
    attempt = PayHotChargeAttempt(
        agreement_id=agreement_id,
        cycle_key=cycle_key,
        expected_end_date=expected_end_date,
        request_payload=json.loads(encoded),
        idempotency_key=idempotency_key,
        merchant_reference=merchant_reference,
        amount_kopeks=int(amount),
        currency='RUB',
        status='RESERVED',
    )
    db.add(attempt)
    await db.flush()
    return attempt


async def request_cancellation(db, agreement_id, *, reason):
    """Persist cancellation intent before HTTP; repeat requests keep first intent."""
    if not isinstance(reason, str) or not reason or len(reason) > 64:
        raise ValueError('Invalid PayHot cancellation reason')
    agreement = await db.scalar(
        select(PayHotAgreement)
        .where(PayHotAgreement.id == agreement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if agreement is None:
        raise ValueError('PayHot agreement not found')
    if agreement.cancel_requested_at is None:
        agreement.cancel_requested_at = datetime.now(UTC)
        agreement.reason = reason
        if agreement.status != 'CANCELLED':
            agreement.status = 'CANCEL_PENDING'
        await db.flush()
    return agreement


async def get_charge_for_cycle(db, *, agreement_id, cycle_key):
    """Read existing journal after cancellation without authorizing another send."""
    return await db.scalar(
        select(PayHotChargeAttempt).where(
            PayHotChargeAttempt.agreement_id == agreement_id,
            PayHotChargeAttempt.cycle_key == _identifier(cycle_key),
        )
    )
