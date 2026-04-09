from urllib.parse import quote, urlparse, urlunparse

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription


logger = structlog.get_logger(__name__)


async def cleanup_duplicate_subscriptions(db: AsyncSession) -> int:
    # В multi-tariff режиме несколько подписок у пользователя — это нормально
    if settings.is_multi_tariff_enabled():
        logger.info('♻️ cleanup_duplicate_subscriptions пропущена: multi-tariff режим')
        return 0

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
