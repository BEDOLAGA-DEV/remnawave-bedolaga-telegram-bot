"""One consent/price implementation for cabinet and Telegram CisPay checkout."""

from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database.models import (
    CasheraSubscription,
    CisPaySubscription,
    LavaSubscription,
    PlategaSubscription,
    Subscription,
    Tariff,
    User,
)
from app.services.cispay_recurrent import cispay_tariff_terms, get_cispay_recurrent_policy, resolve_cispay_period


async def _other_recurring(db, subscription_id):
    from app.database.crud.cispay_subscriptions import LIVE_STATUSES

    for model in (CisPaySubscription, PlategaSubscription, LavaSubscription, CasheraSubscription):
        statuses = LIVE_STATUSES if model is CisPaySubscription else ('PENDING', 'ACTIVE', 'PAST_DUE')
        if await db.scalar(
            select(model.id).where(model.subscription_id == subscription_id, model.status.in_(statuses)).limit(1)
        ):
            return True
    return False


async def _terms(db, *, user, tariff_id, period_days, device_limit, subscription_id, channel):
    from app.services.pricing_engine import pricing_engine
    from app.services.tariff_purchase_devices import resolve_purchase_device_selection

    policy = await get_cispay_recurrent_policy(db, user=user, channel=channel)
    if not policy['enabled'] or getattr(user, 'restriction_subscription', False):
        raise ValueError(policy['reason'] or 'user_restricted')
    if not settings.is_tariffs_mode():
        raise ValueError('tariffs_required')
    tariff = await db.get(Tariff, tariff_id, populate_existing=True)
    if tariff is None or not tariff.is_active:
        raise ValueError('tariff_unavailable')
    promo = user.get_primary_promo_group()
    if not tariff.is_available_for_promo_group(promo.id if promo else None):
        raise ValueError('tariff_unavailable')
    days = resolve_cispay_period(period_days, tariff.is_daily)
    if not tariff.has_configured_price_for_period(days):
        raise ValueError('period_unavailable')
    from app.database.crud.cispay_subscriptions import LIVE_STATUSES

    pending = select(CisPaySubscription.id).where(
        CisPaySubscription.owner_id == user.id, CisPaySubscription.status.in_(LIVE_STATUSES)
    )
    if settings.is_multi_tariff_enabled():
        pending = pending.join(Subscription, Subscription.id == CisPaySubscription.subscription_id).where(
            Subscription.tariff_id == tariff.id
        )
    if await db.scalar(pending.limit(1)) is not None:
        raise ValueError('recurrent_already_exists')
    target = None
    if subscription_id is not None:
        target = await db.scalar(
            select(Subscription)
            .where(Subscription.id == subscription_id, Subscription.user_id == user.id)
            .execution_options(populate_existing=True)
        )
        if target is None:
            raise ValueError('subscription_not_found')
        if target.status == 'disabled':
            raise ValueError('subscription_disabled')
        if not target.is_trial and target.tariff_id != tariff.id:
            raise ValueError('tariff_switch_requires_balance')
        if await _other_recurring(db, target.id):
            raise ValueError('recurrent_already_exists')
        if target.autopay_enabled:
            raise ValueError('disable_balance_autopay_first')
    candidates = list(
        (
            await db.scalars(
                select(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.status.in_(('active', 'trial', 'limited', 'pending')),
                )
            )
        ).all()
    )
    if any(
        (target is None or row.id != target.id)
        and (not settings.is_multi_tariff_enabled() or row.tariff_id == tariff.id)
        for row in candidates
    ):
        raise ValueError('select_existing_subscription')
    # Existing paid subscriptions retain their devices; purchase selection is for new/trial.
    if target is not None and not target.is_trial:
        effective = target.device_limit
        if device_limit is not None and device_limit != effective:
            raise ValueError('device_selection_not_available')
    else:
        default = resolve_purchase_device_selection(tariff, target, None)
        selected = None if device_limit in (None, default.effective_limit) else device_limit
        effective = resolve_purchase_device_selection(tariff, target, selected).effective_limit
    pricing = await pricing_engine.calculate_tariff_purchase_price(tariff, days, device_limit=effective)
    amount = int(pricing.final_total)
    limits = policy['checkout_policy']
    if not max(1, limits['min_amount_kopeks']) <= amount <= min(2_147_483_647, limits['max_amount_kopeks']):
        raise ValueError('amount_out_of_range')
    return dict(
        tariff_id=tariff.id,
        tariff_terms=cispay_tariff_terms(tariff),
        period_days=days,
        device_limit=effective,
        amount_kopeks=amount,
        shop_id=settings.CISPAY_SHOP_ID,
        source_subscription_id=subscription_id,
        source_end_date=target.end_date.isoformat() if target else None,
        trial_source={'tariff_id': target.tariff_id, 'device_limit': target.device_limit}
        if target and target.is_trial
        else None,
        channel=channel,
    )


async def quote_cispay_purchase(
    db,
    *,
    user,
    tariff_id: int,
    period_days: int,
    device_limit: int | None = None,
    subscription_id: int | None = None,
    channel: str,
) -> dict:
    from app.services.tariff_purchase_quote import save_purchase_quote

    terms = await _terms(
        db,
        user=user,
        tariff_id=tariff_id,
        period_days=period_days,
        device_limit=device_limit,
        subscription_id=subscription_id,
        channel=channel,
    )
    quote_id, expires = await save_purchase_quote(user.id, f'cispay:{channel}', terms)
    return {
        **terms,
        'subscription_id': subscription_id,
        'quote_id': str(quote_id),
        'expires_at': expires.isoformat(),
        'first_payment_required': True,
    }


async def purchase_cispay_quote(db, *, user, quote_id: str, checkout_id: str, channel: str) -> dict:
    from app.database.crud.cispay_subscriptions import reserve_agreement
    from app.database.crud.subscription import create_sbp_pending_subscription
    from app.services.cispay_recurrent_service import create_cispay_agreement
    from app.services.tariff_purchase_quote import load_purchase_quote

    checkout_id, quote_id = str(UUID(str(checkout_id))), str(UUID(str(quote_id)))
    try:
        user = await db.scalar(
            select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
        )
        if user is None or user.status != 'active' or user.restriction_subscription or user.restriction_topup:
            raise ValueError('user_restricted')
        previous = await db.scalar(
            select(CisPaySubscription).where(
                CisPaySubscription.owner_id == user.id, CisPaySubscription.checkout_id == checkout_id
            )
        )
        if previous is not None:
            if previous.snapshot.get('quote_id') != quote_id or previous.snapshot.get('channel') != channel:
                raise ValueError('checkout_conflict')
            previous_id = previous.id
            await db.commit()
            return await create_cispay_agreement(db, attempt_id=previous_id)
        agreed = await load_purchase_quote(user.id, f'cispay:{channel}', UUID(quote_id))
        fresh = await _terms(
            db,
            user=user,
            tariff_id=agreed['tariff_id'],
            period_days=agreed['period_days'],
            device_limit=agreed['device_limit'],
            subscription_id=agreed['source_subscription_id'],
            channel=channel,
        )
        if fresh != agreed:
            raise ValueError('quote_changed')
        target_id = fresh['source_subscription_id']
        if target_id is None:
            tariff = await db.get(Tariff, fresh['tariff_id'])
            target = await create_sbp_pending_subscription(
                db, user.id, tariff, device_limit=fresh['device_limit'], commit=False
            )
            target_id = target.id
        agreement = await reserve_agreement(
            db,
            user_id=user.id,
            subscription_id=target_id,
            shop_id=fresh['shop_id'],
            checkout_id=checkout_id,
            snapshot={**fresh, 'quote_id': quote_id},
        )
        return await create_cispay_agreement(db, attempt_id=agreement.id)
    except Exception:
        await db.rollback()
        raise


async def get_cispay_agreement_state(db, *, user_id: int, subscription_id: int) -> dict:
    from app.services.cispay_recurrent_service import agreement_state

    target = await db.scalar(
        select(Subscription.id).where(Subscription.id == subscription_id, Subscription.user_id == user_id)
    )
    if target is None:
        raise ValueError('subscription_not_found')
    agreement = await db.scalar(
        select(CisPaySubscription)
        .where(CisPaySubscription.owner_id == user_id, CisPaySubscription.subscription_id == subscription_id)
        .order_by(CisPaySubscription.id.desc())
        .limit(1)
    )
    if agreement is None:
        return {'status': 'none'}
    return {
        **agreement_state(agreement),
        'reason': agreement.reason,
        'amount_kopeks': agreement.snapshot['amount_kopeks'],
        'period_days': agreement.snapshot['period_days'],
        'next_charge_at': agreement.next_charge_at.isoformat() if agreement.next_charge_at else None,
    }
