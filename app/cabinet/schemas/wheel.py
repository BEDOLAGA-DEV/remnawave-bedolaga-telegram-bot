"""Схемы для колеса удачи (Fortune Wheel)."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# ==================== ENUMS ====================


class WheelPaymentType(str, Enum):
    """Способы оплаты спина."""

    TELEGRAM_STARS = 'telegram_stars'
    SUBSCRIPTION_DAYS = 'subscription_days'


class WheelPrizeType(str, Enum):
    """Типы призов."""

    SUBSCRIPTION_DAYS = 'subscription_days'
    BALANCE_BONUS = 'balance_bonus'
    TRAFFIC_GB = 'traffic_gb'
    PROMOCODE = 'promocode'
    NOTHING = 'nothing'


# ==================== USER SCHEMAS ====================


class WheelPrizeDisplay(BaseModel):
    """Отображение приза для пользователя."""

    id: int
    display_name: str
    emoji: str
    color: str
    prize_type: str

    class Config:
        from_attributes = True


class WheelConfigResponse(BaseModel):
    """Конфигурация колеса для пользователя."""

    is_enabled: bool
    name: str
    spin_cost_stars: int | None = None
    spin_cost_days: int | None = None
    spin_cost_stars_enabled: bool
    spin_cost_days_enabled: bool
    prizes: list[WheelPrizeDisplay]
    daily_limit: int
    user_spins_today: int
    can_spin: bool
    can_spin_reason: str | None = None
    can_pay_stars: bool = False
    can_pay_days: bool = False
    user_balance_kopeks: int = 0
    required_balance_kopeks: int = 0
    has_subscription: bool = False


class SpinAvailabilityResponse(BaseModel):
    """Доступность спина."""

    can_spin: bool
    reason: str | None = None
    spins_remaining_today: int
    can_pay_stars: bool
    can_pay_days: bool
    min_subscription_days: int
    user_subscription_days: int
    user_balance_kopeks: int = 0
    required_balance_kopeks: int = 0


class SpinRequest(BaseModel):
    """Запрос на спин."""

    payment_type: WheelPaymentType


class SpinResultResponse(BaseModel):
    """Результат спина."""

    success: bool
    prize_id: int | None = None
    prize_type: str | None = None
    prize_value: int = 0
    prize_display_name: str = ''
    emoji: str = '🎁'
    color: str = '#3B82F6'
    rotation_degrees: float = 0.0
    message: str = ''
    promocode: str | None = None
    error: str | None = None


class SpinHistoryItem(BaseModel):
    """Элемент истории спинов."""

    id: int
    payment_type: str
    payment_amount: int
    prize_type: str
    prize_value: int
    prize_display_name: str
    emoji: str = '🎁'
    color: str = '#3B82F6'
    prize_value_kopeks: int
    created_at: datetime

    class Config:
        from_attributes = True


class SpinHistoryResponse(BaseModel):
    """История спинов с пагинацией."""

    items: list[SpinHistoryItem]
    total: int
    page: int
    per_page: int
    pages: int


# ==================== ADMIN SCHEMAS ====================


class WheelPrizeAdminResponse(BaseModel):
    """Полная информация о призе для админа."""

    id: int
    config_id: int
    prize_type: str
    prize_value: int
    display_name: str
    emoji: str
    color: str
    prize_value_kopeks: int
    sort_order: int
    manual_probability: float | None = None
    is_active: bool
    promo_balance_bonus_kopeks: int = 0
    promo_subscription_days: int = 0
    promo_traffic_gb: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class AdminWheelConfigResponse(BaseModel):
    """Полная конфигурация колеса для админа."""

    id: int
    is_enabled: bool
    name: str
    spin_cost_stars: int
    spin_cost_days: int
    spin_cost_stars_enabled: bool
    spin_cost_days_enabled: bool
    rtp_percent: int
    daily_spin_limit: int
    min_subscription_days_for_day_payment: int
    promo_prefix: str
    promo_validity_days: int
    prizes: list[WheelPrizeAdminResponse]
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class UpdateWheelConfigRequest(BaseModel):
    """Запрос на обновление конфига колеса."""

    is_enabled: bool | None = None
    name: str | None = Field(None, min_length=1, max_length=255)
    spin_cost_stars: int | None = Field(None, ge=1, le=1000)
    spin_cost_days: int | None = Field(None, ge=1, le=30)
    spin_cost_stars_enabled: bool | None = None
    spin_cost_days_enabled: bool | None = None
    rtp_percent: int | None = Field(None, ge=0, le=100)
    daily_spin_limit: int | None = Field(None, ge=0, le=100)
    min_subscription_days_for_day_payment: int | None = Field(None, ge=1, le=30)
    promo_prefix: str | None = Field(None, min_length=1, max_length=20)
    promo_validity_days: int | None = Field(None, ge=1, le=365)


class CreatePrizeRequest(BaseModel):
    """Запрос на создание приза."""

    prize_type: WheelPrizeType
    prize_value: int = Field(..., ge=0)
    display_name: str = Field(..., min_length=1, max_length=100)
    emoji: str = Field(default='🎁', max_length=10)
    color: str = Field(default='#3B82F6', pattern=r'^#[0-9A-Fa-f]{6}$')
    prize_value_kopeks: int = Field(..., ge=0)
    sort_order: int = Field(default=0, ge=0)
    manual_probability: float | None = Field(None, ge=0, le=1)
    is_active: bool = True
    promo_balance_bonus_kopeks: int = Field(default=0, ge=0)
    promo_subscription_days: int = Field(default=0, ge=0)
    promo_traffic_gb: int = Field(default=0, ge=0)


class UpdatePrizeRequest(BaseModel):
    """Запрос на обновление приза."""

    prize_type: WheelPrizeType | None = None
    prize_value: int | None = Field(None, ge=0)
    display_name: str | None = Field(None, min_length=1, max_length=100)
    emoji: str | None = Field(None, max_length=10)
    color: str | None = Field(None, pattern=r'^#[0-9A-Fa-f]{6}$')
    prize_value_kopeks: int | None = Field(None, ge=0)
    sort_order: int | None = Field(None, ge=0)
    manual_probability: float | None = Field(None, ge=0, le=1)
    is_active: bool | None = None
    promo_balance_bonus_kopeks: int | None = Field(None, ge=0)
    promo_subscription_days: int | None = Field(None, ge=0)
    promo_traffic_gb: int | None = Field(None, ge=0)


class ReorderPrizesRequest(BaseModel):
    """Запрос на переупорядочивание призов."""

    prize_ids: list[int]


class AdminSpinItem(BaseModel):
    """Спин для админки."""

    id: int
    user_id: int
    username: str | None = None
    payment_type: str
    payment_amount: int
    payment_value_kopeks: int
    prize_type: str
    prize_value: int
    prize_display_name: str
    prize_value_kopeks: int
    is_applied: bool
    created_at: datetime

    class Config:
        from_attributes = True


class AdminSpinsResponse(BaseModel):
    """Список спинов для админки с пагинацией."""

    items: list[AdminSpinItem]
    total: int
    page: int
    per_page: int
    pages: int


class WheelStatisticsResponse(BaseModel):
    """Статистика колеса."""

    total_spins: int
    total_revenue_kopeks: int
    total_payout_kopeks: int
    actual_rtp_percent: float
    configured_rtp_percent: int
    spins_by_payment_type: dict
    prizes_distribution: list[dict]
    top_wins: list[dict]
    period_from: str | None = None
    period_to: str | None = None
