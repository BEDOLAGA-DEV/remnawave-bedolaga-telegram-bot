"""Контекст меню для построения кнопок."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiogram.types import InlineKeyboardButton


@dataclass
class MenuContext:
    """Контекст пользователя для построения меню."""

    language: str = 'ru'
    is_admin: bool = False
    is_moderator: bool = False
    has_active_subscription: bool = False
    subscription_is_active: bool = False
    has_had_paid_subscription: bool = False
    balance_kopeks: int = 0
    subscription: Any | None = None
    subscriptions: list[Any] = field(default_factory=list)
    show_resume_checkout: bool = False
    has_saved_cart: bool = False
    custom_buttons: list[InlineKeyboardButton] = field(default_factory=list)
    # Расширенные поля для плейсхолдеров и условий
    username: str = ''
    subscription_days: int = 0
    traffic_used_gb: float = 0.0
    traffic_left_gb: float = 0.0
    referral_count: int = 0
    referral_earnings_kopeks: int = 0
    registration_days: int = 0
    promo_group_id: str | None = None
    has_autopay: bool = False
