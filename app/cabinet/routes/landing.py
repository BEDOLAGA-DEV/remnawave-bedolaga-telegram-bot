"""Public landing page routes for guest quick-purchase flow."""

import re
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.ip_utils import get_client_ip
from app.cabinet.utils.locale import DEFAULT_LOCALE, resolve_locale_text
from app.config import settings
from app.database.crud.landing import get_active_landing_by_slug, get_purchase_by_token
from app.database.models import GuestPurchase, GuestPurchaseStatus, LandingPage, Tariff
from app.services.guest_purchase_service import (
    GuestPurchaseError,
    activate_purchase as activate_guest_purchase,
    create_purchase,
    validate_and_calculate,
)
from app.services.payment_method_config_service import _get_method_defaults
from app.services.payment_service import PaymentService
from app.utils.cache import RateLimitCache


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/landing', tags=['Landing Pages'])


# ============ Schemas ============


class LandingFeature(BaseModel):
    icon: str = ''
    title: str = ''
    description: str = ''


class LandingTariffPeriod(BaseModel):
    days: int
    label: str
    price_kopeks: int
    price_label: str
    original_price_kopeks: int | None = None  # set if discount active
    original_price_label: str | None = None
    discount_percent: int | None = None  # effective discount for this tariff


class LandingTariff(BaseModel):
    id: int
    name: str
    description: str | None = None
    traffic_limit_gb: int
    device_limit: int
    tier_level: int
    periods: list[LandingTariffPeriod]


class LandingPaymentMethodSubOption(BaseModel):
    id: str
    name: str


class LandingPaymentMethod(BaseModel):
    method_id: str
    display_name: str
    description: str | None = None
    icon_url: str | None = None
    sort_order: int = 0
    min_amount_kopeks: int | None = None
    max_amount_kopeks: int | None = None
    currency: str | None = None
    # Enabled sub-options with display labels (e.g. СБП, Карта).
    # None or empty means no sub-option selection needed.
    sub_options: list[LandingPaymentMethodSubOption] | None = None


class LandingDiscountInfo(BaseModel):
    percent: int  # default discount
    ends_at: str  # ISO datetime
    badge_text: str | None = None  # resolved locale text


class LandingConfigResponse(BaseModel):
    slug: str
    title: str
    subtitle: str | None = None
    features: list[LandingFeature]
    footer_text: str | None = None
    tariffs: list[LandingTariff]
    payment_methods: list[LandingPaymentMethod]
    gift_enabled: bool
    custom_css: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    discount: LandingDiscountInfo | None = None  # null if no active discount
    background_config: dict | None = None


_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')
_TELEGRAM_RE = re.compile(r'^@?[a-zA-Z][a-zA-Z0-9_]{4,31}$')


def _validate_contact(contact_type: str, contact_value: str) -> None:
    """Validate contact value matches the declared type format."""
    if contact_type == 'email' and not _EMAIL_RE.match(contact_value):
        raise ValueError('Invalid email format')
    if contact_type == 'telegram' and not _TELEGRAM_RE.match(contact_value):
        raise ValueError('Invalid Telegram username format')


class PurchaseRequest(BaseModel):
    tariff_id: int
    period_days: int
    contact_type: str = Field(pattern=r'^(email|telegram)$')
    contact_value: str = Field(min_length=1, max_length=255)
    payment_method: str = Field(min_length=1, max_length=50, pattern=r'^[a-z0-9_]+$')
    is_gift: bool = False
    gift_recipient_type: str | None = Field(default=None, pattern=r'^(email|telegram)$')
    gift_recipient_value: str | None = Field(default=None, max_length=255)
    gift_message: str | None = Field(default=None, max_length=1000)
    yandex_cid: str | None = Field(default=None, max_length=128)

    @model_validator(mode='after')
    def validate_contacts(self) -> 'PurchaseRequest':
        _validate_contact(self.contact_type, self.contact_value)
        if self.is_gift:
            if not self.gift_recipient_type or not self.gift_recipient_value:
                raise ValueError('Gift recipient type and value are required for gift purchases')
            _validate_contact(self.gift_recipient_type, self.gift_recipient_value)
        return self


class PurchaseResponse(BaseModel):
    purchase_token: str
    payment_url: str


class PurchaseStatusResponse(BaseModel):
    status: str
    subscription_url: str | None = None
    subscription_crypto_link: str | None = None
    is_gift: bool = False
    contact_value: str | None = None
    recipient_contact_value: str | None = None
    period_days: int | None = None
    tariff_name: str | None = None
    gift_message: str | None = None
    contact_type: str | None = None
    cabinet_email: str | None = None
    cabinet_password: str | None = None
    auto_login_token: str | None = None
    recipient_in_bot: bool | None = None
    bot_link: str | None = None


# ============ Helpers ============


def _mask_contact(value: str) -> str:
    """Mask contact value to avoid leaking PII in API responses."""
    if '@' in value and not value.startswith('@'):
        # Email: show first 2 chars + mask + domain
        local, domain = value.rsplit('@', 1)
        return f'{local[:2]}***@{domain}'
    if value.startswith('@'):
        # Telegram: show first 3 chars + mask
        return f'{value[:3]}***'
    return value[:3] + '***'


_SUBSCRIPTION_URL_EXPIRY_HOURS = 24


def _build_purchase_status_response(purchase: GuestPurchase) -> PurchaseStatusResponse:
    """Build a PurchaseStatusResponse from a GuestPurchase record."""
    tariff_name = purchase.tariff.name if purchase.tariff else None

    within_ttl = False
    subscription_url = None
    subscription_crypto_link = None
    if purchase.delivered_at and purchase.subscription_url and not purchase.is_gift:
        age = datetime.now(UTC) - purchase.delivered_at
        if age < timedelta(hours=_SUBSCRIPTION_URL_EXPIRY_HOURS):
            within_ttl = True
            subscription_url = purchase.subscription_url
            subscription_crypto_link = purchase.subscription_crypto_link

    masked_contact = _mask_contact(purchase.contact_value) if purchase.contact_value else None

    recipient_contact_value = None
    gift_message = None
    if purchase.is_gift:
        if purchase.gift_recipient_value:
            recipient_contact_value = _mask_contact(purchase.gift_recipient_value)
        gift_message = purchase.gift_message

    # Determine effective contact type for the recipient
    if purchase.is_gift and purchase.gift_recipient_type:
        effective_contact_type = purchase.gift_recipient_type
    else:
        effective_contact_type = purchase.contact_type

    # Cabinet credentials for email self-purchases (not gifts)
    cabinet_email = None
    cabinet_password = None
    auto_login_token = None
    is_terminal = purchase.status in (GuestPurchaseStatus.DELIVERED.value, GuestPurchaseStatus.PENDING_ACTIVATION.value)
    is_email_self_purchase = effective_contact_type == 'email' and not purchase.is_gift

    if is_terminal and is_email_self_purchase:
        cabinet_email = purchase.contact_value
        # For PENDING_ACTIVATION: cap credential exposure at 72h from paid_at
        pending_within_ttl = (
            purchase.status == GuestPurchaseStatus.PENDING_ACTIVATION.value
            and purchase.paid_at
            and (datetime.now(UTC) - purchase.paid_at) < timedelta(hours=72)
        )
        if within_ttl or pending_within_ttl:
            cabinet_password = purchase.cabinet_password
            auto_login_token = purchase.auto_login_token

    # For telegram gifts: indicate whether recipient is known to the bot
    recipient_in_bot: bool | None = None
    bot_link: str | None = None
    if purchase.is_gift and effective_contact_type == 'telegram':
        recipient_in_bot = purchase.user is not None and purchase.user.telegram_id is not None
        if not recipient_in_bot:
            bot_username = settings.get_bot_username()
            if bot_username:
                bot_link = f'https://t.me/{bot_username}'

    return PurchaseStatusResponse(
        status=purchase.status,
        subscription_url=subscription_url,
        subscription_crypto_link=subscription_crypto_link,
        is_gift=purchase.is_gift,
        contact_value=masked_contact,
        recipient_contact_value=recipient_contact_value,
        period_days=purchase.period_days,
        tariff_name=tariff_name,
        gift_message=gift_message,
        contact_type=effective_contact_type,
        cabinet_email=cabinet_email,
        cabinet_password=cabinet_password,
        auto_login_token=auto_login_token,
        recipient_in_bot=recipient_in_bot,
        bot_link=bot_link,
    )


def _period_label(days: int) -> str:
    """Human-readable label for a period in days."""
    if days == 1:
        return '1 day'
    if days <= 6:
        return f'{days} days'
    if days == 7:
        return '1 week'
    if days == 14:
        return '2 weeks'
    if days == 30:
        return '1 month'
    if days == 60:
        return '2 months'
    if days == 90:
        return '3 months'
    if days == 180:
        return '6 months'
    if days == 365:
        return '1 year'
    if days == 456:
        return '1 year + 3 mo.'

    months = days // 30
    remainder = days % 30
    if months > 0 and remainder == 0:
        return f'{months} mo.'
    if months > 0:
        return f'{months} mo. + {remainder} d.'
    return f'{days} days'


def _get_active_discount(landing: LandingPage, lang: str) -> LandingDiscountInfo | None:
    """Return discount info if currently active, else None."""
    if not landing.discount_percent or not landing.discount_starts_at or not landing.discount_ends_at:
        return None
    now = datetime.now(UTC)
    if not (landing.discount_starts_at <= now < landing.discount_ends_at):
        return None
    badge = resolve_locale_text(landing.discount_badge_text, lang) if landing.discount_badge_text else None
    return LandingDiscountInfo(
        percent=landing.discount_percent,
        ends_at=landing.discount_ends_at.isoformat(),
        badge_text=badge or None,
    )


async def _load_landing_tariffs(
    db: AsyncSession, landing: LandingPage, discount: LandingDiscountInfo | None = None
) -> list[LandingTariff]:
    """Load tariffs for a landing page, filtered by allowed IDs and periods."""
    allowed_ids = landing.allowed_tariff_ids or []
    if not allowed_ids:
        return []

    result = await db.execute(
        select(Tariff)
        .where(Tariff.id.in_(allowed_ids), Tariff.is_active.is_(True))
        .order_by(Tariff.display_order, Tariff.id)
    )
    tariffs = result.scalars().all()

    allowed_periods = landing.allowed_periods or {}
    landing_tariffs = []

    for tariff in tariffs:
        # Determine which periods to show
        tariff_period_override = allowed_periods.get(str(tariff.id))
        if tariff_period_override is not None:
            period_days_list = sorted(tariff_period_override)
        else:
            period_days_list = tariff.get_available_periods()

        periods = []
        for days in period_days_list:
            price = tariff.get_price_for_period(days)
            if price is None:
                continue

            original_price_kopeks = None
            original_price_label = None
            effective_discount = None

            if discount:
                # Per-tariff override takes priority (read from landing model, not response DTO)
                overrides = landing.discount_overrides or {}
                tariff_override = overrides.get(str(tariff.id))
                effective_discount = tariff_override if tariff_override is not None else discount.percent
                original_price_kopeks = price
                original_price_label = settings.format_price(price)
                from app.services.pricing_engine import PricingEngine

                price = max(1, PricingEngine.apply_discount(price, effective_discount))

            periods.append(
                LandingTariffPeriod(
                    days=days,
                    label=_period_label(days),
                    price_kopeks=price,
                    price_label=settings.format_price(price),
                    original_price_kopeks=original_price_kopeks,
                    original_price_label=original_price_label,
                    discount_percent=effective_discount,
                )
            )

        if not periods:
            continue

        landing_tariffs.append(
            LandingTariff(
                id=tariff.id,
                name=tariff.name,
                description=tariff.description,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                tier_level=tariff.tier_level,
                periods=periods,
            )
        )

    return landing_tariffs


# ============ Routes ============

# IMPORTANT: /purchase/{token} must come BEFORE /{slug} to avoid shadowing
# (FastAPI checks routes in definition order; "purchase" would match {slug})


@router.get('/purchase/{token}', response_model=PurchaseStatusResponse)
async def get_purchase_status(
    token: str,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get the status of a guest purchase by token.

    No authentication required.
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'purchase_status', limit=30, window=60, fail_closed=True):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many requests')

    purchase = await get_purchase_by_token(db, token)
    if purchase is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Purchase not found',
        )

    response = _build_purchase_status_response(purchase)

    # Cleanup: null expired credentials from DB
    needs_cleanup = False
    if purchase.delivered_at and (purchase.cabinet_password or purchase.auto_login_token):
        age = datetime.now(UTC) - purchase.delivered_at
        if age >= timedelta(hours=_SUBSCRIPTION_URL_EXPIRY_HOURS):
            needs_cleanup = True
    elif (
        purchase.status == GuestPurchaseStatus.PENDING_ACTIVATION.value
        and purchase.paid_at
        and (purchase.cabinet_password or purchase.auto_login_token)
        and (datetime.now(UTC) - purchase.paid_at) >= timedelta(hours=72)
    ):
        needs_cleanup = True

    if needs_cleanup:
        purchase.cabinet_password = None
        purchase.auto_login_token = None
        await db.commit()

    return response


@router.post('/activate/{token}', response_model=PurchaseStatusResponse)
async def activate_purchase(
    token: str,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Activate a pending guest purchase, replacing the user's current subscription.

    No authentication required (token is the secret).
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'activate_purchase', limit=5, window=60, fail_closed=True):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many requests')

    try:
        purchase = await activate_guest_purchase(db, token)
    except GuestPurchaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return _build_purchase_status_response(purchase)


@router.get('/{slug}', response_model=LandingConfigResponse)
async def get_landing_config(
    raw_request: Request,
    slug: str = Path(max_length=100),
    lang: str = Query(DEFAULT_LOCALE, max_length=5, description='Locale: ru, en, zh, fa'),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get public landing page configuration with tariffs and payment methods.

    No authentication required. Pass ``?lang=en`` to get localized text.
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'landing_config', limit=60, window=60, fail_closed=True):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many requests')

    landing = await get_active_landing_by_slug(db, slug)
    if landing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Landing page not found',
        )

    discount = _get_active_discount(landing, lang)
    tariffs = await _load_landing_tariffs(db, landing, discount)

    # Build payment methods from landing config
    raw_methods = landing.payment_methods or []
    method_defaults = _get_method_defaults()

    payment_methods: list[LandingPaymentMethod] = []
    for m in raw_methods:
        method_id = m.get('method_id', '')
        raw_sub_options = m.get('sub_options')  # dict[str, bool] | None

        # Resolve sub-options: filter enabled ones and attach display names
        resolved_sub_options: list[LandingPaymentMethodSubOption] | None = None
        method_def = method_defaults.get(method_id)
        available = method_def.get('available_sub_options') if method_def else None
        if available:
            resolved = []
            for opt in available:
                opt_id = opt['id']
                # If landing has explicit sub_options config, respect it; otherwise all enabled
                if raw_sub_options is None or raw_sub_options.get(opt_id, True):
                    resolved.append(LandingPaymentMethodSubOption(id=opt_id, name=opt['name']))
            if resolved:
                resolved_sub_options = resolved

        payment_methods.append(
            LandingPaymentMethod(
                method_id=method_id,
                display_name=m.get('display_name', ''),
                description=m.get('description'),
                icon_url=m.get('icon_url'),
                sort_order=m.get('sort_order', 0),
                min_amount_kopeks=m.get('min_amount_kopeks'),
                max_amount_kopeks=m.get('max_amount_kopeks'),
                currency=m.get('currency'),
                sub_options=resolved_sub_options,
            )
        )

    # Resolve locale dicts to flat strings for the requested language
    features = [
        LandingFeature(
            icon=f.get('icon', ''),
            title=resolve_locale_text(f.get('title'), lang),
            description=resolve_locale_text(f.get('description'), lang),
        )
        for f in (landing.features or [])
    ]

    return LandingConfigResponse(
        slug=landing.slug,
        title=resolve_locale_text(landing.title, lang),
        subtitle=resolve_locale_text(landing.subtitle, lang) or None,
        features=features,
        footer_text=resolve_locale_text(landing.footer_text, lang) or None,
        tariffs=tariffs,
        payment_methods=payment_methods,
        gift_enabled=landing.gift_enabled,
        custom_css=landing.custom_css,
        meta_title=resolve_locale_text(landing.meta_title, lang) or None,
        meta_description=resolve_locale_text(landing.meta_description, lang) or None,
        discount=discount,
        background_config=landing.background_config,
    )


@router.post('/{slug}/purchase', response_model=PurchaseResponse)
async def create_landing_purchase(
    slug: str,
    body: PurchaseRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a guest purchase on a landing page.

    No authentication required.
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'landing_purchase', limit=30, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many purchase attempts, please try again later',
        )

    landing = await get_active_landing_by_slug(db, slug)
    if landing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Landing page not found',
        )

    if body.is_gift and not landing.gift_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Gift purchases are not enabled for this landing page',
        )

    # Validate payment method is available on this landing.
    # The frontend may send a suffixed method ID (e.g. "platega_2", "yookassa_sbp")
    # to select a specific sub-option. We match against the base method_id and
    # validate the suffix against known & enabled sub-options.
    raw_methods = landing.payment_methods or []
    method_defaults = _get_method_defaults()

    method_config = next((m for m in raw_methods if m.get('method_id') == body.payment_method), None)
    if method_config is None:
        # Try matching by prefix: "platega_2" → base "platega"
        # Sort by length descending so "freekassa_sbp" is checked before "freekassa"
        sorted_methods = sorted(raw_methods, key=lambda m: len(m.get('method_id', '')), reverse=True)
        for m in sorted_methods:
            mid = m.get('method_id', '')
            if body.payment_method.startswith(mid + '_'):
                suffix = body.payment_method[len(mid) + 1 :]
                # Validate suffix is a known sub-option
                method_def = method_defaults.get(mid)
                available = (method_def.get('available_sub_options') if method_def else None) or []
                valid_ids = {opt['id'] for opt in available}
                if suffix not in valid_ids:
                    break  # invalid suffix → reject
                # Validate suffix is enabled on this landing
                raw_sub_options = m.get('sub_options')  # dict[str, bool] | None
                if raw_sub_options is not None and not raw_sub_options.get(suffix, True):
                    break  # disabled sub-option → reject
                method_config = m
                break
    if method_config is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Payment method is not available on this landing page',
        )

    # Validate tariff + period + calculate price
    try:
        tariff, amount_kopeks = await validate_and_calculate(db, landing, body.tariff_id, body.period_days)
    except GuestPurchaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    # Gift purchases require the tariff to be visible in the gift section
    if body.is_gift and not tariff.show_in_gift:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This tariff is not available for gift purchases',
        )

    # Validate amount against per-method min/max limits (before creating purchase record)
    min_amount = method_config.get('min_amount_kopeks')
    max_amount = method_config.get('max_amount_kopeks')
    if min_amount is not None and amount_kopeks < min_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Amount is below the minimum ({settings.format_price(min_amount)}) for this payment method',
        )
    if max_amount is not None and amount_kopeks > max_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Amount exceeds the maximum ({settings.format_price(max_amount)}) for this payment method',
        )

    # Create purchase record (no commit yet — wait for payment creation)
    purchase = await create_purchase(
        db,
        landing=landing,
        tariff=tariff,
        period_days=body.period_days,
        amount_kopeks=amount_kopeks,
        contact_type=body.contact_type,
        contact_value=body.contact_value,
        payment_method=body.payment_method,
        is_gift=body.is_gift,
        gift_recipient_type=body.gift_recipient_type,
        gift_recipient_value=body.gift_recipient_value,
        gift_message=body.gift_message,
        yandex_cid=body.yandex_cid,
        commit=False,
    )

    # Determine return URL: per-method override → default cabinet URL
    cabinet_base = (settings.CABINET_URL or '').rstrip('/')
    default_return_url = f'{cabinet_base}/buy/success/{purchase.token}'
    method_return_url = method_config.get('return_url')
    if method_return_url:
        # Allow {token} placeholder in custom return URLs
        return_url = method_return_url.replace('{token}', purchase.token)
    else:
        return_url = default_return_url

    payment_service = PaymentService()
    payment_result = await payment_service.create_guest_payment(
        db=db,
        amount_kopeks=amount_kopeks,
        payment_method=body.payment_method,
        description=f'{tariff.name} — {body.period_days}d',
        purchase_token=purchase.token,
        return_url=return_url,
    )

    if payment_result is None:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Payment provider is unavailable, please try again later',
        )

    payment_url = payment_result.get('payment_url')
    if not payment_url:
        await db.rollback()
        logger.error(
            'Payment created but no payment_url returned',
            purchase_token=purchase.token[:5],
            provider=payment_result.get('provider'),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Payment provider returned an invalid response',
        )

    await db.commit()
    await db.refresh(purchase)

    return PurchaseResponse(
        purchase_token=purchase.token,
        payment_url=payment_url,
    )
