"""Пачка проверок: чистые правила — нарезка целей по лимиту API, оценка времени, статус и цена из задач.

Одна кнопка «Проверить серверы» = ⌈N/10⌉ задач probe (API берёт не больше 10 целей за запрос),
которые идут не более трёх одновременно (лимит API на пробы с несколькими целями).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud import reachability as crud
from app.database.crud.reachability import TERMINAL_STATUSES
from app.database.models import ReachabilityBatch
from app.services.reachability.kinds import KIND_PROBE
from app.services.reachability.preview import PreviewResult
from app.services.reachability.pricing import enforce_cost_limit
from app.services.reachability.requests import MAX_PROBE_TARGETS
from app.services.reachability.targets import Target


logger = structlog.get_logger(__name__)


BATCH_PARALLEL = 3
# Больше — это уже не «проверить серверы», а нагрузка на счёт и на API в часы.
MAX_BATCH_TARGETS = 300
_TEMPLATE_KEYS = ('units', 'dpi', 'probes', 'sni_hosts')
# Полный флот в 15 симок проходит одну пробу за 10–15 минут: база плюс доля на каждую симку.
MINUTES_PER_ROUND_BASE = 3.0
MINUTES_PER_UNIT = 0.8

STATUS_DONE = 'done'
STATUS_FAILED = 'failed'
STATUS_CANCELLED = 'cancelled'


def chunk_targets[T](items: Sequence[T], size: int = MAX_PROBE_TARGETS) -> list[list[T]]:
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


def estimate_batch_minutes(chunks: int, units: int, parallel: int = BATCH_PARALLEL) -> int:
    """Примерное время всей пачки: раунды по ``parallel`` чашек, раунд длится по числу симок."""
    rounds = max(1, math.ceil(chunks / parallel))
    return max(1, rounds * round(MINUTES_PER_ROUND_BASE + MINUTES_PER_UNIT * max(1, units)))


def batch_status_from_jobs(jobs: Iterable[Any], *, cancelling: bool) -> str | None:
    """None — пачка ещё идёт; иначе итог: отменена, не удалась целиком или завершена."""
    statuses = [job.status for job in jobs]
    if any(status not in TERMINAL_STATUSES for status in statuses):
        return None
    if cancelling or STATUS_CANCELLED in statuses:
        return STATUS_CANCELLED
    if statuses and all(status == STATUS_FAILED for status in statuses):
        return STATUS_FAILED
    return STATUS_DONE


def batch_cost_kopeks(jobs: Iterable[Any]) -> int | None:
    costs = [job.cost_kopeks for job in jobs if job.cost_kopeks is not None]
    return sum(costs) if costs else None


def batch_done_targets(jobs: Iterable[Any]) -> int:
    return sum(len(job.targets or []) for job in jobs if job.status in TERMINAL_STATUSES)


# ------------------------------------------------------------------ сервис


class _Invalidatable(Protocol):
    def invalidate(self) -> None:
        pass


class _BatchRunner(Protocol):
    def spawn_batch(self, batch_id: int) -> Any:
        pass


class BatchService(Protocol):
    """Что пачке нужно от сервиса: превью чашки, потолок, поля задачи, кэш баланса и драйвер.

    Протокол вместо импорта класса сервиса — иначе сервис и пачки импортируют друг друга.
    """

    runner: _BatchRunner
    _account: _Invalidatable

    async def preview(self, db: AsyncSession, payload: dict) -> PreviewResult:
        pass

    def cost_limit_kopeks(self) -> int:
        pass

    def _job_fields(self, preview: PreviewResult, payload: dict, admin_id: int) -> dict[str, Any]:
        pass


@dataclass(frozen=True)
class BatchPreview:
    targets: list[Target]
    chunks: list[PreviewResult]
    units_resolved: list[str]
    cost_kopeks: int | None
    estimated_minutes: int
    warnings: list[str]
    balance_kopeks: int | None


def _host_refs(payload: dict) -> list[str]:
    refs = [str(ref) for ref in payload.get('host_refs') or [] if ref]
    if not refs:
        raise ValueError('Выберите хотя бы один сервер')
    if len(refs) > MAX_BATCH_TARGETS:
        raise ValueError(f'За одну проверку можно взять не больше {MAX_BATCH_TARGETS} серверов')
    return list(dict.fromkeys(refs))


async def preview_batch(service: BatchService, db: AsyncSession, payload: dict) -> BatchPreview:
    """Цена и время всей пачки: превью каждой чашки (бесплатно, без троттла) и сумма."""
    refs = _host_refs(payload)
    template = {key: payload.get(key) for key in _TEMPLATE_KEYS}
    chunks = [
        await service.preview(
            db, {'kind': KIND_PROBE, 'targets': [{'kind': 'host', 'ref': ref} for ref in chunk], **template}
        )
        for chunk in chunk_targets(refs)
    ]
    costs = [chunk.cost_kopeks for chunk in chunks]
    units = list(chunks[0].units_resolved) if chunks else []
    return BatchPreview(
        targets=[target for chunk in chunks for target in chunk.targets],
        chunks=chunks,
        units_resolved=units,
        cost_kopeks=None if any(cost is None for cost in costs) else sum(costs),
        estimated_minutes=estimate_batch_minutes(len(chunks), len(units)),
        warnings=list(dict.fromkeys(warning for chunk in chunks for warning in chunk.warnings)),
        balance_kopeks=chunks[0].balance_kopeks if chunks else None,
    )


async def create_batch(service: BatchService, db: AsyncSession, payload: dict, admin_id: int) -> ReachabilityBatch:
    """Одна пачка и задача на каждую чашку; деньги проверяются до записи, драйвер стартует после коммита."""
    preview = await preview_batch(service, db, payload)
    if not preview.units_resolved:
        raise ValueError('Под фильтр Белого списка не попала ни одна симка — выберите режим «любой» или другие симки')
    enforce_cost_limit(preview.cost_kopeks, service.cost_limit_kopeks())
    if preview.balance_kopeks is not None and (preview.cost_kopeks or 0) > preview.balance_kopeks:
        raise ValueError('На балансе bschekbot не хватает средств на эту проверку')
    batch = await crud.create_batch(
        db,
        status='pending',
        started_by_user_id=admin_id,
        scope={'kind': str(payload.get('scope_kind') or 'manual'), 'host_refs': _host_refs(payload)},
        request={key: payload.get(key) for key in _TEMPLATE_KEYS},
        total_targets=len(preview.targets),
        estimated_kopeks=preview.cost_kopeks,
    )
    for chunk in preview.chunks:
        await crud.create_job(db, **service._job_fields(chunk, payload, admin_id), batch_id=batch.id)
    await db.commit()
    service._account.invalidate()
    service.runner.spawn_batch(batch.id)
    logger.info('Пачка проверок запущена', batch_id=batch.id, targets=batch.total_targets, admin_id=admin_id)
    return batch
