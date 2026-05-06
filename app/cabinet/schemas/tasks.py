"""Pydantic schemas для системы заданий с наградами."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Task partner channels (admin)
# ---------------------------------------------------------------------------


class TaskPartnerChannelBase(BaseModel):
    channel_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=255)
    channel_link: str | None = Field(default=None, max_length=500)
    description: str | None = None
    is_active: bool = True
    sort_order: int = 0


class TaskPartnerChannelCreateRequest(TaskPartnerChannelBase):
    pass


class TaskPartnerChannelUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    channel_link: str | None = Field(default=None, max_length=500)
    description: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class TaskPartnerChannelResponse(TaskPartnerChannelBase):
    id: int
    created_at: datetime
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Tasks (admin)
# ---------------------------------------------------------------------------


TASK_TYPES: tuple[str, ...] = (
    'purchase_tariff',
    'subscribe_channel',
    'traffic_used',
    'referrals_invited',
    'purchase_period',
    'spend_amount',
    'multi_tariff',
    'gift_purchased',
    'gifts_count',
)

REWARD_TYPES: tuple[str, ...] = ('balance', 'subscription_days')

USER_AUDIENCES: tuple[str, ...] = ('telegram', 'email', 'both')


class TaskCreateRequest(BaseModel):
    """Создание шаблона задания."""

    title: dict[str, str] = Field(..., description='i18n: { "ru": "...", "en": "..." }')
    description: dict[str, str] = Field(default_factory=dict)
    icon: str | None = None
    is_active: bool = True
    sort_order: int = 0

    task_type: str = Field(...)
    target_value: int = Field(default=1, ge=1)
    target_meta: dict[str, Any] = Field(default_factory=dict)

    reward_type: str = Field(...)
    reward_value: int = Field(default=0, ge=0)
    reward_meta: dict[str, Any] = Field(default_factory=dict)
    allow_user_choice: bool = False

    user_audience: str = Field(default='both')
    promo_group_id: int | None = None

    parent_task_id: int | None = None
    level: int = Field(default=1, ge=1)

    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator('task_type')
    @classmethod
    def _validate_task_type(cls, v: str) -> str:
        if v not in TASK_TYPES:
            raise ValueError(f'invalid task_type: {v}')
        return v

    @field_validator('reward_type')
    @classmethod
    def _validate_reward_type(cls, v: str) -> str:
        if v not in REWARD_TYPES:
            raise ValueError(f'invalid reward_type: {v}')
        return v

    @field_validator('user_audience')
    @classmethod
    def _validate_user_audience(cls, v: str) -> str:
        if v not in USER_AUDIENCES:
            raise ValueError(f'invalid user_audience: {v}')
        return v

    @field_validator('title')
    @classmethod
    def _validate_title(cls, v: dict[str, str]) -> dict[str, str]:
        if not v or not any(value.strip() for value in v.values() if isinstance(value, str)):
            raise ValueError('title must contain at least one non-empty translation')
        return v

    @model_validator(mode='after')
    def _validate_meta_per_type(self) -> TaskCreateRequest:
        """Per-type validation: target_meta required keys, reward_value sanity."""
        # PURCHASE_TARIFF требует tariff_id
        if self.task_type == 'purchase_tariff' and 'tariff_id' not in (self.target_meta or {}):
            raise ValueError('PURCHASE_TARIFF requires target_meta.tariff_id')
        # SUBSCRIBE_CHANNEL требует channel_id (строкой, как в TaskPartnerChannel.channel_id)
        if self.task_type == 'subscribe_channel':
            channel_id = (self.target_meta or {}).get('channel_id')
            if channel_id is None:
                raise ValueError('SUBSCRIBE_CHANNEL requires target_meta.channel_id')
            if not isinstance(channel_id, str) or not channel_id.strip():
                raise ValueError('SUBSCRIBE_CHANNEL target_meta.channel_id must be a non-empty string')
        # PURCHASE_PERIOD требует period_days
        if self.task_type == 'purchase_period' and 'period_days' not in (self.target_meta or {}):
            raise ValueError('PURCHASE_PERIOD requires target_meta.period_days')
        # BALANCE reward требует reward_value > 0
        if self.reward_type == 'balance' and self.reward_value <= 0:
            raise ValueError('BALANCE reward requires reward_value > 0')
        # SUBSCRIPTION_DAYS reward: либо reward_value > 0, либо tariff_id указан
        if self.reward_type == 'subscription_days':
            tariff_id = (self.reward_meta or {}).get('tariff_id')
            if self.reward_value <= 0 and tariff_id is None:
                raise ValueError(
                    'SUBSCRIPTION_DAYS reward requires reward_value > 0 or '
                    'reward_meta.tariff_id (to use Tariff.bonus_days_per_purchase)'
                )
        return self


class TaskUpdateRequest(BaseModel):
    """Частичное обновление задания (все поля опциональны)."""

    title: dict[str, str] | None = None
    description: dict[str, str] | None = None
    icon: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None

    task_type: str | None = None
    target_value: int | None = Field(default=None, ge=1)
    target_meta: dict[str, Any] | None = None

    reward_type: str | None = None
    reward_value: int | None = Field(default=None, ge=0)
    reward_meta: dict[str, Any] | None = None
    allow_user_choice: bool | None = None

    user_audience: str | None = None
    promo_group_id: int | None = None

    parent_task_id: int | None = None
    level: int | None = Field(default=None, ge=1)

    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator('task_type')
    @classmethod
    def _validate_task_type(cls, v: str | None) -> str | None:
        if v is not None and v not in TASK_TYPES:
            raise ValueError(f'invalid task_type: {v}')
        return v

    @field_validator('reward_type')
    @classmethod
    def _validate_reward_type(cls, v: str | None) -> str | None:
        if v is not None and v not in REWARD_TYPES:
            raise ValueError(f'invalid reward_type: {v}')
        return v

    @field_validator('user_audience')
    @classmethod
    def _validate_user_audience(cls, v: str | None) -> str | None:
        if v is not None and v not in USER_AUDIENCES:
            raise ValueError(f'invalid user_audience: {v}')
        return v


class TaskResponse(BaseModel):
    """Полное представление задания (для админа)."""

    id: int
    title: dict[str, str]
    description: dict[str, str]
    icon: str | None = None
    is_active: bool
    sort_order: int
    task_type: str
    target_value: int
    target_meta: dict[str, Any]
    reward_type: str
    reward_value: int
    reward_meta: dict[str, Any]
    allow_user_choice: bool
    user_audience: str
    promo_group_id: int | None = None
    parent_task_id: int | None = None
    level: int
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TaskListItem(BaseModel):
    """Компактное представление для списка."""

    id: int
    title: dict[str, str]
    icon: str | None = None
    is_active: bool
    sort_order: int
    task_type: str
    target_value: int
    reward_type: str
    reward_value: int
    user_audience: str
    promo_group_id: int | None = None
    parent_task_id: int | None = None
    level: int
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# User-side schemas
# ---------------------------------------------------------------------------


class UserTaskProgressResponse(BaseModel):
    """Прогресс пользователя по конкретному заданию."""

    task_id: int
    title: dict[str, str]
    description: dict[str, str]
    icon: str | None = None
    task_type: str
    target_value: int
    target_meta: dict[str, Any] = Field(default_factory=dict)
    reward_type: str
    reward_value: int
    reward_meta: dict[str, Any] = Field(default_factory=dict)
    allow_user_choice: bool

    level: int
    parent_task_id: int | None = None

    current_value: int
    percent: int
    is_completed: bool
    is_claimed: bool
    completed_at: datetime | None = None
    claimed_at: datetime | None = None
    reward_granted_meta: dict[str, Any] | None = None


class UserTasksListResponse(BaseModel):
    """Список заданий пользователя."""

    items: list[UserTaskProgressResponse]
    has_unclaimed: bool
    unclaimed_count: int


class UserTasksAvailabilityResponse(BaseModel):
    """Краткая инфа для условного показа вкладки."""

    has_available_tasks: bool
    unclaimed_count: int


class ClaimRewardRequest(BaseModel):
    """Запрос на получение награды."""

    chosen_subscription_id: int | None = Field(
        default=None,
        description='Для multi-tariff / subscription_days reward — какой подписке начислить дни',
    )
    chosen_reward_type: Literal['balance', 'subscription_days'] | None = Field(
        default=None,
        description='Если allow_user_choice=true, юзер может выбрать тип награды',
    )


class ClaimRewardResponse(BaseModel):
    """Результат claim награды."""

    success: bool
    reward: dict[str, Any]
