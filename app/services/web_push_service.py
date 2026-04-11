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
    """Service for sending Web Push notifications via pywebpush."""

    def __init__(self) -> None:
        self._enabled = settings.WEB_PUSH_ENABLED
        self._private_key = self._load_private_key(settings.WEB_PUSH_VAPID_PRIVATE_KEY)
        self._public_key = settings.WEB_PUSH_VAPID_PUBLIC_KEY
        self._email = settings.WEB_PUSH_VAPID_EMAIL
        self._vapid_claims = {'sub': f'mailto:{self._email}'}
        self._pywebpush_available = False
        self._webpush_fn = None
        self._webpush_exception_cls: type[Exception] = Exception
        self._load_pywebpush()

    @staticmethod
    def _load_private_key(value: str) -> str:
        """Resolve the private key config value.

        Supported formats:
        - Full PEM string starting with '-----BEGIN'
        - Relative or absolute path to a .pem file

        If the value looks like a path and the file exists, read its contents.
        Otherwise pass the value through verbatim (pywebpush will interpret).
        Resolution is done from the project root AND from the current working dir,
        so the bot works regardless of how it's launched.
        """
        if not value:
            return ''

        # Already a PEM string — pass through
        if value.lstrip().startswith('-----BEGIN'):
            return value

        # Candidate paths: raw value, project root, /data, cwd
        import os
        from pathlib import Path

        project_root = Path(__file__).resolve().parent.parent.parent
        candidates = [
            Path(value),  # relative to cwd or absolute
            project_root / value,  # relative to project root
        ]

        for candidate in candidates:
            try:
                if candidate.is_file():
                    content = candidate.read_text(encoding='ascii')
                    logger.info('VAPID private key loaded from file', path=str(candidate))
                    return content
            except OSError:
                continue

        # Fallback: return as-is and let pywebpush try
        logger.warning(
            'VAPID private key config value is not a recognizable PEM or existing file',
            value_preview=value[:32] + '...' if len(value) > 32 else value,
        )
        return value

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
