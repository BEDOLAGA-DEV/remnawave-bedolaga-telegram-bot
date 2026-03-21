"""Admin routes for landing page management in cabinet."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.utils.locale import (
    ensure_locale_dict,
    validate_locale_dict,
)
from app.database.crud.landing import (
    create_landing,
    delete_landing,
    get_all_landing_purchase_stats,
    get_all_landings,
    get_landing_by_id,
    get_landing_by_slug,
    update_landing,
    update_landing_order,
)
from app.database.models import GuestPurchase, GuestPurchaseStatus, LandingPage, Tariff, User

from ..dependencies import get_cabinet_db, require_permission
from .branding import ALLOWED_BG_TYPES, _validate_settings


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/landings', tags=['Cabinet Admin Landings'])

# Slugs that conflict with public landing router path segments
_RESERVED_SLUGS = frozenset(
    {
        'purchase',
        'admin',
        'api',
        'health',
        'static',
        'assets',
        'favicon',
        'robots',
        'sitemap',
        'well-known',
    }
)


_ALLOWED_BG_CONFIG_KEYS = frozenset({'enabled', 'type', 'settings', 'opacity', 'blur', 'reducedOnMobile'})


def _validate_background_config(v: dict | None) -> dict | None:
    """Validate and sanitize background_config, reusing branding constraints."""
    if v is None:
        return None
    if not isinstance(v.get('enabled'), bool):
        raise ValueError('background_config.enabled must be a boolean')
    bg_type = v.get('type')
    if not isinstance(bg_type, str) or bg_type not in ALLOWED_BG_TYPES:
        raise ValueError(f'background_config.type must be one of: {", ".join(ALLOWED_BG_TYPES)}')
    if 'settings' in v:
        if not isinstance(v['settings'], dict):
            raise ValueError('background_config.settings must be a dict')
        _validate_settings(v['settings'])
    if 'opacity' in v:
        opacity = v['opacity']
        if not isinstance(opacity, int | float) or not (0 <= opacity <= 1):
            raise ValueError('background_config.opacity must be 0-1')
    if 'blur' in v:
        blur = v['blur']
        if not isinstance(blur, int | float) or not (0 <= blur <= 100):
            raise ValueError('background_config.blur must be 0-100')
    if 'reducedOnMobile' in v and not isinstance(v['reducedOnMobile'], bool):
        raise ValueError('background_config.reducedOnMobile must be a boolean')
    # Strip unknown keys
    return {k: val for k, val in v.items() if k in _ALLOWED_BG_CONFIG_KEYS}


# ============ Schemas ============


class LandingFeatureInput(BaseModel):
    icon: str = Field(default='', max_length=100)
    title: dict[str, str] = Field(default_factory=dict)
    description: dict[str, str] = Field(default_factory=dict)

    @field_validator('title', 'description', mode='before')
    @classmethod
    def coerce_to_dict(cls, v: dict[str, str] | str | None) -> dict[str, str]:
        return ensure_locale_dict(v)

    @field_validator('title')
    @classmethod
    def validate_title_length(cls, v: dict[str, str]) -> dict[str, str]:
        return validate_locale_dict(v, max_length=200, field_name='feature.title')

    @field_validator('description')
    @classmethod
    def validate_description_length(cls, v: dict[str, str]) -> dict[str, str]:
        return validate_locale_dict(v, max_length=500, field_name='feature.description')


class LandingPaymentMethodInput(BaseModel):
    method_id: str = Field(max_length=50)
    display_name: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=500)
    icon_url: str | None = Field(default=None, max_length=500)
    sort_order: int = 0
    min_amount_kopeks: int | None = None
    max_amount_kopeks: int | None = None
    currency: str | None = Field(default=None, max_length=10)
    return_url: str | None = Field(default=None, max_length=500)
    sub_options: dict[str, bool] | None = None

    @field_validator('sub_options', mode='before')
    @classmethod
    def validate_sub_options(cls, v: dict[str, bool] | None) -> dict[str, bool] | None:
        if not v:
            return None
        if len(v) > 20:
            raise ValueError('sub_options cannot have more than 20 keys')
        for key in v:
            if not isinstance(key, str) or len(key) > 50:
                raise ValueError('sub_options keys must be strings of at most 50 characters')
        return v

    @field_validator('icon_url', mode='before')
    @classmethod
    def validate_icon_url(cls, v: str | None) -> str | None:
        if not v:
            return None
        if not v.startswith(('https://', '/')):
            raise ValueError('icon_url must use HTTPS or be a relative path')
        return v

    @field_validator('return_url', mode='before')
    @classmethod
    def validate_return_url(cls, v: str | None) -> str | None:
        if not v:
            return None
        if not v.startswith('https://'):
            raise ValueError('return_url must use HTTPS')
        parsed = urlparse(v)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('return_url must be a valid HTTPS URL without credentials')
        return v

    @field_validator('currency', mode='before')
    @classmethod
    def validate_currency(cls, v: str | None) -> str | None:
        if not v:
            return None
        return v.strip().upper()

    @field_validator('min_amount_kopeks', 'max_amount_kopeks')
    @classmethod
    def validate_amounts(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError('Amount cannot be negative')
        return v

    @model_validator(mode='after')
    def validate_amount_range(self) -> 'LandingPaymentMethodInput':
        if (
            self.min_amount_kopeks is not None
            and self.max_amount_kopeks is not None
            and self.min_amount_kopeks > self.max_amount_kopeks
        ):
            raise ValueError('min_amount_kopeks cannot be greater than max_amount_kopeks')
        return self


class LandingCreateRequest(BaseModel):
    slug: str = Field(pattern=r'^[a-z0-9\-]+$', min_length=1, max_length=100)
    title: dict[str, str] = Field(default_factory=lambda: {'ru': ''})
    subtitle: dict[str, str] | None = None
    is_active: bool = True
    features: list[LandingFeatureInput] = Field(default_factory=list, max_length=20)
    footer_text: dict[str, str] | None = None
    allowed_tariff_ids: list[int] = Field(default_factory=list, max_length=50)
    allowed_periods: dict[str, list[int]] = Field(default_factory=dict)
    payment_methods: list[LandingPaymentMethodInput] = Field(default_factory=list, max_length=10)

    @field_validator('allowed_periods')
    @classmethod
    def validate_allowed_periods_size(cls, v: dict[str, list[int]]) -> dict[str, list[int]]:
        if len(v) > 50:
            raise ValueError('allowed_periods cannot have more than 50 entries')
        for key, periods in v.items():
            if len(periods) > 20:
                raise ValueError(f'allowed_periods[{key}] cannot have more than 20 periods')
        return v

    gift_enabled: bool = True
    custom_css: str | None = Field(default=None, max_length=10000)
    meta_title: dict[str, str] | None = None
    meta_description: dict[str, str] | None = None
    discount_percent: int | None = Field(default=None, ge=1, le=99)
    discount_overrides: dict[str, int] | None = None  # {"tariff_id": percent}
    discount_starts_at: datetime | None = None
    discount_ends_at: datetime | None = None
    discount_badge_text: dict[str, str] | None = None
    background_config: dict | None = None

    @field_validator('background_config')
    @classmethod
    def validate_background_config(cls, v: dict | None) -> dict | None:
        return _validate_background_config(v)

    @field_validator(
        'title', 'subtitle', 'footer_text', 'meta_title', 'meta_description', 'discount_badge_text', mode='before'
    )
    @classmethod
    def coerce_text_to_dict(cls, v: dict[str, str] | str | None) -> dict[str, str] | None:
        if v is None:
            return None
        return ensure_locale_dict(v)

    @field_validator('title')
    @classmethod
    def validate_title(cls, v: dict[str, str]) -> dict[str, str]:
        return validate_locale_dict(v, max_length=500, field_name='title')

    @field_validator('subtitle')
    @classmethod
    def validate_subtitle(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=1000, field_name='subtitle')

    @field_validator('footer_text')
    @classmethod
    def validate_footer_text(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=5000, field_name='footer_text')

    @field_validator('meta_title')
    @classmethod
    def validate_meta_title(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=200, field_name='meta_title')

    @field_validator('meta_description')
    @classmethod
    def validate_meta_description(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=500, field_name='meta_description')

    @field_validator('discount_badge_text')
    @classmethod
    def validate_discount_badge_text(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=200, field_name='discount_badge_text')

    @field_validator('discount_starts_at', 'discount_ends_at', mode='after')
    @classmethod
    def ensure_aware_datetime(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v

    @model_validator(mode='after')
    def validate_discount(self) -> 'LandingCreateRequest':
        has_discount = self.discount_percent is not None
        has_dates = self.discount_starts_at is not None or self.discount_ends_at is not None
        if has_dates and not has_discount:
            raise ValueError('discount_percent is required when discount dates are set')
        if has_discount and not (self.discount_starts_at and self.discount_ends_at):
            raise ValueError('discount_starts_at and discount_ends_at are required when discount_percent is set')
        if self.discount_starts_at and self.discount_ends_at:
            if self.discount_starts_at >= self.discount_ends_at:
                raise ValueError('discount_starts_at must be before discount_ends_at')
        if self.discount_overrides:
            if len(self.discount_overrides) > 100:
                raise ValueError('discount_overrides cannot have more than 100 entries')
            for key, val in self.discount_overrides.items():
                if not key.isdigit():
                    raise ValueError('discount_overrides keys must be tariff ID strings')
                if not (1 <= val <= 99):
                    raise ValueError('discount_overrides values must be 1-99')
            if self.allowed_tariff_ids:
                allowed_set = {str(tid) for tid in self.allowed_tariff_ids}
                invalid = set(self.discount_overrides.keys()) - allowed_set
                if invalid:
                    raise ValueError(f'discount_overrides contains tariff IDs not in allowed_tariff_ids: {invalid}')
        return self


class LandingUpdateRequest(BaseModel):
    slug: str | None = Field(default=None, pattern=r'^[a-z0-9\-]+$', min_length=1, max_length=100)
    title: dict[str, str] | None = None
    subtitle: dict[str, str] | None = None
    is_active: bool | None = None
    features: list[LandingFeatureInput] | None = Field(default=None, max_length=20)
    footer_text: dict[str, str] | None = None
    allowed_tariff_ids: list[int] | None = Field(default=None, max_length=50)
    allowed_periods: dict[str, list[int]] | None = None
    payment_methods: list[LandingPaymentMethodInput] | None = Field(default=None, max_length=10)
    gift_enabled: bool | None = None
    custom_css: str | None = Field(default=None, max_length=10000)
    meta_title: dict[str, str] | None = None
    meta_description: dict[str, str] | None = None
    discount_percent: int | None = Field(default=None, ge=1, le=99)
    discount_overrides: dict[str, int] | None = None
    discount_starts_at: datetime | None = None
    discount_ends_at: datetime | None = None
    discount_badge_text: dict[str, str] | None = None
    background_config: dict | None = None

    @field_validator('background_config')
    @classmethod
    def validate_background_config(cls, v: dict | None) -> dict | None:
        return _validate_background_config(v)

    @field_validator('allowed_periods')
    @classmethod
    def validate_allowed_periods_size(cls, v: dict[str, list[int]] | None) -> dict[str, list[int]] | None:
        if v is None:
            return None
        if len(v) > 50:
            raise ValueError('allowed_periods cannot have more than 50 entries')
        for key, periods in v.items():
            if len(periods) > 20:
                raise ValueError(f'allowed_periods[{key}] cannot have more than 20 periods')
        return v

    @field_validator(
        'title', 'subtitle', 'footer_text', 'meta_title', 'meta_description', 'discount_badge_text', mode='before'
    )
    @classmethod
    def coerce_text_to_dict(cls, v: dict[str, str] | str | None) -> dict[str, str] | None:
        if v is None:
            return None
        return ensure_locale_dict(v)

    @field_validator('title')
    @classmethod
    def validate_title(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=500, field_name='title')

    @field_validator('subtitle')
    @classmethod
    def validate_subtitle(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=1000, field_name='subtitle')

    @field_validator('footer_text')
    @classmethod
    def validate_footer_text(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=5000, field_name='footer_text')

    @field_validator('meta_title')
    @classmethod
    def validate_meta_title(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=200, field_name='meta_title')

    @field_validator('meta_description')
    @classmethod
    def validate_meta_description(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=500, field_name='meta_description')

    @field_validator('discount_badge_text')
    @classmethod
    def validate_discount_badge_text(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_locale_dict(v, max_length=200, field_name='discount_badge_text')

    @field_validator('discount_starts_at', 'discount_ends_at', mode='after')
    @classmethod
    def ensure_aware_datetime(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v

    @model_validator(mode='after')
    def validate_discount(self) -> 'LandingUpdateRequest':
        if self.discount_starts_at is not None and self.discount_ends_at is not None:
            if self.discount_starts_at >= self.discount_ends_at:
                raise ValueError('discount_starts_at must be before discount_ends_at')
        if self.discount_overrides:
            if len(self.discount_overrides) > 100:
                raise ValueError('discount_overrides cannot have more than 100 entries')
            for key, val in self.discount_overrides.items():
                if not key.isdigit():
                    raise ValueError('discount_overrides keys must be tariff ID strings')
                if not (1 <= val <= 99):
                    raise ValueError('discount_overrides values must be 1-99')
        return self


class PurchaseStats(BaseModel):
    total: int = 0
    pending: int = 0
    paid: int = 0
    delivered: int = 0
    pending_activation: int = 0
    failed: int = 0
    expired: int = 0


class LandingListItem(BaseModel):
    id: int
    slug: str
    title: dict[str, str]
    is_active: bool
    display_order: int
    gift_enabled: bool
    tariff_count: int
    method_count: int
    purchase_stats: PurchaseStats
    has_active_discount: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator('title', mode='before')
    @classmethod
    def coerce_title(cls, v: dict[str, str] | str | None) -> dict[str, str]:
        return ensure_locale_dict(v)

    class Config:
        from_attributes = True


class LandingDetailResponse(BaseModel):
    id: int
    slug: str
    title: dict[str, str]
    subtitle: dict[str, str] | None = None
    is_active: bool
    display_order: int
    features: list[LandingFeatureInput]
    footer_text: dict[str, str] | None = None
    allowed_tariff_ids: list[int]
    allowed_periods: dict[str, list[int]]
    payment_methods: list[LandingPaymentMethodInput]
    gift_enabled: bool
    custom_css: str | None = None
    meta_title: dict[str, str] | None = None
    meta_description: dict[str, str] | None = None
    discount_percent: int | None = None
    discount_overrides: dict[str, int] | None = None
    discount_starts_at: datetime | None = None
    discount_ends_at: datetime | None = None
    discount_badge_text: dict[str, str] | None = None
    background_config: dict | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator(
        'title', 'subtitle', 'footer_text', 'meta_title', 'meta_description', 'discount_badge_text', mode='before'
    )
    @classmethod
    def coerce_to_dict(cls, v: dict[str, str] | str | None) -> dict[str, str] | None:
        if v is None:
            return None
        return ensure_locale_dict(v)

    class Config:
        from_attributes = True


class OrderRequest(BaseModel):
    landing_ids: list[int]


class LandingDailyStat(BaseModel):
    date: str  # YYYY-MM-DD
    created: int = 0
    purchases: int
    revenue_kopeks: int
    gifts: int


class LandingTariffStat(BaseModel):
    tariff_id: int | None
    tariff_name: str
    purchases: int
    revenue_kopeks: int


class LandingStatsResponse(BaseModel):
    # Summary
    total_purchases: int
    total_revenue_kopeks: int
    total_gifts: int
    total_regular: int
    avg_purchase_kopeks: int
    # Conversion: created -> paid/delivered
    total_created: int
    total_successful: int  # paid + delivered + pending_activation
    conversion_rate: float  # percent
    # Daily chart data (last 30 days)
    daily_stats: list[LandingDailyStat]
    # Tariff breakdown
    tariff_stats: list[LandingTariffStat]


class LandingPurchaseItem(BaseModel):
    id: int
    token: str
    contact_type: str
    contact_value: str
    is_gift: bool
    gift_recipient_type: str | None = None
    gift_recipient_value: str | None = None
    tariff_name: str | None = None
    period_days: int
    amount_kopeks: int
    currency: str
    payment_method: str | None = None
    status: str
    created_at: datetime | None = None
    paid_at: datetime | None = None


class LandingPurchaseListResponse(BaseModel):
    items: list[LandingPurchaseItem]
    total: int


# ============ Routes ============

# IMPORTANT: /order MUST come before /{landing_id} to avoid "order" being
# parsed as a landing_id path parameter.


@router.get('', response_model=list[LandingListItem])
async def list_landings(
    admin: User = Depends(require_permission('landings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List all landing pages with purchase stats."""
    landings = await get_all_landings(db)
    all_stats = await get_all_landing_purchase_stats(db)
    empty_stats = {
        'total': 0,
        'pending': 0,
        'paid': 0,
        'delivered': 0,
        'pending_activation': 0,
        'failed': 0,
        'expired': 0,
    }

    now = datetime.now(UTC)
    items = []
    for landing in landings:
        stats = all_stats.get(landing.id, empty_stats)
        discount_active = bool(
            landing.discount_percent
            and landing.discount_starts_at
            and landing.discount_ends_at
            and landing.discount_starts_at <= now < landing.discount_ends_at
        )
        items.append(
            LandingListItem(
                id=landing.id,
                slug=landing.slug,
                title=landing.title,
                is_active=landing.is_active,
                display_order=landing.display_order,
                gift_enabled=landing.gift_enabled,
                tariff_count=len(landing.allowed_tariff_ids or []),
                method_count=len(landing.payment_methods or []),
                purchase_stats=PurchaseStats(
                    total=stats.get('total', 0),
                    pending=stats.get('pending', 0),
                    paid=stats.get('paid', 0),
                    delivered=stats.get('delivered', 0),
                    pending_activation=stats.get('pending_activation', 0),
                    failed=stats.get('failed', 0),
                    expired=stats.get('expired', 0),
                ),
                has_active_discount=discount_active,
                created_at=landing.created_at,
                updated_at=landing.updated_at,
            )
        )
    return items


@router.post('', response_model=LandingDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_landing_page(
    request: LandingCreateRequest,
    admin: User = Depends(require_permission('landings:create')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new landing page."""
    if request.slug in _RESERVED_SLUGS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Slug "{request.slug}" is reserved and cannot be used',
        )

    existing = await get_landing_by_slug(db, request.slug)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Landing page with slug "{request.slug}" already exists',
        )

    landing = await create_landing(
        db,
        slug=request.slug,
        title=request.title,
        subtitle=request.subtitle,
        is_active=request.is_active,
        features=[f.model_dump() for f in request.features],
        footer_text=request.footer_text,
        allowed_tariff_ids=request.allowed_tariff_ids,
        allowed_periods=request.allowed_periods,
        payment_methods=[m.model_dump() for m in request.payment_methods],
        gift_enabled=request.gift_enabled,
        custom_css=request.custom_css,
        meta_title=request.meta_title,
        meta_description=request.meta_description,
        discount_percent=request.discount_percent,
        discount_overrides=request.discount_overrides,
        discount_starts_at=request.discount_starts_at,
        discount_ends_at=request.discount_ends_at,
        discount_badge_text=request.discount_badge_text,
        background_config=request.background_config,
    )

    logger.info('Admin created landing page', admin_id=admin.id, slug=landing.slug, landing_id=landing.id)

    return _landing_to_detail(landing)


@router.put('/order')
async def update_landings_order(
    request: OrderRequest,
    admin: User = Depends(require_permission('landings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Batch update display order for landing pages."""
    await update_landing_order(db, request.landing_ids)
    logger.info('Admin updated landing page order', admin_id=admin.id, landing_ids=request.landing_ids)
    return {'success': True}


@router.get('/{landing_id}', response_model=LandingDetailResponse)
async def get_landing_detail(
    landing_id: int,
    admin: User = Depends(require_permission('landings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get a single landing page with full details."""
    landing = await get_landing_by_id(db, landing_id)
    if landing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Landing page not found',
        )
    return _landing_to_detail(landing)


@router.put('/{landing_id}', response_model=LandingDetailResponse)
async def update_landing_page(
    landing_id: int,
    request: LandingUpdateRequest,
    admin: User = Depends(require_permission('landings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update a landing page."""
    data = request.model_dump(exclude_unset=True)

    # If slug is being changed, check reserved and uniqueness
    if 'slug' in data:
        if data['slug'] in _RESERVED_SLUGS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Slug "{data["slug"]}" is reserved and cannot be used',
            )
        existing = await get_landing_by_slug(db, data['slug'])
        if existing is not None and existing.id != landing_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'Landing page with slug "{data["slug"]}" already exists',
            )

    # Serialize nested Pydantic models to dicts for JSON storage
    if 'features' in data and data['features'] is not None:
        data['features'] = [f.model_dump() if hasattr(f, 'model_dump') else f for f in data['features']]
    if 'payment_methods' in data and data['payment_methods'] is not None:
        data['payment_methods'] = [m.model_dump() if hasattr(m, 'model_dump') else m for m in data['payment_methods']]

    # Cascade-clear all discount fields when discount_percent is explicitly set to None
    if 'discount_percent' in data and data['discount_percent'] is None:
        data['discount_overrides'] = None
        data['discount_starts_at'] = None
        data['discount_ends_at'] = None
        data['discount_badge_text'] = None

    # Validate merged discount dates on partial update
    if 'discount_starts_at' in data or 'discount_ends_at' in data:
        existing_landing = await get_landing_by_id(db, landing_id)
        if existing_landing is not None:
            effective_starts = data.get('discount_starts_at', existing_landing.discount_starts_at)
            effective_ends = data.get('discount_ends_at', existing_landing.discount_ends_at)
            if effective_starts and effective_ends and effective_starts >= effective_ends:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='discount_starts_at must be before discount_ends_at',
                )

    landing = await update_landing(db, landing_id, data)
    if landing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Landing page not found',
        )

    logger.info('Admin updated landing page', admin_id=admin.id, slug=landing.slug, landing_id=landing.id)

    return _landing_to_detail(landing)


@router.delete('/{landing_id}')
async def delete_landing_page(
    landing_id: int,
    admin: User = Depends(require_permission('landings:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a landing page."""
    deleted = await delete_landing(db, landing_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Landing page not found',
        )
    logger.info('Admin deleted landing page', admin_id=admin.id, landing_id=landing_id)
    return {'success': True}


@router.post('/{landing_id}/toggle', response_model=LandingDetailResponse)
async def toggle_landing_active(
    landing_id: int,
    admin: User = Depends(require_permission('landings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle active/inactive state of a landing page."""
    landing = await get_landing_by_id(db, landing_id)
    if landing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Landing page not found',
        )

    new_active = not landing.is_active
    landing = await update_landing(db, landing_id, {'is_active': new_active})

    logger.info(
        'Admin toggled landing page',
        admin_id=admin.id,
        landing_id=landing_id,
        is_active=new_active,
    )

    return _landing_to_detail(landing)


_SUCCESSFUL_STATUSES = (
    GuestPurchaseStatus.PAID.value,
    GuestPurchaseStatus.DELIVERED.value,
    GuestPurchaseStatus.PENDING_ACTIVATION.value,
)

_STATS_PERIOD_DAYS = 30


@router.get('/{landing_id}/stats', response_model=LandingStatsResponse)
async def get_landing_stats(
    landing_id: int,
    admin: User = Depends(require_permission('landings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> LandingStatsResponse:
    """Get daily statistics and tariff breakdown for a landing page."""
    landing = await get_landing_by_id(db, landing_id)
    if landing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Landing page not found',
        )

    # -- Summary stats (single query) --
    is_successful = GuestPurchase.status.in_(_SUCCESSFUL_STATUSES)
    summary_result = await db.execute(
        select(
            func.count(GuestPurchase.id).label('total_created'),
            func.count(case((is_successful, GuestPurchase.id))).label('total_successful'),
            func.coalesce(func.sum(case((is_successful, GuestPurchase.amount_kopeks))), 0).label(
                'total_revenue_kopeks'
            ),
            func.count(case((and_(is_successful, GuestPurchase.is_gift.is_(True)), GuestPurchase.id))).label(
                'total_gifts'
            ),
        ).where(GuestPurchase.landing_id == landing_id)
    )
    row = summary_result.one()
    total_created: int = row.total_created
    total_successful: int = row.total_successful
    total_revenue_kopeks: int = row.total_revenue_kopeks
    total_gifts: int = row.total_gifts
    total_regular = total_successful - total_gifts
    avg_purchase_kopeks = total_revenue_kopeks // total_successful if total_successful > 0 else 0
    conversion_rate = round(total_successful / total_created * 100, 1) if total_created > 0 else 0.0

    # -- Daily stats for last N days --
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=_STATS_PERIOD_DAYS)
    day_at_utc = func.date(func.timezone('UTC', GuestPurchase.paid_at))
    daily_result = await db.execute(
        select(
            day_at_utc.label('day'),
            func.count(GuestPurchase.id).label('purchases'),
            func.coalesce(func.sum(GuestPurchase.amount_kopeks), 0).label('revenue_kopeks'),
            func.count(case((GuestPurchase.is_gift.is_(True), GuestPurchase.id))).label('gifts'),
        )
        .where(
            GuestPurchase.landing_id == landing_id,
            is_successful,
            GuestPurchase.paid_at >= cutoff,
        )
        .group_by(day_at_utc)
        .order_by(day_at_utc)
    )
    daily_rows = {str(r.day): r for r in daily_result.all()}

    # Created per day (all statuses, by created_at)
    day_created_utc = func.date(func.timezone('UTC', GuestPurchase.created_at))
    created_result = await db.execute(
        select(
            day_created_utc.label('day'),
            func.count(GuestPurchase.id).label('created'),
        )
        .where(
            GuestPurchase.landing_id == landing_id,
            GuestPurchase.created_at >= cutoff,
        )
        .group_by(day_created_utc)
        .order_by(day_created_utc)
    )
    created_rows = {str(r.day): r.created for r in created_result.all()}

    # Fill missing days with zeros
    today = now.date()
    daily_stats: list[LandingDailyStat] = []
    for i in range(_STATS_PERIOD_DAYS, -1, -1):
        day = today - timedelta(days=i)
        day_str = day.isoformat()
        day_created = created_rows.get(day_str, 0)
        if day_str in daily_rows:
            r = daily_rows[day_str]
            daily_stats.append(
                LandingDailyStat(
                    date=day_str,
                    created=day_created,
                    purchases=r.purchases,
                    revenue_kopeks=r.revenue_kopeks,
                    gifts=r.gifts,
                )
            )
        else:
            daily_stats.append(
                LandingDailyStat(
                    date=day_str,
                    created=day_created,
                    purchases=0,
                    revenue_kopeks=0,
                    gifts=0,
                )
            )

    # -- Tariff breakdown --
    tariff_result = await db.execute(
        select(
            GuestPurchase.tariff_id,
            func.coalesce(Tariff.name, 'Unknown').label('tariff_name'),
            func.count(GuestPurchase.id).label('purchases'),
            func.coalesce(func.sum(GuestPurchase.amount_kopeks), 0).label('revenue_kopeks'),
        )
        .outerjoin(Tariff, GuestPurchase.tariff_id == Tariff.id)
        .where(
            GuestPurchase.landing_id == landing_id,
            is_successful,
        )
        .group_by(GuestPurchase.tariff_id, Tariff.name)
        .order_by(func.coalesce(func.sum(GuestPurchase.amount_kopeks), 0).desc())
    )
    tariff_stats = [
        LandingTariffStat(
            tariff_id=r.tariff_id,
            tariff_name=r.tariff_name,
            purchases=r.purchases,
            revenue_kopeks=r.revenue_kopeks,
        )
        for r in tariff_result.all()
    ]

    return LandingStatsResponse(
        total_purchases=total_created,
        total_revenue_kopeks=total_revenue_kopeks,
        total_gifts=total_gifts,
        total_regular=total_regular,
        avg_purchase_kopeks=avg_purchase_kopeks,
        total_created=total_created,
        total_successful=total_successful,
        conversion_rate=conversion_rate,
        daily_stats=daily_stats,
        tariff_stats=tariff_stats,
    )


_PURCHASE_LIST_MAX_LIMIT = 100
_PURCHASE_LIST_DEFAULT_LIMIT = 20
_PURCHASE_TOKEN_VISIBLE_CHARS = 8


@router.get('/{landing_id}/purchases', response_model=LandingPurchaseListResponse)
async def get_landing_purchases(
    landing_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=_PURCHASE_LIST_DEFAULT_LIMIT, ge=1, le=_PURCHASE_LIST_MAX_LIMIT),
    status_filter: GuestPurchaseStatus | None = Query(default=None, alias='status'),
    admin: User = Depends(require_permission('landings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> LandingPurchaseListResponse:
    """Get paginated list of purchases for a landing page."""
    landing = await get_landing_by_id(db, landing_id)
    if landing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Landing page not found',
        )

    # Base filter conditions
    conditions = [GuestPurchase.landing_id == landing_id]
    if status_filter is not None:
        conditions.append(GuestPurchase.status == status_filter.value)

    where_clause = and_(*conditions)

    # Total count
    count_result = await db.execute(select(func.count(GuestPurchase.id)).select_from(GuestPurchase).where(where_clause))
    total: int = count_result.scalar_one()

    # Fetch page with tariff name join
    items_result = await db.execute(
        select(
            GuestPurchase.id,
            GuestPurchase.token,
            GuestPurchase.contact_type,
            GuestPurchase.contact_value,
            GuestPurchase.is_gift,
            GuestPurchase.gift_recipient_type,
            GuestPurchase.gift_recipient_value,
            func.coalesce(Tariff.name, 'Unknown').label('tariff_name'),
            GuestPurchase.period_days,
            GuestPurchase.amount_kopeks,
            GuestPurchase.currency,
            GuestPurchase.payment_method,
            GuestPurchase.status,
            GuestPurchase.created_at,
            GuestPurchase.paid_at,
        )
        .outerjoin(Tariff, GuestPurchase.tariff_id == Tariff.id)
        .where(where_clause)
        .order_by(GuestPurchase.created_at.desc())
        .offset(offset)
        .limit(limit)
    )

    items = [
        LandingPurchaseItem(
            id=row.id,
            token=(row.token[:_PURCHASE_TOKEN_VISIBLE_CHARS] + '...') if row.token else '???',
            contact_type=row.contact_type,
            contact_value=row.contact_value,
            is_gift=row.is_gift,
            gift_recipient_type=row.gift_recipient_type,
            gift_recipient_value=row.gift_recipient_value,
            tariff_name=row.tariff_name,
            period_days=row.period_days,
            amount_kopeks=row.amount_kopeks,
            currency=row.currency,
            payment_method=row.payment_method,
            status=row.status,
            created_at=row.created_at,
            paid_at=row.paid_at,
        )
        for row in items_result.all()
    ]

    return LandingPurchaseListResponse(items=items, total=total)


# ============ Helpers ============


def _landing_to_detail(landing: LandingPage) -> LandingDetailResponse:
    """Convert a LandingPage model to LandingDetailResponse.

    Admin detail view returns full locale dicts for all text fields.
    """
    features = [
        LandingFeatureInput(
            icon=f.get('icon', ''),
            title=f.get('title', {}),
            description=f.get('description', {}),
        )
        for f in (landing.features or [])
    ]

    payment_methods = [
        LandingPaymentMethodInput(
            method_id=m.get('method_id', ''),
            display_name=m.get('display_name', ''),
            description=m.get('description'),
            icon_url=m.get('icon_url'),
            sort_order=m.get('sort_order', 0),
            min_amount_kopeks=m.get('min_amount_kopeks'),
            max_amount_kopeks=m.get('max_amount_kopeks'),
            currency=m.get('currency'),
            return_url=m.get('return_url'),
            sub_options=m.get('sub_options'),
        )
        for m in (landing.payment_methods or [])
    ]

    return LandingDetailResponse(
        id=landing.id,
        slug=landing.slug,
        title=landing.title or {},
        subtitle=landing.subtitle,
        is_active=landing.is_active,
        display_order=landing.display_order,
        features=features,
        footer_text=landing.footer_text,
        allowed_tariff_ids=landing.allowed_tariff_ids or [],
        allowed_periods=landing.allowed_periods or {},
        payment_methods=payment_methods,
        gift_enabled=landing.gift_enabled,
        custom_css=landing.custom_css,
        meta_title=landing.meta_title,
        meta_description=landing.meta_description,
        discount_percent=landing.discount_percent,
        discount_overrides=landing.discount_overrides,
        discount_starts_at=landing.discount_starts_at,
        discount_ends_at=landing.discount_ends_at,
        discount_badge_text=landing.discount_badge_text,
        background_config=landing.background_config,
        created_at=landing.created_at,
        updated_at=landing.updated_at,
    )
