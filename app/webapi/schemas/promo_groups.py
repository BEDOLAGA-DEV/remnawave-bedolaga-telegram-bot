from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, validator


def _normalize_period_discounts(value: dict[object, object] | None) -> dict[int, int] | None:
    if value is None:
        return None

    normalized: dict[int, int] = {}
    if isinstance(value, dict):
        for raw_key, raw_value in value.items():
            try:
                key = int(raw_key)
                normalized[key] = int(raw_value)
            except (TypeError, ValueError):
                continue

    # Return empty dict (not None) so the backend can distinguish
    # "clear all discounts" ({}) from "don't touch discounts" (None/absent).
    return normalized


class PromoGroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    server_discount_percent: int
    traffic_discount_percent: int
    device_discount_percent: int
    period_discounts: dict[int, int] = Field(default_factory=dict)
    auto_assign_total_spent_kopeks: int | None = None
    apply_discounts_to_addons: bool
    is_default: bool
    members_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class _PromoGroupBase(BaseModel):
    period_discounts: dict[int, int] | None = Field(
        default=None,
        description=(
            'Словарь скидок по длительности подписки. Ключ — количество месяцев, '
            'значение — скидка в процентах. Например: {1: 10, 6: 20}.'
        ),
        example={1: 10, 6: 20},
    )

    @validator('period_discounts', pre=True)
    def validate_period_discounts(cls, value):
        return _normalize_period_discounts(value)


class PromoGroupCreateRequest(_PromoGroupBase):
    name: str
    server_discount_percent: int = 0
    traffic_discount_percent: int = 0
    device_discount_percent: int = 0
    auto_assign_total_spent_kopeks: int | None = None
    apply_discounts_to_addons: bool = True
    is_default: bool = False


class PromoGroupUpdateRequest(_PromoGroupBase):
    name: str | None = None
    server_discount_percent: int | None = None
    traffic_discount_percent: int | None = None
    device_discount_percent: int | None = None
    auto_assign_total_spent_kopeks: int | None = None
    apply_discounts_to_addons: bool | None = None
    is_default: bool | None = None


class PromoGroupListResponse(BaseModel):
    items: list[PromoGroupResponse]
    total: int
    limit: int
    offset: int
