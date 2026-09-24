"""Фасад DPI//CHECKER: единственная точка, которую зовут ручки кабинета, вебхук и обходчик мониторов.

Порядок у платного запуска: строка ``dpichecker_actions`` с ключом идемпотентности пишется и
коммитится ДО обращения к сервису; обрыв ответа повторяется тем же ключом (сервис вернёт исходный
ответ без второго списания); отказ сервиса оставляет строку ``rejected`` с кодом; молчание сервиса
после повторов — ``unknown`` (деньги могли списаться — проверка видна в истории).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import dpichecker as crud
from app.database.models import DpiCheckerAction
from app.external.dpichecker_api import DpiCheckerAPI, DpiCheckerAPIError, DpiCheckerGatewayError
from app.services.dpichecker.errors import ActionNotFound, DpiCheckerDisabled, LaunchRefused, human_error
from app.services.dpichecker.presenter import present_check
from app.services.dpichecker.regions import group_pops
from app.services.dpichecker.targets import PanelTarget, host_addresses, node_addresses, subscription_keys


logger = structlog.get_logger(__name__)

GATEWAY_RETRIES = 2
GATEWAY_PAUSE_SEC = 2.0
USD = Decimal('0.0001')
FINISHED = frozenset({'completed', 'failed', 'cancelled'})
CHEREMSHA_MAX = 20
MONITOR_PATCH_FIELDS = frozenset({'is_active', 'interval_hours', 'notify_on_success', 'alert_after_fails'})
# Адрес вебхука и код привязки группы их бота — служебное, в кабинет не нужно.
HIDDEN_MONITOR_FIELDS = frozenset({'callback_url', 'link_code', 'link_instructions'})


def usd(value: Any) -> Decimal | None:
    """Сумма сервиса (число у проверок, строка у Зонда) → Decimal с 4 знаками."""
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value)).quantize(USD)
    except (InvalidOperation, ValueError):
        return None


def _default_panel_client() -> Any:
    from app.services.remnawave_service import RemnaWaveService

    return RemnaWaveService().get_api_client()


class DpiCheckerService:
    def __init__(
        self,
        *,
        api_factory: Callable[[], Any] | None = None,
        panel_client: Callable[[], Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_factory = api_factory
        self._panel_client = panel_client or _default_panel_client
        self._sleep = sleep
        self._secret: tuple[str, str] | None = None  # (отпечаток ключа API, секрет подписи)

    # ------------------------------------------------------------ доступ

    @staticmethod
    def _guard() -> None:
        if not settings.is_dpichecker_enabled():
            raise DpiCheckerDisabled('DPI//CHECKER выключен в настройках')
        if not settings.is_dpichecker_configured():
            raise DpiCheckerDisabled('Не задан ключ API DPI//CHECKER')

    def _api(self) -> Any:
        self._guard()
        if self._api_factory is not None:
            return self._api_factory()
        return DpiCheckerAPI(
            settings.DPICHECKER_API_KEY or '',
            base_url=settings.DPICHECKER_API_URL,
            timeout=float(settings.DPICHECKER_REQUEST_TIMEOUT),
        )

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        async with self._api() as api:
            return await getattr(api, method)(*args, **kwargs)

    # ------------------------------------------------------------ статус и справочники

    async def status(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            'enabled': settings.is_dpichecker_enabled(),
            'configured': settings.is_dpichecker_configured(),
            'balance': None,
            'total_spent': None,
            'noisy': None,
            'monitors': None,
            'webhook_ready': settings.get_dpichecker_webhook_url() is not None,
            'error': None,
        }
        if not (state['enabled'] and state['configured']):
            return state
        try:
            async with self._api() as api:
                profile = await api.profile()
                quota = await api.quota()
        except DpiCheckerAPIError as exc:
            logger.warning('DPI//CHECKER: статус не получен', code=exc.code)
            return {**state, 'error': human_error(exc)}
        noisy = quota.get('noisy') or {}
        return {
            **state,
            'balance': profile.get('balance'),
            'total_spent': profile.get('total_spent'),
            'noisy': {key: noisy.get(key) for key in ('limit', 'used', 'remaining', 'unlimited', 'resets_at')},
            'monitors': quota.get('monitors'),
        }

    async def pops(self, location: str) -> dict[str, Any]:
        data = await self._call('pops', location)
        pops = list(data.get('pops') or [])
        return {'pops': pops, 'groups': group_pops(location, pops)}

    async def optimal(self, location: str) -> list[int]:
        return [int(pop_id) for pop_id in (await self._call('optimal_pops', location)).get('pop_ids') or []]

    async def tariffs(self) -> dict[str, Any]:
        return await self._call('tariffs')

    async def parse(self, check_type: str, text: str) -> dict[str, Any]:
        return await self._call('parse', check_type, text)

    async def panel_targets(
        self, db: AsyncSession, *, kind: str, user_id: int | None = None, uuids: list[str] | tuple[str, ...] = ()
    ) -> list[PanelTarget]:
        if kind == 'subscription':
            if user_id is None:
                raise ValueError('Не выбран пользователь')
            return await subscription_keys(db, user_id=user_id, panel_client=self._panel_client)
        if kind == 'hosts':
            return await host_addresses(panel_client=self._panel_client, host_uuids=list(uuids))
        if kind == 'nodes':
            return await node_addresses(panel_client=self._panel_client, node_uuids=list(uuids))
        raise ValueError(f'Неизвестный источник целей: {kind}')

    @staticmethod
    def _resources_field(check_type: str, values: list[str]) -> dict[str, list[str]]:
        # VPN — ключами: подписку кабинет заранее разворачивает (/checks/parse), иначе сервис
        # считает её за один ресурс и цена выходит неверной.
        return {'keys': values} if check_type == 'vpn' else {'resources': values}

    async def estimate(self, check_type: str, location: str, pop_ids: list[int], values: list[str]) -> dict:
        body = {'check_type': check_type, 'location': location, 'pop_ids': list(pop_ids)}
        return await self._call('estimate', {**body, **self._resources_field(check_type, list(values))})

    # ------------------------------------------------------------ запуск

    async def _submit(self, db: AsyncSession, action: DpiCheckerAction, call: Callable[[Any], Awaitable[dict]]) -> dict:
        """Платный POST тем же ключом до ответа; отказ — строка rejected, молчание — unknown."""
        gateway_failures = 0
        waited_rate_limit = False
        async with self._api() as api:
            while True:
                try:
                    return await call(api)
                except DpiCheckerGatewayError as exc:
                    gateway_failures += 1
                    if gateway_failures > GATEWAY_RETRIES:
                        action.status, action.error_code = 'unknown', exc.code
                        await db.commit()
                        logger.warning('DPI//CHECKER молчит на запуске', action_id=action.id, code=exc.code)
                        raise
                    await self._sleep(GATEWAY_PAUSE_SEC)
                except DpiCheckerAPIError as exc:
                    if exc.code == 'rate_limited' and not waited_rate_limit:
                        waited_rate_limit = True
                        await self._sleep(exc.retry_after or 1.0)
                        continue
                    action.status, action.error_code = 'rejected', exc.code
                    await db.commit()
                    logger.info('DPI//CHECKER отказал в запуске', action_id=action.id, code=exc.code)
                    raise LaunchRefused(
                        code=exc.code, message=human_error(exc), status=exc.status or 400, rejected=exc.rejected
                    ) from exc

    async def launch_check(
        self,
        db: AsyncSession,
        *,
        admin_id: int | None,
        check_type: str,
        location: str,
        pop_ids: list[int],
        targets: list[dict[str, str]],
        source: str,
        source_ref: str | None,
        label: str,
        probe_mode: str = 'auto',
    ) -> DpiCheckerAction:
        self._guard()
        values = [str(target['value']) for target in targets]
        body: dict[str, Any] = {
            'location': location,
            'pop_ids': list(pop_ids),
            **self._resources_field(check_type, values),
        }
        if check_type == 'ip':
            body['probe_mode'] = probe_mode
        action = await crud.create_action(
            db,
            kind=crud.KIND_CHECK,
            admin_user_id=admin_id,
            check_type=check_type,
            location=location,
            pop_count=len(pop_ids),
            resource_count=len(values),
            source=source,
            source_ref=source_ref,
            label=label or ', '.join(target.get('name') or '' for target in targets)[:255],
            targets=[{'value': str(t['value']), 'name': str(t.get('name') or t['value'])} for t in targets],
            request=body,
        )
        await db.commit()
        response = await self._submit(
            db, action, lambda api: api.start_check(check_type, body, idempotency_key=action.idempotency_key)
        )
        action.remote_id = int(response['check_id'])
        action.status = str(response.get('status') or 'pending')
        action.cost_usd = usd(response.get('estimated_cost'))
        await db.commit()
        return action

    # ------------------------------------------------------------ проверка

    async def _action(self, db: AsyncSession, action_id: int, kind: str) -> DpiCheckerAction:
        action = await crud.get_action(db, action_id)
        if action is None or action.kind != kind:
            raise ActionNotFound
        return action

    @staticmethod
    def _names(action: DpiCheckerAction) -> dict[str, str]:
        return {str(t.get('value')): str(t.get('name') or '') for t in action.targets or [] if t.get('name')}

    async def get_check(self, db: AsyncSession, action_id: int, *, wait: int = 0) -> dict[str, Any]:
        action = await self._action(db, action_id, crud.KIND_CHECK)
        if action.remote_id is None:
            stub = {'id': None, 'status': action.status, 'check_type': action.check_type, 'location': action.location}
            return {'action': action, 'check': present_check({**stub, 'results': None}, {})}
        if wait > 0 and action.status not in FINISHED:
            check = await self._call('wait_check', action.remote_id, wait)
        else:
            check = await self._call('get_check', action.remote_id)
        self._apply_status(action, check.get('status'))
        await db.commit()
        return {'action': action, 'check': present_check(check, self._names(action))}

    @staticmethod
    def _apply_status(action: DpiCheckerAction, status: Any) -> None:
        if status:
            action.status = str(status)
        if action.status == 'cancelled' and action.refunded_usd is None:
            action.refunded_usd = action.cost_usd

    async def cancel_check(self, db: AsyncSession, action_id: int) -> DpiCheckerAction:
        action = await self._action(db, action_id, crud.KIND_CHECK)
        if action.remote_id is None:
            raise LaunchRefused(
                code='not_cancellable', message='Проверка не дошла до DPI//CHECKER', status=409, rejected=[]
            )
        try:
            response = await self._call('cancel_check', action.remote_id)
        except DpiCheckerGatewayError:
            raise  # молчание сервиса — не отказ: пусть ручка скажет «не ответил»
        except DpiCheckerAPIError as exc:
            raise LaunchRefused(
                code=exc.code, message=human_error(exc), status=exc.status or 409, rejected=exc.rejected
            ) from exc
        action.status = str(response.get('status') or 'cancelled')
        action.refunded_usd = usd(response.get('refunded'))
        await db.commit()
        return action

    async def report_csv(self, db: AsyncSession, action_id: int) -> tuple[bytes, str]:
        action = await self._action(db, action_id, crud.KIND_CHECK)
        if action.remote_id is None:
            raise ActionNotFound
        return await self._call('report_csv', action.remote_id)

    async def check_map(self, db: AsyncSession, action_id: int) -> tuple[bytes, str]:
        action = await self._action(db, action_id, crud.KIND_CHECK)
        if action.remote_id is None:
            raise ActionNotFound
        return await self._call('check_map', action.remote_id)

    # ------------------------------------------------------------ Зонд и Шумные соседи

    async def _launch_scan(
        self,
        db: AsyncSession,
        *,
        kind: str,
        admin_id: int | None,
        target: str,
        source: str,
        source_ref: str | None,
        label: str,
    ) -> DpiCheckerAction:
        self._guard()
        target = target.strip()
        action = await crud.create_action(
            db,
            kind=kind,
            admin_user_id=admin_id,
            check_type=None,
            location='russia',
            pop_count=0,
            resource_count=1,
            source=source,
            source_ref=source_ref,
            label=label or target,
            targets=[{'value': target, 'name': label or target}],
            request={'target': target},
        )
        await db.commit()
        start = 'start_probe' if kind == crud.KIND_PROBE else 'start_noisy'
        response = await self._submit(
            db, action, lambda api: getattr(api, start)(target, idempotency_key=action.idempotency_key)
        )
        action.remote_id = int(response['scan_id'])
        action.status = str(response.get('status') or 'pending')
        action.cost_usd = usd(response.get('fixed_cost')) if kind == crud.KIND_PROBE else Decimal(0).quantize(USD)
        await db.commit()
        return action

    async def launch_probe(
        self, db: AsyncSession, *, admin_id: int | None, target: str, source: str, source_ref: str | None, label: str
    ) -> DpiCheckerAction:
        """Зонд: фикс списывается сразу, трафик — по итогу (дописывается в get_scan)."""
        return await self._launch_scan(
            db,
            kind=crud.KIND_PROBE,
            admin_id=admin_id,
            target=target,
            source=source,
            source_ref=source_ref,
            label=label,
        )

    async def launch_noisy(
        self, db: AsyncSession, *, admin_id: int | None, target: str, source: str, source_ref: str | None, label: str
    ) -> DpiCheckerAction:
        """Шумные соседи: бесплатно, до 10 в сутки (квота — у сервиса, отказ — 429 quota_exceeded)."""
        return await self._launch_scan(
            db,
            kind=crud.KIND_NOISY,
            admin_id=admin_id,
            target=target,
            source=source,
            source_ref=source_ref,
            label=label,
        )

    async def get_scan(self, db: AsyncSession, action_id: int) -> dict[str, Any]:
        action = await crud.get_action(db, action_id)
        if action is None or action.kind not in (crud.KIND_PROBE, crud.KIND_NOISY) or action.remote_id is None:
            raise ActionNotFound
        getter = 'get_probe' if action.kind == crud.KIND_PROBE else 'get_noisy'
        scan = dict(await self._call(getter, action.remote_id))
        if scan.get('status'):
            action.status = str(scan['status'])
        if action.kind == crud.KIND_PROBE:
            fixed, traffic = usd(scan.get('fixed_cost')), usd(scan.get('traffic_cost'))
            if action.status == 'done' and fixed is not None:
                action.cost_usd = fixed + (traffic or Decimal(0))
            # Суммы Зонда сервис отдаёт строками — наружу числами, как у проверок.
            scan['fixed_cost'] = float(fixed) if fixed is not None else None
            scan['traffic_cost'] = float(traffic) if traffic is not None else None
        await db.commit()
        return {'action': action, 'scan': scan}

    async def noisy_csv(self, db: AsyncSession, action_id: int) -> tuple[bytes, str]:
        action = await crud.get_action(db, action_id)
        if action is None or action.kind != crud.KIND_NOISY or action.remote_id is None:
            raise ActionNotFound
        return await self._call('noisy_csv', action.remote_id)

    # ------------------------------------------------------------ бесплатные справки

    async def cheremsha(self, resources: list[str]) -> dict[str, Any]:
        cleaned = [item.strip() for item in resources if item.strip()][:CHEREMSHA_MAX]
        return await self._call('cheremsha', cleaned)

    async def ip_lookup(self, ip: str, *, bgp: bool = False) -> dict[str, Any]:
        return await self._call('ip_lookup', ip, bgp=bgp)

    async def blacklist(self, resource: str) -> dict[str, Any]:
        return await self._call('blacklist_check', resource)

    # ------------------------------------------------------------ мониторы

    async def create_monitor(
        self,
        db: AsyncSession,
        *,
        admin_id: int | None,
        check_type: str,
        location: str,
        pop_ids: list[int],
        targets: list[dict[str, str]],
        source: str,
        source_ref: str | None,
        label: str,
        interval_hours: int,
        alert_after_fails: int,
        notify_on_success: bool,
        probe_mode: str = 'auto',
    ) -> DpiCheckerAction:
        """Монитор бесплатен, каждый прогон стоит как проверка. VPN-ключи — в ``resources`` (так шлёт сайт)."""
        self._guard()
        body: dict[str, Any] = {
            'check_type': check_type,
            'location': location,
            'pop_ids': list(pop_ids),
            'resources': [str(target['value']) for target in targets],
            'interval_hours': interval_hours,
            'alert_after_fails': alert_after_fails,
            'notify_on_success': notify_on_success,
        }
        if check_type == 'ip':
            body['probe_mode'] = probe_mode
        callback = settings.get_dpichecker_webhook_url()
        if callback:
            body['callback_url'] = callback
        action = await crud.create_action(
            db,
            kind=crud.KIND_MONITOR,
            admin_user_id=admin_id,
            check_type=check_type,
            location=location,
            pop_count=len(pop_ids),
            resource_count=len(targets),
            source=source,
            source_ref=source_ref,
            label=label or ', '.join(target.get('name') or '' for target in targets)[:255],
            targets=[{'value': str(t['value']), 'name': str(t.get('name') or t['value'])} for t in targets],
            request=body,
        )
        await db.commit()
        response = await self._submit(db, action, lambda api: api.create_monitor(body))
        action.remote_id = int(response['id'])
        action.status = 'active' if response.get('is_active', True) else 'paused'
        await db.commit()
        return action

    async def list_monitors(self, db: AsyncSession) -> list[dict[str, Any]]:
        """Мониторы сервиса; у созданных из кабинета — номер своей строки и имя."""
        data = await self._call('list_monitors', limit=100)
        own = {action.remote_id: action for action in await crud.list_monitors(db)}
        items = []
        for monitor in data.get('items') or []:
            action = own.get(monitor.get('id'))
            public = {key: value for key, value in monitor.items() if key not in HIDDEN_MONITOR_FIELDS}
            items.append(
                {**public, 'action_id': action.id if action else None, 'label': action.label if action else None}
            )
        return items

    async def _monitor(self, db: AsyncSession, action_id: int) -> DpiCheckerAction:
        action = await self._action(db, action_id, crud.KIND_MONITOR)
        if action.remote_id is None:
            raise ActionNotFound
        return action

    async def update_monitor(self, db: AsyncSession, action_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        action = await self._monitor(db, action_id)
        body = {key: value for key, value in patch.items() if key in MONITOR_PATCH_FIELDS and value is not None}
        monitor = await self._call('update_monitor', action.remote_id, body)
        if 'is_active' in monitor:
            action.status = 'active' if monitor['is_active'] else 'paused'
        await db.commit()
        return {key: value for key, value in monitor.items() if key not in HIDDEN_MONITOR_FIELDS}

    async def delete_monitor(self, db: AsyncSession, action_id: int) -> dict[str, Any]:
        action = await self._monitor(db, action_id)
        result = await self._call('delete_monitor', action.remote_id)
        action.status = 'deleted'
        await db.commit()
        return result

    async def monitor_runs(
        self, db: AsyncSession, action_id: int, *, limit: int = 25, offset: int = 0
    ) -> dict[str, Any]:
        action = await self._monitor(db, action_id)
        return await self._call('monitor_runs', action.remote_id, limit=limit, offset=offset)

    # ------------------------------------------------------------ история и траты

    async def history(
        self,
        db: AsyncSession,
        *,
        kind: str | None,
        check_type: str | None,
        admin_user_id: int | None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[DpiCheckerAction], int]:
        return await crud.list_actions(
            db, kind=kind, check_type=check_type, admin_user_id=admin_user_id, limit=limit, offset=offset
        )

    async def spend(self, db: AsyncSession) -> list[tuple[int | None, Decimal]]:
        return await crud.spend_by_admin(db)

    # ------------------------------------------------------------ вебхук: секрет подписи

    async def webhook_secret(self, *, refresh: bool = False) -> str:
        """Секрет подписи — у сервиса; держим в памяти, ключ кэша — отпечаток ключа API."""
        fingerprint = hashlib.sha256((settings.DPICHECKER_API_KEY or '').encode()).hexdigest()
        if not refresh and self._secret is not None and self._secret[0] == fingerprint:
            return self._secret[1]
        secret = await self._call('webhook_secret')
        self._secret = (fingerprint, secret)
        return secret


dpichecker_service = DpiCheckerService()
