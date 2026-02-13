"""Service for managing payment method display configurations in cabinet."""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import PaymentMethodConfig, PromoGroup


logger = logging.getLogger(__name__)


# ============ Default method definitions ============


# Mapping: method_id -> (default_display_name_func, is_configured_func, default_min, default_max, has_sub_options)
def _get_method_defaults() -> dict:
    """Get default configuration for each payment method based on env vars."""
    return {
        'telegram_stars': {
            'default_display_name': settings.get_telegram_stars_display_name(),
            'is_configured': settings.TELEGRAM_STARS_ENABLED,
            'default_min': 100,
            'default_max': 1000000,
            'available_sub_options': None,
        },
        'tribute': {
            'default_display_name': 'Tribute',
            'is_configured': settings.TRIBUTE_ENABLED and bool(getattr(settings, 'TRIBUTE_DONATE_LINK', '')),
            'default_min': 10000,
            'default_max': 10000000,
            'available_sub_options': None,
        },
        'cryptobot': {
            'default_display_name': settings.get_cryptobot_display_name(),
            'is_configured': settings.is_cryptobot_enabled(),
            'default_min': 1000,
            'default_max': 10000000,
            'available_sub_options': None,
        },
        'heleket': {
            'default_display_name': settings.get_heleket_display_name(),
            'is_configured': settings.is_heleket_enabled(),
            'default_min': 1000,
            'default_max': 10000000,
            'available_sub_options': None,
        },
        'yookassa': {
            'default_display_name': settings.get_yookassa_display_name(),
            'is_configured': settings.is_yookassa_enabled(),
            'default_min': settings.YOOKASSA_MIN_AMOUNT_KOPEKS,
            'default_max': settings.YOOKASSA_MAX_AMOUNT_KOPEKS,
            'available_sub_options': [
                {'id': 'card', 'name': 'Карта'},
                {'id': 'sbp', 'name': 'СБП'},
            ],
        },
        'mulenpay': {
            'default_display_name': settings.get_mulenpay_display_name(),
            'is_configured': settings.is_mulenpay_enabled(),
            'default_min': settings.MULENPAY_MIN_AMOUNT_KOPEKS,
            'default_max': settings.MULENPAY_MAX_AMOUNT_KOPEKS,
            'available_sub_options': None,
        },
        'pal24': {
            'default_display_name': settings.get_pal24_display_name(),
            'is_configured': settings.is_pal24_enabled(),
            'default_min': settings.PAL24_MIN_AMOUNT_KOPEKS,
            'default_max': settings.PAL24_MAX_AMOUNT_KOPEKS,
            'available_sub_options': [
                {'id': 'sbp', 'name': 'СБП'},
                {'id': 'card', 'name': 'Карта'},
            ],
        },
        'platega': {
            'default_display_name': settings.get_platega_display_name(),
            'is_configured': settings.is_platega_enabled(),
            'default_min': settings.PLATEGA_MIN_AMOUNT_KOPEKS,
            'default_max': settings.PLATEGA_MAX_AMOUNT_KOPEKS,
            'available_sub_options': _get_platega_sub_options(),
        },
        'wata': {
            'default_display_name': settings.get_wata_display_name(),
            'is_configured': settings.is_wata_enabled(),
            'default_min': settings.WATA_MIN_AMOUNT_KOPEKS,
            'default_max': settings.WATA_MAX_AMOUNT_KOPEKS,
            'available_sub_options': None,
        },
        'freekassa': {
            'default_display_name': settings.get_freekassa_display_name(),
            'is_configured': settings.is_freekassa_enabled(),
            'default_min': settings.FREEKASSA_MIN_AMOUNT_KOPEKS,
            'default_max': settings.FREEKASSA_MAX_AMOUNT_KOPEKS,
            'available_sub_options': [
                {'id': 'sbp', 'name': 'NSPK СБП'},
                {'id': 'card', 'name': 'Карта'},
            ],
        },
        'cloudpayments': {
            'default_display_name': settings.get_cloudpayments_display_name(),
            'is_configured': settings.is_cloudpayments_enabled(),
            'default_min': settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS,
            'default_max': settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS,
            'available_sub_options': [
                {'id': 'card', 'name': 'Карта'},
                {'id': 'sbp', 'name': 'СБП'},
            ],
        },
        'kassa_ai': {
            'default_display_name': settings.get_kassa_ai_display_name(),
            'is_configured': settings.is_kassa_ai_enabled(),
            'default_min': settings.KASSA_AI_MIN_AMOUNT_KOPEKS,
            'default_max': settings.KASSA_AI_MAX_AMOUNT_KOPEKS,
            'available_sub_options': None,
        },
    }


def _get_platega_sub_options() -> list[dict] | None:
    """Get available Platega sub-options from config."""
    try:
        active_methods = settings.get_platega_active_methods()
        definitions = settings.get_platega_method_definitions()
        if not active_methods:
            return None
        options = []
        for method_code in active_methods:
            info = definitions.get(method_code, {})
            options.append(
                {
                    'id': str(method_code),
                    'name': info.get('title') or info.get('name') or f'Platega {method_code}',
                }
            )
        return options if options else None
    except Exception:
        return None


# Default order of methods
DEFAULT_METHOD_ORDER = [
    'telegram_stars',
    'tribute',
    'cryptobot',
    'heleket',
    'yookassa',
    'mulenpay',
    'pal24',
    'platega',
    'wata',
    'freekassa',
    'cloudpayments',
    'kassa_ai',
]


# ============ Initialization ============


async def ensure_payment_method_configs(db: AsyncSession) -> None:
    """Initialize payment method configs if they don't exist yet.

    Called on startup to seed defaults from env vars.
    Also adds any missing methods that were added after initial setup.
    """
    # Get existing method IDs
    existing_result = await db.execute(select(PaymentMethodConfig.method_id))
    existing_method_ids = set(existing_result.scalars().all())

    if not existing_method_ids:
        # First-time initialization
        logger.info('Initializing payment method configurations from env vars...')
        defaults = _get_method_defaults()

        for idx, method_id in enumerate(DEFAULT_METHOD_ORDER):
            method_def = defaults.get(method_id, {})
            is_configured = method_def.get('is_configured', False)
            sub_options = None
            available = method_def.get('available_sub_options')
            if available:
                # Enable all sub-options by default
                sub_options = {opt['id']: True for opt in available}

            config = PaymentMethodConfig(
                method_id=method_id,
                sort_order=idx,
                is_enabled=is_configured,
                display_name=None,
                sub_options=sub_options,
                min_amount_kopeks=None,
                max_amount_kopeks=None,
                user_type_filter='all',
                first_topup_filter='any',
                promo_group_filter_mode='all',
            )
            db.add(config)

        await db.commit()
        logger.info(f'Payment method configurations initialized ({len(DEFAULT_METHOD_ORDER)} methods).')
        return

    # Add missing methods (for cases when new methods are added to code)
    defaults = _get_method_defaults()
    missing_methods = [m for m in DEFAULT_METHOD_ORDER if m not in existing_method_ids]

    if missing_methods:
        logger.info(f'Adding missing payment methods: {missing_methods}')
        # Get max sort_order to append new methods at the end
        max_order_result = await db.execute(select(func.max(PaymentMethodConfig.sort_order)))
        max_order = max_order_result.scalar() or 0

        for idx, method_id in enumerate(missing_methods, start=max_order + 1):
            method_def = defaults.get(method_id, {})
            is_configured = method_def.get('is_configured', False)
            sub_options = None
            available = method_def.get('available_sub_options')
            if available:
                sub_options = {opt['id']: True for opt in available}

            config = PaymentMethodConfig(
                method_id=method_id,
                sort_order=idx,
                is_enabled=is_configured,
                display_name=None,
                sub_options=sub_options,
                min_amount_kopeks=None,
                max_amount_kopeks=None,
                user_type_filter='all',
                first_topup_filter='any',
                promo_group_filter_mode='all',
            )
            db.add(config)

        await db.commit()
        logger.info(f'Added {len(missing_methods)} missing payment method(s).')


# ============ CRUD ============


async def get_all_configs(db: AsyncSession) -> list[PaymentMethodConfig]:
    """Get all payment method configs ordered by sort_order."""
    result = await db.execute(
        select(PaymentMethodConfig)
        .options(selectinload(PaymentMethodConfig.allowed_promo_groups))
        .order_by(PaymentMethodConfig.sort_order)
    )
    return list(result.scalars().all())


async def get_config_by_method_id(db: AsyncSession, method_id: str) -> PaymentMethodConfig | None:
    """Get a single config by method_id."""
    result = await db.execute(
        select(PaymentMethodConfig)
        .options(selectinload(PaymentMethodConfig.allowed_promo_groups))
        .where(PaymentMethodConfig.method_id == method_id)
    )
    return result.scalar_one_or_none()


async def update_config(
    db: AsyncSession,
    method_id: str,
    data: dict,
    promo_group_ids: list[int] | None = None,
) -> PaymentMethodConfig | None:
    """Update a payment method config."""
    config = await get_config_by_method_id(db, method_id)
    if not config:
        return None

    # Update scalar fields
    updatable_fields = (
        'is_enabled',
        'display_name',
        'sub_options',
        'min_amount_kopeks',
        'max_amount_kopeks',
        'user_type_filter',
        'first_topup_filter',
        'promo_group_filter_mode',
    )
    for key in updatable_fields:
        if key in data:
            setattr(config, key, data[key])

    # Update promo groups M2M if specified
    if promo_group_ids is not None:
        if promo_group_ids:
            result = await db.execute(select(PromoGroup).where(PromoGroup.id.in_(promo_group_ids)))
            groups = list(result.scalars().all())
        else:
            groups = []
        config.allowed_promo_groups = groups

    await db.commit()
    await db.refresh(config)
    return config


async def update_sort_order(db: AsyncSession, ordered_method_ids: list[str]) -> None:
    """Batch update sort order for all methods."""
    for index, method_id in enumerate(ordered_method_ids):
        result = await db.execute(select(PaymentMethodConfig).where(PaymentMethodConfig.method_id == method_id))
        config = result.scalar_one_or_none()
        if config:
            config.sort_order = index

    await db.commit()


async def get_all_promo_groups(db: AsyncSession) -> list[PromoGroup]:
    """Get all promo groups for the filter selector."""
    result = await db.execute(select(PromoGroup).order_by(PromoGroup.priority.desc(), PromoGroup.name))
    return list(result.scalars().all())


# ============ User-facing methods ============


async def get_enabled_methods_for_user(
    db: AsyncSession,
    user: 'User | None' = None,
    is_first_topup: bool | None = None,
) -> list[dict]:
    """Get payment methods available for a specific user.

    Applies all filters from PaymentMethodConfig:
    - is_enabled
    - is_provider_configured (from env)
    - user_type_filter
    - first_topup_filter
    - promo_group_filter

    Returns list of dicts with method info ready for API response.
    """
    from app.database.models import UserPromoGroup

    configs = await get_all_configs(db)
    defaults = _get_method_defaults()

    result = []

    for config in configs:
        method_id = config.method_id
        method_def = defaults.get(method_id, {})

        # Skip if not enabled in admin panel
        if not config.is_enabled:
            continue

        # Skip if provider not configured in env
        if not method_def.get('is_configured', False):
            continue

        # Apply user_type_filter
        if user and config.user_type_filter != 'all':
            if config.user_type_filter == 'telegram' and not user.telegram_id:
                continue
            if config.user_type_filter == 'email' and not getattr(user, 'email', None):
                continue

        # Apply first_topup_filter
        if config.first_topup_filter != 'any' and is_first_topup is not None:
            if config.first_topup_filter == 'yes' and not is_first_topup:
                continue
            if config.first_topup_filter == 'no' and is_first_topup:
                continue

        # Apply promo_group_filter
        if config.promo_group_filter_mode == 'selected' and user:
            allowed_group_ids = {pg.id for pg in config.allowed_promo_groups}
            if allowed_group_ids:
                # Get user's promo groups
                user_groups_result = await db.execute(
                    select(UserPromoGroup.promo_group_id).where(UserPromoGroup.user_id == user.id)
                )
                user_group_ids = set(user_groups_result.scalars().all())

                # Check if user has at least one allowed group
                if not user_group_ids.intersection(allowed_group_ids):
                    continue

        # Build display name
        display_name = config.display_name or method_def.get('default_display_name', method_id)

        # Build min/max amounts (DB overrides env defaults)
        min_amount = (
            config.min_amount_kopeks if config.min_amount_kopeks is not None else method_def.get('default_min', 1000)
        )
        max_amount = (
            config.max_amount_kopeks
            if config.max_amount_kopeks is not None
            else method_def.get('default_max', 10000000)
        )

        # Build options (filter by sub_options config)
        options = None
        available_sub_options = method_def.get('available_sub_options')
        if available_sub_options and config.sub_options:
            enabled_options = []
            for opt in available_sub_options:
                opt_id = opt['id']
                if config.sub_options.get(opt_id, True):
                    enabled_options.append(opt)
            if enabled_options:
                options = enabled_options

        result.append(
            {
                'id': method_id,
                'name': display_name,
                'min_amount_kopeks': min_amount,
                'max_amount_kopeks': max_amount,
                'options': options,
                'sort_order': config.sort_order,
            }
        )

    return result
