"""Bounded recovery and polling of durable traffic accounting work.

Only live legacy subscriptions with active purchased packages are migrated.
All other candidates already have an accounting fence. Retry metadata survives
process restarts; unprepared legacy failures use a bounded rotating cooldown.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, case, exists, func, or_, select, update

from app.config import settings
from app.database.models import Subscription, TrafficAccountingState, TrafficAllowance, TrafficPurchase, User
from app.services.subscription_service import SubscriptionService
from app.services.traffic_accounting_delivery import deliver_account_projection
from app.services.traffic_accounting_service import AccountingAccountMissing, ensure_managed_account
from app.services.traffic_accounting_store import _aware


logger = structlog.get_logger(__name__)


class TrafficAccountingWorker:
    def __init__(
        self,
        session_factory=None,
        service_factory=None,
        *,
        batch_size=40,
        interval_seconds=15,
        poll_seconds=60,
        request_timeout=20,
        concurrency=4,
        drain_pause_seconds=0.25,
    ):
        if min(batch_size, interval_seconds, poll_seconds, request_timeout, concurrency, drain_pause_seconds) <= 0:
            raise ValueError('Worker limits must be positive')
        self.session_factory = session_factory
        self.service_factory = service_factory or SubscriptionService
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds
        self.poll_seconds = poll_seconds
        self.request_timeout = request_timeout
        self.concurrency = concurrency
        self.drain_pause_seconds = drain_pause_seconds
        self.task = None
        self._legacy_cursor = 0
        self._legacy_cooldowns = OrderedDict()

    def _factory(self):
        if self.session_factory is None:
            from app.database.database import AsyncSessionLocal

            self.session_factory = AsyncSessionLocal
        return self.session_factory

    async def start(self):
        if self.task is not None and not self.task.done():
            return
        if not getattr(self.service_factory(), 'is_configured', True):
            logger.info('Traffic accounting worker waits for configured panel API')
            return
        self.task = asyncio.create_task(self._run(), name='traffic-accounting-worker')

    async def stop(self):
        task = self.task
        if task is not None:
            task.cancel()
            done, _ = await asyncio.wait([task], timeout=5)
            if done:
                await asyncio.gather(task, return_exceptions=True)
                self.task = None
            else:
                logger.error('Traffic accounting worker cancellation timed out')
        from app.services.traffic_accounting_service import stop_projection_tasks

        await stop_projection_tasks(timeout=5)

    async def _run(self):
        while True:
            processed = 0
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('Traffic accounting scan failed', error_type=type(error).__name__)
            await asyncio.sleep(self.drain_pause_seconds if processed >= self.batch_size else self.interval_seconds)

    async def _legacy_candidates(self, db, now):
        allowance = max(1, self.batch_size // 5)
        query = (
            select(Subscription.id)
            .join(User, User.id == Subscription.user_id)
            .where(
                Subscription.traffic_limit_gb > 0,
                Subscription.traffic_accounting_mode == 'legacy',
                Subscription.end_date > now,
                Subscription.grace_session_open.is_(False),
                User.status == 'active',
                ~exists(
                    select(TrafficAccountingState.subscription_id).where(
                        TrafficAccountingState.subscription_id == Subscription.id
                    )
                ),
                exists(
                    select(TrafficPurchase.id).where(
                        TrafficPurchase.subscription_id == Subscription.id, TrafficPurchase.expires_at > now
                    )
                ),
            )
            .order_by(Subscription.id)
            .limit(allowance * 4)
        )
        ids = list(await db.scalars(query.where(Subscription.id > self._legacy_cursor)))
        if not ids and self._legacy_cursor:
            self._legacy_cursor = 0
            ids = list(await db.scalars(query))
        candidates = []
        for subscription_id in ids:
            self._legacy_cursor = subscription_id
            previous = self._legacy_cooldowns.get(subscription_id)
            if previous is None or previous[1] <= now:
                candidates.append((subscription_id, 'legacy'))
                if len(candidates) == allowance:
                    break
        return candidates

    async def run_once(self, *, now=None):
        moment = now or datetime.now(UTC)
        async with self._factory()() as db:
            candidates = await self._legacy_candidates(db, moment)
            state = TrafficAccountingState
            uncovered = and_(state.grace_excluded.is_(False), Subscription.grace_session_open.is_(False))
            work_due = or_(
                state.revision > state.projected_revision,
                state.next_refresh_at <= moment,
                exists(
                    select(TrafficAllowance.subscription_id).where(
                        TrafficAllowance.subscription_id == state.subscription_id,
                        TrafficAllowance.remaining_bytes > 0,
                        TrafficAllowance.expires_at <= moment,
                    )
                ),
            )
            query = (
                select(state.subscription_id, state.phase)
                .join(Subscription, Subscription.id == state.subscription_id)
                .where(
                    state.phase.in_(('prepared', 'ready', 'unlimited', 'recreating')),
                    or_(
                        state.next_delivery_at.is_(None),
                        state.next_delivery_at <= moment,
                        and_(state.delivery_failures == 0, uncovered, work_due),
                    ),
                )
                .order_by(
                    case((and_(uncovered, work_due), 0), else_=1),
                    func.coalesce(state.next_delivery_at, state.updated_at),
                    state.subscription_id,
                )
                .limit(self.batch_size - len(candidates))
            )
            # Alternate durable and legacy work so neither class monopolizes a batch.
            durable = list((await db.execute(query)).all())
        ordered = []
        for index in range(max(len(durable), len(candidates))):
            if index < len(durable):
                ordered.append(durable[index])
            if index < len(candidates):
                ordered.append(candidates[index])
        semaphore = asyncio.Semaphore(self.concurrency)

        async def process_candidate(subscription_id, phase):
            async with semaphore:
                await self._process_candidate(subscription_id, phase, now=now)

        # TaskGroup propagates shutdown cancellation to active requests and
        # queued slots alike. Every subscription retains its own DB sessions;
        # the existing User → grace → Subscription locks serialize same-owner work.
        async with asyncio.TaskGroup() as tasks:
            for subscription_id, phase in ordered:
                tasks.create_task(process_candidate(subscription_id, phase))
        return len(ordered)

    async def _process_candidate(self, subscription_id, phase, *, now=None):
        operation_time = now if now is not None else datetime.now(UTC)
        error_type = None
        try:
            async with asyncio.timeout(self.request_timeout):
                async with self.service_factory().get_api_client() as api:
                    if phase in ('legacy', 'prepared'):
                        await ensure_managed_account(
                            self._factory(),
                            api,
                            subscription_id,
                            now=operation_time,
                            timezone=settings.REMNAWAVE_TRAFFIC_RESET_TIMEZONE,
                        )
                    if phase == 'recreating':
                        from app.services.traffic_accounting_recreation import ensure_account_recreated

                        await ensure_account_recreated(self._factory(), api, subscription_id, now=operation_time)
                    try:
                        await deliver_account_projection(self._factory(), api, subscription_id, now=operation_time)
                    except AccountingAccountMissing:
                        from app.services.traffic_accounting_recreation import ensure_account_recreated

                        await ensure_account_recreated(self._factory(), api, subscription_id, now=operation_time)
                        await deliver_account_projection(self._factory(), api, subscription_id, now=operation_time)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error_type = type(error).__name__[:128]
            logger.warning('Traffic accounting work deferred', subscription_id=subscription_id, error_type=error_type)
        await self._record_result(subscription_id, now if now is not None else datetime.now(UTC), error_type)

    async def _record_result(self, subscription_id, now, error_type):
        async with self._factory()() as db:
            state = await db.get(TrafficAccountingState, subscription_id)
            if state is None:
                if error_type is not None:
                    previous = self._legacy_cooldowns.pop(subscription_id, (0, now))
                    failures = previous[0] + 1
                    self._legacy_cooldowns[subscription_id] = (
                        failures,
                        now + timedelta(seconds=self._backoff(failures)),
                    )
                    while len(self._legacy_cooldowns) > 2048:
                        self._legacy_cooldowns.popitem(last=False)
                return
            self._legacy_cooldowns.pop(subscription_id, None)
            failures = (state.delivery_failures or 0) + 1 if error_type else 0
            due = now + timedelta(seconds=self._backoff(failures) if error_type else self.poll_seconds)
            if not error_type and not state.grace_excluded:
                expiry = await db.scalar(
                    select(func.min(TrafficAllowance.expires_at)).where(
                        TrafficAllowance.subscription_id == subscription_id,
                        TrafficAllowance.remaining_bytes > 0,
                        TrafficAllowance.expires_at > now,
                    )
                )
                for boundary in (state.next_refresh_at, expiry):
                    if boundary is not None and _aware(boundary) > now:
                        due = min(due, _aware(boundary))
            # A concurrent grant clearing the retry fence must win this update.
            await db.execute(
                update(TrafficAccountingState)
                .where(
                    TrafficAccountingState.subscription_id == subscription_id,
                    TrafficAccountingState.revision == state.revision,
                    TrafficAccountingState.projected_revision == state.projected_revision,
                )
                .values(next_delivery_at=due, delivery_failures=failures, last_delivery_error=error_type)
            )
            await db.commit()

    @staticmethod
    def _backoff(failures):
        return min(1800, 30 * 2 ** min(max(failures - 1, 0), 6))


traffic_accounting_worker = TrafficAccountingWorker()
