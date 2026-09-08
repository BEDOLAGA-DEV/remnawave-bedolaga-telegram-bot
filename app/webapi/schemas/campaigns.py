from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, validator

from app.localization.texts import get_texts


CampaignBonusType = Annotated[
    Literal['balance', 'subscription', 'none', 'tariff'],
    Field(
        description=get_texts().t(
            'CAMPAIGN_BONUS_TYPE_DESCRIPTION',
            'Тип бонуса кампании: balance (баланс), subscription (пробная подписка), none (без награды), '
            'tariff (тариф)',
        )
    ),
]


class CampaignBase(BaseModel):
    name: str = Field(..., max_length=255)
    start_parameter: str = Field(
        ...,
        max_length=64,
        description=get_texts().t('CAMPAIGN_START_PARAMETER_DESCRIPTION', 'Start parameter для deep-link (уникальный)'),
    )
    bonus_type: CampaignBonusType
    balance_bonus_kopeks: int = Field(0, ge=0)
    subscription_duration_days: int | None = Field(None, ge=0)
    subscription_traffic_gb: int | None = Field(None, ge=0)
    subscription_device_limit: int | None = Field(None, ge=0)
    subscription_squads: list[str] = Field(default_factory=list)
    # Поля для типа "tariff"
    tariff_id: int | None = Field(
        None, ge=1, description=get_texts().t('CAMPAIGN_TARIFF_ID_DESCRIPTION', 'ID тарифа для выдачи')
    )
    tariff_duration_days: int | None = Field(
        None, ge=1, description=get_texts().t('CAMPAIGN_TARIFF_DURATION_DAYS_DESCRIPTION', 'Длительность тарифа в днях')
    )

    @validator('name', 'start_parameter')
    def strip_strings(cls, value: str) -> str:
        return value.strip()


class CampaignCreateRequest(CampaignBase):
    is_active: bool = True

    @validator('balance_bonus_kopeks')
    def validate_balance_bonus(cls, value: int, values: dict) -> int:
        if values.get('bonus_type') == 'balance' and value <= 0:
            raise ValueError(
                get_texts().t(
                    'CAMPAIGN_BALANCE_BONUS_MUST_BE_POSITIVE', 'balance_bonus_kopeks must be positive for balance bonus'
                )
            )
        return value

    @validator('subscription_duration_days')
    def validate_subscription_bonus(cls, value: int | None, values: dict):
        if values.get('bonus_type') == 'subscription':
            if value is None or value <= 0:
                raise ValueError(
                    get_texts().t(
                        'CAMPAIGN_SUBSCRIPTION_DURATION_MUST_BE_POSITIVE',
                        'subscription_duration_days must be positive for subscription bonus',
                    )
                )
        return value

    @validator('tariff_id')
    def validate_tariff_id(cls, value: int | None, values: dict):
        if values.get('bonus_type') == 'tariff':
            if value is None or value <= 0:
                raise ValueError(
                    get_texts().t('CAMPAIGN_TARIFF_ID_REQUIRED', 'tariff_id must be specified for tariff bonus')
                )
        return value

    @validator('tariff_duration_days')
    def validate_tariff_duration(cls, value: int | None, values: dict):
        if values.get('bonus_type') == 'tariff':
            if value is None or value <= 0:
                raise ValueError(
                    get_texts().t(
                        'CAMPAIGN_TARIFF_DURATION_MUST_BE_POSITIVE',
                        'tariff_duration_days must be positive for tariff bonus',
                    )
                )
        return value


class CampaignResponse(BaseModel):
    id: int
    name: str
    start_parameter: str
    bonus_type: CampaignBonusType
    balance_bonus_kopeks: int
    balance_bonus_rubles: float
    subscription_duration_days: int | None = None
    subscription_traffic_gb: int | None = None
    subscription_device_limit: int | None = None
    subscription_squads: list[str] = Field(default_factory=list)
    # Поля для типа "tariff"
    tariff_id: int | None = None
    tariff_duration_days: int | None = None
    tariff_name: str | None = None  # Для отображения названия тарифа
    is_active: bool
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime
    registrations_count: int = 0


class CampaignListResponse(BaseModel):
    items: list[CampaignResponse]
    total: int
    limit: int
    offset: int


class CampaignUpdateRequest(BaseModel):
    name: str | None = Field(None, max_length=255)
    start_parameter: str | None = Field(None, max_length=64)
    bonus_type: CampaignBonusType | None = None
    balance_bonus_kopeks: int | None = Field(None, ge=0)
    subscription_duration_days: int | None = Field(None, ge=0)
    subscription_traffic_gb: int | None = Field(None, ge=0)
    subscription_device_limit: int | None = Field(None, ge=0)
    subscription_squads: list[str] | None = None
    # Поля для типа "tariff"
    tariff_id: int | None = Field(None, ge=1)
    tariff_duration_days: int | None = Field(None, ge=1)
    is_active: bool | None = None

    @validator('name', 'start_parameter', pre=True)
    def strip_optional_strings(cls, value: str | None):
        if isinstance(value, str):
            return value.strip()
        return value

    @validator('balance_bonus_kopeks')
    def validate_balance_bonus(cls, value: int | None, values: dict):
        bonus_type = values.get('bonus_type')
        if bonus_type == 'balance' and value is not None and value <= 0:
            raise ValueError(
                get_texts().t(
                    'CAMPAIGN_BALANCE_BONUS_MUST_BE_POSITIVE', 'balance_bonus_kopeks must be positive for balance bonus'
                )
            )
        return value

    @validator('subscription_duration_days')
    def validate_subscription_bonus(cls, value: int | None, values: dict):
        bonus_type = values.get('bonus_type')
        if bonus_type == 'subscription' and value is not None and value <= 0:
            raise ValueError(
                get_texts().t(
                    'CAMPAIGN_SUBSCRIPTION_DURATION_MUST_BE_POSITIVE',
                    'subscription_duration_days must be positive for subscription bonus',
                )
            )
        return value

    @validator('tariff_id')
    def validate_tariff_id(cls, value: int | None, values: dict):
        bonus_type = values.get('bonus_type')
        if bonus_type == 'tariff' and value is not None and value <= 0:
            raise ValueError(
                get_texts().t('CAMPAIGN_TARIFF_ID_MUST_BE_POSITIVE', 'tariff_id must be positive for tariff bonus')
            )
        return value

    @validator('tariff_duration_days')
    def validate_tariff_duration(cls, value: int | None, values: dict):
        bonus_type = values.get('bonus_type')
        if bonus_type == 'tariff' and value is not None and value <= 0:
            raise ValueError(
                get_texts().t(
                    'CAMPAIGN_TARIFF_DURATION_MUST_BE_POSITIVE',
                    'tariff_duration_days must be positive for tariff bonus',
                )
            )
        return value
