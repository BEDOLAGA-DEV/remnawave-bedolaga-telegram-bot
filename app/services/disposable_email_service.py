"""Service for blocking disposable/temporary email domains."""

import asyncio
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import lru_cache

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


_EXTRA_SEPARATORS = re.compile(r'[\s,;]+')


@lru_cache(maxsize=8)
def parse_extra_domains(raw: str | None) -> frozenset[str]:
    """Список оператора из настройки: любые разделители, регистр и ведущие '@'/'.' не важны."""
    if not raw:
        return frozenset()
    return frozenset(
        cleaned for token in _EXTRA_SEPARATORS.split(raw) if (cleaned := token.strip().lower().lstrip('@.'))
    )


def domain_and_parents(domain: str) -> Iterator[str]:
    """'deep.sub.mail.tm' → 'deep.sub.mail.tm', 'sub.mail.tm', 'mail.tm' (голый TLD не считается)."""
    parts = domain.split('.')
    for start in range(len(parts) - 1):
        yield '.'.join(parts[start:])


class DisposableEmailService:
    """
    Downloads and caches a list of disposable email domains from GitHub.

    Domains are stored in a frozenset for O(1) thread-safe lookups.
    The list is refreshed every 24 hours via an asyncio background task.
    If the download fails, the service falls back to an empty set (no blocking).
    """

    DOMAINS_URL = 'https://raw.githubusercontent.com/disposable/disposable-email-domains/master/domains.txt'
    UPDATE_INTERVAL_HOURS = 24

    def __init__(self) -> None:
        self._domains: frozenset[str] = frozenset()
        self._task: asyncio.Task[None] | None = None
        self._last_updated: datetime | None = None
        self._domain_count: int = 0

    async def start(self) -> None:
        """Load domains and start periodic refresh task."""
        await self._update_domains()
        self._task = asyncio.create_task(self._periodic_loop())
        logger.info('DisposableEmailService started (domains loaded)', domain_count=self._domain_count)

    async def stop(self) -> None:
        """Cancel periodic refresh task."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info('DisposableEmailService stopped')

    async def _update_domains(self) -> None:
        """Fetch domains.txt from GitHub and swap the in-memory set."""
        try:
            async with aiohttp.ClientSession() as session, session.get(self.DOMAINS_URL) as resp:
                if resp.status != 200:
                    logger.error('Failed to fetch disposable domains: HTTP', resp_status=resp.status)
                    return

                text = await resp.text()

            domains = frozenset(
                line.strip().lower() for line in text.splitlines() if line.strip() and not line.startswith('#')
            )

            self._domains = domains
            self._domain_count = len(domains)
            self._last_updated = datetime.now(UTC)
            logger.info('Disposable email domains updated: domains', domain_count=self._domain_count)

        except Exception:
            logger.exception('Error updating disposable email domains')

    async def _periodic_loop(self) -> None:
        """Sleep then refresh, repeating forever until cancelled."""
        while True:
            await asyncio.sleep(self.UPDATE_INTERVAL_HOURS * 3600)
            await self._update_domains()

    def is_disposable(self, email: str) -> bool:
        """Домен письма или любой его родитель — в скачанном списке или в списке оператора.

        Returns False when the feature is disabled via settings.
        """
        if not getattr(settings, 'DISPOSABLE_EMAIL_CHECK_ENABLED', True):
            return False

        extra = parse_extra_domains(getattr(settings, 'DISPOSABLE_EMAIL_EXTRA_DOMAINS', ''))
        if not self._domains and not extra:
            return False

        try:
            domain = email.rsplit('@', 1)[1].strip().lower()
        except IndexError:
            return False

        return any(candidate in self._domains or candidate in extra for candidate in domain_and_parents(domain))

    def get_status(self) -> dict:
        """Return service status for monitoring / health checks."""
        return {
            'enabled': getattr(settings, 'DISPOSABLE_EMAIL_CHECK_ENABLED', True),
            'domain_count': self._domain_count,
            'extra_domain_count': len(parse_extra_domains(getattr(settings, 'DISPOSABLE_EMAIL_EXTRA_DOMAINS', ''))),
            'last_updated': self._last_updated.isoformat() if self._last_updated else None,
            'running': self._task is not None and not self._task.done(),
        }


disposable_email_service = DisposableEmailService()
