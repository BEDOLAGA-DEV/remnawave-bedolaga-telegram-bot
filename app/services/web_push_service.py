"""Web Push (VAPID) service for browser push notifications.

Sends OS-level push notifications to users' browsers even when the cabinet tab
is closed. Uses the W3C Web Push standard with VAPID authentication — no
Google/Firebase dependency.

Generate VAPID keys once:
    py-vapid generate_keys
    # or via openssl:
    # openssl ecparam -name prime256v1 -genkey -noout -out private_key.pem
    # openssl ec -in private_key.pem -pubout -out public_key.pem

Then set in .env:
    WEB_PUSH_ENABLED=true
    WEB_PUSH_VAPID_PRIVATE_KEY=<base64url private key>
    WEB_PUSH_VAPID_PUBLIC_KEY=<base64url public key>
    WEB_PUSH_VAPID_EMAIL=admin@example.com
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.web_push_subscription import (
    deactivate_subscription,
    get_active_by_user,
    get_all_active,
    mark_last_used,
)
from app.database.models import WebPushSubscription


logger = structlog.get_logger(__name__)


class WebPushService:
    """Service for sending Web Push notifications via pywebpush.

    All VAPID settings (enabled flag, public key, private key path/PEM, email)
    are read dynamically from ``settings`` on every access. This is intentional:
    after runtime migration of .env values into the ``system_settings`` table,
    those values are pushed back into the ``settings`` object via
    ``BotConfigurationService._apply_to_settings`` *after* this module is
    imported. Caching them in __init__ would make the service read the empty
    defaults forever. The only cached field is the PEM contents of the private
    key file — re-resolved whenever the config path changes.
    """

    def __init__(self) -> None:
        self._pywebpush_available = False
        self._webpush_fn = None
        self._webpush_exception_cls: type[Exception] = Exception
        # Private-key cache: remember the last raw config value and its resolved
        # PEM so we don't hit the filesystem on every send.
        self._cached_private_key_raw: str = ''
        self._cached_private_key_pem: str = ''
        self._load_pywebpush()

    @staticmethod
    def _load_private_key(value: str) -> str:
        """Resolve the VAPID private key into the form pywebpush expects.

        Accepts a PEM string, a path to a .pem file, or an already-encoded
        value. The result is base64url(PKCS#8 DER): pywebpush feeds the value
        straight to ``py_vapid.Vapid.from_string``, which (in the installed
        version) base64url-decodes it and calls ``load_der_private_key`` — it
        CANNOT parse PEM text, so a PEM passed verbatim fails with
        "ASN.1 parsing error: invalid length". We therefore convert PEM → DER
        → base64url here. Path resolution is tried from the CWD and the project
        root so the bot works regardless of how it is launched.
        """
        if not value:
            return ''

        pem_text: str | None = None
        if value.lstrip().startswith('-----BEGIN'):
            pem_text = value
        else:
            from pathlib import Path

            project_root = Path(__file__).resolve().parent.parent.parent
            for candidate in (Path(value), project_root / value):
                try:
                    if candidate.is_file():
                        pem_text = candidate.read_text(encoding='ascii')
                        logger.info('VAPID private key loaded from file', path=str(candidate))
                        break
                except OSError:
                    continue

        if pem_text is None:
            # Not a PEM and not a resolvable file — assume it is already a form
            # from_string accepts (base64url DER or raw32) and pass it through.
            logger.warning(
                'VAPID private key is not a PEM or existing file; passing through verbatim',
                value_preview=value[:32] + '...' if len(value) > 32 else value,
            )
            return value

        try:
            import base64

            from cryptography.hazmat.primitives import serialization

            key = serialization.load_pem_private_key(pem_text.encode('ascii'), password=None)
            der = key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            return base64.urlsafe_b64encode(der).rstrip(b'=').decode('ascii')
        except Exception as e:
            logger.error('Failed to convert VAPID PEM to base64url-DER', error=e)
            return pem_text

    def _load_pywebpush(self) -> None:
        """Lazily import pywebpush so absence doesn't break app startup."""
        try:
            from pywebpush import WebPushException, webpush  # type: ignore

            self._webpush_fn = webpush
            self._webpush_exception_cls = WebPushException
            self._pywebpush_available = True
        except ImportError:
            logger.warning(
                'pywebpush not installed — Web Push disabled. '
                'Install with: pip install pywebpush'
            )
            self._pywebpush_available = False

    # ------------------------------------------------------------------
    # Dynamic config accessors — always read from live ``settings`` so that
    # runtime edits via the admin panel or DB migrations take effect without
    # a bot restart.
    # ------------------------------------------------------------------
    @property
    def _enabled(self) -> bool:
        return bool(settings.WEB_PUSH_ENABLED)

    @property
    def _public_key(self) -> str:
        return settings.WEB_PUSH_VAPID_PUBLIC_KEY or ''

    @property
    def _email(self) -> str:
        return settings.WEB_PUSH_VAPID_EMAIL or 'admin@example.com'

    @property
    def _vapid_claims(self) -> dict[str, str]:
        return {'sub': f'mailto:{self._email}'}

    @property
    def _private_key(self) -> str:
        raw = settings.WEB_PUSH_VAPID_PRIVATE_KEY or ''
        if raw != self._cached_private_key_raw:
            self._cached_private_key_pem = self._load_private_key(raw)
            self._cached_private_key_raw = raw
        return self._cached_private_key_pem

    @property
    def is_enabled(self) -> bool:
        return (
            self._enabled
            and self._pywebpush_available
            and bool(self._private_key)
            and bool(self._public_key)
        )

    @property
    def public_key(self) -> str:
        return self._public_key

    async def send_to_subscription(
        self,
        db: AsyncSession,
        subscription: WebPushSubscription,
        payload: dict[str, Any],
    ) -> bool:
        """Send a push message to a single subscription.

        Returns True on success. On permanent failure (404/410), the subscription
        is auto-deactivated. Other exceptions are logged and return False.
        """
        if not self.is_enabled or self._webpush_fn is None:
            return False

        subscription_info = {
            'endpoint': subscription.endpoint,
            'keys': {
                'p256dh': subscription.p256dh,
                'auth': subscription.auth,
            },
        }

        try:
            # pywebpush is synchronous — run in executor to avoid blocking the event loop
            await asyncio.to_thread(
                self._webpush_fn,
                subscription_info=subscription_info,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=self._private_key,
                vapid_claims=dict(self._vapid_claims),
                ttl=3600,
            )
            try:
                await mark_last_used(db, subscription.id)
            except Exception as e:  # pragma: no cover
                logger.debug('Failed to update last_used_at', subscription_id=subscription.id, error=e)
            return True
        except self._webpush_exception_cls as ex:
            # pywebpush raises with a response attribute on HTTP errors
            status_code = None
            response = getattr(ex, 'response', None)
            if response is not None:
                status_code = getattr(response, 'status_code', None)

            if status_code in (404, 410):
                logger.info(
                    'Web Push endpoint expired, deactivating',
                    subscription_id=subscription.id,
                    status=status_code,
                )
                try:
                    await deactivate_subscription(db, subscription.id)
                except Exception as deactivate_err:  # pragma: no cover
                    logger.warning(
                        'Failed to deactivate subscription',
                        subscription_id=subscription.id,
                        error=deactivate_err,
                    )
            else:
                logger.warning(
                    'Web Push delivery failed',
                    subscription_id=subscription.id,
                    status=status_code,
                    error=str(ex),
                )
            return False
        except Exception as ex:
            logger.error(
                'Unexpected Web Push error',
                subscription_id=subscription.id,
                error=str(ex),
            )
            return False

    async def send_to_user(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        title: str,
        body: str,
        url: str | None = None,
        level: str = 'info',
        tag: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> int:
        """Send a push to all active subscriptions of a single user.

        Returns the number of successful deliveries.
        """
        if not self.is_enabled:
            return 0

        subscriptions = await get_active_by_user(db, user_id)
        if not subscriptions:
            return 0

        payload: dict[str, Any] = {
            'title': title,
            'body': body,
            'level': level,
            'url': url or '/',
            'tag': tag or 'default',
        }
        if extra_data:
            payload.update(extra_data)

        tasks = [self.send_to_subscription(db, sub, payload) for sub in subscriptions]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success_count = sum(1 for r in results if r is True)
        return success_count

    async def send_to_all(
        self,
        db: AsyncSession,
        *,
        title: str,
        body: str,
        url: str | None = None,
        level: str = 'info',
        tag: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> int:
        """Send push to ALL active subscriptions (for admin broadcasts).

        Returns the number of successful deliveries. Runs batched so a few
        bad endpoints don't block the whole broadcast.
        """
        if not self.is_enabled:
            return 0

        subscriptions = await get_all_active(db)
        if not subscriptions:
            return 0

        payload: dict[str, Any] = {
            'title': title,
            'body': body,
            'level': level,
            'url': url or '/',
            'tag': tag or 'default',
        }
        if extra_data:
            payload.update(extra_data)

        # Batch into groups of 50 to avoid overwhelming push services
        BATCH_SIZE = 50
        total_success = 0
        for i in range(0, len(subscriptions), BATCH_SIZE):
            batch = subscriptions[i : i + BATCH_SIZE]
            tasks = [self.send_to_subscription(db, sub, payload) for sub in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            total_success += sum(1 for r in results if r is True)

        logger.info(
            'Web Push broadcast complete',
            total_subscriptions=len(subscriptions),
            successful=total_success,
        )
        return total_success


# Global singleton
web_push_service = WebPushService()
