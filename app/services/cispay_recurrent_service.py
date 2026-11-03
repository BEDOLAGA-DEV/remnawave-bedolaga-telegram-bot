"""Durable CisPay agreement lifecycle. An ambiguous create must never be repeated."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database.models import CisPaySubscription
from app.services.cispay_recurrent import cispay_tariff_terms_match
from app.services.cispay_service import CisPayAPIError, cispay_service


def safe_payment_url(value):
    from urllib.parse import urlsplit

    if value is None:
        return None
    if not isinstance(value, str) or any(ord(char) < 33 for char in value):
        raise ValueError('Invalid CisPay checkout URL')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Invalid CisPay checkout URL')
    return value


def agreement_state(agreement) -> dict:
    return {
        'status': agreement.status,
        'redirect_url': agreement.redirect_url,
        'subscription_id': agreement.subscription_id,
        'agreement_id': agreement.id,
    }


async def create_cispay_agreement(db, *, attempt_id: int) -> dict:
    agreement = await db.scalar(
        select(CisPaySubscription)
        .where(CisPaySubscription.id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if agreement is None:
        raise ValueError('agreement_not_found')
    if agreement.status != 'CREATING' or agreement.request_started_at is not None:
        await db.commit()
        return agreement_state(agreement)
    config = cispay_service.snapshot()
    if config.shop_id != agreement.shop_id or not config.api_key:
        raise ValueError('cispay_config_changed')
    agreement.request_started_at = datetime.now(UTC)
    agreement.status = 'CREATION_UNKNOWN'
    snapshot, owner_id, purpose = dict(agreement.snapshot), agreement.owner_id, agreement.purpose
    await db.commit()
    try:
        remote = await cispay_service.create_subscription(
            amount_kopeks=snapshot['amount_kopeks'],
            interval_days=snapshot['period_days'],
            customer_id=f'user-{owner_id}',
            purpose=purpose,
            config=config,
        )
    except CisPayAPIError as error:
        agreement = await db.scalar(
            select(CisPaySubscription)
            .where(CisPaySubscription.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if error.status_code in (400, 401, 403, 422):
            agreement.status = 'FAILED'
            agreement.reason = 'provider_permission' if error.status_code == 403 else 'provider_rejected'
        agreement.retry_at = datetime.now(UTC) + timedelta(seconds=max(60, error.retry_after or 0))
        await db.commit()
        return agreement_state(agreement)
    agreement = await db.scalar(
        select(CisPaySubscription)
        .where(CisPaySubscription.id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    agreement.provider_id = remote['id']
    validate_remote_agreement(agreement, remote)
    agreement.status = (
        'CANCEL_PENDING'
        if agreement.status == 'CANCEL_PENDING' or agreement.reason == 'cancel_requested'
        else 'PENDING'
    )
    agreement.remote_status = str(remote.get('status') or 'PENDING')
    agreement.redirect_url = safe_payment_url(remote.get('payment_url'))
    await db.commit()
    return agreement_state(agreement)


def _uuid(value):
    from uuid import UUID

    if not isinstance(value, str):
        raise ValueError('Invalid CisPay identifier')
    return str(UUID(value))


def _date(value):
    if not isinstance(value, str):
        raise ValueError('Invalid CisPay timestamp')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('CisPay timestamp requires timezone')
    return moment


async def _save_event(db, payload, provider_id):
    """Durably retain an early verified webhook; no credentials or arbitrary fields."""
    import hashlib
    import json

    from sqlalchemy.exc import IntegrityError

    from app.database.models import CisPaySubscriptionEvent

    fields = (
        'id',
        'subscription_id',
        'store_id',
        'event',
        'object',
        'status',
        'amount',
        'currency',
        'payment_method',
        'is_sandbox',
        'paid_at',
        'timestamp',
        'reason',
        'initiated_by',
        'customer_id',
    )
    data = {key: payload[key] for key in fields if key in payload}
    event_key = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    existing = await db.scalar(
        select(CisPaySubscriptionEvent).where(
            CisPaySubscriptionEvent.shop_id == payload['store_id'], CisPaySubscriptionEvent.event_key == event_key
        )
    )
    if existing is None:
        try:
            async with db.begin_nested():
                db.add(
                    CisPaySubscriptionEvent(
                        shop_id=payload['store_id'],
                        event_key=event_key,
                        provider_subscription_id=provider_id,
                        payload=data,
                    )
                )
                await db.flush()
        except IntegrityError:
            pass
    await db.commit()
    return True


async def process_cispay_subscription_charge(db, payload: dict, *, bot=None) -> bool:
    from app.config import settings
    from app.database.crud.transaction import create_transaction
    from app.database.models import (
        CisPaySubscriptionCharge,
        PaidTrafficResetIntent,
        PaymentMethod,
        Subscription,
        Tariff,
        TransactionType,
        User,
    )
    from app.services.grace_access_echo import undo_grace_overlay_echo
    from app.services.recurrent_amount import resolve_true_renewal_amount
    from app.services.traffic_base_grant_service import stage_paid_base_grant

    provider_id, charge_id = _uuid(payload.get('subscription_id')), _uuid(payload.get('id'))
    shop = payload.get('store_id')
    agreement = await db.scalar(
        select(CisPaySubscription).where(
            CisPaySubscription.shop_id == shop, CisPaySubscription.provider_id == provider_id
        )
    )
    if agreement is None:
        if shop != settings.CISPAY_SHOP_ID:
            raise ValueError('Invalid CisPay shop')
        return await _save_event(db, payload, provider_id)
    # Financial lock order: account -> target -> agreement -> charge.
    user = await db.scalar(
        select(User).where(User.id == agreement.user_id).with_for_update().execution_options(populate_existing=True)
    )
    from sqlalchemy.orm import selectinload

    target = await db.scalar(
        select(Subscription)
        .where(Subscription.id == agreement.subscription_id)
        .options(selectinload(Subscription.tariff))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    agreement = await db.scalar(
        select(CisPaySubscription)
        .where(CisPaySubscription.id == agreement.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # Validate every monetary event before touching the ledger, including a
    # duplicate or a refund arriving before PAID. Sandbox cannot poison a real charge.
    amount = payload.get('amount')
    if payload.get('status') in ('PAID', 'REFUNDED') and (
        payload.get('is_sandbox') is not False
        or payload.get('currency') != 'RUB'
        or payload.get('payment_method') != 'SBP'
        or type(amount) is not int
        or amount != agreement.snapshot['amount_kopeks']
    ):
        raise ValueError('Invalid CisPay recurring charge')
    charge = await db.scalar(
        select(CisPaySubscriptionCharge).where(
            CisPaySubscriptionCharge.shop_id == shop, CisPaySubscriptionCharge.provider_transaction_id == charge_id
        )
    )
    if charge is not None:
        if payload.get('status') == 'REFUNDED':
            charge.status = 'REFUND_REVIEW'
            await db.commit()
        else:
            await db.commit()
        return True
    if payload.get('status') == 'REFUNDED':
        # Webhooks can arrive out of order. Keep a tombstone so a delayed PAID
        # cannot grant access after the refund has already been reported.
        db.add(
            CisPaySubscriptionCharge(
                agreement_id=agreement.id,
                shop_id=shop,
                provider_transaction_id=charge_id,
                amount_kopeks=agreement.snapshot['amount_kopeks'],
                status='REFUND_REVIEW',
            )
        )
        await db.commit()
        return True
    if payload.get('status') != 'PAID':
        if payload.get('status') not in ('FAILED', 'EXPIRED', 'REFUNDED', 'PENDING'):
            raise ValueError('Invalid CisPay transaction status')
        return await _save_event(db, payload, provider_id)
    paid_at = _date(payload.get('paid_at'))
    snapshot = agreement.snapshot
    trial_source = snapshot.get('trial_source')
    converting_trial = bool(target is not None and target.is_trial and trial_source)
    target_matches = (
        user is not None
        and user.status == 'active'
        and not user.restriction_subscription
        and target is not None
        and target.user_id == agreement.owner_id
        and target.status != 'disabled'
        and (agreement.expected_end_date is None or target.end_date == agreement.expected_end_date)
        and agreement.reason not in ('manual_change', 'subscription_deleted', 'price_changed')
        and (
            (
                converting_trial
                and target.tariff_id == trial_source['tariff_id']
                and target.device_limit == trial_source['device_limit']
            )
            or (
                not target.is_trial
                and target.tariff_id == snapshot['tariff_id']
                and target.device_limit == snapshot['device_limit']
            )
        )
    )
    tariff = await db.get(Tariff, snapshot['tariff_id'])
    target_matches = target_matches and tariff is not None and tariff.is_active and not tariff.is_daily
    target_matches = target_matches and cispay_tariff_terms_match(tariff, snapshot)
    if target_matches and converting_trial:
        from app.services.pricing_engine import pricing_engine

        price = await pricing_engine.calculate_tariff_purchase_price(
            tariff, snapshot['period_days'], device_limit=snapshot['device_limit']
        )
        target_matches = int(price.final_total) == amount
    elif target_matches:
        target_matches = await resolve_true_renewal_amount(db, target, snapshot['period_days']) == amount
    if target_matches:
        # Another ordinary purchase may have won while the hosted bank page was open.
        competing = select(Subscription.id).where(
            Subscription.user_id == user.id,
            Subscription.id != target.id,
            Subscription.is_trial.is_(False),
            Subscription.status.in_(('active', 'limited', 'pending')),
        )
        if settings.is_multi_tariff_enabled():
            competing = competing.where(Subscription.tariff_id == snapshot['tariff_id'])
        target_matches = await db.scalar(competing.limit(1)) is None
    charge = CisPaySubscriptionCharge(
        agreement_id=agreement.id,
        shop_id=shop,
        provider_transaction_id=charge_id,
        amount_kopeks=amount,
        status='REVIEW',
        paid_at=paid_at,
    )
    db.add(charge)
    if user is not None and user.status != 'deleted':
        if target_matches:
            await undo_grace_overlay_echo(db, target)
            if converting_trial:
                from app.database.crud.subscription import extend_subscription, should_carry_trial_remaining_days

                if not should_carry_trial_remaining_days():
                    target.end_date = datetime.now(UTC)
                await extend_subscription(
                    db,
                    target,
                    snapshot['period_days'],
                    tariff_id=tariff.id,
                    device_limit=snapshot['device_limit'],
                    traffic_limit_gb=tariff.traffic_limit_gb,
                    connected_squads=tariff.allowed_squads or [],
                    paid_traffic_reset=False,
                    reset_used_traffic=settings.RESET_TRAFFIC_ON_PAYMENT
                    if target.traffic_accounting_mode == 'managed'
                    else False,
                    commit=False,
                    provider_charge=True,
                )
            else:
                target.extend_subscription(snapshot['period_days'])
            agreement.expected_end_date = target.end_date
            target.autopay_enabled = False
            user.has_had_paid_subscription = True
            target.status = 'active'
            kind = TransactionType.SUBSCRIPTION_PAYMENT
            charge.status = 'APPLIED'
        else:
            user.balance_kopeks += amount
            kind = TransactionType.DEPOSIT
            charge.status = 'BALANCE'
            if agreement.status not in ('CANCELLED', 'FAILED'):
                agreement.status = 'CANCEL_PENDING'
                agreement.reason = 'target_changed'
        tx = await create_transaction(
            db,
            user_id=user.id,
            type=kind,
            amount_kopeks=amount,
            description='Оплата периода CisPay' if target_matches else 'CisPay: оплата по изменённой подписке',
            payment_method=PaymentMethod.CISPAY,
            external_id=f'cisrec:{charge_id}',
            commit=False,
        )
        charge.transaction_id = tx.id
        if target_matches and settings.RESET_TRAFFIC_ON_PAYMENT and target.traffic_accounting_mode == 'legacy':
            db.add(
                PaidTrafficResetIntent(
                    subscription_id=target.id,
                    user_id=user.id,
                    transaction_id=tx.id,
                    operation_key=f'cispay:payment:{tx.id}',
                    charge_at=target.end_date,
                    reason='CisPay: оплаченный период',
                    state='pending',
                )
            )
        if target_matches and not converting_trial:
            await stage_paid_base_grant(
                db, target, operation_key=f'cispay:payment:{tx.id}', reset_base=settings.RESET_TRAFFIC_ON_PAYMENT
            )
    agreement.last_charge_at = max(filter(None, (agreement.last_charge_at, paid_at)))
    await db.commit()
    if charge.transaction_id is not None:
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            tx,
            amount_kopeks=amount,
            user_id=user.id,
            type=kind,
            payment_method=PaymentMethod.CISPAY,
            external_id=f'cisrec:{charge_id}',
            description=tx.description,
        )
    try:
        from app.cabinet.routes.websocket import cabinet_ws_manager
        from app.services.notification_delivery_service import notification_delivery_service

        if user is not None:
            await cabinet_ws_manager.send_to_user(
                user.id,
                {
                    'type': 'cispay_recurring.updated',
                    'subscription_id': agreement.subscription_id,
                    'status': agreement.status,
                },
            )
            if target_matches:
                await notification_delivery_service.notify_autopay_success(
                    user,
                    amount,
                    target.end_date,
                    bot=bot,
                )
            elif charge.status == 'BALANCE':
                await notification_delivery_service.notify_balance_topup(
                    user,
                    amount,
                    user.balance_kopeks,
                    bot=bot,
                )
    except Exception:
        import structlog

        structlog.get_logger(__name__).warning('CisPay payment notification unavailable', charge_id=charge.id)
    if target_matches:
        await deliver_cispay_charge(db, charge.id)
    return True


async def deliver_cispay_charge(db, charge_id: int):
    from app.config import settings
    from app.database.models import CisPaySubscriptionCharge, PaidTrafficResetIntent, Subscription, User
    from app.services.subscription_service import SubscriptionService

    charge = await db.get(CisPaySubscriptionCharge, charge_id)
    if charge is None or charge.status != 'APPLIED' or charge.delivery_completed_at is not None:
        return
    agreement = await db.get(CisPaySubscription, charge.agreement_id)
    target = await db.get(Subscription, agreement.subscription_id) if agreement.subscription_id else None
    user = await db.get(User, agreement.user_id) if agreement.user_id else None
    if target is None or user is None or target.user_id != agreement.owner_id or user.status != 'active':
        return
    panel_id = target.remnawave_id if settings.is_multi_tariff_enabled() else user.remnawave_id
    service = SubscriptionService()
    if panel_id or target.subscription_url:
        result = await service.update_remnawave_user(db, target, reset_traffic=False)
    else:
        result = await service.create_remnawave_user(db, target, reset_traffic=False)
    if result is not None:
        intent_id = await db.scalar(
            select(PaidTrafficResetIntent.id).where(
                PaidTrafficResetIntent.operation_key == f'cispay:payment:{charge.transaction_id}'
            )
        )
        if intent_id is not None:
            from app.services.paid_traffic_reset_service import deliver_daily_reset

            await deliver_daily_reset(db, intent_id)
        charge = await db.get(CisPaySubscriptionCharge, charge_id)
        charge.delivery_completed_at = datetime.now(UTC)
        await db.commit()


async def process_cispay_subscription_event(db, payload: dict) -> bool:
    from app.config import settings

    provider_id = _uuid(payload.get('id'))
    if payload.get('object') != 'subscription' or payload.get('event') not in (
        'subscription.activated',
        'subscription.cancelled',
        'subscription.failed',
    ):
        raise ValueError('Invalid CisPay event')
    if payload.get('store_id') != settings.CISPAY_SHOP_ID:
        raise ValueError('Invalid CisPay shop')
    await _save_event(db, payload, provider_id)
    agreement_id = await db.scalar(
        select(CisPaySubscription.id).where(
            CisPaySubscription.shop_id == payload['store_id'], CisPaySubscription.provider_id == provider_id
        )
    )
    if agreement_id is not None:
        await refresh_cispay_agreement(db, agreement_id=agreement_id)
    return True


def validate_remote_agreement(agreement, remote):
    snapshot = agreement.snapshot
    if (
        _uuid(remote.get('id')) != agreement.provider_id
        or remote.get('customer_id') != f'user-{agreement.owner_id}'
        or remote.get('amount') != snapshot['amount_kopeks']
        or type(remote.get('amount')) is not int
        or remote.get('currency') != 'RUB'
        or remote.get('payment_method') != 'SBP'
        or remote.get('interval_days') != snapshot['period_days']
        or remote.get('status') not in ('PENDING', 'ACTIVE', 'PAUSED', 'CANCELLED', 'FAILED')
    ):
        raise ValueError('CisPay agreement does not match consent')


def apply_remote_state(agreement, remote):
    validate_remote_agreement(agreement, remote)
    status = remote['status']
    agreement.remote_status = status
    if status in ('CANCELLED', 'FAILED'):
        agreement.status = status
        agreement.reason = str(remote.get('ended_reason') or agreement.reason or '')[:64] or None
    elif agreement.status not in ('CANCEL_PENDING', 'CANCELLED', 'FAILED'):
        agreement.status = status
    agreement.next_charge_at = _date(remote['next_charge_at']) if remote.get('next_charge_at') else None
    if remote.get('last_charge_at'):
        last = _date(remote['last_charge_at'])
        agreement.last_charge_at = max(filter(None, (agreement.last_charge_at, last)))
    if status != 'PENDING':
        agreement.redirect_url = None


async def refresh_cispay_agreement(db, *, agreement_id: int):
    agreement = await db.get(CisPaySubscription, agreement_id)
    config = cispay_service.snapshot()
    if not agreement or agreement.shop_id != config.shop_id or not agreement.provider_id:
        return False
    provider_id = agreement.provider_id
    await db.commit()
    remote = await cispay_service.get_subscription(provider_id, config=config)
    agreement = await db.scalar(
        select(CisPaySubscription)
        .where(CisPaySubscription.id == agreement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    apply_remote_state(agreement, remote)
    await db.commit()
    return remote


async def _cancel_agreement(db, *, agreement_id: int):
    agreement = await db.scalar(
        select(CisPaySubscription)
        .where(CisPaySubscription.id == agreement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    config = cispay_service.snapshot()
    if agreement.status in ('CANCELLED', 'FAILED'):
        await db.commit()
        return agreement_state(agreement)
    if not agreement.provider_id:
        if agreement.request_started_at is None:
            agreement.status = 'CANCELLED'
        else:
            # Ambiguous create must be recovered before revocation can be confirmed.
            agreement.reason = 'cancel_requested'
        await db.commit()
        return agreement_state(agreement)
    agreement.status = 'CANCEL_PENDING'
    provider_id = agreement.provider_id
    if config.shop_id != agreement.shop_id or not config.api_key:
        agreement.reason = 'config_changed'
        await db.commit()
        return agreement_state(agreement)
    await db.commit()
    try:
        remote = await cispay_service.cancel_subscription(provider_id, config=config)
    except CisPayAPIError as error:
        agreement = await db.scalar(
            select(CisPaySubscription)
            .where(CisPaySubscription.id == agreement_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        agreement.retry_at = datetime.now(UTC) + timedelta(seconds=max(60, error.retry_after or 0))
        await db.commit()
        return agreement_state(agreement)
    agreement = await db.scalar(
        select(CisPaySubscription)
        .where(CisPaySubscription.id == agreement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    apply_remote_state(agreement, remote)
    await db.commit()
    return agreement_state(agreement)


async def cancel_cispay_agreement(db, *, user_id: int, subscription_id: int, reason: str) -> dict:
    from app.database.crud.cispay_subscriptions import LIVE_STATUSES
    from app.database.models import Subscription, User

    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    target = await db.scalar(
        select(Subscription)
        .where(Subscription.id == subscription_id, Subscription.user_id == user_id)
        .with_for_update()
    )
    if user is None or target is None:
        raise ValueError('subscription_not_found')
    agreement = await db.scalar(
        select(CisPaySubscription)
        .where(
            CisPaySubscription.owner_id == user_id,
            CisPaySubscription.subscription_id == subscription_id,
            CisPaySubscription.status.in_(LIVE_STATUSES),
        )
        .order_by(CisPaySubscription.id.desc())
        .with_for_update()
    )
    if agreement is None:
        await db.commit()
        return {'status': 'none'}
    agreement.reason = reason[:64]
    agreement_id = agreement.id
    await db.commit()
    return await _cancel_agreement(db, agreement_id=agreement_id)


async def _recover_creation(db, agreement_id):
    agreement = await db.get(CisPaySubscription, agreement_id)
    config = cispay_service.snapshot()
    if agreement.shop_id != config.shop_id or agreement.reason == 'ambiguous_remote':
        return
    offset, purpose = agreement.reconcile_offset, agreement.purpose
    await db.commit()
    page = await cispay_service.list_subscriptions(limit=100, offset=offset, config=config)
    agreement = await db.scalar(
        select(CisPaySubscription)
        .where(CisPaySubscription.id == agreement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if agreement.provider_id:
        await db.commit()
        return
    candidates = set(agreement.recovery_candidates or [])
    for remote in page.get('items', []):
        if remote.get('subscription_purpose') == purpose and remote.get('customer_id') == f'user-{agreement.owner_id}':
            candidates.add(_uuid(remote.get('id')))
    agreement.recovery_candidates = sorted(candidates)
    if len(candidates) > 1:
        agreement.reason = 'ambiguous_remote'
    elif page.get('has_more'):
        agreement.reconcile_offset += 90
    elif len(candidates) == 1:
        remote_id = next(iter(candidates))
        await db.commit()
        remote = await cispay_service.get_subscription(remote_id, config=config)
        agreement = await db.scalar(
            select(CisPaySubscription)
            .where(CisPaySubscription.id == agreement_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        agreement.provider_id = remote_id
        validate_remote_agreement(agreement, remote)
        agreement.status = (
            'CANCEL_PENDING'
            if agreement.status == 'CANCEL_PENDING' or agreement.reason == 'cancel_requested'
            else 'PENDING'
        )
        apply_remote_state(agreement, remote)
        agreement.redirect_url = safe_payment_url(remote.get('payment_url')) if remote['status'] == 'PENDING' else None
        agreement.reconcile_offset = 0
    else:
        # An empty list does not prove that an ambiguous POST was not accepted.
        agreement.reconcile_offset = 0
    await db.commit()


async def _queue_changed_agreement(db, agreement_id: int):
    from sqlalchemy.orm import selectinload

    from app.database.crud.cispay_subscriptions import LIVE_STATUSES
    from app.database.models import Subscription, Tariff, User
    from app.services.pricing_engine import pricing_engine

    agreement = await db.get(CisPaySubscription, agreement_id)
    if agreement.status not in LIVE_STATUSES or agreement.status == 'CANCEL_PENDING':
        return
    user = await db.scalar(select(User).where(User.id == agreement.user_id).with_for_update())
    target = await db.scalar(
        select(Subscription)
        .where(Subscription.id == agreement.subscription_id)
        .options(selectinload(Subscription.tariff))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    agreement = await db.scalar(
        select(CisPaySubscription)
        .where(CisPaySubscription.id == agreement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    snapshot = agreement.snapshot
    trial = snapshot.get('trial_source') if target is not None and target.is_trial else None
    expected = trial or snapshot
    valid = (
        user is not None
        and user.status == 'active'
        and not user.restriction_subscription
        and target is not None
        and target.user_id == agreement.owner_id
        and target.status != 'disabled'
        and target.tariff_id == expected['tariff_id']
        and target.device_limit == expected['device_limit']
        and (agreement.expected_end_date is None or target.end_date == agreement.expected_end_date)
    )
    tariff = await db.get(Tariff, snapshot['tariff_id'])
    valid = valid and tariff is not None and tariff.is_active and not tariff.is_daily
    valid = valid and cispay_tariff_terms_match(tariff, snapshot)
    if valid:
        pricing = await pricing_engine.calculate_tariff_purchase_price(
            tariff, snapshot['period_days'], device_limit=snapshot['device_limit']
        )
        valid = int(pricing.final_total) == snapshot['amount_kopeks']
    if valid:
        # Mirror the PAID guard before the next bank charge, including a switch
        # from multi to single mode while multiple subscriptions still exist.
        from app.config import settings

        competing = select(Subscription.id).where(
            Subscription.user_id == user.id,
            Subscription.id != target.id,
            Subscription.is_trial.is_(False),
            Subscription.status.in_(('active', 'limited', 'pending')),
        )
        if settings.is_multi_tariff_enabled():
            competing = competing.where(Subscription.tariff_id == snapshot['tariff_id'])
        valid = await db.scalar(competing.limit(1)) is None
    if not valid and agreement.status in LIVE_STATUSES:
        agreement.status = 'CANCEL_PENDING'
        agreement.reason = 'target_changed'
    await db.commit()


async def reconcile_cispay_agreements(db, *, limit: int = 25, bot=None) -> dict:
    from sqlalchemy import or_

    from app.database.models import CisPaySubscriptionCharge, CisPaySubscriptionEvent

    config = cispay_service.snapshot()
    result = {'inspected': 0, 'errors': 0}
    if not config.shop_id or not config.api_key:
        return result
    now = datetime.now(UTC)
    ids = list(
        (
            await db.scalars(
                select(CisPaySubscription.id)
                .where(
                    CisPaySubscription.shop_id == config.shop_id,
                    or_(CisPaySubscription.retry_at.is_(None), CisPaySubscription.retry_at <= now),
                )
                .order_by(CisPaySubscription.last_reconciled_at.asc().nullsfirst(), CisPaySubscription.id)
                .limit(min(50, max(1, limit)))
            )
        ).all()
    )
    await db.commit()
    for agreement_id in ids:
        try:
            await _queue_changed_agreement(db, agreement_id)
            agreement = await db.get(CisPaySubscription, agreement_id)
            if not agreement.provider_id:
                if agreement.status == 'CANCEL_PENDING':
                    if agreement.request_started_at is None:
                        await _cancel_agreement(db, agreement_id=agreement_id)
                    else:
                        await _recover_creation(db, agreement_id)
                elif agreement.status in ('CREATING', 'CREATION_UNKNOWN'):
                    if agreement.request_started_at is None:
                        await create_cispay_agreement(db, attempt_id=agreement_id)
                    else:
                        await _recover_creation(db, agreement_id)
                agreement = await db.get(CisPaySubscription, agreement_id)
            if agreement.provider_id:
                if agreement.status == 'CANCEL_PENDING':
                    await _cancel_agreement(db, agreement_id=agreement_id)
                remote = await refresh_cispay_agreement(db, agreement_id=agreement_id)
                agreement = await db.get(CisPaySubscription, agreement_id)
                offset = agreement.reconcile_offset
                provider_id = agreement.provider_id
                owner_id = agreement.owner_id
                # Subscription history omits is_sandbox: join to authenticated transaction journal.
                await db.commit()
                page = await cispay_service.list_transactions(limit=100, offset=offset, config=config)
                charges = {entry['id']: entry for entry in remote.get('charges', []) if entry.get('id')}
                for entry in page.get('items', []):
                    charge = charges.get(entry.get('id'))
                    if charge is None or entry.get('status') not in ('PAID', 'REFUNDED'):
                        continue
                    if entry.get('customer_id') not in (None, f'user-{owner_id}'):
                        continue
                    if entry.get('amount') != charge.get('amount') or entry.get('status') != charge.get('status'):
                        continue
                    if entry.get('is_sandbox') is not False:
                        continue
                    await process_cispay_subscription_charge(
                        db,
                        {
                            'id': entry['id'],
                            'subscription_id': provider_id,
                            'store_id': config.shop_id,
                            'status': entry['status'],
                            'amount': entry['amount'],
                            'currency': remote['currency'],
                            'payment_method': entry['payment_method'],
                            'is_sandbox': False,
                            'paid_at': entry.get('paid_at'),
                        },
                        bot=bot,
                    )
                agreement = await db.get(CisPaySubscription, agreement_id)
                agreement.reconcile_offset = offset + 90 if page.get('has_more') else 0
                await db.commit()
                events = list(
                    (
                        await db.scalars(
                            select(CisPaySubscriptionEvent)
                            .where(
                                CisPaySubscriptionEvent.shop_id == config.shop_id,
                                CisPaySubscriptionEvent.provider_subscription_id == provider_id,
                                CisPaySubscriptionEvent.processed_at.is_(None),
                            )
                            .order_by(CisPaySubscriptionEvent.id)
                            .limit(100)
                        )
                    ).all()
                )
                for event in events:
                    event_id, payload = event.id, dict(event.payload)
                    if not payload.get('event'):
                        if payload.get('status') in ('PAID', 'REFUNDED'):
                            await process_cispay_subscription_charge(db, payload, bot=bot)
                    event = await db.get(CisPaySubscriptionEvent, event_id)
                    event.processed_at = now
                    await db.commit()
                pending_delivery = list(
                    (
                        await db.scalars(
                            select(CisPaySubscriptionCharge.id)
                            .where(
                                CisPaySubscriptionCharge.agreement_id == agreement_id,
                                CisPaySubscriptionCharge.status == 'APPLIED',
                                CisPaySubscriptionCharge.delivery_completed_at.is_(None),
                            )
                            .limit(25)
                        )
                    ).all()
                )
                for charge_id in pending_delivery:
                    await deliver_cispay_charge(db, charge_id)
            agreement = await db.get(CisPaySubscription, agreement_id)
            agreement.last_reconciled_at = now
            await db.commit()
            result['inspected'] += 1
        except (CisPayAPIError, ValueError) as error:
            await db.rollback()
            agreement = await db.get(CisPaySubscription, agreement_id)
            agreement.retry_at = now + timedelta(seconds=max(60, getattr(error, 'retry_after', None) or 0))
            agreement.last_reconciled_at = now
            await db.commit()
            result['errors'] += 1
            if getattr(error, 'status_code', None) == 429:
                break
        except Exception:
            import structlog

            await db.rollback()
            structlog.get_logger(__name__).exception('CisPay reconciliation failed', agreement_id=agreement_id)
            agreement = await db.get(CisPaySubscription, agreement_id)
            if agreement is not None:
                agreement.retry_at = now + timedelta(minutes=5)
                agreement.last_reconciled_at = now
                await db.commit()
            result['errors'] += 1
    return result
