"""Весь аккаунт DPI//CHECKER, а не только запуски из кабинета: проверки с сайта, из их бота, через API
и прогоны мониторов (``GET /checks``), взятие любого из них в историю кабинета, построчный отчёт
(``GET /checks/{id}/report?format=json``) и журнал доставки вебхуков (``GET /webhooks/deliveries``).

Взятый запуск — обычная строка ``dpichecker_actions`` (``source='site'``, откуда он — в ``source_ref``):
дальше его открывают, скачивают CSV и смотрят на карте теми же ручками, что и свои.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud import dpichecker as crud
from app.database.models import DpiCheckerAction
from app.external.dpichecker_api import DpiCheckerAPIError
from app.services.dpichecker.common import SOURCE_SITE, _label, _row_targets, _status, usd
from app.services.dpichecker.errors import ActionNotFound
from app.services.dpichecker.presenter import normalize_location, present_report


logger = structlog.get_logger(__name__)

ACCOUNT_KINDS = (crud.KIND_CHECK, crud.KIND_PROBE, crud.KIND_NOISY)
GETTERS = {crud.KIND_CHECK: 'get_check', crud.KIND_PROBE: 'get_probe', crud.KIND_NOISY: 'get_noisy'}
RESOURCE_FIELD = {'vpn': 'uri', 'ip': 'resource', 'mtproto': 'resource'}
SOURCE_REF_MAX = 128


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _when(value: Any) -> datetime | None:
    """Время сервиса («2026-09-24T06:25:52.162Z») → aware datetime; непонятное — без времени."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def _scan_cost(kind: str, scan: dict[str, Any]) -> Decimal | None:
    """Соседи бесплатны; Зонд — фикс плюс трафик (суммы строками)."""
    if kind == crud.KIND_NOISY:
        return Decimal(0).quantize(Decimal('0.0001'))
    fixed, traffic = usd(scan.get('fixed_cost')), usd(scan.get('traffic_cost'))
    return fixed + (traffic or Decimal(0)) if fixed is not None else None


def _remote_item(kind: str, item: dict[str, Any], own: dict[int, DpiCheckerAction]) -> dict[str, Any]:
    """Строка истории аккаунта: страна кодом, суммы числами, номер своей строки, если запуск уже взят."""
    public = dict(item)
    if 'location' in public:
        public['location'] = normalize_location(public['location'])
    if kind != crud.KIND_CHECK:
        public['usd_cost'] = _money(_scan_cost(kind, item))
    if kind == crud.KIND_PROBE:
        public['fixed_cost'] = _money(usd(item.get('fixed_cost')))
        public['traffic_cost'] = _money(usd(item.get('traffic_cost')))
    action = own.get(item.get('id')) if item.get('id') is not None else None
    return {**public, 'action_id': action.id if action else None}


def _check_row(check: dict[str, Any]) -> dict[str, Any]:
    """Поля строки для проверки: цели из результатов (у VPN имя — «host» ключа, сам ключ — только в строке)."""
    check_type = str(check.get('check_type') or 'ip')
    if check_type not in RESOURCE_FIELD:
        check_type = 'ip'
    field = RESOURCE_FIELD[check_type]
    seen: dict[str, dict[str, Any]] = {}
    for row in check.get('results') or []:
        value = row.get(field)
        if value and not row.get('is_direct') and str(value) not in seen:
            seen[str(value)] = row
    given = [{'value': value, 'name': str(row.get('host') or '')} for value, row in seen.items()]
    targets = _row_targets(check_type, given)
    return {
        'check_type': check_type,
        'pop_count': len(check.get('selected_pop_ids') or []),
        'resource_count': int(check.get('resource_count') or len(targets)),
        'targets': targets,
        'label': _label('', targets),
        'request': {},
        'status': _status(check.get('status'), 'pending'),
        'cost_usd': usd(check.get('usd_cost')),
    }


def _scan_row(kind: str, scan: dict[str, Any]) -> dict[str, Any]:
    target = str(scan.get('raw_target') or scan.get('cidr') or '')
    return {
        'check_type': None,
        'pop_count': 0,
        'resource_count': 1,
        'targets': [{'value': target, 'name': target}],
        'label': target[:255],
        'request': {'target': target},
        'status': _status(scan.get('status'), 'pending'),
        'cost_usd': _scan_cost(kind, scan),
    }


class AccountMixin:
    """Часть фасада :class:`DpiCheckerService` — ему нужны только ``_call`` и ``_action``."""

    _call: Callable[..., Awaitable[Any]]
    _action: Callable[..., Awaitable[DpiCheckerAction]]

    async def account_checks(
        self, db: AsyncSession, *, kind: str, check_type: str | None, limit: int = 25, offset: int = 0
    ) -> dict[str, Any]:
        """Страница всех запусков аккаунта у сервиса; у взятых в кабинет — номер своей строки."""
        if kind not in ACCOUNT_KINDS:
            raise ValueError(f'Неизвестный вид запуска: {kind}')
        wanted_type = check_type if kind == crud.KIND_CHECK else None
        data = await self._call('list_checks', kind=kind, check_type=wanted_type, limit=limit, offset=offset)
        items = [item for item in data.get('items') or [] if isinstance(item, dict)]
        own = await crud.by_remote(db, kind, [int(item['id']) for item in items if item.get('id') is not None])
        return {'items': [_remote_item(kind, item, own) for item in items], 'total': int(data.get('total') or 0)}

    async def adopt_remote(
        self, db: AsyncSession, kind: str, remote_id: int, *, admin_id: int | None
    ) -> DpiCheckerAction:
        """Запуск не из кабинета — в историю кабинета (повтор отдаёт ту же строку). ``admin_id`` — кто
        его открыл: запускал не он, поэтому в строку он не пишется, а остаётся в журнале действий."""
        if kind not in ACCOUNT_KINDS:
            raise ValueError(f'Неизвестный вид запуска: {kind}')
        existing = await crud.get_by_remote(db, kind, remote_id)
        if existing is not None:
            return existing
        try:
            remote = await self._call(GETTERS[kind], remote_id)
        except DpiCheckerAPIError as exc:
            if exc.status == 404:
                raise ActionNotFound from exc
            raise
        fields = _check_row(remote) if kind == crud.KIND_CHECK else _scan_row(kind, remote)
        source_ref = str(remote.get('source') or '')[:SOURCE_REF_MAX] or None
        return await insert_adopted(
            db,
            kind=kind,
            remote_id=remote_id,
            location=normalize_location(remote.get('location')) or 'russia',
            source_ref=source_ref,
            created_at=_when(remote.get('created_at')),
            **fields,
        )

    async def report_table(self, db: AsyncSession, action_id: int) -> dict[str, Any]:
        """Построчный отчёт сервиса по своей строке: все поля, ключи VPN и ссылки MTProto — именами."""
        action = await self._action(db, action_id, crud.KIND_CHECK)
        if action.remote_id is None:
            raise ActionNotFound
        remote_id = action.remote_id
        names = {str(t.get('value')): str(t.get('name') or '') for t in action.targets or [] if t.get('name')}
        await db.commit()  # не держать соединение базы, пока сервис собирает отчёт
        return present_report(await self._call('report_json', remote_id), names)

    async def webhook_deliveries(self, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        """Журнал доставки вебхуков сервиса боту: событие, дошло ли, код ответа бота, ошибка."""
        data = await self._call('webhook_deliveries', limit=limit, offset=offset)
        items = [item for item in data.get('items') or [] if isinstance(item, dict)]
        return {'items': items, 'total': int(data.get('total') or len(items))}


async def insert_adopted(
    db: AsyncSession,
    *,
    kind: str,
    remote_id: int,
    status: str,
    cost_usd: Decimal | None,
    admin_user_id: int | None = None,
    **fields: Any,
) -> DpiCheckerAction:
    """Новая строка для запуска или монитора с сайта. Два одновременных «взять» упираются в уникальность
    (вид, номер у сервиса): второй откатывается и получает строку первого, а не ошибку."""
    try:
        action = await crud.create_action(db, kind=kind, admin_user_id=admin_user_id, source=SOURCE_SITE, **fields)
        action.remote_id = remote_id
        action.status = status
        action.cost_usd = cost_usd
        await db.commit()
        return action
    except IntegrityError:
        await db.rollback()
        existing = await crud.get_by_remote(db, kind, remote_id)
        if existing is None:
            raise
        logger.info('DPI//CHECKER: запуск уже взят параллельно', kind=kind, remote_id=remote_id)
        return existing
