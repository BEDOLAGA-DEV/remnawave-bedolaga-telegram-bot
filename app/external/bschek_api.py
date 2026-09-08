"""Клиент bschekbot API v1 — проверка достижимости из мобильных сетей РФ.

Живое поведение API (расхождения с контрактом) записано в фикстурах tests/fixtures/bschek/
и в их README. Здесь важны три вещи:

* единый конверт ошибок ``{"error": {code, message, details}}`` на любых статусах,
  включая коды, которых нет в контракте — они сохраняются как есть;
* ответ без конверта (524/502 от Cloudflare, таймаут, обрыв) — это НЕ ошибка API:
  платный запрос мог отработать и списать деньги, результат достаётся повтором
  с тем же Idempotency-Key. Для этого отдельный класс :class:`BschekGatewayError`;
* ``/account`` отдаёт ``webhook_secret`` — он отбрасывается на этой границе.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

import aiohttp
import structlog


logger = structlog.get_logger(__name__)

DEFAULT_BASE_URL = 'https://bsbord.com/v1'
DEFAULT_TIMEOUT = 200.0


@dataclass
class BschekAPIError(Exception):
    """Ошибка API в едином конверте."""

    code: str
    message: str
    status: int | None = None
    retryable: bool = False
    retry_after: float | None = None
    request_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f'{self.code}: {self.message}')


class BschekGatewayError(BschekAPIError):
    """Ответ без конверта API: шлюз, таймаут или сеть. Результат надо переспросить тем же ключом."""


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def build_operators_params(
    *,
    dpi: str | None = None,
    operator: list[str] | None = None,
    region: list[str] | None = None,
    probeable: bool | None = None,
) -> dict[str, str]:
    """Query для GET /operators. aiohttp сам percent-encode'ит кириллицу."""
    params: dict[str, str] = {}
    if dpi:
        params['dpi'] = dpi
    if operator:
        params['operator'] = ','.join(operator)
    if region:
        params['region'] = ','.join(region)
    if probeable is not None:
        params['probeable'] = 'true' if probeable else 'false'
    return params


class BschekAPI:
    """Тонкий HTTP-клиент: один метод на эндпоинт, без бизнес-логики."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip('/')
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def __repr__(self) -> str:
        return f'BschekAPI(base_url={self.base_url!r})'

    async def __aenter__(self) -> Self:
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers={'Authorization': f'Bearer {self._api_key}', 'Accept': 'application/json'},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ разбор

    @staticmethod
    def parse_response(status: int, text: str, headers: Mapping[str, str]) -> dict:
        """Единая точка разбора: конверт ошибки → BschekAPIError, без конверта → BschekGatewayError."""
        request_id = _header(headers, 'X-Request-Id')
        try:
            body: Any = json.loads(text) if text else {}
        except json.JSONDecodeError:
            body = None

        if isinstance(body, dict) and isinstance(body.get('error'), dict):
            err = body['error']
            details = dict(err.get('details') or {})
            raise BschekAPIError(
                code=str(err.get('code') or 'unknown_error'),
                message=str(err.get('message') or ''),
                status=status,
                retryable=bool(details.get('retryable', False)),
                retry_after=_float_or_none(details.get('retry_after'))
                or _float_or_none(_header(headers, 'Retry-After')),
                request_id=details.get('request_id') or request_id,
                details=details,
            )

        if body is None or status >= 500:
            raise BschekGatewayError(
                code=f'http_{status}',
                message=f'Ответ без конверта API (HTTP {status})',
                status=status,
                retryable=True,
                request_id=request_id,
            )
        if status >= 400:
            raise BschekAPIError(code=f'http_{status}', message=text[:200], status=status, request_id=request_id)
        return body if isinstance(body, dict) else {'_raw': body}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        if self._session is None:
            raise RuntimeError('BschekAPI используется только как async context manager')
        headers = {'Idempotency-Key': idempotency_key} if idempotency_key else None
        try:
            async with self._session.request(
                method, f'{self.base_url}{path}', params=params or None, json=json_body, headers=headers
            ) as response:
                text = await response.text()
                return self.parse_response(response.status, text, response.headers)
        except TimeoutError as exc:
            raise BschekGatewayError(code='timeout', message='Таймаут запроса к bschekbot', retryable=True) from exc
        except aiohttp.ClientError as exc:
            raise BschekGatewayError(code='network_error', message=str(exc), retryable=True) from exc

    # ------------------------------------------------------------------ ручки

    async def get_operators(
        self,
        *,
        dpi: str | None = None,
        operator: list[str] | None = None,
        region: list[str] | None = None,
        probeable: bool | None = None,
    ) -> dict:
        params = build_operators_params(dpi=dpi, operator=operator, region=region, probeable=probeable)
        return await self._request('GET', '/operators', params=params)

    async def get_openapi(self) -> dict:
        """OpenAPI-описание API (бесплатно): единственное место, где названы версии ядер Xray."""
        return await self._request('GET', '/openapi.json')

    async def get_account(self) -> dict:
        account = await self._request('GET', '/account')
        return {key: value for key, value in account.items() if key != 'webhook_secret'}

    async def preview_probe(self, body: dict) -> dict:
        return await self._request('POST', '/probe/preview', json_body=body)

    async def probe(self, body: dict, idempotency_key: str) -> dict:
        return await self._request('POST', '/probe', json_body=body, idempotency_key=idempotency_key)

    async def cancel_probe(self, idempotency_key: str) -> dict:
        """Остановить идущую пробу: ручка — тот же Idempotency-Key; 404 = проба уже завершилась."""
        return await self._request('POST', '/probe/cancel', idempotency_key=idempotency_key)

    async def preview_scan(self, body: dict) -> dict:
        return await self._request('POST', '/scans/preview', json_body=body)

    async def start_scan(self, body: dict, idempotency_key: str) -> dict:
        return await self._request('POST', '/scans', json_body=body, idempotency_key=idempotency_key)

    async def get_scan(self, scan_id: int) -> dict:
        return await self._request('GET', f'/scans/{scan_id}')

    async def cancel_scan(self, scan_id: int) -> dict:
        return await self._request('POST', f'/scans/{scan_id}/cancel')

    async def start_vless(self, body: dict, idempotency_key: str) -> dict:
        return await self._request('POST', '/vless', json_body=body, idempotency_key=idempotency_key)

    async def get_vless(self, test_id: int) -> dict:
        return await self._request('GET', f'/vless/{test_id}')

    async def cancel_vless(self, test_id: int) -> dict:
        return await self._request('POST', f'/vless/{test_id}/cancel')
