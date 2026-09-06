"""Загрузка чужой подписки по URL для поля «Конфиг или подписка» (как в оригинале bsbord).

Админ вводит адрес руками, но бот всё равно не ходит во внутреннюю сеть: только публичные
http(s)-адреса без учётных данных, проверка хоста и после редиректов. Панели отдают
конфиги только клиентам — представляемся клиентом. Тело ограничено по размеру.
Проверка по DNS (домен, указывающий во внутреннюю сеть) не делается: раздел админский.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from app.services.reachability.panel_links import CLIENT_USER_AGENT, decode_subscription_body


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


def _default_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS))


async def fetch_subscription_links(url: str, *, session_factory: Callable[[], Any] | None = None) -> list[str]:
    """Ссылки конфигов по публичному URL подписки; страница или заглушка вместо них — ошибка."""
    url = validate_public_url(url)
    session = (session_factory or _default_session)()
    headers = {'User-Agent': CLIENT_USER_AGENT, 'Accept': 'text/plain, */*'}
    try:
        async with session.get(url, headers=headers, allow_redirects=True, max_redirects=MAX_REDIRECTS) as response:
            _check_host(response.url.host)
            if response.status >= 400:
                raise SubscriptionFetchError(f'Подписка ответила HTTP {response.status}')
            body = await response.content.read(MAX_BODY_BYTES + 1)
    except aiohttp.ClientError as exc:
        raise SubscriptionFetchError(f'Не удалось загрузить подписку: {exc}'[:200]) from exc
    except TimeoutError as exc:
        raise SubscriptionFetchError('Подписка не ответила за отведённое время') from exc
    finally:
        await session.close()
    if len(body) > MAX_BODY_BYTES:
        raise SubscriptionFetchError('Ответ подписки слишком велик')
    links = decode_subscription_body(body.decode('utf-8', errors='replace'))
    if not links:
        raise SubscriptionFetchError('По этому адресу нет конфигов: страница или заглушка вместо подписки')
    return links
