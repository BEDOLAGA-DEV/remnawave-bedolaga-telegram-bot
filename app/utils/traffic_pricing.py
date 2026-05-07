# app/utils/traffic_pricing.py
"""Shared traffic-pricing helpers used by both bot and cabinet."""

from __future__ import annotations

from typing import Literal

from app.config import PERIOD_PRICES, settings


TrafficKind = Literal['regular', 'wl']


def _get_field(subscription, kind: TrafficKind, field: str) -> int:
    if kind == 'wl':
        return getattr(subscription, f'wl_{field}', 0) or 0
    return getattr(subscription, field, 0) or 0


def _get_unit_price(gb: int, kind: TrafficKind) -> int:
    if kind == 'wl':
        return settings.get_wl_traffic_price(gb)
    if hasattr(settings, 'get_traffic_price'):
        return settings.get_traffic_price(gb)
    return settings.get_wl_traffic_price(gb)


def calculate_traffic_reset_price(subscription, *, kind: TrafficKind) -> int:
    """Return the price (in kopeks) for resetting traffic counter on a subscription.

    Modes (from settings.get_traffic_reset_price_mode):
      - 'period': fixed = settings.get_traffic_reset_base_price() or PERIOD_PRICES[30].
      - 'traffic': max(unit_price(current_limit), base_price).
      - 'traffic_with_purchased': unit_price(base_gb) + unit_price(purchased_gb), floored at base_price.
      - anything else: base_price (fallback).
    """
    mode = settings.get_traffic_reset_price_mode()
    base_price = settings.get_traffic_reset_base_price()
    if base_price == 0:
        base_price = PERIOD_PRICES.get(30, 0)

    current_limit = _get_field(subscription, kind, 'traffic_limit_gb')
    purchased_gb = _get_field(subscription, kind, 'purchased_traffic_gb')

    if mode == 'period':
        return base_price

    if mode == 'traffic':
        traffic_price = _get_unit_price(current_limit, kind)
        return max(traffic_price, base_price)

    if mode == 'traffic_with_purchased':
        base_gb = max(0, current_limit - purchased_gb)
        base_traffic_price = _get_unit_price(base_gb, kind) if base_gb > 0 else 0
        purchased_traffic_price = _get_unit_price(purchased_gb, kind) if purchased_gb > 0 else 0
        total = base_traffic_price + purchased_traffic_price
        return max(total, base_price)

    return base_price
