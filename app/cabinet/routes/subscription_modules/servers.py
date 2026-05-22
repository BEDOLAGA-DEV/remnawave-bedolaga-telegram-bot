"""Server/country management endpoints.

GET /subscription/countries
POST /subscription/countries
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.subscription_service import SubscriptionService

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get('/countries')
async def get_available_countries(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Get available countries/servers for the user."""
    from app.database.crud.server_squad import get_available_server_squads
    from app.utils.pricing_utils import apply_percentage_discount, calculate_prorated_price

    subscription = await resolve_subscription(db, user, subscription_id)

    promo_group_id = user.promo_group_id
    available_servers = await get_available_server_squads(db, promo_group_id=promo_group_id)

    connected_squads = []
    days_left = 0
    if subscription:
        connected_squads = subscription.connected_squads or []
        if subscription.end_date:
            delta = subscription.end_date - datetime.now(UTC)
            days_left = max(0, delta.days)

    # Get discount from promo group via PricingEngine (respects apply_discounts_to_addons flag)
    from app.services.pricing_engine import PricingEngine

    servers_discount_percent = PricingEngine.get_addon_discount_percent(user, 'servers', None)

    countries = []
    for server in available_servers:
        base_price = server.price_kopeks

        # Apply discount
        if servers_discount_percent > 0:
            discounted_price, _ = apply_percentage_discount(base_price, servers_discount_percent)
        else:
            discounted_price = base_price

        # Calculate prorated price if subscription exists
        prorated_price = discounted_price
        if subscription and subscription.end_date:
            prorated_price, _ = calculate_prorated_price(
                discounted_price,
                subscription.end_date,
            )

        countries.append(
            {
                'uuid': server.squad_uuid,
                'name': server.display_name,
                'country_code': server.country_code,
                'base_price_kopeks': base_price,
                'price_kopeks': prorated_price,  # Prorated price with discount
                'price_per_month_kopeks': discounted_price,  # Monthly price with discount
                'price_rubles': prorated_price / 100,
                'is_available': server.is_available and not server.is_full,
                'is_connected': server.squad_uuid in connected_squads,
                'has_discount': servers_discount_percent > 0,
                'discount_percent': servers_discount_percent,
            }
        )

    return {
        'countries': countries,
        'connected_count': len(connected_squads),
        'has_subscription': subscription is not None,
        'days_left': days_left,
        'discount_percent': servers_discount_percent,
    }


@router.post('/countries')
async def update_countries(
    request: dict[str, Any],
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Update subscription countries/servers."""
    from app.database.crud.server_squad import add_user_to_servers, get_available_server_squads, get_server_ids_by_uuids
    from app.database.crud.subscription import add_subscription_servers
    from app.database.crud.transaction import create_transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import TransactionType
    from app.utils.pricing_utils import apply_percentage_discount, calculate_prorated_price

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if subscription.is_trial:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Country management is not available for trial subscriptions',
        )

    selected_countries = request.get('countries', [])
    if not selected_countries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='At least one country must be selected',
        )

    current_countries = subscription.connected_squads or []
    promo_group_id = user.promo_group_id

    available_servers = await get_available_server_squads(db, promo_group_id=promo_group_id)
    allowed_country_ids = {server.squad_uuid for server in available_servers}

    # Validate selected countries
    for country_uuid in selected_countries:
        if country_uuid not in allowed_country_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Country {country_uuid} is not available',
            )

    added = [c for c in selected_countries if c not in current_countries]
    removed = [c for c in current_countries if c not in selected_countries]

    if not added and not removed:
        return {
            'message': 'No changes detected',
            'connected_squads': current_countries,
        }

    # Lock user row to prevent TOCTOU on promo-offer state
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    # Calculate cost for added servers
    total_cost = 0
    added_names = []
    removed_names = []

    from app.services.pricing_engine import PricingEngine

    servers_discount_percent = PricingEngine.get_addon_discount_percent(user, 'servers', None)

    added_server_prices = []

    for server in available_servers:
        if server.squad_uuid in added:
            server_price_per_month = server.price_kopeks
            if servers_discount_percent > 0:
                discounted_per_month, _ = apply_percentage_discount(
                    server_price_per_month,
                    servers_discount_percent,
                )
            else:
                discounted_per_month = server_price_per_month

            charged_price, charged_days = calculate_prorated_price(
                discounted_per_month,
                subscription.end_date,
            )

            total_cost += charged_price
            added_names.append(server.display_name)
            added_server_prices.append(charged_price)

        if server.squad_uuid in removed:
            removed_names.append(server.display_name)

    # Check balance
    if total_cost > 0 and user.balance_kopeks < total_cost:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f'Insufficient balance. Need {total_cost / 100:.2f} RUB, have {user.balance_kopeks / 100:.2f} RUB',
        )

    # Deduct balance and update subscription
    if added and total_cost > 0:
        success = await subtract_user_balance(db, user, total_cost, f'Adding countries: {", ".join(added_names)}')
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to charge balance',
            )

        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=total_cost,
            description=f'Adding countries to subscription: {", ".join(added_names)}',
        )

    # Add servers to subscription
    if added:
        added_server_ids = await get_server_ids_by_uuids(db, added)
        if added_server_ids:
            await add_subscription_servers(db, subscription, added_server_ids, added_server_prices)
            try:
                await add_user_to_servers(db, added_server_ids)
            except Exception as e:
                logger.error('Ошибка обновления счётчика серверов', error=e)

    # Update connected squads
    subscription.connected_squads = selected_countries
    subscription.updated_at = datetime.now(UTC)
    await db.commit()

    # Sync with RemnaWave
    try:
        from app.config import settings

        subscription_service = SubscriptionService()
        _has_panel = (
            getattr(subscription, 'remnawave_uuid', None)
            if settings.is_multi_tariff_enabled()
            else getattr(user, 'remnawave_uuid', None)
        )
        if _has_panel:
            await subscription_service.update_remnawave_user(db, subscription, sync_squads=True)
        else:
            await subscription_service.create_remnawave_user(db, subscription)
    except Exception as e:
        logger.error('Failed to sync countries with RemnaWave', error=e)
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=user.id,
            action='update' if _has_panel else 'create',
        )

    await db.refresh(subscription)

    return {
        'message': 'Countries updated successfully',
        'added': added_names,
        'removed': removed_names,
        'amount_paid_kopeks': total_cost,
        'connected_squads': subscription.connected_squads,
    }
