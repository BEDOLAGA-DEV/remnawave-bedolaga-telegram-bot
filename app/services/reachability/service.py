"""Фасад раздела BSCHEKER для роутов кабинета.

Собирает клиент bschekbot, каталог симок, резолвер целей, цены и сервис задач.
Ничего платного не делает сам: preview бесплатен, запуск — через JobRunner в фоне.
Ошибки доступа/тарифа гасят интеграцию на 5 минут, чтобы не долбить API.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import reachability as crud
from app.database.database import AsyncSessionLocal
from app.database.models import ReachabilityBatch, ReachabilityJob, ReachabilityTargetPref, Subscription
from app.external.bschek_api import BschekAPI, BschekAPIError
from app.services.reachability import batches as batch_ops
from app.services.reachability.gate import PaidCallGate
from app.services.reachability.jobs import JobNotCancellable, JobRunner
from app.services.reachability.kinds import KIND_PROBE, KIND_SCAN, KIND_VLESS
from app.services.reachability.links import RejectedLink, expand_raw_input, parse_links
from app.services.reachability.panel_links import fetch_panel_links
from app.services.reachability.preview import PreviewResult
from app.services.reachability.pricing import credits_to_kopeks, enforce_cost_limit, estimate_vless_kopeks
from app.services.reachability.requests import (
    DEFAULT_SNI_HOST,
    build_probe_request,
    build_scan_request,
    build_vless_request,
    resolve_sni_hosts,
)
from app.services.reachability.resolver import (
    HostView,
    NodeView,
    PrefsMap,
    SubscriptionConfigs,
    TargetResolutionError,
    TargetResolver,
    target_from_link,
)
from app.services.reachability.status import AccountCache, collect_status
from app.services.reachability.subscriptions import (
    SubscriptionFetchError,
    fetch_subscription_links,
    is_subscription_url,
)
from app.services.reachability.summary import build_summary_rows
from app.services.reachability.targets import KIND_CIDR, KIND_CUSTOM, KIND_HOST, PURPOSE_BS, Target
from app.services.reachability.units import Expansion, SelectorError, Unit, UnitsCache


logger = structlog.get_logger(__name__)

AUTH_CODES = frozenset({'unauthenticated', 'api_not_available', 'tier_too_low', 'subscription_required'})
UNHEALTHY_FOR = timedelta(minutes=5)
JOB_KINDS = (KIND_PROBE, KIND_VLESS, KIND_SCAN)
EXCLUSIVE_KINDS = (KIND_VLESS, KIND_SCAN)
NO_UNITS_MESSAGE = 'Под фильтр Белого списка не попала ни одна симка'
# Последний сегмент URL подписки, похожий на shortUuid панели — сначала спрашиваем свою панель.
_SHORT_UUID_RE = re.compile(r'^[A-Za-z0-9_-]{4,64}$')


class ReachabilityDisabled(Exception):
    """Интеграция выключена или не настроена — роут отдаёт 503 с причиной."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ReachabilityUnhealthy(Exception):
    """API отверг ключ/тариф; запуски не принимаются до ``until``."""

    def __init__(self, reason: str, until: datetime) -> None:
        self.reason, self.until = reason, until
        super().__init__(reason)


class ReachabilityBusy(Exception):
    """На аккаунте уже идёт задача этого вида (один VLESS и один скан)."""

    def __init__(self, job: ReachabilityJob) -> None:
        self.job = job
        super().__init__(f'Уже идёт задача #{job.id}')


class PanelUnavailable(Exception):
    """Панель Remnawave не ответила — ничего не потрачено, админу 503."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class JobNotFound(Exception):
    pass


@dataclass(frozen=True)
class Health:
    until: datetime | None = None
    reason: str | None = None

    def is_healthy(self, now: datetime) -> bool:
        return self.until is None or now >= self.until


@dataclass(frozen=True)
class Quote:
    request: dict
    cost_kopeks: int | None
    exact: bool
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedConfig:
    """Конфиг из поля «Конфиг или подписка»: цель и то, что кабинет пришлёт обратно в задаче."""

    target: Target
    target_in: dict


@dataclass(frozen=True)
class ParsedInput:
    configs: list[ParsedConfig]
    rejected: list[RejectedLink]
    sources: list[dict]


def _skipped(expansion: Expansion) -> dict:
    return {
        'dpi_off': [unit.as_dict() for unit in expansion.skipped_dpi_off],
        'unavailable': [unit.as_dict() for unit in expansion.skipped_unavailable],
        'unknown': [],
        'blocked_targets': [],
    }


class ReachabilityService:
    def __init__(
        self,
        *,
        settings_obj: Any = settings,
        session_factory: Callable[[], Any] = AsyncSessionLocal,
        remnawave_factory: Callable[[], Any] | None = None,
        runner: JobRunner | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        url_fetcher: Callable[[str], Any] | None = None,
    ) -> None:
        self._settings = settings_obj
        self._session_factory = session_factory
        self._remnawave_factory = remnawave_factory
        self._url_fetcher = url_fetcher or fetch_subscription_links
        self._clock = clock
        self._now = now
        self._client_factory: Callable[[], BschekAPI] = self._make_client
        self._gate = PaidCallGate()
        self.runner = runner or JobRunner(
            client_factory=lambda: self._client_factory(),
            gate=self._gate,
            session_factory=session_factory,
            cost_limit_kopeks=self.cost_limit_kopeks,
        )
        self._units = UnitsCache(self._fetch_operators, clock=clock)
        self._account = AccountCache(self._fetch_account, clock=clock)
        self._health = Health()
        self._background: asyncio.Task | None = None

    # ------------------------------------------------------------ настройки и здоровье

    def is_enabled(self) -> bool:
        return bool(self._settings.is_bschek_enabled())

    def is_configured(self) -> bool:
        return bool(self._settings.is_bschek_configured())

    def cost_limit_kopeks(self) -> int:
        return int(self._settings.BSCHEK_JOB_COST_LIMIT_KOPEKS or 0)

    def reference_short_uuid(self) -> str | None:
        return self._settings.BSCHEK_REFERENCE_SUBSCRIPTION or None

    @staticmethod
    def default_sni() -> str:
        """Белый домен по умолчанию для TLS-SNI (зашит в код, как плейсхолдер оригинала)."""
        return DEFAULT_SNI_HOST

    def health(self) -> tuple[bool, str | None]:
        healthy = self._health.is_healthy(self._now())
        return healthy, None if healthy else self._health.reason

    def mark_unhealthy(self, reason: str) -> None:
        self._health = Health(until=self._now() + UNHEALTHY_FOR, reason=reason)

    def _ensure_enabled(self) -> None:
        if not self.is_enabled():
            raise ReachabilityDisabled('Интеграция bschekbot выключена (BSCHEK_ENABLED)')
        if not self.is_configured():
            raise ReachabilityDisabled('Не задан ключ API bschekbot (BSCHEK_API_KEY)')
        if not self._health.is_healthy(self._now()):
            raise ReachabilityUnhealthy(self._health.reason or 'API bschekbot недоступен', self._health.until)

    # ------------------------------------------------------------ клиент API

    def _make_client(self) -> BschekAPI:
        return BschekAPI(
            api_key=self._settings.BSCHEK_API_KEY,
            base_url=self._settings.get_bschek_api_url(),
            timeout=float(self._settings.BSCHEK_REQUEST_TIMEOUT),
        )

    async def _call(self, fn: Callable[[BschekAPI], Any]) -> Any:
        self._ensure_enabled()
        try:
            async with self._client_factory() as api:
                return await fn(api)
        except BschekAPIError as exc:
            if exc.code in AUTH_CODES:
                self.mark_unhealthy(exc.message)
            raise

    async def _fetch_operators(self) -> dict:
        return await self._call(lambda api: api.get_operators())

    async def _fetch_account(self) -> dict:
        return await self._call(lambda api: api.get_account())

    async def account(self) -> dict:
        return await self._account.get()

    async def _balance_kopeks(self) -> int | None:
        try:
            return credits_to_kopeks((await self.account()).get('balance_total'))
        except BschekAPIError:
            return None

    async def units(
        self, *, dpi: str | None = None, operator: list[str] | None = None, region: list[str] | None = None
    ) -> list[Unit]:
        self._ensure_enabled()
        units = (await self._units.get()).units
        if dpi and dpi != 'any':
            units = [unit for unit in units if unit.dpi == dpi]
        if operator:
            wanted = {name.lower() for name in operator}
            units = [unit for unit in units if unit.operator.lower() in wanted]
        if region:
            wanted = {name.lower() for name in region}
            units = [unit for unit in units if unit.region.lower() in wanted or unit.region_code.lower() in wanted]
        return units

    async def _expand_units(self, selectors: list[str], dpi: str) -> Expansion:
        expansion = (await self._units.get()).expand(selectors, dpi)
        if expansion.unknown:
            raise SelectorError(f'Неизвестные симки: {", ".join(expansion.unknown)} — обновите список')
        return expansion

    # ------------------------------------------------------------ панель и цели

    def _panel(self) -> Any:
        if self._remnawave_factory is not None:
            return self._remnawave_factory()
        from app.services.remnawave_service import RemnaWaveService

        return RemnaWaveService()

    @contextlib.asynccontextmanager
    async def _panel_client(self) -> AsyncIterator[Any]:
        try:
            async with self._panel().get_api_client() as api:
                yield api
        except PanelUnavailable:
            raise
        except Exception as exc:
            raise PanelUnavailable(f'Панель Remnawave недоступна: {exc}'[:200]) from exc

    async def _prefs(self, db: AsyncSession) -> PrefsMap:
        return {
            (pref.target_kind, pref.target_ref): (pref.purpose, pref.excluded) for pref in await crud.list_prefs(db)
        }

    def _resolver_with(self, prefs: PrefsMap) -> TargetResolver:
        async def fetch_hosts():
            async with self._panel_client() as api:
                return await api.get_all_hosts()

        async def fetch_nodes():
            async with self._panel_client() as api:
                return await api.get_all_nodes()

        async def fetch_links(short_uuid: str):
            # Своя подписка по умолчанию — как видит клиент (с балансировщиком «АВТО»);
            # подписки пользователей — через API, чтобы не занимать им HWID-слот.
            async with self._panel_client() as api:
                return await fetch_panel_links(api, short_uuid, prefer_public=short_uuid == self.reference_short_uuid())

        return TargetResolver(
            fetch_hosts=fetch_hosts,
            fetch_nodes=fetch_nodes,
            fetch_links=fetch_links,
            fetch_url_links=self._url_fetcher,
            prefs=prefs,
        )

    async def resolver(self, db: AsyncSession) -> TargetResolver:
        return self._resolver_with(await self._prefs(db))

    async def hosts(self, db: AsyncSession, include_disabled: bool = False) -> list[HostView]:
        return await (await self.resolver(db)).hosts(include_disabled=include_disabled)

    async def nodes(self, db: AsyncSession) -> list[NodeView]:
        return await (await self.resolver(db)).nodes()

    async def subscription_configs(
        self, db: AsyncSession, *, short_uuid: str | None = None, user_id: int | None = None
    ) -> SubscriptionConfigs:
        if not short_uuid and user_id is not None:
            short_uuid = await self._short_uuid_for_user(db, user_id)
            if not short_uuid:
                raise TargetResolutionError(f'У пользователя #{user_id} нет подписки панели Remnawave')
        short_uuid = short_uuid or self.reference_short_uuid()
        if not short_uuid:
            raise ReachabilityDisabled('Не задана эталонная подписка панели (BSCHEK_REFERENCE_SUBSCRIPTION)')
        return await (await self.resolver(db)).subscription_configs(short_uuid)

    # ------------------------------------------------------------ поле «Конфиг или подписка»

    async def parse_input(self, db: AsyncSession, text: str) -> ParsedInput:
        """Ссылки, URL подписок и base64 из одного поля → конфиги с готовыми ссылками для задачи."""
        resolver = await self.resolver(db)
        configs: list[ParsedConfig] = []
        rejected: list[RejectedLink] = []
        sources: list[dict] = []
        links_count = 0
        for line in expand_raw_input(text):
            if is_subscription_url(line):
                found, bad = await self._parse_subscription_url(resolver, line)
                configs.extend(found)
                rejected.extend(bad)
                sources.append({'kind': 'subscription', 'label': line, 'count': len(found)})
                continue
            parsed, bad = parse_links(line)
            rejected.extend(bad)
            for link in parsed:
                target = target_from_link(link, KIND_CUSTOM, {})
                configs.append(ParsedConfig(target=target, target_in={'kind': 'custom', 'value': line}))
                links_count += 1
        if links_count:
            sources.insert(0, {'kind': 'links', 'label': 'ссылки', 'count': links_count})
        return ParsedInput(configs=configs, rejected=rejected, sources=sources)

    async def _parse_subscription_url(
        self, resolver: TargetResolver, url: str
    ) -> tuple[list[ParsedConfig], list[RejectedLink]]:
        """Подписка своей панели — через её API по shortUuid из адреса; иначе загружаем сам URL."""
        candidate = url.rstrip('/').rsplit('/', 1)[-1]
        if _SHORT_UUID_RE.match(candidate):
            try:
                own = await resolver.subscription_configs(candidate)
            except (PanelUnavailable, TargetResolutionError):
                own = None
            if own is not None and own.configs:
                return self._parsed_configs(own, {'short_uuid': candidate}), list(own.rejected)
        try:
            fetched = await resolver.subscription_configs(url)
        except SubscriptionFetchError as exc:
            logger.info('Подписка по URL не загружена', url=url, error=str(exc))
            return [], [RejectedLink(url, 'subscription_failed')]
        return self._parsed_configs(fetched, {'url': url}), list(fetched.rejected)

    @staticmethod
    def _parsed_configs(configs: SubscriptionConfigs, source_ref: dict) -> list[ParsedConfig]:
        return [
            ParsedConfig(
                target=target,
                target_in={
                    'kind': 'subscription_config',
                    **source_ref,
                    'index': index,
                    'target_key': target.target_key,
                },
            )
            for index, target in enumerate(configs.configs)
        ]

    @staticmethod
    async def _short_uuid_for_user(db: AsyncSession, user_id: int) -> str | None:
        rows = await db.execute(
            select(Subscription.remnawave_short_uuid)
            .where(Subscription.user_id == user_id, Subscription.remnawave_short_uuid.is_not(None))
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return rows.scalar_one_or_none()

    # ------------------------------------------------------------ preview

    async def _quote_probe(self, db: AsyncSession, targets: list[Target], units: list[str], payload: dict) -> Quote:
        request = build_probe_request(
            targets,
            units,
            str(payload.get('dpi') or 'on'),
            payload.get('probes') or {},
            sni_hosts=list(payload.get('sni_hosts') or []),
            default_sni=self.default_sni(),
        )
        warnings = []
        if any(target.purpose == PURPOSE_BS for target in targets) and not request['probes']['sni']:
            warnings.append('У хостов под Белый список без SNI-пробы вердикт ненадёжен: Reality даёт ложный blocked')
        price = await self._call(lambda api: api.preview_probe(request))
        return Quote(request, credits_to_kopeks(price.get('cost_credits')), True, warnings)

    async def _quote_vless(self, db: AsyncSession, targets: list[Target], units: list[str], payload: dict) -> Quote:
        request = build_vless_request(targets, units, str(payload.get('dpi') or 'on'), str(payload.get('core') or ''))
        leg_kopeks = await crud.last_vless_leg_price_kopeks(db)
        cost = estimate_vless_kopeks(len(targets), len(units), leg_kopeks)
        return Quote(request, cost, False, ['Точная цена VLESS-теста известна только после запуска'])

    async def _quote_scan(self, db: AsyncSession, targets: list[Target], units: list[str], payload: dict) -> Quote:
        cidr = next((target for target in targets if target.kind == KIND_CIDR), None)
        if cidr is None:
            raise ValueError('Для скана нужна подсеть /24')
        # Свои имена → имена целей рядом с подсетью (хост панели) → «SNI-хост по умолчанию».
        sni_hosts = resolve_sni_hosts(
            [target for target in targets if target.kind != KIND_CIDR],
            list(payload.get('sni_hosts') or []),
            self.default_sni(),
        )
        request = build_scan_request(
            cidr, units, str(payload.get('dpi') or 'on'), payload.get('probes') or {}, sni_hosts
        )
        price = await self._call(lambda api: api.preview_scan(request))
        return Quote(request, credits_to_kopeks(price.get('cost_credits')), True)

    async def preview(self, db: AsyncSession, payload: dict) -> PreviewResult:
        """Всё, что можно узнать до денег: цели, симки, пропуски, цена, предупреждения."""
        self._ensure_enabled()
        kind = str(payload.get('kind') or '')
        if kind not in JOB_KINDS:
            raise ValueError(f'Неизвестный вид задачи «{kind}»')
        targets = await (await self.resolver(db)).resolve(list(payload.get('targets') or []))
        expansion = await self._expand_units(list(payload.get('units') or []), str(payload.get('dpi') or 'on'))
        quote_by_kind = {KIND_PROBE: self._quote_probe, KIND_VLESS: self._quote_vless, KIND_SCAN: self._quote_scan}
        quote = await quote_by_kind[kind](db, targets, expansion.resolved, payload)
        warnings = [*quote.warnings, NO_UNITS_MESSAGE] if not expansion.resolved else list(quote.warnings)
        return PreviewResult(
            kind=kind,
            targets=targets,
            units_resolved=expansion.resolved,
            skipped=_skipped(expansion),
            cost_kopeks=quote.cost_kopeks,
            estimate_is_exact=quote.exact,
            warnings=warnings,
            balance_kopeks=await self._balance_kopeks(),
            request=quote.request,
        )

    # ------------------------------------------------------------ запуск

    async def create_job(self, db: AsyncSession, payload: dict, admin_id: int) -> ReachabilityJob:
        self._ensure_enabled()
        kind = str(payload.get('kind') or '')
        if kind in EXCLUSIVE_KINDS:
            active = await crud.get_active_job(db, kind)
            if active is not None:
                raise ReachabilityBusy(active)
        preview = await self.preview(db, payload)
        if not preview.units_resolved:
            raise ValueError(f'{NO_UNITS_MESSAGE} — выберите режим «любой» или другие симки')
        enforce_cost_limit(preview.cost_kopeks, self.cost_limit_kopeks())
        if preview.balance_kopeks is not None and (preview.cost_kopeks or 0) > preview.balance_kopeks:
            raise ValueError('На балансе bschekbot не хватает средств на эту задачу')
        job = await crud.create_job(db, **self._job_fields(preview, payload, admin_id))
        await db.commit()
        self._account.invalidate()
        self.runner.spawn(job.id)
        logger.info('Задача проверки запущена', job_id=job.id, kind=job.kind, admin_id=admin_id)
        return job

    @staticmethod
    def _job_fields(preview: PreviewResult, payload: dict, admin_id: int) -> dict[str, Any]:
        return {
            'kind': preview.kind,
            'status': 'pending',
            'trigger': 'manual',
            'started_by_user_id': admin_id,
            'idempotency_key': str(uuid.uuid4()),
            'request': preview.request,
            'targets': [target.as_dict() for target in preview.targets],
            'units_requested': list(payload.get('units') or []),
            'units_resolved': preview.units_resolved,
            'skipped': preview.skipped,
            'dpi': str(payload.get('dpi') or 'on'),
            'estimated_kopeks': preview.cost_kopeks,
            'estimate_is_exact': preview.estimate_is_exact,
        }

    # ------------------------------------------------------------ история и управление

    async def list_jobs(self, db: AsyncSession, **filters: Any) -> tuple[list[ReachabilityJob], int]:
        return await crud.list_jobs(db, **filters)

    async def get_job(self, db: AsyncSession, job_id: int) -> ReachabilityJob:
        job = await crud.get_job(db, job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job

    async def cancel_job(self, db: AsyncSession, job_id: int) -> ReachabilityJob:
        self._ensure_enabled()
        job = await self.get_job(db, job_id)
        await self.runner.cancel(db, job)
        if not self.runner.is_active(job.id):
            self.runner.spawn_resume(job.id)
        return job

    async def retrieve_job(self, db: AsyncSession, job_id: int) -> ReachabilityJob:
        """Кнопка «Забрать результат»: поднять зависшую задачу, не дожидаясь обходчика."""
        self._ensure_enabled()
        job = await self.get_job(db, job_id)
        if job.status not in crud.ACTIVE_STATUSES:
            raise JobNotCancellable('Задача уже завершена, забирать нечего')
        if not self.runner.is_active(job.id):
            self.runner.spawn_resume(job.id)
        return job

    # ------------------------------------------------------------ пачки проверок

    async def preview_batch(self, db: AsyncSession, payload: dict) -> batch_ops.BatchPreview:
        self._ensure_enabled()
        return await batch_ops.preview_batch(self, db, payload)

    async def create_batch(self, db: AsyncSession, payload: dict, admin_id: int) -> ReachabilityBatch:
        self._ensure_enabled()
        return await batch_ops.create_batch(self, db, payload, admin_id)

    async def get_batch(self, db: AsyncSession, batch_id: int) -> ReachabilityBatch:
        batch = await crud.get_batch(db, batch_id)
        if batch is None:
            raise JobNotFound(batch_id)
        return batch

    async def list_batches(
        self, db: AsyncSession, *, offset: int = 0, limit: int = 20
    ) -> tuple[list[ReachabilityBatch], int]:
        return await crud.list_batches(db, offset=offset, limit=limit)

    async def cancel_batch(self, db: AsyncSession, batch_id: int) -> ReachabilityBatch:
        """Погасить очередь и остановить идущие пробы; итог пачке подведёт драйвер."""
        self._ensure_enabled()
        batch = await self.get_batch(db, batch_id)
        await self.runner.cancel_batch(db, batch)
        if not self.runner.is_batch_active(batch.id):
            self.runner.spawn_batch(batch.id)
        return batch

    async def status(self, db: AsyncSession) -> dict:
        return await collect_status(self, db)

    async def _summary_units(self, dpi: str | None, legs: list[Any]) -> list[dict]:
        try:
            catalog = [{**unit.as_dict(), 'in_catalog': True} for unit in await self.units(dpi=dpi)]
        except (ReachabilityDisabled, ReachabilityUnhealthy, BschekAPIError):
            catalog = []
        known = {unit['op_key'] for unit in catalog}
        gone = sorted({leg.op_key for leg in legs} - known)
        return [*catalog, *({'op_key': op_key, 'in_catalog': False} for op_key in gone)]

    async def summary(self, db: AsyncSession, dpi: str = 'on') -> dict:
        dpi_filter = None if dpi == 'any' else dpi
        legs = await crud.latest_legs(db, target_kind=KIND_HOST, dpi=dpi_filter)
        prefs = await self._prefs(db)
        panel_error = None
        try:
            hosts = await self._resolver_with(prefs).hosts()
        except PanelUnavailable as exc:
            hosts, panel_error = [], exc.reason
        return {
            'dpi': dpi,
            'units': await self._summary_units(dpi_filter, legs),
            'rows': build_summary_rows(legs, hosts, prefs),
            'panel_error': panel_error,
        }

    async def update_pref(
        self,
        db: AsyncSession,
        *,
        target_kind: str,
        target_ref: str,
        purpose: str | None,
        excluded: bool | None,
        note: str | None,
        admin_id: int,
    ) -> ReachabilityTargetPref:
        pref = await crud.upsert_pref(
            db,
            target_kind=target_kind,
            target_ref=target_ref,
            purpose=purpose,
            excluded=excluded,
            note=note,
            user_id=admin_id,
        )
        await db.commit()
        return pref

    # ------------------------------------------------------------ фон

    def start_background(self) -> None:
        """Идемпотентно: живой обходчик не трогает, упавший — перезапускает с записью причины."""
        task = self._background
        if task is not None and not task.done():
            return
        if task is not None and not task.cancelled() and task.exception() is not None:
            logger.error('Обходчик задач проверки упал, перезапуск', error=str(task.exception()))
        self._background = asyncio.create_task(self.runner.sweeper_loop())

    async def stop_background(self) -> None:
        self.runner.stop()
        task, self._background = self._background, None
        if task is not None:
            task.cancel()
            await asyncio.wait([task])


reachability_service = ReachabilityService()
