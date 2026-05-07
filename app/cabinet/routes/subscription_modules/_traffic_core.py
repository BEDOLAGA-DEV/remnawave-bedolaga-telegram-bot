"""Shared kind-parameterised helpers for cabinet traffic endpoints."""

from __future__ import annotations

from typing import Any, Literal

import structlog
from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    add_subscription_traffic,
    add_subscription_wl_traffic,
)
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import Subscription, TrafficPurchase, WlTrafficPurchase
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

TrafficKind = Literal['regular', 'wl']


def get_limit_gb(subscription: Subscription, kind: TrafficKind) -> int:
    return getattr(subscription, f'{"wl_" if kind == "wl" else ""}traffic_limit_gb', 0) or 0


def get_used_gb(subscription: Subscription, kind: TrafficKind) -> float:
    return getattr(subscription, f'{"wl_" if kind == "wl" else ""}traffic_used_gb', 0.0) or 0.0


def get_purchased_gb(subscription: Subscription, kind: TrafficKind) -> int:
    field = 'wl_purchased_traffic_gb' if kind == 'wl' else 'purchased_traffic_gb'
    return getattr(subscription, field, 0) or 0


async def resolve_traffic_packages(
    db: AsyncSession,
    subscription: Subscription,
    *,
    kind: TrafficKind,
) -> list[dict[str, Any]]:
    """Return the list of available top-up packages for the given kind."""
    if subscription.is_trial:
        return []

    if kind == 'wl' and not getattr(settings, 'WL_TRAFFIC_TOPUP_ENABLED', True):
        return []

    if get_limit_gb(subscription, kind) == 0:
        return []

    if settings.is_tariffs_mode() and subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff is not None:
            if kind == 'wl':
                if getattr(tariff, 'wl_traffic_topup_packages', None):
                    raw = tariff.wl_traffic_topup_packages or {}
                    return [
                        {'gb': int(gb), 'price': int(price), 'is_unlimited': int(gb) == 0}
                        for gb, price in raw.items()
                        if price and int(price) > 0
                    ]
            else:
                if getattr(tariff, 'traffic_topup_enabled', False):
                    raw = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
                    return [
                        {'gb': int(gb), 'price': int(price), 'is_unlimited': int(gb) == 0}
                        for gb, price in raw.items()
                        if price and int(price) > 0
                    ]

    if kind == 'wl':
        raw_packages = settings.get_wl_traffic_packages()
    else:
        if not settings.is_traffic_topup_enabled():
            return []
        raw_packages = settings.get_traffic_topup_packages()

    result: list[dict[str, Any]] = []
    for pkg in raw_packages:
        if not pkg.get('enabled', True):
            continue
        if pkg.get('price', 0) <= 0:
            continue
        result.append({
            'gb': int(pkg['gb']),
            'price': int(pkg['price']),
            'is_unlimited': int(pkg['gb']) == 0,
        })

    return result


async def resolve_package_price(
    db: AsyncSession,
    subscription: Subscription,
    *,
    gb: int,
    kind: TrafficKind,
) -> int:
    """Return the per-month base price for one top-up package.

    Returns 0 when the package is unknown — caller is expected to reject.
    """
    if settings.is_tariffs_mode() and subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff is not None:
            if kind == 'wl':
                pkgs = tariff.wl_traffic_topup_packages or {}
                if gb in pkgs:
                    return int(pkgs[gb])
            else:
                if hasattr(tariff, 'get_traffic_topup_packages'):
                    pkgs = tariff.get_traffic_topup_packages() or {}
                    if gb in pkgs:
                        return int(pkgs[gb])

    if kind == 'wl':
        if not settings.WL_TRAFFIC_TOPUP_ENABLED:
            return 0
        return int(settings.get_wl_traffic_topup_price(gb))

    if not settings.is_traffic_topup_enabled():
        return 0
    pkgs = settings.get_traffic_topup_packages()
    match = next((p for p in pkgs if p['gb'] == gb and p.get('enabled', True)), None)
    return int(match['price']) if match else 0


async def apply_purchase_db(
    db: AsyncSession,
    subscription: Subscription,
    *,
    gb: int,
    kind: TrafficKind,
) -> None:
    """Persist a successful top-up: increments limit + creates *TrafficPurchase row."""
    if kind == 'wl':
        await add_subscription_wl_traffic(db, subscription, gb)
    else:
        await add_subscription_traffic(db, subscription, gb)


async def delete_purchases_for_switch(
    db: AsyncSession,
    subscription: Subscription,
    *,
    kind: TrafficKind,
) -> None:
    """Wipe accumulated *TrafficPurchase rows before switching the package."""
    table = WlTrafficPurchase if kind == 'wl' else TrafficPurchase
    await db.execute(sql_delete(table).where(table.subscription_id == subscription.id))


async def sync_remnawave_after_purchase(
    db: AsyncSession,
    subscription: Subscription,
    user,
) -> None:
    """Best-effort RemnaWave sync after any traffic purchase.

    On hard failure the subscription is enqueued for retry.
    """
    should_create = False
    try:
        service = SubscriptionService()
        if settings.is_multi_tariff_enabled():
            should_create = not subscription.remnawave_uuid
        else:
            should_create = not getattr(user, 'remnawave_uuid', None)
        if should_create:
            await service.create_remnawave_user(db, subscription)
        else:
            await service.update_remnawave_user(db, subscription)
    except Exception as e:
        logger.error('Failed to sync traffic with RemnaWave', error=str(e))
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=user.id,
            action='create' if should_create else 'update',
        )
