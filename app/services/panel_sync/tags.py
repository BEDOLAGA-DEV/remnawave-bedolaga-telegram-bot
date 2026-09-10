"""Тег панельного пользователя: у тарифа свой, иначе общие теги из настроек.

Панель показывает тег в списке пользователей — по нему администратор видит, на каком
тарифе человек. Правила формата те же, что у панели и у глобальных тегов
``TRIAL_USER_TAG``/``PAID_SUBSCRIPTION_USER_TAG``: до 16 символов, A–Z, 0–9, ``_``.
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.utils.panel_tag import PANEL_TAG_MAX_LENGTH, PANEL_TAG_RULES, normalize_panel_tag


__all__ = ['PANEL_TAG_MAX_LENGTH', 'PANEL_TAG_RULES', 'normalize_panel_tag', 'resolve_panel_user_tag']


def resolve_panel_user_tag(subscription: Any) -> str | None:
    """Тег для панельного аккаунта подписки.

    Тег тарифа побеждает всегда, в том числе у триала: он описывает тариф. Без него —
    прежнее правило: триалу общий триальный тег, остальным общий платный.
    """
    tariff = getattr(subscription, 'tariff', None)
    tariff_tag = normalize_panel_tag(getattr(tariff, 'panel_tag', None)) if tariff is not None else None
    if tariff_tag:
        return tariff_tag
    if getattr(subscription, 'is_trial', False):
        return settings.get_trial_user_tag()
    return settings.get_paid_subscription_user_tag()
