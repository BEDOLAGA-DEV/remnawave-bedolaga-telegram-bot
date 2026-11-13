"""HOT Pay merchant-scheduled consent, canonical receipts and renewal delivery.

Provider I/O never constitutes a financial receipt. User, subscription, agreement
and receipt locks protect each atomic grant. Unknown requests retain their keys.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    PaidTrafficResetIntent,
    PayHotAgreement,
    PayHotChargeAttempt,
    PayHotPayment,
    PaymentMethod,
    Subscription,
    Tariff,
    TransactionType,
    User,
)
from app.services.payhot_recurrent_api import PayHotRecurrentAPI, generate_opaque_reference
from app.services.payhot_service import PayHotAPIError, _https_url, _identifier, payhot_service


logger = structlog.get_logger(__name__)
recurrent_api = PayHotRecurrentAPI()
TERMINAL_AGREEMENTS = ('CANCELLED', 'FAILED')


def _config(agreement):
    config = payhot_service.snapshot()
    # Compare the provider-key fingerprint, never authenticate a human password.
    expected = agreement.consent_snapshot.get('api_key_fingerprint')
    if expected and expected != hashlib.sha256(config.api_key.encode()).hexdigest():
        raise ValueError('credentials_changed')
    return config


def agreement_state(agreement, payment=None):
    snapshot = agreement.consent_snapshot
    return {
        'id': agreement.id,
        'subscription_id': agreement.subscription_id,
        'status': agreement.status,
        'reason': agreement.reason,
        'amount_kopeks': snapshot['amount_kopeks'],
        'period_days': snapshot['period_days'],
        'payment_method': snapshot['payment_method'],
        'next_charge_at': agreement.next_charge_at.isoformat() if agreement.next_charge_at else None,
        'redirect_url': payment.payment_url
        if payment and not payment.is_paid and not agreement.cancel_requested_at
        else None,
    }


async def _lock_target(db, agreement_id):
    binding = (
        await db.execute(
            select(PayHotAgreement.user_id, PayHotAgreement.subscription_id).where(PayHotAgreement.id == agreement_id)
        )
    ).one_or_none()
    if binding is None:
        raise ValueError('agreement_not_found')
    user = await db.scalar(
        select(User).where(User.id == binding.user_id).with_for_update().execution_options(populate_existing=True)
    )
    target = await db.scalar(
        select(Subscription)
        .where(Subscription.id == binding.subscription_id)
        .options(selectinload(Subscription.tariff))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    agreement = await db.scalar(
        select(PayHotAgreement)
        .where(PayHotAgreement.id == agreement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        agreement is None
        or agreement.user_id != binding.user_id
        or agreement.subscription_id != binding.subscription_id
    ):
        raise ValueError('agreement_binding_changed')
    return user, target, agreement


async def terms_match(db, agreement, target, *, first=False):
    from app.services.payhot_recurrent_checkout import current_payhot_terms_match

    return await current_payhot_terms_match(db, target, agreement.consent_snapshot, first=first)


def _financial_match(data, snapshot):
    if not isinstance(data, dict) or data.get('environment') not in (None, 'production'):
        raise ValueError('Invalid PayHot recurring environment')
    if any(data.get(key) != snapshot[key] for key in ('currency', 'payment_method')):
        raise ValueError('PayHot recurring method or currency mismatch')
    if data.get('amount_minor') != str(snapshot['amount_kopeks']):
        raise ValueError('PayHot recurring principal mismatch')
    fees = data.get('fees')
    if fees is not None and (
        not isinstance(fees, dict) or fees.get('principal_minor') != str(snapshot['amount_kopeks'])
    ):
        raise ValueError('PayHot recurring fee principal mismatch')


async def _target_matches(db, user, target, agreement, expected_end, *, first):
    if (
        user is None
        or user.id != agreement.owner_id
        or user.status != 'active'
        or user.restriction_subscription
        or target is None
        or target.user_id != agreement.owner_id
        or (not first and target.is_daily_paused)
        or target.status == 'disabled'
        or (expected_end is not None and target.end_date != expected_end)
    ):
        return False
    # Cancellation after a submitted charge must not discard money; cancellation
    # due to a changed target/terms must not buy the replacement configuration.
    if agreement.reason in (
        'manual_change',
        'subscription_deleted',
        'price_changed',
        'account_merge',
        'account_merged',
        'account_deleted',
    ):
        return False
    if not await terms_match(db, agreement, target, first=first):
        return False
    competing = select(Subscription.id).where(
        Subscription.user_id == user.id,
        Subscription.id != target.id,
        Subscription.is_trial.is_(False),
        Subscription.status.in_(('active', 'limited', 'pending')),
    )
    if settings.is_multi_tariff_enabled():
        competing = competing.where(Subscription.tariff_id == agreement.consent_snapshot['tariff_id'])
    return await db.scalar(competing.limit(1)) is None


async def _grant(db, user, target, agreement, *, external_id, matched):
    from app.database.crud.transaction import create_transaction

    snapshot = agreement.consent_snapshot
    amount = snapshot['amount_kopeks']
    if user is None or user.id != agreement.owner_id or user.status == 'deleted':
        agreement.status, agreement.reason = 'REVIEW', 'owner_missing'
        agreement.next_charge_at = None
        return None, False
    if matched:
        from app.database.crud.subscription import (
            extend_subscription,
            should_carry_trial_remaining_days,
            update_daily_charge_time,
        )
        from app.services.grace_access_echo import undo_grace_overlay_echo

        await undo_grace_overlay_echo(db, target)
        tariff = await db.get(Tariff, snapshot['tariff_id']) if snapshot.get('tariff_id') else None
        was_trial = target.is_trial
        if was_trial and not should_carry_trial_remaining_days():
            target.end_date = datetime.now(UTC)
        kwargs = {}
        if tariff:
            kwargs = dict(
                tariff_id=tariff.id,
                device_limit=snapshot['device_limit'],
                traffic_limit_gb=tariff.traffic_limit_gb,
                connected_squads=snapshot['tariff_terms']['allowed_squads'],
            )
        await extend_subscription(
            db,
            target,
            0 if snapshot.get('is_daily') else snapshot['period_days'],
            paid_trial_conversion=was_trial,
            provider_charge=True,
            commit=False,
            paid_traffic_reset=False,
            reset_used_traffic=False,
            **kwargs,
        )
        if snapshot.get('is_daily'):
            await update_daily_charge_time(db, target, commit=False)
            target.is_daily_paused = False
            target.daily_devices_billing_version = snapshot.get('daily_devices_billing_version', 0)
        target.autopay_enabled = False
        target.status = 'active'
        user.has_had_paid_subscription = True
        kind = TransactionType.SUBSCRIPTION_PAYMENT
    else:
        user.balance_kopeks += amount
        agreement.status, agreement.reason = 'CANCEL_PENDING', 'target_changed'
        agreement.next_charge_at = None
        agreement.cancel_requested_at = agreement.cancel_requested_at or datetime.now(UTC)
        kind = TransactionType.DEPOSIT
    tx = await create_transaction(
        db,
        user_id=user.id,
        type=kind,
        amount_kopeks=amount,
        description='HOT Pay: оплата периода' if matched else 'HOT Pay: оплата по изменённой подписке',
        payment_method=PaymentMethod.PAYHOT,
        external_id=external_id,
        commit=False,
    )
    if matched:
        from app.services.traffic_base_grant_service import stage_paid_base_grant
        from app.services.traffic_reset_policy import should_reset_traffic_on_daily_charge

        reset = (
            should_reset_traffic_on_daily_charge(tariff)
            if snapshot.get('is_daily')
            else settings.RESET_TRAFFIC_ON_PAYMENT
        )
        await stage_paid_base_grant(db, target, operation_key=external_id, reset_base=reset)
        if reset and target.traffic_accounting_mode == 'legacy':
            db.add(
                PaidTrafficResetIntent(
                    subscription_id=target.id,
                    user_id=user.id,
                    transaction_id=tx.id,
                    operation_key=external_id,
                    charge_at=target.end_date,
                    reason='HOT Pay: оплаченный период',
                    state='pending',
                )
            )
    return tx, matched


def _enrollment_state(agreement, payment, data, target):
    enrollment = data.get('recurring_enrollment')
    if not isinstance(enrollment, dict):
        return
    mandate = enrollment.get('mandate_id')
    if mandate is not None:
        _identifier(mandate)
        if agreement.mandate_id not in (None, mandate):
            raise ValueError('PayHot mandate changed')
        agreement.mandate_id = mandate
    applied = payment.fulfillment_state or {}
    if agreement.cancel_requested_at is not None or agreement.status in (
        *TERMINAL_AGREEMENTS,
        'CANCEL_PENDING',
        'REVIEW',
    ):
        return
    if (
        payment.is_paid
        and applied.get('applied')
        and target is not None
        and applied.get('end_date') == target.end_date.isoformat()
        and enrollment.get('status') == 'active'
        and mandate
    ):
        agreement.status, agreement.reason = 'ACTIVE', None
        agreement.next_charge_at = target.end_date
    elif enrollment.get('status') in ('failed', 'cancelled', 'inactive', 'revoked', 'suspended', 'expired'):
        agreement.status, agreement.reason, agreement.next_charge_at = 'FAILED', 'binding_failed', None


async def _block_refunded_reset(db, operation_key):
    intent = await db.scalar(
        select(PaidTrafficResetIntent)
        .where(PaidTrafficResetIntent.operation_key == operation_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if intent is not None and intent.state == 'pending':
        intent.state = 'blocked'
        intent.last_error = 'refund_review'


async def accept_enrollment_payment(db, payment_id, data, *, bot=None):
    from app.services.payhot_payment_validation import _validate_remote

    payment = await db.get(PayHotPayment, payment_id)
    if payment is None or (payment.metadata_json or {}).get('purpose') != 'payhot_recurring':
        raise ValueError('Unknown PayHot enrollment payment')
    agreement_id = (payment.metadata_json or {}).get('agreement_id')
    tx = None
    try:
        user, target, agreement = await _lock_target(db, agreement_id)
        payment = await db.scalar(
            select(PayHotPayment)
            .where(PayHotPayment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if agreement.first_payment_id != payment.id or payment.user_id not in (None, agreement.owner_id):
            raise ValueError('PayHot enrollment owner mismatch')
        payment.payhot_payment_id = _validate_remote(payment, data)
        _financial_match(data, agreement.consent_snapshot)
        if data['status'] in ('refunded', 'partially_refunded'):
            await _block_refunded_reset(db, f'payhot:first:{payment.id}')
            agreement.status, agreement.reason, agreement.next_charge_at = 'REVIEW', 'refund_review', None
            payment.status = data['status']
            payment.fulfillment_state = {**(payment.fulfillment_state or {}), 'state': 'review'}
        elif (
            not payment.is_paid
            and data['status'] == 'succeeded'
            and (payment.fulfillment_state or {}).get('state') != 'review'
        ):
            source = (payment.metadata_json or {}).get('target_end_date') or agreement.consent_snapshot.get(
                'source_end_date'
            )
            expected = datetime.fromisoformat(source) if source else None
            matched = await _target_matches(db, user, target, agreement, expected, first=True)
            tx, applied = await _grant(
                db, user, target, agreement, external_id=f'payhot:first:{payment.id}', matched=matched
            )
            payment.transaction_id = tx.id if tx else None
            payment.is_paid, payment.paid_at = True, datetime.now(UTC)
            payment.fulfillment_state = {
                'kind': 'recurring',
                'state': 'pending' if applied else 'review' if tx is None else 'completed',
                'applied': applied,
                'subscription_id': agreement.subscription_id,
                'end_date': target.end_date.isoformat() if applied else None,
            }
            payment.status = 'succeeded'
        elif not payment.is_paid and (payment.fulfillment_state or {}).get('state') != 'review':
            payment.status = data['status']
        _enrollment_state(agreement, payment, data, target)
        agreement.updated_at = payment.updated_at = datetime.now(UTC)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    if tx is not None:
        from app.services.payhot_recurrent_notifications import emit_payhot_receipt

        await emit_payhot_receipt(db, tx, user, target, agreement, applied=applied, bot=bot)
    await deliver_payhot_renewal(db, agreement_id, payment_id=payment_id)
    return True


async def deliver_payhot_renewal(db, agreement_id, *, payment_id=None, attempt_id=None):
    """Recover panel delivery without replaying money or the calendar grant."""
    agreement = await db.get(PayHotAgreement, agreement_id)
    row = await db.get(PayHotPayment, payment_id) if payment_id else await db.get(PayHotChargeAttempt, attempt_id)
    if agreement is None or row is None:
        return
    if payment_id:
        state = row.fulfillment_state or {}
        if not state.get('applied') or state.get('state') != 'pending':
            return
    elif row.status != 'SUCCEEDED' or row.delivery_completed_at is not None:
        return
    target = await db.get(Subscription, agreement.subscription_id) if agreement.subscription_id else None
    user = await db.get(User, agreement.user_id) if agreement.user_id else None
    if target is None or user is None or target.user_id != agreement.owner_id or user.status != 'active':
        return
    try:
        from app.services.subscription_service import SubscriptionService

        service = SubscriptionService()
        panel_id = target.remnawave_id if settings.is_multi_tariff_enabled() else user.remnawave_id
        if panel_id or target.subscription_url:
            result = await service.update_remnawave_user(db, target, reset_traffic=False)
        else:
            result = await service.create_remnawave_user(db, target, reset_traffic=False)
        if result is None:
            return
        operation_key = f'payhot:first:{payment_id}' if payment_id else f'payhot:charge:{attempt_id}'
        intent_id = await db.scalar(
            select(PaidTrafficResetIntent.id).where(PaidTrafficResetIntent.operation_key == operation_key)
        )
        if intent_id:
            from app.services.paid_traffic_reset_service import deliver_daily_reset

            if not await deliver_daily_reset(db, intent_id):
                return
        # Panel I/O and reset delivery may commit or await while a refund is
        # accepted elsewhere. Re-read under lock before completing this receipt.
        model = PayHotPayment if payment_id else PayHotChargeAttempt
        row = await db.scalar(
            select(model)
            .where(model.id == (payment_id or attempt_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or (payment_id and (row.fulfillment_state or {}).get('state') != 'pending')
            or (not payment_id and row.status != 'SUCCEEDED')
        ):
            await db.rollback()
            return
        if payment_id:
            row.fulfillment_state = {**row.fulfillment_state, 'state': 'completed'}
        else:
            row.delivery_completed_at = datetime.now(UTC)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning('HOT Pay recurring delivery pending', agreement_id=agreement_id)


async def create_payhot_agreement(db, agreement_id, *, bot=None):
    """Serialise checkout link creation; every unknown retry uses the saved key."""
    agreement = await db.scalar(
        select(PayHotAgreement)
        .where(PayHotAgreement.id == agreement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if agreement is None:
        raise ValueError('agreement_not_found')
    payment = await db.scalar(
        select(PayHotPayment)
        .where(PayHotPayment.id == agreement.first_payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None:
        await db.rollback()
        raise ValueError('enrollment_payment_missing')
    if agreement.cancel_requested_at or agreement.status in (*TERMINAL_AGREEMENTS, 'CANCEL_PENDING', 'REVIEW'):
        result = agreement_state(agreement, payment)
        await db.commit()
        return result
    if payment.payment_url or payment.is_paid:
        result = agreement_state(agreement, payment)
        await db.commit()
        return result
    try:
        config = _config(agreement)
        if payment.payhot_payment_id:
            response = await payhot_service.reissue_checkout(
                payment.payhot_payment_id, idempotency_key=payment.order_id, config=config
            )
        else:
            response = await recurrent_api.create_enrollment_payment(
                payment.request_payload, payment.order_id, config=config
            )
        from app.services.payhot_payment_validation import _validate_remote

        data = response['payment']
        payment.payhot_payment_id = _validate_remote(payment, data)
        checkout = response.get('checkout') or {}
        if (
            not checkout.get('url')
            and checkout.get('reissue_required')
            and data['status'] not in ('succeeded', 'refunded', 'cancelled', 'failed')
        ):
            response = await payhot_service.reissue_checkout(
                payment.payhot_payment_id, idempotency_key=payment.order_id, config=config
            )
            _validate_remote(payment, response['payment'])
            checkout = response.get('checkout') or {}
        if checkout.get('url'):
            payment.payment_url = _https_url(checkout['url'])
        payment.status = 'processing' if data['status'] == 'succeeded' else data['status']
        agreement.status = 'PENDING'
        agreement.updated_at = datetime.now(UTC)
        await db.commit()
        if data['status'] == 'succeeded':
            await refresh_payhot_enrollment(db, agreement.id, bot=bot)
    except PayHotAPIError:
        # A timeout/HTTP error is not proof that no payment was created.
        payment.status, agreement.status = 'create_unknown', 'CREATION_UNKNOWN'
        agreement.updated_at = datetime.now(UTC)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    agreement = await db.get(PayHotAgreement, agreement_id, populate_existing=True)
    payment = await db.get(PayHotPayment, agreement.first_payment_id, populate_existing=True)
    return agreement_state(agreement, payment)


async def refresh_payhot_enrollment(db, agreement_id, *, bot=None):
    agreement = await db.get(PayHotAgreement, agreement_id)
    payment = (
        await db.get(PayHotPayment, agreement.first_payment_id) if agreement and agreement.first_payment_id else None
    )
    if payment is None:
        raise ValueError('enrollment_payment_missing')
    if not payment.payhot_payment_id:
        return await create_payhot_agreement(db, agreement_id, bot=bot)
    config, remote_id, payment_id = _config(agreement), payment.payhot_payment_id, payment.id
    await db.commit()
    remote = await payhot_service.get_payment(remote_id, config=config)
    await accept_enrollment_payment(db, payment_id, remote['payment'], bot=bot)
    return None


async def accept_recurring_charge(db, attempt_id, data, *, refunded=False, bot=None):
    attempt = await db.get(PayHotChargeAttempt, attempt_id)
    if attempt is None:
        raise ValueError('Unknown PayHot recurring charge')
    agreement_id = attempt.agreement_id
    tx = None
    try:
        user, target, agreement = await _lock_target(db, agreement_id)
        attempt = await db.scalar(
            select(PayHotChargeAttempt)
            .where(PayHotChargeAttempt.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        _identifier(data.get('id'))
        _financial_match(data, agreement.consent_snapshot)
        if (
            attempt.provider_charge_id not in (None, data['id'])
            or data.get('mandate_id') != agreement.mandate_id
            or data.get('merchant_recurring_charge_reference') != attempt.merchant_reference
            or data.get('amount_minor') != str(attempt.amount_kopeks)
        ):
            raise ValueError('PayHot charge identity mismatch')
        if data.get('status') not in ('processing', 'succeeded', 'failed', 'cancelled'):
            raise ValueError('Unknown PayHot charge status')
        attempt.provider_charge_id = data['id']
        if refunded:
            await _block_refunded_reset(db, f'payhot:charge:{attempt.id}')
            attempt.status = 'REFUND_REVIEW'
            agreement.status, agreement.reason, agreement.next_charge_at = 'REVIEW', 'refund_review', None
        elif attempt.status == 'REFUND_REVIEW' or attempt.accepted_at is not None:
            pass
        elif data['status'] == 'succeeded':
            matched = await _target_matches(db, user, target, agreement, attempt.expected_end_date, first=False)
            tx, applied = await _grant(
                db, user, target, agreement, external_id=f'payhot:charge:{attempt.id}', matched=matched
            )
            attempt.transaction_id = tx.id if tx else None
            attempt.accepted_at = datetime.now(UTC)
            attempt.status = 'SUCCEEDED' if applied else 'BALANCE' if tx else 'REVIEW'
            if applied and agreement.status == 'ACTIVE' and agreement.cancel_requested_at is None:
                agreement.next_charge_at = target.end_date
            else:
                agreement.next_charge_at = None
        else:
            attempt.status = data['status'].upper()
            if data['status'] in ('failed', 'cancelled') and agreement.status == 'ACTIVE':
                agreement.status, agreement.reason, agreement.next_charge_at = 'PAUSED', 'payment_failed', None
        attempt.updated_at = agreement.updated_at = datetime.now(UTC)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    if tx is not None:
        from app.services.payhot_recurrent_notifications import emit_payhot_receipt

        await emit_payhot_receipt(db, tx, user, target, agreement, applied=applied, bot=bot)
    await deliver_payhot_renewal(db, agreement_id, attempt_id=attempt_id)
    return True


async def process_payhot_recurring_callback(db, payload, *, bot=None):
    data = payload.get('data') or {}
    charge_id = data.get('recurring_charge_id') if data.get('object') == 'payment' else data.get('id')
    _identifier(charge_id)
    reference = data.get('merchant_recurring_charge_reference')
    attempt = await db.scalar(
        select(PayHotChargeAttempt).where(
            or_(
                PayHotChargeAttempt.provider_charge_id == charge_id, PayHotChargeAttempt.merchant_reference == reference
            )
        )
    )
    if attempt is None:
        raise ValueError('Unknown PayHot recurring charge')
    agreement = await db.get(PayHotAgreement, attempt.agreement_id)
    config, attempt_id = _config(agreement), attempt.id
    # Validate any supplied monetary fields before even a refund review marker.
    for field, expected in (
        ('amount_minor', str(attempt.amount_kopeks)),
        ('currency', 'RUB'),
        ('payment_method', agreement.consent_snapshot['payment_method']),
        ('mandate_id', agreement.mandate_id),
    ):
        if field in data and data[field] != expected:
            raise ValueError('PayHot recurring event mismatch')
    if payload.get('environment') not in (None, 'production'):
        raise ValueError('PayHot environment mismatch')
    await db.commit()
    canonical = await recurrent_api.get_recurring_charge(charge_id, config=config)
    return await accept_recurring_charge(
        db, attempt_id, canonical['recurring_charge'], refunded=payload.get('event_type') == 'payment.refunded', bot=bot
    )


async def cancel_payhot_agreement(db, *, user_id, subscription_id, reason='customer', bot=None):
    from app.database.crud.payhot_recurrent import request_cancellation

    target = await db.scalar(
        select(Subscription.id).where(Subscription.id == subscription_id, Subscription.user_id == user_id)
    )
    if target is None:
        raise ValueError('subscription_not_found')
    agreement = await db.scalar(
        select(PayHotAgreement)
        .where(PayHotAgreement.owner_id == user_id, PayHotAgreement.subscription_id == subscription_id)
        .order_by(PayHotAgreement.id.desc())
        .limit(1)
    )
    if agreement is None:
        return {'status': 'none', 'subscription_id': subscription_id}
    await request_cancellation(db, agreement.id, reason=reason)
    agreement.next_charge_at = None
    aid = agreement.id
    await db.commit()
    try:
        await revoke_payhot_agreement(db, aid, bot=bot)
    except (PayHotAPIError, ValueError):
        # Local scheduling is already off; durable intent retries remotely.
        await db.rollback()
    agreement = await db.get(PayHotAgreement, aid, populate_existing=True)
    return agreement_state(agreement)


async def revoke_payhot_agreement(db, agreement_id, *, bot=None):
    agreement = await db.get(PayHotAgreement, agreement_id)
    if agreement is None or agreement.status == 'CANCELLED':
        return
    if not agreement.cancel_requested_at and agreement.status != 'CANCEL_PENDING':
        return
    mandate, config = agreement.mandate_id, _config(agreement)
    if mandate is None:
        # A hosted first checkout may still be completed later. Keep recovering
        # its binding instead of pretending that there is no future mandate.
        payment = await db.get(PayHotPayment, agreement.first_payment_id) if agreement.first_payment_id else None
        if payment and payment.payhot_payment_id:
            await db.commit()
            await refresh_payhot_enrollment(db, agreement_id, bot=bot)
            agreement = await db.get(PayHotAgreement, agreement_id, populate_existing=True)
            mandate = agreement.mandate_id
        if mandate is None:
            return
    await db.commit()
    await recurrent_api.revoke_mandate(mandate, 'consent_withdrawn', config=config)
    agreement = await db.scalar(
        select(PayHotAgreement)
        .where(PayHotAgreement.id == agreement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if agreement.mandate_id != mandate:
        raise ValueError('PayHot mandate changed during cancellation')
    agreement.status, agreement.next_charge_at = 'CANCELLED', None
    agreement.updated_at = datetime.now(UTC)
    await db.commit()


async def new_charges_enabled(db, user, agreement):
    from app.services.payhot_recurrent_policy import get_payhot_recurrent_policy

    policy = await get_payhot_recurrent_policy(db, user=user, channel=agreement.consent_snapshot['channel'])
    return policy['enabled'] and agreement.consent_snapshot['payment_method'] in policy['methods']


async def prepare_payhot_charge(db, agreement_id):
    from app.database.crud.payhot_recurrent import reserve_charge

    user, target, agreement = await _lock_target(db, agreement_id)
    now = datetime.now(UTC)
    if (
        agreement.status != 'ACTIVE'
        or agreement.cancel_requested_at
        or agreement.next_charge_at is None
        or agreement.next_charge_at > now
    ):
        await db.commit()
        return None
    if (
        user is None
        or target is None
        or target.is_daily_paused
        or target.is_trial
        or not await new_charges_enabled(db, user, agreement)
    ):
        agreement.status, agreement.reason, agreement.next_charge_at = 'PAUSED', 'provider_disabled', None
        await db.commit()
        return None
    if not await _target_matches(db, user, target, agreement, agreement.next_charge_at, first=False):
        agreement.status, agreement.reason, agreement.next_charge_at = 'CANCEL_PENDING', 'price_changed', None
        agreement.cancel_requested_at = now
        await db.commit()
        return None
    expected = agreement.next_charge_at
    previous = await db.scalar(
        select(PayHotChargeAttempt).where(
            PayHotChargeAttempt.agreement_id == agreement_id, PayHotChargeAttempt.expected_end_date == expected
        )
    )
    if previous is not None:
        await db.commit()
        return previous
    snapshot = agreement.consent_snapshot
    reference = generate_opaque_reference()
    payload = {
        'merchant_recurring_charge_reference': reference,
        'mandate_id': agreement.mandate_id,
        'amount_minor': str(snapshot['amount_kopeks']),
        'currency': 'RUB',
        'payment_method': snapshot['payment_method'],
    }
    attempt = await reserve_charge(
        db,
        agreement_id=agreement_id,
        cycle_key=expected.isoformat(),
        expected_end_date=expected,
        request_payload=payload,
        idempotency_key=str(uuid4()),
        merchant_reference=reference,
    )
    await db.commit()
    return attempt


async def submit_payhot_charge(db, attempt_id, *, bot=None):
    """Recover a single immutable request; never replace an unknown charge."""
    attempt = await db.get(PayHotChargeAttempt, attempt_id)
    if attempt is None:
        return
    user, target, agreement = await _lock_target(db, attempt.agreement_id)
    attempt = await db.scalar(
        select(PayHotChargeAttempt)
        .where(PayHotChargeAttempt.id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    config = _config(agreement)
    if attempt.provider_charge_id:
        remote_id = attempt.provider_charge_id
        await db.commit()
        remote = await recurrent_api.get_recurring_charge(remote_id, config=config)
        await accept_recurring_charge(db, attempt_id, remote['recurring_charge'], bot=bot)
        return
    if attempt.status not in ('RESERVED', 'UNKNOWN'):
        await db.commit()
        return
    if (
        agreement.status != 'ACTIVE'
        or agreement.cancel_requested_at
        or user is None
        or target is None
        or target.is_daily_paused
        or not await new_charges_enabled(db, user, agreement)
        or not await _target_matches(db, user, target, agreement, attempt.expected_end_date, first=False)
    ):
        # After an ambiguous send, replaying POST could create a previously
        # unsent debit. Cancellation/changed consent forbids that recovery path.
        attempt.status = 'CANCELLED' if attempt.status == 'RESERVED' else 'REVIEW'
        agreement.status = 'CANCEL_PENDING'
        agreement.reason = 'unknown_charge' if attempt.status == 'REVIEW' else 'price_changed'
        agreement.cancel_requested_at = agreement.cancel_requested_at or datetime.now(UTC)
        agreement.next_charge_at = None
        await db.commit()
        return
    payload, key = attempt.request_payload, attempt.idempotency_key
    # Persist possible send BEFORE network. A process crash cannot turn it back
    # into a known-unsent request. Concurrent retries share the same provider key.
    attempt.status = 'UNKNOWN'
    attempt.updated_at = datetime.now(UTC)
    await db.commit()
    response = await recurrent_api.create_recurring_charge(payload, key, config=config)
    remote = response['recurring_charge']
    attempt = await db.scalar(
        select(PayHotChargeAttempt)
        .where(PayHotChargeAttempt.id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if attempt.provider_charge_id not in (None, remote['id']):
        await db.rollback()
        raise ValueError('PayHot charge identifier changed')
    attempt.provider_charge_id = remote['id']
    attempt.updated_at = datetime.now(UTC)
    await db.commit()
    canonical = await recurrent_api.get_recurring_charge(remote['id'], config=config)
    await accept_recurring_charge(db, attempt_id, canonical['recurring_charge'], bot=bot)


async def reconcile_payhot_agreements(db, *, limit=25, bot=None):
    """Bounded fair polling also runs when operators disable new enrollments."""
    limit = max(1, min(int(limit), 100))
    cutoff = datetime.now(UTC) - timedelta(seconds=30)
    ids = list(
        (
            await db.scalars(
                select(PayHotAgreement.id)
                .where(PayHotAgreement.status.notin_(TERMINAL_AGREEMENTS), PayHotAgreement.updated_at <= cutoff)
                .order_by(PayHotAgreement.updated_at, PayHotAgreement.id)
                .limit(limit)
            )
        ).all()
    )
    await db.commit()
    result = {'inspected': 0, 'errors': 0}
    for aid in ids:
        try:
            agreement = await db.get(PayHotAgreement, aid, populate_existing=True)
            if agreement is None:
                continue
            result['inspected'] += 1
            if agreement.status == 'CANCEL_PENDING' or agreement.cancel_requested_at:
                await revoke_payhot_agreement(db, aid, bot=bot)
            elif agreement.status in ('CREATING', 'CREATION_UNKNOWN', 'PENDING'):
                await refresh_payhot_enrollment(db, aid, bot=bot)
            elif agreement.status == 'ACTIVE':
                # Check drift before due date too; never silently retain changed
                # prices or service composition under old consent.
                user, target, agreement = await _lock_target(db, aid)
                if user is None or target is None or not await terms_match(db, agreement, target):
                    agreement.status, agreement.reason, agreement.next_charge_at = (
                        'CANCEL_PENDING',
                        'price_changed',
                        None,
                    )
                    agreement.cancel_requested_at = datetime.now(UTC)
                    await db.commit()
                else:
                    await db.commit()
                    attempt = await prepare_payhot_charge(db, aid)
                    if attempt is not None:
                        await submit_payhot_charge(db, attempt.id, bot=bot)
            agreement = await db.get(PayHotAgreement, aid, populate_existing=True)
            if agreement and agreement.first_payment_id:
                await deliver_payhot_renewal(db, aid, payment_id=agreement.first_payment_id)
            if agreement:
                agreement.updated_at = datetime.now(UTC)
                await db.commit()
        except Exception as error:
            await db.rollback()
            result['errors'] += 1
            logger.warning(
                'HOT Pay recurring reconciliation pending', agreement_id=aid, error_type=type(error).__name__
            )
            agreement = await db.get(PayHotAgreement, aid, populate_existing=True)
            if agreement:
                agreement.updated_at = datetime.now(UTC)
                await db.commit()
    # Keep canonical reads/delivery for already submitted charges after cancellation.
    attempts = list(
        (
            await db.scalars(
                select(PayHotChargeAttempt.id)
                .where(
                    PayHotChargeAttempt.status.in_(('RESERVED', 'UNKNOWN', 'PROCESSING', 'SUCCEEDED', 'REVIEW')),
                    PayHotChargeAttempt.delivery_completed_at.is_(None),
                    PayHotChargeAttempt.updated_at <= cutoff,
                )
                .order_by(PayHotChargeAttempt.updated_at, PayHotChargeAttempt.id)
                .limit(limit)
            )
        ).all()
    )
    await db.commit()
    for attempt_id in attempts:
        try:
            attempt = await db.get(PayHotChargeAttempt, attempt_id)
            if attempt.status == 'SUCCEEDED':
                await deliver_payhot_renewal(db, attempt.agreement_id, attempt_id=attempt_id)
            else:
                await submit_payhot_charge(db, attempt_id, bot=bot)
        except Exception as error:
            await db.rollback()
            result['errors'] += 1
            logger.warning('HOT Pay recurring charge pending', attempt_id=attempt_id, error_type=type(error).__name__)
        attempt = await db.get(PayHotChargeAttempt, attempt_id, populate_existing=True)
        if attempt:
            attempt.updated_at = datetime.now(UTC)
            await db.commit()
    from app.services.payhot_recurrent_recovery import recover_terminal_payhot_receipts

    recovered = await recover_terminal_payhot_receipts(db, limit=limit, bot=bot)
    result['inspected'] += recovered['inspected']
    result['errors'] += recovered['errors']
    return result
