"""Клиент DPI//CHECKER Public API v1 (dpichecker.st) — проверки из сетей РФ, Китая, Ирана, Туркменистана.

Живое поведение (расхождения с OpenAPI) записано в tests/fixtures/dpichecker/README.md. Важно здесь:

* ошибка — ``{"error": <текст>, "code": <стабильный код>, "retry_after"?, "rejected"?}``; решения
  принимаются только по ``code``, текст сервис вправе менять;
* ответ без JSON (таймаут, сеть, 5xx шлюза) — :class:`DpiCheckerGatewayError`: платный POST мог пройти,
  повтор — тем же Idempotency-Key (сервис вернёт исходный ответ без второго списания);
* ``probe_mode`` у проверки IP в OpenAPI нет, но API его принимает (``auto|server|noserver``) — так шлёт сайт.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Self

import aiohttp
import structlog


logger = structlog.get_logger(__name__)

DEFAULT_BASE_URL = 'https://dpichecker.st/api/v1'
DEFAULT_TIMEOUT = 30.0
WAIT_EXTRA_SEC = 15
CHECK_TYPES = ('vpn', 'ip', 'mtproto')
LOCATIONS = ('russia', 'china', 'iran', 'turkmenistan')
WAIT_MAX_SEC = 120


@dataclass
class DpiCheckerAPIError(Exception):
    """Отказ сервиса: ``code`` стабилен, ``message`` — для лога."""

    code: str
    message: str
    status: int | None = None
    retry_after: float | None = None
    rejected: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(f'{self.code}: {self.message}')


class DpiCheckerGatewayError(DpiCheckerAPIError):
    """Ответа API нет: результат платного запроса надо переспросить тем же ключом."""


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class DpiCheckerAPI:
    """Тонкий HTTP-клиент: метод на ручку, без бизнес-логики."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip('/')
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Any = None

    def __repr__(self) -> str:
        return f'DpiCheckerAPI(base_url={self.base_url!r})'

    def _headers(self) -> dict[str, str]:
        return {'X-API-Key': self._api_key, 'Accept': 'application/json'}

    async def __aenter__(self) -> Self:
        self._session = aiohttp.ClientSession(timeout=self._timeout, headers=self._headers())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ разбор

    @staticmethod
    def parse_response(status: int, text: str) -> dict:
        """Единая точка разбора: тело с ``code`` → отказ, без JSON или 5xx → шлюз."""
        try:
            body: Any = json.loads(text) if text else {}
        except json.JSONDecodeError:
            body = None
        if status >= 400 and isinstance(body, dict) and isinstance(body.get('code'), str):
            raise DpiCheckerAPIError(
                code=body['code'],
                message=str(body.get('error') or ''),
                status=status,
                retry_after=_float_or_none(body.get('retry_after')),
                rejected=[str(item) for item in body.get('rejected') or []],
            )
        if body is None or status >= 500:
            raise DpiCheckerGatewayError(
                code=f'http_{status}', message=f'Ответ без JSON (HTTP {status})', status=status
            )
        if status >= 400:
            raise DpiCheckerAPIError(code=f'http_{status}', message=text[:200], status=status)
        return body if isinstance(body, dict) else {'items': body}

    def _open(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._session is None:
            raise RuntimeError('DpiCheckerAPI используется только как async context manager')
        return self._session.request(method, f'{self.base_url}{path}', **kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict | None = None,
        idempotency_key: str | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict:
        headers = {'Idempotency-Key': idempotency_key} if idempotency_key else None
        # timeout=None у aiohttp значит «без таймаута вообще» — передаём только заданный явно.
        extra: dict[str, Any] = {'timeout': timeout} if timeout is not None else {}
        try:
            async with self._open(
                method, path, params=params or None, json=json_body, headers=headers, **extra
            ) as response:
                return self.parse_response(response.status, await response.text())
        except TimeoutError as exc:
            raise DpiCheckerGatewayError(code='timeout', message='Таймаут запроса к DPI//CHECKER') from exc
        except aiohttp.ClientError as exc:
            raise DpiCheckerGatewayError(code='network_error', message=str(exc)[:200]) from exc

    async def _bytes(self, path: str, params: dict[str, str] | None = None) -> tuple[bytes, str]:
        """Файл (CSV, PNG): байты и тип содержимого; ошибка — тем же разбором, что JSON."""
        try:
            async with self._open('GET', path, params=params or None, headers=None) as response:
                data = await response.read()
                if response.status >= 400:
                    self.parse_response(response.status, data.decode('utf-8', 'replace'))
                return data, response.headers.get('Content-Type', 'application/octet-stream')
        except TimeoutError as exc:
            raise DpiCheckerGatewayError(code='timeout', message='Таймаут запроса к DPI//CHECKER') from exc
        except aiohttp.ClientError as exc:
            raise DpiCheckerGatewayError(code='network_error', message=str(exc)[:200]) from exc

    # --------------------------------------------------------------- справочники (бесплатно)

    async def pops(self, location: str | None = None) -> dict:
        return await self._request('GET', '/pops', params={'location': location} if location else None)

    async def optimal_pops(self, location: str) -> dict:
        return await self._request('GET', '/pops/optimal', params={'location': location})

    async def tariffs(self) -> dict:
        return await self._request('GET', '/tariffs')

    async def profile(self) -> dict:
        return await self._request('GET', '/profile')

    async def quota(self) -> dict:
        return await self._request('GET', '/quota')

    async def blacklist_check(self, resource: str) -> dict:
        return await self._request('GET', '/blacklist/check', params={'resource': resource})

    async def ip_lookup(self, ip: str, *, bgp: bool = False) -> dict:
        return await self._request('GET', f'/ip/{ip}', params={'bgp': 'true'} if bgp else None)

    async def cheremsha(self, resources: list[str]) -> dict:
        return await self._request('GET', '/cheremsha', params={'resource': ','.join(resources)})

    async def webhook_secret(self) -> str:
        return str((await self._request('GET', '/webhooks/secret')).get('secret') or '')

    async def webhook_deliveries(self, *, limit: int = 25, offset: int = 0) -> dict:
        params = {'limit': str(limit), 'offset': str(offset)}
        return await self._request('GET', '/webhooks/deliveries', params=params)

    # --------------------------------------------------------------- проверки

    async def parse(self, check_type: str, text: str) -> dict:
        return await self._request('POST', '/checks/parse', json_body={'check_type': check_type, 'text': text})

    async def estimate(self, body: dict) -> dict:
        return await self._request('POST', '/checks/estimate', json_body=body)

    async def start_check(self, check_type: str, body: dict, *, idempotency_key: str) -> dict:
        if check_type not in CHECK_TYPES:
            raise ValueError(f'Неизвестный вид проверки: {check_type}')
        return await self._request('POST', f'/checks/{check_type}', json_body=body, idempotency_key=idempotency_key)

    async def list_checks(
        self, *, kind: str = 'check', check_type: str | None = None, limit: int = 25, offset: int = 0
    ) -> dict:
        """Все запуски аккаунта: с сайта, из их бота, через API, прогоны мониторов (``kind`` — check|probe|noisy)."""
        params = {'kind': kind, **({'check_type': check_type} if check_type else {})}
        return await self._request('GET', '/checks', params={**params, 'limit': str(limit), 'offset': str(offset)})

    async def get_check(self, check_id: int) -> dict:
        return await self._request('GET', f'/checks/{check_id}')

    async def wait_check(self, check_id: int, timeout: int = 60) -> dict:
        seconds = max(1, min(int(timeout), WAIT_MAX_SEC))
        # Long-poll держит ответ до `seconds` — своему запросу нужен таймаут длиннее общего короткого.
        return await self._request(
            'GET',
            f'/checks/{check_id}/wait',
            params={'timeout': str(seconds)},
            timeout=aiohttp.ClientTimeout(total=seconds + WAIT_EXTRA_SEC),
        )

    async def cancel_check(self, check_id: int) -> dict:
        return await self._request('DELETE', f'/checks/{check_id}')

    async def report_csv(self, check_id: int) -> tuple[bytes, str]:
        return await self._bytes(f'/checks/{check_id}/report', {'format': 'csv'})

    async def report_json(self, check_id: int) -> dict:
        """Построчный отчёт ``{id, check_type, columns, rows}`` — все поля строки ресурс × точка."""
        return await self._request('GET', f'/checks/{check_id}/report', params={'format': 'json'})

    async def check_map(self, check_id: int) -> tuple[bytes, str]:
        return await self._bytes(f'/checks/{check_id}/map.png')

    # --------------------------------------------------------------- сканы

    async def start_probe(self, target: str, *, idempotency_key: str, callback_url: str | None = None) -> dict:
        body = {'target': target, **({'callback_url': callback_url} if callback_url else {})}
        return await self._request('POST', '/checks/probe', json_body=body, idempotency_key=idempotency_key)

    async def get_probe(self, scan_id: int) -> dict:
        return await self._request('GET', f'/checks/probe/{scan_id}')

    async def start_noisy(self, target: str, *, idempotency_key: str, callback_url: str | None = None) -> dict:
        body = {'target': target, **({'callback_url': callback_url} if callback_url else {})}
        return await self._request('POST', '/checks/noisy', json_body=body, idempotency_key=idempotency_key)

    async def get_noisy(self, scan_id: int) -> dict:
        return await self._request('GET', f'/checks/noisy/{scan_id}')

    async def noisy_csv(self, scan_id: int) -> tuple[bytes, str]:
        return await self._bytes(f'/checks/noisy/{scan_id}.csv')

    # --------------------------------------------------------------- мониторы

    async def list_monitors(self, *, limit: int = 100, offset: int = 0) -> dict:
        return await self._request('GET', '/monitors', params={'limit': str(limit), 'offset': str(offset)})

    async def create_monitor(self, body: dict) -> dict:
        return await self._request('POST', '/monitors', json_body=body)

    async def get_monitor(self, monitor_id: int) -> dict:
        return await self._request('GET', f'/monitors/{monitor_id}')

    async def update_monitor(self, monitor_id: int, body: dict) -> dict:
        return await self._request('PATCH', f'/monitors/{monitor_id}', json_body=body)

    async def delete_monitor(self, monitor_id: int) -> dict:
        return await self._request('DELETE', f'/monitors/{monitor_id}')

    async def monitor_runs(self, monitor_id: int, *, limit: int = 25, offset: int = 0) -> dict:
        params = {'limit': str(limit), 'offset': str(offset)}
        return await self._request('GET', f'/monitors/{monitor_id}/runs', params=params)
