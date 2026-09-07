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
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver

from app.services.reachability.panel_links import (
    CLIENT_USER_AGENT,
    HWID_HEADERS,
    decode_subscription_body,
    hwid_required,
)


MAX_BODY_BYTES = 1_000_000
TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 3


class SubscriptionFetchError(ValueError):
    """Подписку по URL не загрузить — сообщение для админа."""


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
        timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
    )


async def _get(session: Any, url: str, headers: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    async with session.get(url, headers=headers, allow_redirects=True, max_redirects=MAX_REDIRECTS) as response:
        _check_host(response.url.host)
        if response.status >= 400:
            raise SubscriptionFetchError(f'Подписка ответила HTTP {response.status}')
        body = await response.content.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise SubscriptionFetchError('Ответ подписки слишком велик')
        raw_headers = getattr(response, 'headers', None) or {}
        return body, {str(key).lower(): str(value) for key, value in dict(raw_headers).items()}


async def fetch_subscription_links(url: str, *, session_factory: Callable[[], Any] | None = None) -> list[str]:
    """Ссылки конфигов по публичному URL подписки; страница или заглушка вместо них — ошибка."""
    url = validate_public_url(url)
    session = (session_factory or _default_session)()
    headers = {'User-Agent': CLIENT_USER_AGENT, 'Accept': 'text/plain, */*'}
    try:
        body, response_headers = await _get(session, url, headers)
        # Панель с HWID-лимитом без заголовков устройства отдаёт заглушки — повторяем как устройство.
        if hwid_required(response_headers):
            body, _ = await _get(session, url, {**headers, **HWID_HEADERS})
    except aiohttp.ClientError as exc:
        raise SubscriptionFetchError(f'Не удалось загрузить подписку: {exc}'[:200]) from exc
    except TimeoutError as exc:
        raise SubscriptionFetchError('Подписка не ответила за отведённое время') from exc
    finally:
        await session.close()
    links = decode_subscription_body(body.decode('utf-8', errors='replace'))
    if not links:
        raise SubscriptionFetchError('По этому адресу нет конфигов: страница или заглушка вместо подписки')
    return links
