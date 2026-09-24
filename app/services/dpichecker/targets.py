"""Цели DPI//CHECKER из панели Remnawave.

Хост панели — это адрес, порт и SNI, а не ключ: ключ VPN всегда чей-то. Поэтому VPN «из панели» —
ключи подписки выбранного пользователя (по умолчанию — самого админа), взятые теми же помощниками,
что у BSCHEKER; имя ключа — его remark. IP, Зонд и Соседи «из панели» — адреса хостов и нод.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reachability.panel_links import fetch_panel_links, short_uuid_for_user


KEY_SCHEMES = frozenset({'vless', 'vmess', 'trojan', 'ss', 'hysteria2', 'hy2'})


class PanelTargetError(ValueError):
    """Цель из панели не получилась — сообщение для админа."""


@dataclass(frozen=True)
class PanelTarget:
    value: str
    name: str
    ref: str


def _key_name(link: str) -> str:
    if '#' in link:
        name = unquote(link.split('#', 1)[1]).strip()
        if name:
            return name
    parts = urlsplit(link)
    return f'{parts.hostname}:{parts.port}' if parts.port else str(parts.hostname or link[:40])


def _is_key(link: str) -> bool:
    return '://' in link and link.split('://', 1)[0].lower() in KEY_SCHEMES


def is_vpn_key(value: str) -> bool:
    """Ссылка ключа VPN (не подписка): только её сервис проверяет как ключ."""
    return _is_key(value.strip())


def _mtproto_name(link: str) -> str:
    parts = urlsplit(link)
    query = parse_qs(parts.query)
    server = (query.get('server') or [''])[0]
    port = (query.get('port') or [''])[0]
    return f'{server}:{port}' if server and port else server or 'MTProto'


def safe_name(check_type: str, value: str, given: str | None) -> str:
    """Имя цели для людей: данное админом, иначе из ссылки — но никогда сам ключ или секрет прокси."""
    name = (given or '').strip()
    if check_type == 'ip':
        return name or value
    if name and name != value and '://' not in name:
        return name
    return _key_name(value) if check_type == 'vpn' else _mtproto_name(value)


async def subscription_keys(
    db: AsyncSession | None, *, user_id: int, panel_client: Callable[[], Any]
) -> list[PanelTarget]:
    short_uuid = await short_uuid_for_user(db, user_id)
    if not short_uuid:
        raise PanelTargetError(f'У пользователя #{user_id} нет подписки в панели')
    async with panel_client() as api:
        links = await fetch_panel_links(api, short_uuid)
    keys = [PanelTarget(value=link, name=_key_name(link), ref=short_uuid) for link in links if _is_key(link)]
    if not keys:
        raise PanelTargetError(f'В подписке пользователя #{user_id} нет ключей для проверки')
    return keys


def _unique_by_address(items: list[PanelTarget]) -> list[PanelTarget]:
    seen: set[str] = set()
    unique: list[PanelTarget] = []
    for item in items:
        if item.value not in seen:
            seen.add(item.value)
            unique.append(item)
    return unique


async def host_addresses(*, panel_client: Callable[[], Any], host_uuids: list[str]) -> list[PanelTarget]:
    """Адреса выбранных хостов; пустой выбор — все включённые (список для выбора в кабинете)."""
    async with panel_client() as api:
        hosts = {host.uuid: host for host in await api.get_all_hosts()}
    wanted = list(dict.fromkeys(host_uuids)) or [
        uuid for uuid, host in hosts.items() if not getattr(host, 'is_disabled', False)
    ]
    found = [
        PanelTarget(value=hosts[uuid].address.lower(), name=hosts[uuid].remark or hosts[uuid].address, ref=uuid)
        for uuid in wanted
        if uuid in hosts and hosts[uuid].address
    ]
    if not found:
        raise PanelTargetError('Выбранные хосты не найдены в панели')
    return _unique_by_address(found)


async def node_addresses(*, panel_client: Callable[[], Any], node_uuids: list[str]) -> list[PanelTarget]:
    """Адреса выбранных нод; пустой выбор — все ноды (список для выбора в кабинете)."""
    async with panel_client() as api:
        nodes = {node.uuid: node for node in await api.get_all_nodes()}
    wanted = list(dict.fromkeys(node_uuids)) or list(nodes)
    found = [
        PanelTarget(value=nodes[uuid].address.lower(), name=nodes[uuid].name or nodes[uuid].address, ref=uuid)
        for uuid in wanted
        if uuid in nodes and nodes[uuid].address
    ]
    if not found:
        raise PanelTargetError('Выбранные ноды не найдены в панели')
    return _unique_by_address(found)
