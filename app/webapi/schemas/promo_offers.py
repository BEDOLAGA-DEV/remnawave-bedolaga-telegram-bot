from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, Field, validator

from app.localization.texts import get_texts


class PromoOfferUserInfo(BaseModel):
    id: int
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None


class PromoOfferSubscriptionInfo(BaseModel):
    id: int
    status: str
    is_trial: bool
    start_date: datetime
    end_date: datetime
    autopay_enabled: bool


class PromoOfferResponse(BaseModel):
    id: int
    user_id: int
    subscription_id: int | None = None
    notification_type: str
    discount_percent: int
    bonus_amount_kopeks: int
    expires_at: datetime
    claimed_at: datetime | None = None
    is_active: bool
    effect_type: str
    extra_data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    user: PromoOfferUserInfo | None = None
    subscription: PromoOfferSubscriptionInfo | None = None


class PromoOfferListResponse(BaseModel):
    items: list[PromoOfferResponse]
    total: int
    limit: int
    offset: int


class PromoOfferCreateRequest(BaseModel):
    user_id: int | None = Field(None, ge=1)
    telegram_id: int | None = Field(None, ge=1)
    notification_type: str = Field(..., min_length=1)
    valid_hours: int = Field(
        ..., ge=1, description=get_texts().t('PROMO_OFFER_VALID_HOURS_DESCRIPTION', 'Срок действия предложения в часах')
    )
    discount_percent: int = Field(0, ge=0)
    bonus_amount_kopeks: int = Field(0, ge=0)
    subscription_id: int | None = None
    effect_type: str = Field('percent_discount', min_length=1)
    extra_data: dict[str, Any] = Field(default_factory=dict)


class PromoOfferBroadcastRequest(PromoOfferCreateRequest):
    target: str | None = Field(
        None,
        description=get_texts().t(
            'PROMO_OFFER_BROADCAST_TARGET_DESCRIPTION',
            'Категория пользователей для рассылки. Поддерживает те же сегменты, что '
            'и API рассылок (all, active, trial, custom_today и т.д.).',
        ),
    )

    _ALLOWED_TARGETS: ClassVar[set[str]] = {
        'all',
        'active',
        'trial',
        'trial_ending',
        'trial_expired',
        'no',
        'expiring',
        'expiring_subscribers',
        'expired',
        'expired_subscribers',
        'canceled_subscribers',
        'active_zero',
        'trial_zero',
        'zero',
        'autopay_failed',
        'low_balance',
        'inactive_30d',
        'inactive_60d',
        'inactive_90d',
    }
    _CUSTOM_TARGETS: ClassVar[set[str]] = {
        'today',
        'week',
        'month',
        'active_today',
        'inactive_week',
        'inactive_month',
        'referrals',
        'direct',
    }
    _TARGET_ALIASES: ClassVar[dict[str, str]] = {
        'no_sub': 'no',
        'all_users': 'all',
        'active_subscribers': 'active',
        'trial_users': 'trial',
    }

    @validator('target')
    def validate_target(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip().lower()
        normalized = cls._TARGET_ALIASES.get(normalized, normalized)

        if normalized in cls._ALLOWED_TARGETS:
            return normalized

        if normalized.startswith('custom_'):
            criteria = normalized[len('custom_') :]
            if criteria in cls._CUSTOM_TARGETS:
                return normalized

        raise ValueError(get_texts().t('BROADCAST_UNSUPPORTED_TARGET', 'Unsupported target value'))


class PromoOfferBroadcastResponse(BaseModel):
    created_offers: int
    user_ids: list[int]
    target: str | None = None


class PromoOfferTemplateResponse(BaseModel):
    id: int
    name: str
    offer_type: str
    message_text: str
    button_text: str
    valid_hours: int
    discount_percent: int
    bonus_amount_kopeks: int
    active_discount_hours: int | None = None
    test_duration_hours: int | None = None
    test_squad_uuids: list[str]
    is_active: bool
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime


class PromoOfferTemplateListResponse(BaseModel):
    items: list[PromoOfferTemplateResponse]


class PromoOfferTemplateUpdateRequest(BaseModel):
    name: str | None = None
    message_text: str | None = None
    button_text: str | None = None
    valid_hours: int | None = Field(None, ge=1)
    discount_percent: int | None = Field(None, ge=0)
    bonus_amount_kopeks: int | None = Field(None, ge=0)
    active_discount_hours: int | None = Field(None, ge=1)
    test_duration_hours: int | None = Field(None, ge=1)
    test_squad_uuids: list[str] | None = None
    is_active: bool | None = None


class PromoOfferLogOfferInfo(BaseModel):
    id: int
    notification_type: str | None = None
    discount_percent: int | None = None
    bonus_amount_kopeks: int | None = None
    effect_type: str | None = None
    expires_at: datetime | None = None
    claimed_at: datetime | None = None
    is_active: bool | None = None


class PromoOfferLogResponse(BaseModel):
    id: int
    user_id: int | None = None
    offer_id: int | None = None
    action: str
    source: str | None = None
    percent: int | None = None
    effect_type: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    user: PromoOfferUserInfo | None = None
    offer: PromoOfferLogOfferInfo | None = None


class PromoOfferLogListResponse(BaseModel):
    items: list[PromoOfferLogResponse]
    total: int
    limit: int
    offset: int
