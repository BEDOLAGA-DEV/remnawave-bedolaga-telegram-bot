"""Правило «старой подписки» — одно на бота и кабинет.

Старая подписка — платная, без тарифа, а оператор уже на тарифах: куплена в
классике, потом включили тарифы. Продлить её нельзя (цены берутся из тарифа),
автоплатёж для неё не работает. Единственный путь — выбрать тариф: он надевается
на эту же подписку (см. ``extend_subscription``), аккаунт панели и ссылка у
человека остаются прежними. Все поверхности (меню бота, список тарифов, ответы
кабинета) сверяются с этим правилом, а не с ``tariff_id`` напрямую.
"""

from __future__ import annotations

from typing import Any

from app.config import settings


def is_legacy_subscription(subscription: Any) -> bool:
    """Платная подписка без тарифа при включённом режиме тарифов."""
    if subscription is None or not settings.is_tariffs_mode():
        return False
    if getattr(subscription, 'is_trial', False):
        return False
    return getattr(subscription, 'tariff_id', None) is None
