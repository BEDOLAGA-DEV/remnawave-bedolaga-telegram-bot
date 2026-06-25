import base64
import os
from datetime import UTC, datetime
from urllib.parse import quote, urlparse, urlunparse

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus


logger = structlog.get_logger(__name__)


async def ensure_single_subscription(db: AsyncSession, user_id: int) -> Subscription | None:
    """
    Return the primary subscription for a user.

    Multi-tariff compatibility shim: in single-tariff setups a user has at
    most one subscription row; in multi-tariff there may be several, in
    which case we prefer an ACTIVE/TRIAL one, falling back to the most
    recent. Returns None if the user has no subscriptions at all.

    Used by happ recovery handlers that operate on "the" subscription
    without caring about which tariff it belongs to.
    """
    result = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
    )
    subs = result.scalars().all()
    if not subs:
        return None

    active_statuses = (
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.TRIAL.value,
    )
    for sub in subs:
        if sub.status in active_statuses:
            return sub
    return subs[0]


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
        # crypto_link is already regenerated for the override host at storage time
        return crypto_link or apply_subscription_domain_override(base_link)

    return apply_subscription_domain_override(base_link)


def apply_subscription_domain_override(url: str | None) -> str | None:
    """Swap the host of a subscription link for the configured override host.

    Non-destructive: the stored ``subscription_url`` is never modified — call
    this only on values being emitted to a user/API. Preserves scheme, path,
    query and fragment. Returns the input unchanged when no override is set,
    the input is empty, or it has no ``//netloc`` (e.g. an opaque token). Never
    pass a crypt5 link here — those are read from storage already overridden.
    """
    if not url:
        return url
    override = settings.get_subscription_domain_override()
    if not override:
        return url
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    return urlunparse(parsed._replace(netloc=override))


async def resolve_crypto_link_for_storage(
    api,
    subscription_url: str | None,
    panel_crypto_link: str | None,
) -> str | None:
    """Return the crypt5 link to persist in ``subscription_crypto_link``.

    With no override → the panel-provided value (current behavior). With an
    override → re-encrypt the overridden subscription URL via the open panel
    ``api`` client so the client fetches the new host. Encryption failure falls
    back to the panel value (never raises). ``api`` is any object exposing an
    async ``encrypt_happ_crypto_link(link)``.
    """
    if not settings.get_subscription_domain_override():
        return panel_crypto_link
    if not subscription_url:
        return panel_crypto_link
    overridden_url = apply_subscription_domain_override(subscription_url)
    try:
        encrypted = await api.encrypt_happ_crypto_link(overridden_url)
    except Exception:
        logger.warning('resolve_crypto_link_for_storage: encryption failed')
        encrypted = None
    return encrypted or panel_crypto_link


def build_scheme_redirect_link(deep_link: str | None, template: str | None) -> str | None:
    """Wrap a custom-scheme deep link (happ://, incy://, ...) in an HTTP redirect.

    Telegram inline buttons reject custom URL schemes, so the deep link is
    handed to an HTTP redirect host that 302s to the scheme. ``template`` may use
    ``{link}``/``{subscription_link}`` placeholders (filled with the url-encoded
    deep link), and ``{link_raw}``/``{subscription_link_raw}`` placeholders
    (filled with the unencoded deep link). When no placeholder is present,
    the url-encoded deep link is appended to the template as-is — so the
    template should normally end with ``=``, ``?``, or ``&``. Returns None
    when either argument is empty.
    """
    if not deep_link or not template:
        return None

    encoded_link = quote(deep_link, safe='')
    replacements = {
        '{subscription_link}': encoded_link,
        '{link}': encoded_link,
        '{subscription_link_raw}': deep_link,
        '{link_raw}': deep_link,
    }

    replaced = False
    for placeholder, value in replacements.items():
        if placeholder in template:
            template = template.replace(placeholder, value)
            replaced = True

    if replaced:
        return template
    return f'{template}{encoded_link}'


def get_happ_cryptolink_redirect_link(subscription_link: str | None) -> str | None:
    """Backward-compatible HAPP wrapper over :func:`build_scheme_redirect_link`."""
    template = settings.get_happ_cryptolink_redirect_template()
    return build_scheme_redirect_link(subscription_link, template)


def convert_subscription_link_to_happ_scheme(subscription_link: str | None) -> str | None:
    if not subscription_link:
        return None

    parsed_link = urlparse(subscription_link)

    if parsed_link.scheme.lower() == 'happ':
        return subscription_link

    if not parsed_link.scheme:
        return subscription_link

    return urlunparse(parsed_link._replace(scheme='happ'))


def generate_redhash(url: str) -> str | None:
    """Encrypt a URL with AES-256-GCM and return a base64url-encoded token.

    Token layout: 12-byte nonce || GCM ciphertext+tag (16-byte tag appended by AESGCM).
    Returns None when HAPP_REDIRECT_HASH_SECRET is not configured or invalid.
    """
    key = settings.get_happ_redirect_hash_secret()
    if not key:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, url.encode('utf-8'), None)
        token_bytes = nonce + ciphertext
        token = base64.urlsafe_b64encode(token_bytes).rstrip(b'=').decode('ascii')
        return token
    except Exception:
        logger.warning('generate_redhash: encryption failed')
        return None


def build_redhash_url(url: str) -> str | None:
    """Generate a full redirect URL with an encrypted redhash parameter.

    Returns None when the base URL or hash secret is not configured.
    """
    base = settings.get_happ_fallback_redirect_base_url()
    if not base:
        return None
    token = generate_redhash(url)
    if not token:
        return None
    separator = '&' if '?' in base else '?'
    return f'{base.rstrip("/")}/{separator}redhash={token}'
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
