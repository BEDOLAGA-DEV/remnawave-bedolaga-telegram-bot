"""Фасад DPI//CHECKER: единственная точка, которую зовут ручки кабинета, вебхук и обходчик мониторов.

Порядок у платного запуска: строка ``dpichecker_actions`` с ключом идемпотентности пишется и
коммитится ДО обращения к сервису; обрыв ответа повторяется тем же ключом (сервис вернёт исходный
ответ без второго списания); отказ сервиса оставляет строку ``rejected`` с кодом; молчание сервиса
после повторов — ``unknown`` (деньги могли списаться — проверка видна в истории).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import dpichecker as crud
from app.database.models import DpiCheckerAction
from app.external.dpichecker_api import DpiCheckerAPI, DpiCheckerAPIError, DpiCheckerGatewayError
from app.services.dpichecker.account import AccountMixin, insert_adopted
from app.services.dpichecker.common import USD, _label, _row_targets, _status, usd
from app.services.dpichecker.errors import ActionNotFound, DpiCheckerDisabled, LaunchRefused, human_error
from app.services.dpichecker.monitor_watch import MonitorWatch
from app.services.dpichecker.presenter import normalize_location, present_check
from app.services.dpichecker.regions import group_pops
from app.services.dpichecker.targets import (
    PanelTarget,
    PanelTargetError,
    host_addresses,
    is_vpn_key,
    node_addresses,
    safe_name,
    subscription_keys,
)


logger = structlog.get_logger(__name__)

GATEWAY_RETRIES = 2
GATEWAY_PAUSE_SEC = 2.0
IN_FLIGHT_CODE = 'idempotency_in_flight'
SECRET_REFRESH_MIN_SEC = 60.0
RESUBMITTABLE = frozenset({'submitting', 'unknown'})
DELETED_REASON = 'deleted_via_api'
FINISHED = frozenset({'completed', 'failed', 'cancelled'})
CHEREMSHA_MAX = 20
MONITOR_PATCH_FIELDS = frozenset({'is_active', 'interval_hours', 'notify_on_success', 'alert_after_fails'})
# Адрес вебхука и ресурсы (ключи VPN, ссылки прокси) — служебное, в кабинет не нужно.
HIDDEN_MONITOR_FIELDS = frozenset({'callback_url', 'link_code', 'link_instructions', 'resources'})
NOTIFY_GROUP = 'group'


def _public_monitor(monitor: dict[str, Any]) -> dict[str, Any]:
    """Монитор для кабинета: без служебного; код привязки группы — только пока группа не привязана
    (его отправляют их боту в группе командой ``/link <код>``)."""
    public = {key: value for key, value in monitor.items() if key not in HIDDEN_MONITOR_FIELDS}
    waiting_group = monitor.get('notify') == NOTIFY_GROUP and not monitor.get('group_linked')
    return {
        **public,
        'resource_count': len(monitor.get('resources') or []),
        'link_code': monitor.get('link_code') if waiting_group else None,
    }


def _default_panel_client() -> Any:
    from app.services.remnawave_service import RemnaWaveService

    return RemnaWaveService().get_api_client()


class DpiCheckerService(AccountMixin):
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
        self._watch: MonitorWatch | None = None
        self._secret_lock = asyncio.Lock()
        self._secret_refreshed_at = 0.0
        self._background: asyncio.Task | None = None

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
            'reference': None,
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
            'reference': self._reference_status(),
            'balance': profile.get('balance'),
            'total_spent': profile.get('total_spent'),
            'noisy': {key: noisy.get(key) for key in ('limit', 'used', 'remaining', 'unlimited', 'resets_at')},
            'monitors': quota.get('monitors'),
        }

    @staticmethod
    def _reference() -> str:
        """Подписка по умолчанию из настроек: ссылка подписки или shortUuid панели (как у BSCHEKER)."""
        return (settings.DPICHECKER_REFERENCE_SUBSCRIPTION or '').strip()

    async def _reference_keys(self) -> list[PanelTarget]:
        """Ключи подписки по умолчанию. Ссылку разворачивает сам DPI//CHECKER — панель не нужна
        (подходит и чужая подписка); shortUuid читается из своей панели теми же помощниками, что у BSCHEKER."""
        reference = self._reference()
        if not reference:
            raise PanelTargetError(
                'Подписка по умолчанию не задана (DPICHECKER_REFERENCE_SUBSCRIPTION) — выберите пользователя'
            )
        if not reference.startswith(('http://', 'https://')):
            return await subscription_keys(None, short_uuid=reference, panel_client=self._panel_client)
        ref = reference.rstrip('/').rsplit('/', 1)[-1]
        parsed = await self.parse('vpn', reference)
        keys = [
            PanelTarget(value=str(key['uri']), name=safe_name('vpn', str(key['uri']), key.get('name')), ref=ref)
            for key in parsed.get('keys') or []
            if key.get('uri')
        ]
        if not keys:
            raise PanelTargetError('В подписке по умолчанию нет ключей для проверки')
        return keys

    def _reference_status(self) -> dict[str, Any]:
        """Какая подписка задана — без сети: статус открывает каждую вкладку, а разворот подписки у сервиса
        шёл до 10 с и держал раздел пустым. Ключи и ошибка подписки видны, когда форма их загружает."""
        reference = self._reference()
        if not reference:
            return {'short_uuid': None, 'configs': None, 'error': 'Подписка по умолчанию не задана'}
        return {'short_uuid': reference.rstrip('/').rsplit('/', 1)[-1], 'configs': None, 'error': None}

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
                return await self._reference_keys()
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

    async def _mark_unknown(self, db: AsyncSession, action: DpiCheckerAction, code: str) -> None:
        action.status, action.error_code = 'unknown', code[:64]
        with contextlib.suppress(Exception):
            await db.commit()
        logger.warning('DPI//CHECKER: исход запуска неизвестен', action_id=action.id, code=code)

    async def _submit(
        self,
        db: AsyncSession,
        action: DpiCheckerAction,
        call: Callable[[Any], Awaitable[dict]],
        apply: Callable[[dict], None],
        *,
        retry: bool = True,
    ) -> DpiCheckerAction:
        """Платный POST тем же ключом до ответа и запись итога в строку.

        Отказ сервиса — ``rejected`` с кодом; молчание, «ещё обрабатывается» сверх терпения, обрыв
        запроса или непонятный ответ — ``unknown`` (деньги могли списаться; такой запуск можно
        переспросить тем же ключом через :meth:`resubmit`). ``retry=False`` — для POST без ключа
        идемпотентности (мониторы): повтор создал бы второй платный монитор.
        """
        failures = 0
        waited_rate_limit = False
        try:
            async with self._api() as api:
                while True:
                    try:
                        response = await call(api)
                        break
                    except DpiCheckerAPIError as exc:
                        waiting = isinstance(exc, DpiCheckerGatewayError) or exc.code == IN_FLIGHT_CODE
                        if waiting:
                            failures += 1
                            if not retry or failures > GATEWAY_RETRIES:
                                await self._mark_unknown(db, action, exc.code)
                                if isinstance(exc, DpiCheckerGatewayError):
                                    raise
                                raise DpiCheckerGatewayError(code=exc.code, message=exc.message) from exc
                            await self._sleep(GATEWAY_PAUSE_SEC)
                            continue
                        if exc.code == 'rate_limited' and not waited_rate_limit:
                            waited_rate_limit = True
                            await self._sleep(exc.retry_after or 1.0)
                            continue
                        action.status, action.error_code = 'rejected', exc.code[:64]
                        await db.commit()
                        logger.info('DPI//CHECKER отказал в запуске', action_id=action.id, code=exc.code)
                        raise LaunchRefused(
                            code=exc.code, message=human_error(exc), status=exc.status or 400, rejected=exc.rejected
                        ) from exc
            try:
                apply(response)
            except (KeyError, TypeError, ValueError) as exc:
                raise DpiCheckerAPIError(code='bad_response', message=f'Непонятный ответ сервиса: {exc}') from exc
            await db.commit()
            return action
        except (DpiCheckerGatewayError, LaunchRefused):
            raise
        except BaseException as exc:
            if action.status == 'submitting' or (action.remote_id is None and action.status != 'rejected'):
                await self._mark_unknown(db, action, getattr(exc, 'code', type(exc).__name__))
            raise

    def _callback(self) -> dict[str, str]:
        url = settings.get_dpichecker_webhook_url()
        return {'callback_url': url} if url else {}

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
        values = [str(target['value']).strip() for target in targets]
        if check_type == 'vpn':
            not_keys = [value for value in values if not is_vpn_key(value)]
            if not_keys:
                raise ValueError(
                    'Для VPN нужны ключи, а не ссылка на подписку — разверните подписку кнопкой «Продолжить»'
                )
        body: dict[str, Any] = {
            'location': location,
            'pop_ids': list(pop_ids),
            **self._resources_field(check_type, values),
            **self._callback(),
        }
        if check_type == 'ip':
            body['probe_mode'] = probe_mode
        rows = _row_targets(check_type, [{**t, 'value': v} for t, v in zip(targets, values, strict=True)])
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
            label=_label(label, rows),
            targets=rows,
            request=body,
        )
        await db.commit()
        return await self._submit(
            db,
            action,
            lambda api: api.start_check(check_type, body, idempotency_key=action.idempotency_key),
            lambda response: self._apply_check_start(action, response),
        )

    @staticmethod
    def _apply_check_start(action: DpiCheckerAction, response: dict) -> None:
        action.remote_id = int(response['check_id'])
        action.status = _status(response.get('status'), 'pending')
        action.cost_usd = usd(response.get('estimated_cost'))

    async def resubmit(self, db: AsyncSession, action_id: int) -> DpiCheckerAction:
        """Незаконченный запуск (сервис не ответил) — переспросить тем же ключом и телом.

        Сервис помнит ключ 24 ч: если запуск прошёл, вернёт исходный ответ без второго списания,
        если нет — выполнит его сейчас.
        """
        self._guard()
        action = await crud.get_action(db, action_id)
        if action is None:
            raise ActionNotFound
        if action.kind == crud.KIND_MONITOR or action.status not in RESUBMITTABLE or action.remote_id is not None:
            raise LaunchRefused(
                code='not_resubmittable', message='Этот запуск уже завершён — повторять нечего', status=409, rejected=[]
            )
        body = dict(action.request or {})
        key = action.idempotency_key
        action.status = 'submitting'
        await db.commit()
        if action.kind == crud.KIND_CHECK:
            return await self._submit(
                db,
                action,
                lambda api: api.start_check(action.check_type, body, idempotency_key=key),
                lambda response: self._apply_check_start(action, response),
            )
        start = 'start_probe' if action.kind == crud.KIND_PROBE else 'start_noisy'
        return await self._submit(
            db,
            action,
            lambda api: getattr(api, start)(body['target'], idempotency_key=key, **self._callback()),
            lambda response: self._apply_scan_start(action, response),
        )

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
        remote_id, status = action.remote_id, action.status
        await db.commit()  # не держать соединение базы, пока ждём сервис (long-poll до минуты)
        if wait > 0 and status not in FINISHED:
            check = await self._call('wait_check', remote_id, wait)
        else:
            check = await self._call('get_check', remote_id)
        self._apply_status(action, check.get('status'))
        await db.commit()
        return {'action': action, 'check': present_check(check, self._names(action))}

    @staticmethod
    def _apply_status(action: DpiCheckerAction, status: Any) -> None:
        if status:
            action.status = _status(status, action.status)
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
        action.status = _status(response.get('status'), 'cancelled')
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
        return await self._submit(
            db,
            action,
            lambda api: getattr(api, start)(target, idempotency_key=action.idempotency_key, **self._callback()),
            lambda response: self._apply_scan_start(action, response),
        )

    @staticmethod
    def _apply_scan_start(action: DpiCheckerAction, response: dict) -> None:
        action.remote_id = int(response['scan_id'])
        action.status = _status(response.get('status'), 'pending')
        action.cost_usd = (
            usd(response.get('fixed_cost')) if action.kind == crud.KIND_PROBE else Decimal(0).quantize(USD)
        )

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
        notify: str = 'dm',
    ) -> DpiCheckerAction:
        """Монитор бесплатен, каждый прогон стоит как проверка. VPN-ключи — в ``resources`` (так шлёт сайт).

        ``notify`` — куда тревоги шлёт их бот: ``dm`` владельцу ключа или ``group`` (код привязки — в списке).
        Итоги в админ-чат бота присылает обходчик — независимо от этого."""
        self._guard()
        body: dict[str, Any] = {
            'check_type': check_type,
            'location': location,
            'pop_ids': list(pop_ids),
            'resources': [str(target['value']) for target in targets],
            'interval_hours': interval_hours,
            'alert_after_fails': alert_after_fails,
            'notify_on_success': notify_on_success,
            'notify': notify,
        }
        if check_type == 'ip':
            body['probe_mode'] = probe_mode
        body.update(self._callback())
        rows = _row_targets(check_type, targets)
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
            label=_label(label, rows),
            targets=rows,
            request=body,
        )
        await db.commit()

        def apply(response: dict) -> None:
            action.remote_id = int(response['id'])
            action.status = 'active' if response.get('is_active', True) else 'paused'

        # У POST /monitors нет ключа идемпотентности — повтор создал бы второй платный монитор.
        return await self._submit(db, action, lambda api: api.create_monitor(body), apply, retry=False)

    async def list_monitors(self, db: AsyncSession) -> list[dict[str, Any]]:
        """Мониторы сервиса; у созданных из кабинета — номер своей строки и имя."""
        data = await self._call('list_monitors', limit=100)
        monitors = list(data.get('items') or [])
        own = await crud.by_remote(db, crud.KIND_MONITOR, [int(m['id']) for m in monitors if m.get('id') is not None])
        items = []
        for monitor in monitors:
            action = own.get(monitor.get('id'))
            items.append(
                {
                    **_public_monitor(monitor),
                    'action_id': action.id if action else None,
                    'label': action.label if action else None,
                    # DELETE у сервиса не стирает монитор — он висит на паузе с этой причиной.
                    'deleted': monitor.get('paused_reason') == DELETED_REASON
                    or (action is not None and action.status == 'deleted'),
                }
            )
        return items

    async def adopt_monitor(self, db: AsyncSession, remote_id: int, *, admin_id: int | None) -> DpiCheckerAction:
        """Монитор, созданный не из кабинета (на сайте, в их боте, через API), — под управление кабинета:
        своя строка, как у созданного здесь, — пауза, отключение, история прогонов и итоги в админ-чат.
        Повтор возвращает ту же строку."""
        existing = await crud.get_by_remote(db, crud.KIND_MONITOR, remote_id)
        if existing is not None and existing.status != 'deleted':
            return existing
        try:
            monitor = await self._call('get_monitor', remote_id)
        except DpiCheckerAPIError as exc:
            if exc.status == 404:
                raise ActionNotFound from exc
            raise
        check_type = str(monitor.get('check_type') or 'ip')
        rows = _row_targets(check_type, [{'value': value} for value in monitor.get('resources') or []])
        status = 'active' if monitor.get('is_active', True) else 'paused'
        if existing is not None:
            # Удалённый из кабинета, но живой у сервиса (восстановлен на сайте) — та же строка снова в деле.
            action = existing
            action.status = status
            await db.commit()
        else:
            action = await insert_adopted(
                db,
                kind=crud.KIND_MONITOR,
                remote_id=remote_id,
                status=status,
                cost_usd=None,
                admin_user_id=admin_id,
                check_type=check_type,
                location=normalize_location(monitor.get('location')),
                pop_count=len(monitor.get('pop_ids') or []),
                resource_count=len(rows),
                source_ref=None,
                label=_label('', rows),
                targets=rows,
                request={},
            )
        await self._attach_webhook(monitor, remote_id)
        return action

    async def _attach_webhook(self, monitor: dict[str, Any], remote_id: int) -> None:
        """Монитору с сайта — адрес вебхука бота: без него начало прогона (``monitor.run``) не приходит,
        и итог ждёт обхода раз в 5 минут. Сбой здесь не отменяет взятие — обходчик всё равно увидит итог."""
        url = self._callback().get('callback_url')
        if not url or monitor.get('callback_url') == url or monitor.get('paused_reason') == DELETED_REASON:
            return
        try:
            await self._call('update_monitor', remote_id, {'callback_url': url})
        except DpiCheckerAPIError as exc:
            logger.warning('DPI//CHECKER: адрес вебхука монитору не выставлен', remote_id=remote_id, code=exc.code)

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
        return _public_monitor(monitor)

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

    # ------------------------------------------------------------ история

    async def history(
        self,
        db: AsyncSession,
        *,
        kind: str | None,
        check_type: str | None,
        admin_user_id: int | None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Страница истории, счётчики фильтров (с учётом «только мои») и имена админов строк."""
        items, total = await crud.list_actions(
            db, kind=kind, check_type=check_type, admin_user_id=admin_user_id, limit=limit, offset=offset
        )
        return {
            'items': items,
            'total': total,
            'counts': await crud.count_by_filter(db, admin_user_id=admin_user_id),
            'admin_names': await crud.admin_names(db, [item.admin_user_id for item in items if item.admin_user_id]),
        }

    # ------------------------------------------------------------ вебхук: секрет подписи

    async def webhook_secret(self, *, refresh: bool = False) -> str:
        """Секрет подписи — у сервиса; держим в памяти, ключ кэша — отпечаток ключа API.

        Перечитывание по неверной подписи — не чаще раза в минуту и в один поток: вебхук открыт всем,
        и мусорные запросы иначе выедали бы лимит ключа (120 запросов в минуту) и глушили запуски.
        """
        fingerprint = hashlib.sha256((settings.DPICHECKER_API_KEY or '').encode()).hexdigest()
        cached = self._secret if self._secret is not None and self._secret[0] == fingerprint else None
        if cached is not None and not refresh:
            return cached[1]
        async with self._secret_lock:
            cached = self._secret if self._secret is not None and self._secret[0] == fingerprint else None
            recent = time.monotonic() - self._secret_refreshed_at < SECRET_REFRESH_MIN_SEC
            if cached is not None and (not refresh or recent):
                return cached[1]
            secret = await self._call('webhook_secret')
            self._secret = (fingerprint, secret)
            self._secret_refreshed_at = time.monotonic()
            return secret

    # ------------------------------------------------------------ вебхук: события

    async def handle_webhook(self, *, event: str, delivery_id: int, payload: dict[str, Any]) -> None:
        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await self.handle_webhook_in(db, event=event, delivery_id=delivery_id, payload=payload)

    async def handle_webhook_in(
        self, db: AsyncSession, *, event: str, delivery_id: int, payload: dict[str, Any]
    ) -> None:
        """Обновить свою строку по событию; уведомлений отсюда нет (итоги мониторов — у обходчика)."""
        if event == 'monitor.run':
            monitor_id = (payload.get('run') or {}).get('monitor_id')
            self.poke_monitor(int(monitor_id) if monitor_id else None)
            return
        kinds = {
            'check.completed': crud.KIND_CHECK,
            'check.cancelled': crud.KIND_CHECK,
            'noisy.done': crud.KIND_NOISY,
            'probe.done': crud.KIND_PROBE,
        }
        kind = kinds.get(event)
        if kind is None:
            logger.info('DPI//CHECKER webhook: событие без обработчика', webhook_event=event)
            return
        body = payload.get('check') if kind == crud.KIND_CHECK else payload.get('scan')
        remote_id = (body or {}).get('id')
        action = await crud.get_by_remote(db, kind, int(remote_id)) if remote_id else None
        if action is None:
            logger.info('DPI//CHECKER webhook: не наш номер', webhook_event=event, remote_id=remote_id)
            return
        if delivery_id and not await crud.claim_delivery(db, action, delivery_id):
            return
        self._apply_status(action, body.get('status'))
        if kind == crud.KIND_PROBE:
            fixed, traffic = usd(body.get('fixed_cost')), usd(body.get('traffic_cost'))
            if fixed is not None:
                action.cost_usd = fixed + (traffic or Decimal(0))
        await db.commit()

    # ------------------------------------------------------------ фон: обходчик мониторов

    def poke_monitor(self, monitor_id: int | None) -> None:
        if self._watch is not None:
            self._watch.poke(monitor_id)

    @property
    def background_running(self) -> bool:
        return self._background is not None and not self._background.done()

    def sync_background(self, notify: Callable[[str], Awaitable[Any]]) -> None:
        """По живым настройкам: включён и с ключом — обходчик идёт (упавший перезапускается), иначе стоит.

        Модуль включают из кабинета без перезапуска бота — поэтому решение каждый раз заново.
        """
        if settings.is_dpichecker_enabled() and settings.is_dpichecker_configured():
            self.start_background(notify)
        elif self.background_running:
            if self._watch is not None:
                self._watch.stop()
            if self._background is not None:
                self._background.cancel()

    def start_background(self, notify: Callable[[str], Awaitable[Any]]) -> None:
        """Идемпотентно: живой обходчик не трогает, упавший — перезапускает с записью причины."""
        task = self._background
        if task is not None and not task.done():
            return
        if task is not None and not task.cancelled() and task.exception() is not None:
            logger.error('Обходчик мониторов DPI//CHECKER упал, перезапуск', error=str(task.exception()))
        from app.database.database import AsyncSessionLocal

        self._watch = MonitorWatch(
            api_factory=self._api,
            session_factory=AsyncSessionLocal,
            notify=notify,
            cabinet_url=lambda: settings.CABINET_URL,
        )
        self._background = asyncio.create_task(self._watch.loop())

    async def stop_background(self) -> None:
        if self._watch is not None:
            self._watch.stop()
        task, self._background = self._background, None
        if task is not None:
            task.cancel()
            await asyncio.wait([task])


dpichecker_service = DpiCheckerService()
