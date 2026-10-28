from urllib.parse import quote, urlparse, urlunparse

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription


logger = structlog.get_logger(__name__)


def resolve_tariff_cart_traffic(tariff, cart_data: dict) -> tuple[int | None, int]:
    """Resolve explicit custom traffic; reject ambiguous legacy or stale selections."""
    if 'custom_traffic_gb' not in cart_data:
        legacy = cart_data.get('traffic_limit_gb')
        if legacy is not None and legacy != tariff.traffic_limit_gb:
            raise ValueError('Legacy cart traffic selection must be recreated')
    custom = cart_data.get('custom_traffic_gb')
    if custom is None:
        return None, tariff.traffic_limit_gb
    if (
        isinstance(custom, bool)
        or not isinstance(custom, int)
        or not tariff.can_purchase_custom_traffic()
        or tariff.get_price_for_custom_traffic(custom) is None
    ):
        raise ValueError('Custom traffic selection is unavailable')
    return custom, custom


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
                '🗑️ Удалена дублирующаяся подписка',
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


def device_limit_needs_heal(value: int | None) -> bool:
    """Return True if a stored ``device_limit`` is structurally invalid.

    ``0`` is a legitimate "unlimited devices" state synced from RemnaWave
    (see :func:`coerce_panel_device_limit`) and must NOT be healed back to
    ``1`` — that was the original sync bug (every heal pass reverted
    unlimited-device subscriptions). Only ``None`` and negative values are
    truly broken.
    """
    return value is None or value < 0


def coerce_panel_device_limit(value: object, default: int = 1) -> int:
    """Normalize ``hwidDeviceLimit`` from a RemnaWave panel response.

    The panel returns ``0`` to signal HWID limit disabled (unlimited devices).
    A naive ``value or default`` collapses that ``0`` into the fallback and
    silently overwrites unlimited-device subscriptions on every sync.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value >= 0:
        return value
    return default


def resolve_tariff_renewal_device_limit(base_limit: int | None, current_limit: int | None) -> int:
    """Продление сохраняет докупки лимитного тарифа; ноль у тарифа — безлимит."""
    if base_limit == 0:
        return 0
    return max(base_limit or 0, current_limit or 0)


def resolve_tariff_purchase_device_limit(tariff, subscription: Subscription | None = None) -> int:
    """Price and provision the same device limit when a paid classic row gets its first tariff."""
    base = tariff.device_limit
    if subscription is not None and subscription.tariff_id == tariff.id:
        return resolve_tariff_renewal_device_limit(base, subscription.device_limit)
    if base == 0:
        return 0
    if subscription is not None and subscription.tariff_id is None and not subscription.is_trial:
        limit = max(base or 1, subscription.device_limit or 0)
        maximum = tariff.max_device_limit or (settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None)
        return min(limit, maximum) if maximum else limit
    return base if base is not None else 1


def resolve_min_device_limit(tariff: object | None = None) -> int:
    """Нижняя граница, до которой пользователь может уменьшить лимит устройств.

    По умолчанию (``ALLOW_DEVICES_BELOW_TARIFF_LIMIT=False``) опустить лимит
    ниже включённого в тариф нельзя: уменьшают почти всегда не ради
    самоограничения, а чтобы платить меньше — либо промахиваются и потом
    спрашивают в поддержке, почему устройств меньше, чем положено по тарифу.

    Вне тарифного режима (тариф не передан / у него нет лимита) граница — 1,
    как и раньше.
    """
    from app.config import settings

    if settings.ALLOW_DEVICES_BELOW_TARIFF_LIMIT:
        return 1

    tariff_devices = getattr(tariff, 'device_limit', None) if tariff is not None else None
    try:
        return max(1, int(tariff_devices or 0))
    except (TypeError, ValueError):
        return 1


def resolve_hwid_device_limit(subscription: Subscription | None) -> int | None:
    """Return a positive effective device limit, or None for no finite limit.

    Ноль или пусто в ``device_limit`` — «без ограничения» (HWID выключен): это
    штатное состояние подписки, а не ошибка, поэтому пишем об этом только
    в debug. Payload resolver отдельно передаёт явный ноль. Синхронизация прогоняет через эту
    функцию каждую подписку — warning здесь превращался в шум на всю базу.
    """
    import structlog

    _logger = structlog.get_logger('resolve_hwid_device_limit')

    if subscription is None:
        return None

    # Tariff limits are purchased per subscription. A classic-mode forced
    # amount must not replace paid tariff devices when projecting to the panel.
    if getattr(subscription, 'tariff_id', None) is None and not settings.is_devices_selection_enabled():
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
        _logger.debug(
            'device_limit не задаёт положительного ограничения HWID',
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

    Explicit zero must be sent on updates too: omitting it would preserve an
    old finite panel limit. Missing/invalid limits still omit the field, and a
    positive forced override retains precedence.
    """
    import structlog

    _logger = structlog.get_logger('resolve_hwid_device_limit')

    resolved_limit = resolve_hwid_device_limit(subscription)
    stored_limit = getattr(subscription, 'device_limit', None)
    if resolved_limit is None and type(stored_limit) is int and stored_limit == 0:
        return 0
    if resolved_limit is not None:
        _logger.info(
            'hwid_device_limit resolved',
            resolved_limit=resolved_limit,
            subscription_id=getattr(subscription, 'id', None),
        )
    return resolved_limit


def resolve_simple_subscription_device_limit() -> int:
    """Return the effective device limit for simple subscription flows."""

    if settings.is_devices_selection_enabled():
        return int(getattr(settings, 'SIMPLE_SUBSCRIPTION_DEVICE_LIMIT', 0) or 0)

    forced_limit = settings.get_disabled_mode_device_limit()
    if forced_limit is not None:
        return forced_limit

    return int(getattr(settings, 'SIMPLE_SUBSCRIPTION_DEVICE_LIMIT', 0) or 0)
