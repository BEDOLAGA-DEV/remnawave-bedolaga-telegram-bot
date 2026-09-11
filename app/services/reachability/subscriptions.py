"""Загрузка чужой подписки по URL для поля «Конфиг или подписка» (как в оригинале bsbord).

Админ вводит адрес руками, но бот всё равно не ходит во внутреннюю сеть: только публичные
http(s)-адреса без учётных данных, проверка хоста до запроса и после редиректов. Панели отдают
конфиги только клиентам — представляемся клиентом. Тело ограничено по размеру.
DNS резолвится своим резолвером, который отдаёт соединению только публичные адреса —
домен, глядящий во внутреннюю сеть (в том числе подменённый после первого ответа), не пройдёт.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver

from app.services.reachability.links import parse_links
from app.services.reachability.notes import (
    explain_missing_configs,
    note_for_userinfo,
    parse_userinfo,
    stub_remarks,
)
from app.services.reachability.panel_links import (
    CLIENT_USER_AGENT,
    HWID_HEADERS,
    decode_subscription_body,
    hwid_required,
)


#: В подписке бывает и 10 тысяч серверов: JSON-подписка такого размера — десятки мегабайт
#: (у реальной панели ~2 КБ на сервер со служебными полями). Потолок — защита от бесконечного
#: тела, а не оценка «нормальной» подписки.
MAX_BODY_BYTES = 64_000_000
#: Десятки мегабайт с медленной панели за 15 секунд не приходят: общий срок щедрый,
#: а соединение и каждое чтение ограничены отдельно, чтобы зависший сервер не держал запрос.
TIMEOUT_TOTAL_SECONDS = 180
TIMEOUT_CONNECT_SECONDS = 15
TIMEOUT_READ_SECONDS = 60
MAX_REDIRECTS = 3
_READ_CHUNK_BYTES = 64 * 1024


class SubscriptionFetchError(ValueError):
    """Подписку по URL не загрузить — сообщение для админа."""


@dataclass(frozen=True)
class FetchedSubscription:
    """Ссылки подписки и, если панель на что-то жалуется, пометка словами («Подписка истекла …»)."""

    links: list[str]
    note: str | None = None


def is_subscription_url(text: str) -> bool:
    return (text or '').strip().lower().startswith(('http://', 'https://'))


def _check_host(host: str | None) -> None:
    if not host or host.lower() == 'localhost':
        raise SubscriptionFetchError('В адресе подписки нет публичного хоста')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if not ip.is_global:
        raise SubscriptionFetchError(f'{host} — служебный адрес, такие подписки не загружаются')


def validate_public_url(url: str) -> str:
    text = (url or '').strip()
    parts = urlsplit(text)
    if parts.scheme.lower() not in ('http', 'https'):
        raise SubscriptionFetchError('Подписка загружается только по http(s)-адресу')
    if parts.username or parts.password:
        raise SubscriptionFetchError('Адрес подписки не должен содержать логин и пароль')
    _check_host(parts.hostname)
    return text


class PublicOnlyResolver(AbstractResolver):
    """DNS для загрузки подписок: адреса не из публичного пространства не отдаются соединению вовсе."""

    def __init__(self, inner: AbstractResolver | None = None) -> None:
        self._inner = inner or DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        for item in results:
            address = str(item['host'])
            try:
                is_global = ipaddress.ip_address(address).is_global
            except ValueError:
                is_global = False
            if not is_global:
                raise SubscriptionFetchError(
                    f'{host} указывает на служебный адрес {address}, такие подписки не загружаются'
                )
        return results

    async def close(self) -> None:
        await self._inner.close()


def _default_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicOnlyResolver()),
        timeout=aiohttp.ClientTimeout(
            total=TIMEOUT_TOTAL_SECONDS, connect=TIMEOUT_CONNECT_SECONDS, sock_read=TIMEOUT_READ_SECONDS
        ),
    )


async def _get(session: Any, url: str, headers: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    async with session.get(url, headers=headers, allow_redirects=True, max_redirects=MAX_REDIRECTS) as response:
        _check_host(response.url.host)
        if response.status >= 400:
            raise SubscriptionFetchError(f'Подписка ответила HTTP {response.status}')
        body = await _read_capped(response.content)
        raw_headers = getattr(response, 'headers', None) or {}
        return body, {str(key).lower(): str(value) for key, value in dict(raw_headers).items()}


async def _read_capped(content: Any) -> bytes:
    """Всё тело ответа, но не больше потолка.

    ``read(n)`` у aiohttp отдаёт то, что уже пришло по сети (не больше n), а не ждёт
    n байт: JSON-подписка на сотни килобайт приходила первым куском в несколько
    килобайт, обрезанный JSON не разбирался — «по этому адресу нет конфигов».
    """
    body = bytearray()
    while True:
        chunk = await content.read(_READ_CHUNK_BYTES)
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise SubscriptionFetchError('Ответ подписки слишком велик')


async def fetch_subscription(
    url: str, *, session_factory: Callable[[], Any] | None = None, now: datetime | None = None
) -> FetchedSubscription:
    """Подписка по публичному URL: ссылки и пометка; ни одного сервера — ошибка с причиной словами."""
    url = validate_public_url(url)
    session = (session_factory or _default_session)()
    headers = {'User-Agent': CLIENT_USER_AGENT, 'Accept': 'text/plain, */*'}
    device_retry = False
    try:
        body, response_headers = await _get(session, url, headers)
        # Панель с HWID-лимитом без заголовков устройства отдаёт заглушки — повторяем как устройство.
        if hwid_required(response_headers):
            device_retry = True
            body, _ = await _get(session, url, {**headers, **HWID_HEADERS})
    except aiohttp.ClientError as exc:
        raise SubscriptionFetchError(f'Не удалось загрузить подписку: {exc}'[:200]) from exc
    except TimeoutError as exc:
        raise SubscriptionFetchError('Подписка не ответила за отведённое время') from exc
    finally:
        await session.close()
    text = body.decode('utf-8', errors='replace')
    links = decode_subscription_body(text)
    parsed, rejected = parse_links('\n'.join(links))
    info = parse_userinfo(response_headers.get('subscription-userinfo'))
    if not parsed:
        raise SubscriptionFetchError(
            explain_missing_configs(
                info=info,
                stubs=stub_remarks([item.raw for item in rejected if item.reason == 'stub']),
                body=text,
                device_retry=device_retry,
                now=now,
            )
        )
    return FetchedSubscription(links=links, note=note_for_userinfo(info, now=now))


async def fetch_subscription_links(url: str, *, session_factory: Callable[[], Any] | None = None) -> list[str]:
    """Только ссылки — для вызывающих, которым пометка не нужна."""
    return (await fetch_subscription(url, session_factory=session_factory)).links
