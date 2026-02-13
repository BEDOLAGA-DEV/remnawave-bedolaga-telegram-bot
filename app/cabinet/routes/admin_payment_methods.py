"""Admin routes for payment method configuration in cabinet."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.payment_method_config_service import (
    _get_method_defaults,
    get_all_configs,
    get_all_promo_groups,
    get_config_by_method_id,
    update_config,
    update_sort_order,
)

from ..dependencies import get_cabinet_db, get_current_admin_user


logger = logging.getLogger(__name__)

router = APIRouter(prefix='/admin/payment-methods', tags=['Cabinet Admin Payment Methods'])


# ============ Schemas ============


class SubOptionInfo(BaseModel):
    id: str
    name: str


class PaymentMethodConfigResponse(BaseModel):
    method_id: str
    sort_order: int
    is_enabled: bool
    display_name: str | None = None
    default_display_name: str
    sub_options: dict | None = None
    available_sub_options: list[SubOptionInfo] | None = None
    min_amount_kopeks: int | None = None
    max_amount_kopeks: int | None = None
    default_min_amount_kopeks: int
    default_max_amount_kopeks: int
    user_type_filter: str
    first_topup_filter: str
    promo_group_filter_mode: str
    allowed_promo_group_ids: list[int] = Field(default_factory=list)
    is_provider_configured: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class PaymentMethodConfigUpdateRequest(BaseModel):
    is_enabled: bool | None = None
    display_name: str | None = Field(default=None, description='Null to reset to default')
    sub_options: dict | None = None
    min_amount_kopeks: int | None = Field(default=None, ge=0)
    max_amount_kopeks: int | None = Field(default=None, ge=0)
    user_type_filter: str | None = Field(default=None, pattern='^(all|telegram|email)$')
    first_topup_filter: str | None = Field(default=None, pattern='^(any|yes|no)$')
    promo_group_filter_mode: str | None = Field(default=None, pattern='^(all|selected)$')
    allowed_promo_group_ids: list[int] | None = None
    # Allow explicitly resetting display_name to null
    reset_display_name: bool = False
    reset_min_amount: bool = False
    reset_max_amount: bool = False


class SortOrderRequest(BaseModel):
    method_ids: list[str]


class PromoGroupSimple(BaseModel):
    id: int
    name: str

    class Config:
        from_attributes = True


# ============ Helpers ============


def _enrich_config(config, defaults: dict) -> PaymentMethodConfigResponse:
    """Enrich a PaymentMethodConfig with env-var defaults."""
    method_def = defaults.get(config.method_id, {})

    available_sub_options = None
    raw_options = method_def.get('available_sub_options')
    if raw_options:
        available_sub_options = [SubOptionInfo(**opt) for opt in raw_options]

    return PaymentMethodConfigResponse(
        method_id=config.method_id,
        sort_order=config.sort_order,
        is_enabled=config.is_enabled,
        display_name=config.display_name,
        default_display_name=method_def.get('default_display_name', config.method_id),
        sub_options=config.sub_options,
        available_sub_options=available_sub_options,
        min_amount_kopeks=config.min_amount_kopeks,
        max_amount_kopeks=config.max_amount_kopeks,
        default_min_amount_kopeks=method_def.get('default_min', 1000),
        default_max_amount_kopeks=method_def.get('default_max', 10000000),
        user_type_filter=config.user_type_filter,
        first_topup_filter=config.first_topup_filter,
        promo_group_filter_mode=config.promo_group_filter_mode,
        allowed_promo_group_ids=[pg.id for pg in config.allowed_promo_groups],
        is_provider_configured=method_def.get('is_configured', False),
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


# ============ Routes ============


@router.get('', response_model=list[PaymentMethodConfigResponse])
async def list_payment_methods(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List all payment method configurations."""
    configs = await get_all_configs(db)
    defaults = _get_method_defaults()
    return [_enrich_config(c, defaults) for c in configs]


@router.get('/promo-groups', response_model=list[PromoGroupSimple])
async def list_promo_groups(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List all promo groups for filter selector."""
    groups = await get_all_promo_groups(db)
    return [PromoGroupSimple(id=g.id, name=g.name) for g in groups]


@router.get('/{method_id}', response_model=PaymentMethodConfigResponse)
async def get_payment_method(
    method_id: str,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get a single payment method configuration."""
    config = await get_config_by_method_id(db, method_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Payment method not found: {method_id}',
        )
    defaults = _get_method_defaults()
    return _enrich_config(config, defaults)


@router.put('/order')
async def update_payment_methods_order(
    request: SortOrderRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Batch update sort order for payment methods."""
    await update_sort_order(db, request.method_ids)
    logger.info(f'Admin {admin.id} updated payment methods order: {request.method_ids}')
    return {'success': True}


@router.put('/{method_id}', response_model=PaymentMethodConfigResponse)
async def update_payment_method(
    method_id: str,
    request: PaymentMethodConfigUpdateRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update a payment method configuration."""
    # Build update data dict
    data = {}

    if request.is_enabled is not None:
        data['is_enabled'] = request.is_enabled

    if request.reset_display_name:
        data['display_name'] = None
    elif request.display_name is not None:
        data['display_name'] = request.display_name.strip() or None

    if request.sub_options is not None:
        data['sub_options'] = request.sub_options

    if request.reset_min_amount:
        data['min_amount_kopeks'] = None
    elif request.min_amount_kopeks is not None:
        data['min_amount_kopeks'] = request.min_amount_kopeks

    if request.reset_max_amount:
        data['max_amount_kopeks'] = None
    elif request.max_amount_kopeks is not None:
        data['max_amount_kopeks'] = request.max_amount_kopeks

    if request.user_type_filter is not None:
        data['user_type_filter'] = request.user_type_filter

    if request.first_topup_filter is not None:
        data['first_topup_filter'] = request.first_topup_filter

    if request.promo_group_filter_mode is not None:
        data['promo_group_filter_mode'] = request.promo_group_filter_mode

    promo_group_ids = request.allowed_promo_group_ids

    config = await update_config(db, method_id, data, promo_group_ids)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Payment method not found: {method_id}',
        )

    logger.info(f'Admin {admin.id} updated payment method config: {method_id}')

    defaults = _get_method_defaults()
    return _enrich_config(config, defaults)
