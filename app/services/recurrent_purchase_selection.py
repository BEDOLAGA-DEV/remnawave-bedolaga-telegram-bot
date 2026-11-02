"""Validate explicit recurring purchase selection before provider side effects."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.subscription import get_subscription_by_id_for_user, get_subscription_by_user_and_tariff
from app.database.models import Subscription


async def resolve_recurrent_purchase_selection(
    db: AsyncSession,
    *,
    user_id: int,
    tariff_id: int,
    subscription_id: int,
) -> Subscription:
    subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    if subscription is None:
        raise ValueError('Подписка не найдена')
    if subscription.tariff_id != tariff_id:
        # Binding/charge callbacks renew the existing row. Mutating its tariff
        # before payment would give unpaid access and corrupt cancellation flow.
        raise ValueError('Для перехода с classic или другого тарифа оплатите с баланса')
    active = await get_subscription_by_user_and_tariff(db, user_id, tariff_id)
    if active is not None and active.id != subscription.id:
        raise ValueError('Тариф уже активен в другой подписке — выберите её для продления')
    return subscription


async def resolve_lava_purchase_product(product_id):
    from app.services.payment.lava import _LavaRecurrentAgent

    return await _LavaRecurrentAgent()._resolve_lava_product(product_id)


async def calculate_recurrent_purchase_terms(tariff, provider, device_limit, subscription=None):
    """The provider's real cadence and recurring price, without temporary discounts."""
    from app.config import settings
    from app.services.autopay_period import resolve_autopay_period_candidate
    from app.services.cashera_recurrent import resolve_cashera_interval, round_up_to_rubles
    from app.services.platega_recurrent import resolve_platega_interval
    from app.services.pricing_engine import pricing_engine

    if provider not in ('platega', 'lava', 'cashera'):
        raise ValueError('invalid_provider')
    if provider == 'lava':
        from app.services.lava_recurrent import resolve_product_charge_days
        from app.services.payment.lava import _lava_amount_to_kopeks

        product_id = (tariff.lava_product_id or '').strip()
        product = await resolve_lava_purchase_product(product_id) if product_id else None
        if not product:
            raise ValueError('Продукт Lava недоступен')
        charge_days = resolve_product_charge_days(product)
        interval = str(product.get('period') or f'{charge_days}_days')
        amount = _lava_amount_to_kopeks(product.get('price'))
    else:
        period = (
            resolve_autopay_period_candidate(getattr(subscription, 'autopay_period_days', None), tariff)
            or resolve_autopay_period_candidate(settings.DEFAULT_AUTOPAY_PERIOD_DAYS, tariff)
            or tariff.get_shortest_period()
            or 30
        )
        resolver = resolve_platega_interval if provider == 'platega' else resolve_cashera_interval
        interval, charge_days = resolver(period, bool(tariff.is_daily))
    pricing = await pricing_engine.calculate_tariff_purchase_price(
        tariff, charge_days, device_limit=device_limit, daily_devices_billing_version=1 if tariff.is_daily else 0
    )
    true_amount = int(pricing.final_total)
    if provider == 'lava':
        if true_amount - amount > 1:
            raise ValueError('Продукт Lava дешевле продления с выбранными устройствами — оплатите с баланса')
    else:
        amount = round_up_to_rubles(true_amount) if provider == 'cashera' else true_amount
    if not 0 < amount <= 2_147_483_647:
        raise ValueError('invalid_recurrent_price')
    return {
        'provider': provider,
        'amount_kopeks': amount,
        'charge_days': charge_days,
        'interval': interval,
        'selected_device_limit': device_limit,
    }


def validate_recurrent_setup_terms(subscription, provider, amount, charge_days, interval):
    """Fail closed if provider setup no longer matches the buyer's consent."""
    expected = getattr(subscription, '_recurrent_purchase_terms', None)
    if expected is not None and any(
        expected[key] != value
        for key, value in (
            ('provider', provider),
            ('amount_kopeks', amount),
            ('charge_days', charge_days),
            ('interval', interval),
        )
    ):
        raise ValueError('quote_changed')


async def _resolve_new_device_target(db, user, tariff, subscription_id, parameters=None):
    from app.config import settings
    from app.database.crud.subscription import get_subscription_by_user_id

    if not tariff.is_active:
        raise ValueError('tariff_unavailable')
    group = user.get_primary_promo_group()
    if not tariff.is_available_for_promo_group(group.id if group else None):
        raise ValueError('tariff_unavailable')
    if subscription_id is not None:
        target = await resolve_recurrent_purchase_selection(
            db, user_id=user.id, tariff_id=tariff.id, subscription_id=subscription_id
        )
    elif settings.is_multi_tariff_enabled():
        target = await get_subscription_by_user_and_tariff(db, user.id, tariff.id, include_inactive=True)
    else:
        target = await get_subscription_by_user_id(db, user.id)
    if target is not None:
        # Only an exact durable checkout replay may reuse its own pending row.
        # An expired paid subscription is never proof of an unpaid checkout.
        if target.is_trial:
            raise ValueError('Для триальной подписки оплатите с баланса')
        if parameters is not None:
            intent = await recoverable_recurrent_checkout(db, user, target)
            if intent is not None and _matches_recurrent_request(intent.parameters['request'], parameters, target.id):
                return intent
        raise ValueError('device_selection_not_available')
    return None


def _recurrent_parameters(tariff_id, provider, selected_device_limit, subscription_id, channel):
    return dict(
        tariff_id=tariff_id,
        provider=provider,
        selected_device_limit=selected_device_limit,
        subscription_id=subscription_id,
        channel=channel,
        kind='recurrent_devices',
    )


async def preview_recurrent_purchase(
    db, *, user, tariff, provider, selected_device_limit, subscription_id=None, channel='cabinet'
):
    from app.services.tariff_purchase_devices import resolve_purchase_device_selection
    from app.services.tariff_purchase_quote import save_purchase_quote

    parameters = _recurrent_parameters(tariff.id, provider, selected_device_limit, subscription_id, channel)
    recovery = await _resolve_new_device_target(db, user, tariff, subscription_id, parameters)
    if recovery is not None:
        parameters = recovery.parameters['request']
    selection = resolve_purchase_device_selection(tariff, None, selected_device_limit)
    binding = await _checkout_binding(db, recovery.subscription_id, provider) if recovery else None
    if binding is not None:
        if binding.status not in ('PENDING', 'ACTIVE', 'PAST_DUE'):
            raise ValueError('checkout_closed')
        terms = recovery.parameters['terms']
    else:
        terms = await calculate_recurrent_purchase_terms(tariff, provider, selection.effective_limit)
    quote_id, expires_at = await save_purchase_quote(
        user.id, f'recurrent:{channel}', {'request': parameters, 'terms': terms}
    )
    return {
        **terms,
        'quote_id': str(quote_id),
        'expires_at': expires_at.isoformat(),
        **({'checkout_id': recovery.checkout_id} if recovery else {}),
    }


async def prepare_recurrent_device_purchase(
    db, *, user, tariff, provider, selected_device_limit, quote_id, checkout_id, subscription_id=None, channel='cabinet'
):
    """Atomically bind one checkout to its new unpaid row, never rewrite paid limits."""
    from uuid import UUID

    from sqlalchemy import select

    from app.database.crud.subscription import create_sbp_pending_subscription
    from app.database.crud.user import lock_user_for_update
    from app.database.models import TariffPurchaseIntent
    from app.services.tariff_purchase_devices import resolve_purchase_device_selection
    from app.services.tariff_purchase_quote import load_purchase_quote, purchase_fingerprint

    if selected_device_limit is None or quote_id is None or checkout_id is None:
        raise ValueError('device_consent_required')
    checkout_id, quote_id = str(UUID(str(checkout_id))), UUID(str(quote_id))
    parameters = _recurrent_parameters(tariff.id, provider, selected_device_limit, subscription_id, channel)
    fingerprint = purchase_fingerprint(parameters)
    user = await lock_user_for_update(db, user)
    intent = await db.scalar(
        select(TariffPurchaseIntent).where(
            TariffPurchaseIntent.user_id == user.id, TariffPurchaseIntent.checkout_id == checkout_id
        )
    )
    if intent is not None:
        if _matches_recurrent_request(intent.parameters.get('request', {}), parameters, intent.subscription_id):
            parameters = intent.parameters['request']
            fingerprint = purchase_fingerprint(parameters)
        if intent.fingerprint != fingerprint:
            raise ValueError('checkout_conflict')
        subscription = await get_subscription_by_id_for_user(db, intent.subscription_id, user.id)
        if (
            subscription is None
            or subscription.tariff_id != tariff.id
            or subscription.device_limit != selected_device_limit
        ):
            raise ValueError('checkout_conflict')
        if subscription.is_trial or subscription.status in ('disabled', 'pending'):
            raise ValueError('checkout_conflict')
        # The original terms remain immutable even after the short quote expires.
        terms = intent.parameters['terms']
        binding = await _checkout_binding(db, subscription.id, provider)
        if binding is not None and binding.status not in ('PENDING', 'ACTIVE', 'PAST_DUE'):
            raise ValueError('checkout_closed')
        if binding is None:
            # No remote binding was recorded: recovery may present fresh terms.
            # Paid/previously charged rows cannot reopen an old checkout.
            if await recoverable_recurrent_checkout(db, user, subscription) is None:
                raise ValueError('checkout_closed')
            try:
                agreed = await load_purchase_quote(user.id, f'recurrent:{channel}', quote_id)
            except ValueError:
                agreed = None  # An unchanged exact replay still survives quote expiry.
            if agreed is not None:
                fresh_terms = await calculate_recurrent_purchase_terms(tariff, provider, selected_device_limit)
                if agreed != {'request': parameters, 'terms': fresh_terms}:
                    raise ValueError('quote_changed')
                terms = fresh_terms
                intent.parameters = {'request': parameters, 'terms': terms}
    else:
        await _resolve_new_device_target(db, user, tariff, subscription_id)
        selection = resolve_purchase_device_selection(tariff, None, selected_device_limit)
        terms = await calculate_recurrent_purchase_terms(tariff, provider, selection.effective_limit)
        agreed = await load_purchase_quote(user.id, f'recurrent:{channel}', quote_id)
        if agreed != {'request': parameters, 'terms': terms}:
            raise ValueError('quote_changed')
        subscription = await create_sbp_pending_subscription(
            db, user.id, tariff, device_limit=selection.effective_limit, commit=False
        )
        if tariff.is_daily:
            subscription.daily_devices_billing_version = 1
        db.add(
            TariffPurchaseIntent(
                user_id=user.id,
                checkout_id=checkout_id,
                fingerprint=fingerprint,
                parameters={'request': parameters, 'terms': terms},
                subscription_id=subscription.id,
                charged_kopeks=0,
            )
        )
    await db.commit()
    subscription._recurrent_purchase_terms = terms
    return subscription


async def _checkout_binding(db, subscription_id, provider):
    from sqlalchemy import select

    from app.database.models import CasheraSubscription, LavaSubscription, PlategaSubscription

    model = {'platega': PlategaSubscription, 'lava': LavaSubscription, 'cashera': CasheraSubscription}[provider]
    return await db.scalar(
        select(model).where(model.subscription_id == subscription_id).order_by(model.id.desc()).limit(1)
    )


async def recoverable_recurrent_checkout(db, user, target):
    """Return durable proof of an untouched owned pending checkout, never a paid row."""
    from sqlalchemy import select

    from app.database.models import CasheraSubscription, LavaSubscription, PlategaSubscription, TariffPurchaseIntent

    if (
        target is None
        or target.user_id != user.id
        or target.is_trial
        or target.status != 'expired'
        or target.start_date is None
        or target.start_date != target.end_date
    ):
        return None
    intents = (
        await db.scalars(
            select(TariffPurchaseIntent)
            .where(
                TariffPurchaseIntent.user_id == user.id,
                TariffPurchaseIntent.subscription_id == target.id,
                TariffPurchaseIntent.transaction_id.is_(None),
                TariffPurchaseIntent.charged_kopeks == 0,
            )
            .order_by(TariffPurchaseIntent.id)
        )
    ).all()
    for intent in intents:
        request = intent.parameters.get('request', {})
        if (
            request.get('kind') != 'recurrent_devices'
            or request.get('tariff_id') != target.tariff_id
            or request.get('selected_device_limit') != target.device_limit
        ):
            continue
        for model in (CasheraSubscription, LavaSubscription, PlategaSubscription):
            charged = await db.scalar(
                select(model.id).where(model.subscription_id == target.id, model.charges_success > 0).limit(1)
            )
            if charged is not None:
                return None
        binding = await _checkout_binding(db, target.id, request.get('provider'))
        if binding is not None and binding.status not in ('PENDING', 'ACTIVE', 'PAST_DUE'):
            return None
        return intent
    return None


async def find_recurrent_pending_intent(db, subscription):
    from types import SimpleNamespace

    if subscription is None:
        return None
    return await recoverable_recurrent_checkout(db, SimpleNamespace(id=subscription.user_id), subscription)


def _matches_recurrent_request(original, requested, subscription_id):
    # A reload may enter from the pending row's detail page instead of catalog.
    # Only alias the owned row already bound to this checkout, not arbitrary IDs.
    normalized = dict(requested)
    if original.get('subscription_id') is None and requested.get('subscription_id') == subscription_id:
        normalized['subscription_id'] = None
    return original == normalized
