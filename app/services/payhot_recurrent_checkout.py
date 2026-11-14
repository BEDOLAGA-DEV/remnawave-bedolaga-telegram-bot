"""One HOT Pay quote/consent implementation for Telegram and cabinet."""

import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import PayHotAgreement, PayHotPayment, SavedPaymentMethod, Subscription, Tariff
from app.services.payhot_recurrent_guard import payhot_obligation_condition
from app.services.payhot_recurrent_policy import get_payhot_recurrent_policy
from app.services.payhot_recurrent_terms import PayHotRecurrentTerms


def payhot_service_terms(tariff) -> dict:
    """Snapshot service configuration separately from its computed price."""
    return {
        'traffic_limit_gb': tariff.traffic_limit_gb,
        'device_limit': tariff.device_limit,
        'allowed_squads': sorted(set(tariff.allowed_squads or [])),
        'server_traffic_limits': dict(tariff.server_traffic_limits or {}),
        'external_squad_uuid': tariff.external_squad_uuid,
        'traffic_reset_mode': str(tariff.traffic_reset_mode or settings.DEFAULT_TRAFFIC_RESET_STRATEGY).upper(),
        'panel_tag': tariff.panel_tag,
        'is_daily': bool(tariff.is_daily),
        'reset_traffic_on_payment': bool(settings.RESET_TRAFFIC_ON_PAYMENT),
    }


async def _source_terms(db, target):
    from app.services.grace_access_echo import terms_without_grace_echo

    squads, traffic_limit = await terms_without_grace_echo(db, target)
    return {
        'traffic_limit_gb': traffic_limit,
        'purchased_traffic_gb': target.purchased_traffic_gb,
        'connected_squads': sorted(set(squads)),
        'device_limit': target.device_limit,
        'daily_devices_billing_version': target.daily_devices_billing_version or 0,
    }


async def current_payhot_terms(db, target, snapshot) -> dict:
    """Reprice entitlements only; does not authorize enrollment or a provider send."""
    from app.services.pricing_engine import PricingEngine, pricing_engine
    from app.services.recurrent_amount import resolve_true_renewal_amount
    from app.services.tariff_purchase_devices import resolve_purchase_device_selection
    from app.services.tariff_purchase_quote import validate_purchase_duration

    days = snapshot['period_days']
    validate_purchase_duration(days, target)
    tariff_id = snapshot.get('tariff_id')
    tariff = await db.get(Tariff, tariff_id, populate_existing=True) if tariff_id is not None else None
    effective = snapshot.get('device_limit')
    # The version only affects daily-device pricing. Trial conversion resets it
    # in the subscription CRUD; a period tariff must not treat that reset as
    # a price change on the next recurring cycle. Keep source_terms exact for
    # the separate first-payment source fence.
    billing_version = (getattr(target, 'daily_devices_billing_version', 0) or 0) if tariff and tariff.is_daily else 0
    if tariff_id is not None:
        if tariff is None or not tariff.is_active:
            raise ValueError('tariff_unavailable')
        if not tariff.has_configured_price_for_period(days) or (tariff.is_daily and days != 1):
            raise ValueError('period_unavailable')
        if target is not None and not target.is_trial and not snapshot.get('new_subscription'):
            if target.tariff_id != tariff_id:
                raise ValueError('tariff_switch_requires_balance')
            if effective is not None and effective != target.device_limit:
                raise ValueError('device_selection_not_available')
            effective = target.device_limit
        else:
            purchase_target = None if snapshot.get('new_subscription') else target
            default = resolve_purchase_device_selection(tariff, purchase_target, None)
            selected = None if effective in (None, default.effective_limit) else effective
            selection = resolve_purchase_device_selection(tariff, purchase_target, selected)
            effective = selection.effective_limit
            if tariff.is_daily and selection.selectable:
                billing_version = 1
        if tariff.is_daily:
            # Same lasting group/device rules as the daily worker, no one-off offer.
            user = snapshot.get('_pricing_user')
            if user is None and target is not None:
                from app.database.crud.user import get_user_by_id

                user = await get_user_by_id(db, target.user_id)
            amount = PricingEngine.daily_subscription_price(
                SimpleNamespace(device_limit=effective, daily_devices_billing_version=billing_version), tariff, user
            )
        else:
            pricing = await pricing_engine.calculate_tariff_purchase_price(tariff, days, device_limit=effective)
            amount = int(pricing.final_total)
        composition = payhot_service_terms(tariff)
        if not composition['allowed_squads']:
            from app.database.crud.server_squad import get_all_server_squads

            servers, _ = await get_all_server_squads(db, available_only=True, limit=10000)
            composition['allowed_squads'] = sorted({server.squad_uuid for server in servers if server.squad_uuid})
    else:
        if target is None or target.is_trial or target.tariff_id is not None:
            raise ValueError('classic_paid_subscription_required')
        if effective is not None and effective != target.device_limit:
            raise ValueError('device_selection_not_available')
        effective = target.device_limit
        amount = await resolve_true_renewal_amount(db, target, days)
        if amount is None:
            raise ValueError('price_unavailable')
        composition = {
            **(await _source_terms(db, target)),
            'traffic_reset_mode': settings.DEFAULT_TRAFFIC_RESET_STRATEGY,
            'reset_traffic_on_payment': bool(settings.RESET_TRAFFIC_ON_PAYMENT),
        }
    PayHotRecurrentTerms(
        amount_kopeks=amount,
        period_days=days,
        tariff_id=tariff_id,
        device_limit=effective,
        payment_method=snapshot['payment_method'],
        is_daily=bool(tariff and tariff.is_daily),
        service_snapshot=composition,
    )
    return dict(
        tariff_id=tariff_id,
        tariff_terms=composition,
        period_days=days,
        device_limit=effective,
        amount_kopeks=amount,
        payment_method=snapshot['payment_method'],
        currency='RUB',
        is_daily=bool(tariff and tariff.is_daily),
        daily_devices_billing_version=billing_version,
    )


async def current_payhot_terms_match(db, target, snapshot, *, first=False) -> bool:
    """Compare immutable financial/service consent, not paid expiry or send eligibility."""
    try:
        if snapshot.get('api_key_fingerprint') != hashlib.sha256((settings.PAYHOT_API_KEY or '').encode()).hexdigest():
            return False
        if target is None:
            return False
        if (
            first
            and snapshot.get('source_terms') is not None
            and (await _source_terms(db, target)) != snapshot['source_terms']
        ):
            return False
        if first and snapshot.get('trial_source'):
            if not target.is_trial or snapshot['trial_source'] != {
                'tariff_id': target.tariff_id,
                'device_limit': target.device_limit,
            }:
                return False
        elif not first and target.is_trial:
            return False
        if not first and snapshot.get('tariff_id') is not None:
            paid_source = await _source_terms(db, target)
            if paid_source['connected_squads'] != snapshot.get('tariff_terms', {}).get('allowed_squads'):
                return False
        current = await current_payhot_terms(
            db, target, {**snapshot, 'new_subscription': bool(first and snapshot.get('new_subscription'))}
        )
        return all(snapshot.get(key) == value for key, value in current.items())
    except (ValueError, KeyError, TypeError):
        return False


async def _owner_provider_conflict(db, user_id, tariff_id):
    from app.database.crud.cispay_subscriptions import LIVE_STATUSES as CISPAY_LIVE
    from app.database.models import CasheraSubscription, CisPaySubscription, LavaSubscription, PlategaSubscription

    for model in (CisPaySubscription, PlategaSubscription, LavaSubscription, CasheraSubscription):
        owner = model.owner_id if model is CisPaySubscription else model.user_id
        statuses = CISPAY_LIVE if model is CisPaySubscription else ('PENDING', 'ACTIVE', 'PAST_DUE')
        query = select(model.id).where(owner == user_id, model.status.in_(statuses))
        if settings.is_multi_tariff_enabled():
            query = query.join(Subscription, Subscription.id == model.subscription_id).where(
                Subscription.tariff_id == tariff_id
            )
        if await db.scalar(query.limit(1)) is not None:
            return True
    return False


async def _terms(db, *, user, tariff_id, period_days, device_limit, subscription_id, payment_method, channel):
    from app.services.cispay_recurrent_checkout import _other_recurring

    policy = await get_payhot_recurrent_policy(db, user=user, channel=channel)
    if not policy['enabled']:
        raise ValueError(policy['reason'])
    if payment_method not in policy['methods']:
        raise ValueError('payment_method_unavailable')
    target = None
    if subscription_id is not None:
        target = await db.scalar(
            select(Subscription)
            .where(Subscription.id == subscription_id, Subscription.user_id == user.id)
            .options(selectinload(Subscription.tariff))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if target is None:
            raise ValueError('subscription_not_found')
        if target.status == 'disabled' or target.is_daily_paused:
            raise ValueError('subscription_disabled')
        if target.autopay_enabled:
            raise ValueError('disable_balance_autopay_first')
        if await _other_recurring(db, target.id):
            raise ValueError('recurrent_already_exists')
    if await db.scalar(
        select(SavedPaymentMethod.id)
        .where(SavedPaymentMethod.user_id == user.id, SavedPaymentMethod.is_active.is_(True))
        .limit(1)
    ):
        raise ValueError('recurrent_already_exists')
    if tariff_id is None and settings.is_tariffs_mode():
        # Existing legacy paid subscriptions may still be renewed in tariff deployments.
        if target is None or target.tariff_id is not None or target.is_trial:
            raise ValueError('classic_paid_subscription_required')
    tariff = await db.get(Tariff, tariff_id, populate_existing=True) if tariff_id is not None else None
    if tariff_id is not None:
        if not settings.is_tariffs_mode() or tariff is None or not tariff.is_active:
            raise ValueError('tariff_unavailable')
        promo = user.get_primary_promo_group()
        if not tariff.is_available_for_promo_group(promo.id if promo else None):
            raise ValueError('tariff_unavailable')
    live = select(PayHotAgreement.id).where(PayHotAgreement.owner_id == user.id, payhot_obligation_condition())
    if settings.is_multi_tariff_enabled():
        live = live.join(Subscription, Subscription.id == PayHotAgreement.subscription_id).where(
            Subscription.tariff_id == tariff_id
        )
    if await db.scalar(live.limit(1)) or await _owner_provider_conflict(db, user.id, tariff_id):
        raise ValueError('recurrent_already_exists')
    candidates = list(
        (
            await db.scalars(
                select(Subscription).where(
                    Subscription.user_id == user.id, Subscription.status.in_(('active', 'trial', 'limited', 'pending'))
                )
            )
        ).all()
    )
    if any(
        (target is None or row.id != target.id)
        and (not settings.is_multi_tariff_enabled() or row.tariff_id == tariff_id)
        for row in candidates
    ):
        raise ValueError('select_existing_subscription')
    values = await current_payhot_terms(
        db,
        target,
        dict(
            tariff_id=tariff_id,
            period_days=period_days,
            device_limit=device_limit,
            payment_method=payment_method,
            _pricing_user=user,
        ),
    )
    limits = policy['checkout_policy']
    if (
        not max(1, limits['min_amount_kopeks'])
        <= values['amount_kopeks']
        <= min(2147483647, limits['max_amount_kopeks'])
    ):
        raise ValueError('amount_out_of_range')
    return {
        **values,
        'source_subscription_id': subscription_id,
        'source_end_date': target.end_date.isoformat() if target else None,
        'source_terms': await _source_terms(db, target) if target else None,
        'trial_source': {'tariff_id': target.tariff_id, 'device_limit': target.device_limit}
        if target and target.is_trial
        else None,
        'new_subscription': target is None,
        'channel': channel,
        # Fingerprint a provider-generated API key to detect credential rotation.
        # This is not password authentication; keep existing consent hashes stable.
        'api_key_fingerprint': hashlib.sha256((settings.PAYHOT_API_KEY or '').encode()).hexdigest(),
    }


async def quote_payhot_purchase(
    db, *, user, tariff_id, period_days, device_limit=None, subscription_id=None, payment_method='card', channel
):
    from app.database.crud.user import lock_user_for_pricing
    from app.services.tariff_purchase_quote import save_purchase_quote

    if user is None:
        raise ValueError('user_restricted')
    user = await lock_user_for_pricing(db, user.id)
    terms = await _terms(
        db,
        user=user,
        tariff_id=tariff_id,
        period_days=period_days,
        device_limit=device_limit,
        subscription_id=subscription_id,
        payment_method=payment_method,
        channel=channel,
    )
    quote_id, expires = await save_purchase_quote(user.id, f'payhot:{channel}', terms)
    return {
        **{
            key: terms[key]
            for key in (
                'amount_kopeks',
                'period_days',
                'device_limit',
                'tariff_id',
                'payment_method',
                'currency',
                'is_daily',
                'source_subscription_id',
            )
        },
        'subscription_id': subscription_id,
        'quote_id': str(quote_id),
        'expires_at': expires.isoformat(),
        'first_payment_required': True,
    }


async def purchase_payhot_quote(db, *, user, quote_id, checkout_id, channel):
    from app.database.crud.subscription import create_sbp_pending_subscription
    from app.database.crud.user import lock_user_for_pricing
    from app.services.tariff_purchase_quote import load_purchase_quote

    quote_id, checkout_id = str(UUID(str(quote_id))), str(UUID(str(checkout_id)))
    try:
        user = await lock_user_for_pricing(db, user.id)
        if user.status != 'active' or user.restriction_subscription or user.restriction_topup:
            raise ValueError('user_restricted')
        previous = await db.scalar(
            select(PayHotAgreement).where(
                PayHotAgreement.owner_id == user.id, PayHotAgreement.checkout_id == checkout_id
            )
        )
        if previous:
            if (
                previous.consent_snapshot.get('quote_id') != quote_id
                or previous.consent_snapshot.get('channel') != channel
            ):
                raise ValueError('checkout_conflict')
            agreement_id = previous.id
        else:
            agreed = await load_purchase_quote(user.id, f'payhot:{channel}', UUID(quote_id))
            fresh = await _terms(
                db,
                user=user,
                tariff_id=agreed['tariff_id'],
                period_days=agreed['period_days'],
                device_limit=agreed['device_limit'],
                subscription_id=agreed['source_subscription_id'],
                payment_method=agreed['payment_method'],
                channel=channel,
            )
            if fresh != agreed:
                raise ValueError('quote_changed')
            target_id = fresh['source_subscription_id']
            allocated_end_date = fresh['source_end_date']
            if target_id is None:
                target = await create_sbp_pending_subscription(
                    db,
                    user.id,
                    await db.get(Tariff, fresh['tariff_id']),
                    device_limit=fresh['device_limit'],
                    commit=False,
                )
                target.daily_devices_billing_version = fresh['daily_devices_billing_version']
                target_id = target.id
                allocated_end_date = target.end_date.isoformat()
            order_id = str(uuid4())
            request = {
                'merchant_order_reference': order_id,
                'amount_minor': str(fresh['amount_kopeks']),
                'currency': 'RUB',
                'payment_method': fresh['payment_method'],
                'customer': {'id': f'user-{user.id}'},
                'recurring_enrollment': {},
            }
            if user.email:
                request['customer']['email'] = user.email
            payment = PayHotPayment(
                user_id=user.id,
                order_id=order_id,
                amount_kopeks=fresh['amount_kopeks'],
                currency='RUB',
                payment_method=fresh['payment_method'],
                request_payload=request,
                status='creating',
                fulfillment_state={'state': 'pending', 'delivery_progress': {}},
            )
            db.add(payment)
            await db.flush()
            agreement = PayHotAgreement(
                user_id=user.id,
                owner_id=user.id,
                subscription_id=target_id,
                checkout_id=checkout_id,
                first_payment_id=payment.id,
                consent_snapshot={**fresh, 'quote_id': quote_id},
                status='CREATING',
            )
            db.add(agreement)
            await db.flush()
            agreement_id = agreement.id
            payment.metadata_json = {
                'purpose': 'payhot_recurring',
                'agreement_id': agreement_id,
                'target_end_date': allocated_end_date,
            }
        await db.commit()
        from app.services.payhot_recurrent_service import create_payhot_agreement

        return await create_payhot_agreement(db, agreement_id)
    except Exception:
        await db.rollback()
        raise


async def get_payhot_agreement_state(db, *, user_id, subscription_id):
    if (
        await db.scalar(
            select(Subscription.id).where(Subscription.id == subscription_id, Subscription.user_id == user_id)
        )
        is None
    ):
        raise ValueError('subscription_not_found')
    agreement = await db.scalar(
        select(PayHotAgreement)
        .where(PayHotAgreement.owner_id == user_id, PayHotAgreement.subscription_id == subscription_id)
        .order_by(PayHotAgreement.id.desc())
        .limit(1)
    )
    if agreement is None:
        return {'status': 'none'}
    payment = await db.get(PayHotPayment, agreement.first_payment_id) if agreement.first_payment_id else None
    return {
        'id': agreement.id,
        'agreement_id': agreement.id,
        'status': agreement.status,
        'reason': agreement.reason,
        'amount_kopeks': agreement.consent_snapshot['amount_kopeks'],
        'period_days': agreement.consent_snapshot['period_days'],
        'payment_method': agreement.consent_snapshot['payment_method'],
        'next_charge_at': agreement.next_charge_at.isoformat() if agreement.next_charge_at else None,
        'redirect_url': payment.payment_url
        if payment and agreement.status in ('CREATING', 'CREATION_UNKNOWN', 'PENDING')
        else None,
    }
