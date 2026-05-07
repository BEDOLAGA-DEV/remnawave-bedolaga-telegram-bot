"""Shared kind-parameterised helpers for cabinet traffic endpoints."""

from __future__ import annotations

from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import Subscription


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
