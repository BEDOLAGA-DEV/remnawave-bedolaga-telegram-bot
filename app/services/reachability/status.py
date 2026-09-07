"""Статус интеграции для шапки раздела: включено/настроено/здорово, баланс, активные задачи, эталон."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud import reachability as crud
from app.database.models import ReachabilityJob
from app.external.bschek_api import BschekAPIError
from app.services.reachability.batches import batch_done_targets
from app.services.reachability.cores import XRAY_CORES
from app.services.reachability.kinds import KIND_SCAN, KIND_VLESS
from app.services.reachability.pricing import credits_to_kopeks
from app.services.reachability.resolver import SubscriptionConfigs


logger = structlog.get_logger(__name__)


class StatusSource(Protocol):
    """Что статусу нужно от сервиса: флаги, здоровье, аккаунт, эталонная подписка, настройки.

    Протокол вместо импорта класса сервиса — иначе сервис и статус импортируют друг друга.
    """

    def is_enabled(self) -> bool:
        pass

    def is_configured(self) -> bool:
        pass

    def health(self) -> tuple[bool, str | None]:
        pass

    async def account(self) -> dict:
        pass

    def reference_short_uuid(self) -> str | None:
        pass

    async def subscription_configs(self, db: AsyncSession, *, short_uuid: str | None = None) -> SubscriptionConfigs:
        pass

    def cost_limit_kopeks(self) -> int:
        pass

    def default_sni(self) -> str:
        pass


class AccountCache:
    """GET /account с коротким кэшем: баланс нужен и статусу, и каждому preview."""

    def __init__(
        self, fetch: Callable[[], Awaitable[dict]], ttl: float = 30.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._cached: dict | None = None
        self._fetched_at = 0.0

    async def get(self, force: bool = False) -> dict:
        now = self._clock()
        if force or self._cached is None or now - self._fetched_at >= self._ttl:
            self._cached = await self._fetch()
            self._fetched_at = now
        return self._cached

    def invalidate(self) -> None:
        self._cached = None


def account_summary(account: dict) -> dict[str, Any]:
    """Поля аккаунта для фронта; webhook_secret клиент уже отбросил."""
    return {
        'balance_kopeks': credits_to_kopeks(account.get('balance_total')),
        'bonus_kopeks': credits_to_kopeks(account.get('bonus_credits')),
        'tier': account.get('tier'),
        'tier_expires_at': account.get('tier_expires_at'),
        'min_interval_sec': account.get('min_interval_sec'),
    }


def _active_job(job: ReachabilityJob) -> dict[str, Any]:
    return {
        'id': job.id,
        'kind': job.kind,
        'phase': job.phase,
        'started_by_user_id': job.started_by_user_id,
        'started_at': job.started_at,
    }


async def _active_jobs(db: AsyncSession) -> list[dict[str, Any]]:
    active = []
    for kind in (KIND_VLESS, KIND_SCAN):
        job = await crud.get_active_job(db, kind)
        if job is not None:
            active.append(_active_job(job))
    return active


async def _active_batch(db: AsyncSession) -> dict[str, Any] | None:
    batch = await crud.get_active_batch(db)
    if batch is None:
        return None
    jobs = await crud.jobs_for_batch(db, batch.id)
    return {
        'id': batch.id,
        'total_targets': batch.total_targets,
        'done_targets': batch_done_targets(jobs),
        'started_at': batch.started_at,
    }


async def reference_status(service: StatusSource, db: AsyncSession) -> dict[str, Any]:
    short_uuid = service.reference_short_uuid()
    if not short_uuid:
        return {
            'short_uuid': None,
            'configs': 0,
            'rejected': 0,
            'error': 'Эталонная подписка не задана (BSCHEK_REFERENCE_SUBSCRIPTION)',
        }
    try:
        configs = await service.subscription_configs(db, short_uuid=short_uuid)
    except Exception as exc:
        # Статус не должен падать из-за панели — причина уходит в поле error.
        logger.warning('Эталонная подписка не прочитана', short_uuid=short_uuid, error=str(exc))
        return {'short_uuid': short_uuid, 'configs': 0, 'rejected': 0, 'error': str(exc)[:200]}
    return {
        'short_uuid': short_uuid,
        'configs': len(configs.configs),
        'rejected': len(configs.rejected),
        'error': None if configs.configs else 'В подписке нет пригодных конфигов',
    }


async def collect_status(service: StatusSource, db: AsyncSession) -> dict[str, Any]:
    enabled, configured = service.is_enabled(), service.is_configured()
    healthy, health_message = service.health()
    account: dict = {}
    if enabled and configured and healthy:
        try:
            account = await service.account()
        except BschekAPIError as exc:
            healthy, health_message = False, exc.message
    reference = await reference_status(service, db) if enabled and configured else None
    return {
        'enabled': enabled,
        'configured': configured,
        'healthy': healthy,
        'health_message': None if healthy else health_message,
        **account_summary(account),
        'active_jobs': await _active_jobs(db),
        'active_batch': await _active_batch(db),
        'reference': reference,
        'cost_limit_kopeks': service.cost_limit_kopeks(),
        'cores': dict(XRAY_CORES),
        'default_sni': service.default_sni(),
    }
