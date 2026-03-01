from datetime import UTC, datetime
from urllib.parse import quote, urlparse, urlunparse

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription


logger = structlog.get_logger(__name__)


async def ensure_single_subscription(db: AsyncSession, user_id: int) -> Subscription | None:
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.created_at.desc())
    )
    subscriptions = result.scalars().all()

    if len(subscriptions) <= 1:
        return subscriptions[0] if subscriptions else None

    latest_subscription = subscriptions[0]
    old_subscriptions = subscriptions[1:]

    logger.warning(
        '🚨 Обнаружено подписок у пользователя . Удаляем старых.',
        subscriptions_count=len(subscriptions),
        user_id=user_id,
        old_subscriptions_count=len(old_subscriptions),
    )

    for old_sub in old_subscriptions:
        await db.delete(old_sub)
        logger.info('🗑️ Удалена подписка ID от', old_sub_id=old_sub.id, created_at=old_sub.created_at)

    await db.commit()
    await db.refresh(latest_subscription)

    logger.info(
        '✅ Оставлена подписка ID от',
        latest_subscription_id=latest_subscription.id,
        created_at=latest_subscription.created_at,
    )
    return latest_subscription


async def update_or_create_subscription(db: AsyncSession, user_id: int, **subscription_data) -> Subscription:
    existing_subscription = await ensure_single_subscription(db, user_id)

    if existing_subscription:
        for key, value in subscription_data.items():
            if hasattr(existing_subscription, key):
                setattr(existing_subscription, key, value)

        existing_subscription.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(existing_subscription)

        logger.info('🔄 Обновлена существующая подписка ID', existing_subscription_id=existing_subscription.id)
        return existing_subscription

    subscription_defaults = dict(subscription_data)
    autopay_enabled = subscription_defaults.pop('autopay_enabled', None)
    autopay_days_before = subscription_defaults.pop('autopay_days_before', None)

    new_subscription = Subscription(
        user_id=user_id,
        autopay_enabled=(settings.is_autopay_enabled_by_default() if autopay_enabled is None else autopay_enabled),
        autopay_days_before=(
            settings.DEFAULT_AUTOPAY_DAYS_BEFORE if autopay_days_before is None else autopay_days_before
        ),
        **subscription_defaults,
    )

    db.add(new_subscription)
    await db.commit()
    await db.refresh(new_subscription)

    logger.info('🆕 Создана новая подписка ID', new_subscription_id=new_subscription.id)
    return new_subscription


async def cleanup_duplicate_subscriptions(db: AsyncSession) -> int:
    result = await db.execute(
        select(Subscription.user_id).group_by(Subscription.user_id).having(func.count(Subscription.id) > 1)
    )
    users_with_duplicates = result.scalars().all()

    total_deleted = 0

    for user_id in users_with_duplicates:
        subscriptions_result = await db.execute(
            select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.created_at.desc())
        )
        subscriptions = subscriptions_result.scalars().all()

        for old_subscription in subscriptions[1:]:
            await db.delete(old_subscription)
            total_deleted += 1
            logger.info(
                '🗑️ Удалена дублирующаяся подписка ID пользователя',
                old_subscription_id=old_subscription.id,
                user_id=user_id,
            )

    await db.commit()
    logger.info('🧹 Очищено дублирующихся подписок', total_deleted=total_deleted)

    return total_deleted


def get_display_subscription_link(subscription: Subscription | None) -> str | None:
    if not subscription:
        return None

    base_link = getattr(subscription, 'subscription_url', None)

    if settings.is_happ_cryptolink_mode():
        crypto_link = getattr(subscription, 'subscription_crypto_link', None)
        return crypto_link or base_link

    return base_link


def get_happ_cryptolink_redirect_link(subscription_link: str | None) -> str | None:
    if not subscription_link:
        return None

    template = settings.get_happ_cryptolink_redirect_template()
    if not template:
        return None

    encoded_link = quote(subscription_link, safe='')
    replacements = {
        '{subscription_link}': encoded_link,
        '{link}': encoded_link,
        '{subscription_link_raw}': subscription_link,
        '{link_raw}': subscription_link,
    }

    replaced = False
    for placeholder, value in replacements.items():
        if placeholder in template:
            template = template.replace(placeholder, value)
            replaced = True

    if replaced:
        return template

    if template.endswith(('=', '?', '&')):
        return f'{template}{encoded_link}'

    return f'{template}{encoded_link}'


def convert_subscription_link_to_happ_scheme(subscription_link: str | None) -> str | None:
    if not subscription_link:
        return None

    parsed_link = urlparse(subscription_link)

    if parsed_link.scheme.lower() == 'happ':
        return subscription_link

    if not parsed_link.scheme:
        return subscription_link

    return urlunparse(parsed_link._replace(scheme='happ'))


def resolve_hwid_device_limit(subscription: Subscription | None) -> int | None:
    """Return a device limit value for RemnaWave payloads when selection is enabled."""
    import structlog

    _logger = structlog.get_logger('resolve_hwid_device_limit')

    if subscription is None:
        return None

    if not settings.is_devices_selection_enabled():
        forced_limit = settings.get_disabled_mode_device_limit()
        if forced_limit is not None and forced_limit > 0:
            _logger.info(
                'DEVICES_SELECTION disabled, using forced limit',
                forced_limit=forced_limit,
                subscription_device_limit=getattr(subscription, 'device_limit', None),
                subscription_id=getattr(subscription, 'id', None),
            )
            return forced_limit
        # forced_limit не задан или равен 0 — используем device_limit из подписки,
        # чтобы при смене тарифа лимит устройств обновлялся в панели

    limit = getattr(subscription, 'device_limit', None)
    if limit is None or limit <= 0:
        _logger.warning(
            'device_limit is None or <= 0, returning None',
            device_limit=limit,
            subscription_id=getattr(subscription, 'id', None),
        )
        return None

    return limit


def resolve_hwid_device_limit_for_payload(
    subscription: Subscription | None,
) -> int | None:
    """Return the device limit that should be sent to RemnaWave APIs.

    When device selection is disabled and no explicit override is configured,
    RemnaWave should continue receiving the subscription's stored limit so the
    external panel stays aligned with the bot configuration.
    """
    import structlog

    _logger = structlog.get_logger('resolve_hwid_device_limit')

    resolved_limit = resolve_hwid_device_limit(subscription)

    if resolved_limit is not None:
        _logger.info(
            'hwid_device_limit resolved',
            resolved_limit=resolved_limit,
            subscription_id=getattr(subscription, 'id', None),
        )
        return resolved_limit

    if subscription is None:
        return None

    fallback_limit = getattr(subscription, 'device_limit', None)
    if fallback_limit is None or fallback_limit <= 0:
        _logger.warning(
            'fallback device_limit is None or <= 0, NOT sending hwidDeviceLimit to RemnaWave',
            fallback_limit=fallback_limit,
            subscription_id=getattr(subscription, 'id', None),
        )
        return None

    _logger.info(
        'using fallback device_limit',
        fallback_limit=fallback_limit,
        subscription_id=getattr(subscription, 'id', None),
    )
    return fallback_limit


def resolve_simple_subscription_device_limit() -> int:
    """Return the effective device limit for simple subscription flows."""

    if settings.is_devices_selection_enabled():
        return int(getattr(settings, 'SIMPLE_SUBSCRIPTION_DEVICE_LIMIT', 0) or 0)

    forced_limit = settings.get_disabled_mode_device_limit()
    if forced_limit is not None:
        return forced_limit

    return int(getattr(settings, 'SIMPLE_SUBSCRIPTION_DEVICE_LIMIT', 0) or 0)
