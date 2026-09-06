"""Разрешение целей: хосты и ноды панели, конфиги подписки, ввод админа, подсети.

Источники дёргаются лениво и один раз на резолвер. Конфиги — только через API панели
(`/api/sub/{shortUuid}/info` → links[]): публичный sub-URL отдаёт заглушки.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.external.remnawave_api import RemnaWaveHost, RemnaWaveNode
from app.services.reachability.links import SUPPORTED_SCHEMES, ParsedLink, RejectedLink, parse_links
from app.services.reachability.subscriptions import is_subscription_url
from app.services.reachability.targets import (
    KIND_CIDR,
    KIND_CUSTOM,
    KIND_HOST,
    KIND_NODE,
    KIND_SUBSCRIPTION_CONFIG,
    PURPOSE_UNKNOWN,
    Target,
    guess_purpose,
    hosts_for_node,
    normalize_custom_target,
    target_key,
    validate_cidr24,
)


PrefsMap = dict[tuple[str, str], tuple[str, bool]]
"""(kind, ref) → (purpose, excluded) — решения админа из reachability_target_prefs."""


class TargetResolutionError(ValueError):
    """Цель не найдена в источнике — сообщение для админа."""


@dataclass(frozen=True)
class HostView:
    host: RemnaWaveHost
    target: Target
    purpose_guessed: bool
    excluded: bool
    node_uuids: list[str]


@dataclass(frozen=True)
class NodeView:
    node: RemnaWaveNode
    target: Target
    host_uuids: list[str]


@dataclass(frozen=True)
class SubscriptionConfigs:
    short_uuid: str
    configs: list[Target]
    rejected: list[RejectedLink]


def target_from_host(host: RemnaWaveHost, purpose: str) -> Target:
    return Target(
        kind=KIND_HOST,
        label=host.remark or host.address,
        address=host.address.lower(),
        port=host.port,
        target_key=target_key(host.address, host.port),
        sni=host.sni or host.host or host.address,
        ref={'host_uuid': host.uuid},
        purpose=purpose,
    )


def target_from_node(node: RemnaWaveNode) -> Target:
    return Target(
        kind=KIND_NODE,
        label=node.name,
        address=node.address.lower(),
        port=None,
        target_key=target_key(node.address, None),
        sni=None,
        ref={'node_uuid': node.uuid},
        purpose=PURPOSE_UNKNOWN,
    )


def target_from_link(link: ParsedLink, kind: str, ref: dict) -> Target:
    return Target(
        kind=kind,
        label=link.name or f'{link.address}:{link.port}',
        address=link.address.lower(),
        port=link.port,
        target_key=target_key(link.address, link.port),
        sni=link.sni,
        ref=ref,
        purpose=guess_purpose(address=link.address, sni=link.sni, remark=link.name),
        raw_link=link.raw,
    )


def target_from_cidr(value: str) -> Target:
    cidr = validate_cidr24(value)
    network = ipaddress.ip_network(cidr)
    return Target(
        kind=KIND_CIDR, label=cidr, address=str(network.network_address), port=None, target_key=cidr, sni=None
    )


def _is_config_link(value: str) -> bool:
    return '://' in value and value.split('://', 1)[0].lower() in SUPPORTED_SCHEMES


def _node_ips(node: RemnaWaveNode) -> list[str]:
    return [str(item['ip']) for item in node.ips or [] if isinstance(item, dict) and item.get('ip')]


def _linked_hosts(hosts: list[RemnaWaveHost], node: RemnaWaveNode) -> list[RemnaWaveHost]:
    return hosts_for_node(
        hosts, node_active_inbounds=node.active_inbound_uuids, node_address=node.address, node_ips=_node_ips(node)
    )


class TargetResolver:
    def __init__(
        self,
        *,
        fetch_hosts: Callable[[], Awaitable[list[RemnaWaveHost]]],
        fetch_nodes: Callable[[], Awaitable[list[RemnaWaveNode]]],
        fetch_links: Callable[[str], Awaitable[list[str]]],
        prefs: PrefsMap,
        fetch_url_links: Callable[[str], Awaitable[list[str]]] | None = None,
    ) -> None:
        self._fetch_hosts = fetch_hosts
        self._fetch_nodes = fetch_nodes
        self._fetch_links = fetch_links
        self._fetch_url_links = fetch_url_links
        self._prefs = prefs
        self._hosts: list[RemnaWaveHost] | None = None
        self._nodes: list[RemnaWaveNode] | None = None
        self._configs: dict[str, SubscriptionConfigs] = {}

    # ------------------------------------------------------------------ источники

    async def _all_hosts(self) -> list[RemnaWaveHost]:
        if self._hosts is None:
            self._hosts = list(await self._fetch_hosts())
        return self._hosts

    async def _all_nodes(self) -> list[RemnaWaveNode]:
        if self._nodes is None:
            self._nodes = list(await self._fetch_nodes())
        return self._nodes

    def _host_view(self, host: RemnaWaveHost, nodes: list[RemnaWaveNode]) -> HostView:
        pref = self._prefs.get((KIND_HOST, host.uuid))
        guessed = pref is None or pref[0] == PURPOSE_UNKNOWN
        purpose = (
            guess_purpose(address=host.address, sni=host.sni, remark=host.remark, tag=host.tag) if guessed else pref[0]
        )
        node_uuids = [node.uuid for node in nodes if _linked_hosts([host], node)]
        return HostView(
            host=host,
            target=target_from_host(host, purpose),
            purpose_guessed=guessed,
            excluded=bool(pref and pref[1]),
            node_uuids=node_uuids,
        )

    async def hosts(self, include_disabled: bool = False) -> list[HostView]:
        nodes = await self._all_nodes()
        hosts = [host for host in await self._all_hosts() if include_disabled or not host.is_disabled]
        return [self._host_view(host, nodes) for host in sorted(hosts, key=lambda h: h.view_position)]

    async def nodes(self) -> list[NodeView]:
        hosts = await self._all_hosts()
        return [
            NodeView(node=node, target=target_from_node(node), host_uuids=[h.uuid for h in _linked_hosts(hosts, node)])
            for node in await self._all_nodes()
        ]

    async def _links_for(self, source: str) -> list[str]:
        """Ссылки источника: shortUuid — через панель, http(s)-адрес — загрузкой подписки."""
        if is_subscription_url(source):
            if self._fetch_url_links is None:
                raise TargetResolutionError('Загрузка подписок по URL недоступна')
            return list(await self._fetch_url_links(source))
        return list(await self._fetch_links(source))

    async def subscription_configs(self, source: str) -> SubscriptionConfigs:
        """Конфиги подписки по источнику — shortUuid панели или URL чужой подписки (кэш на резолвер)."""
        if source not in self._configs:
            parsed, rejected = parse_links('\n'.join(await self._links_for(source)))
            ref_key = 'url' if is_subscription_url(source) else 'short_uuid'
            configs = [
                target_from_link(link, KIND_SUBSCRIPTION_CONFIG, {ref_key: source, 'index': index})
                for index, link in enumerate(parsed)
            ]
            self._configs[source] = SubscriptionConfigs(short_uuid=source, configs=configs, rejected=rejected)
        return self._configs[source]

    # ------------------------------------------------------------------ разрешение

    async def _resolve_host(self, item: dict) -> Target:
        host = next((h for h in await self._all_hosts() if h.uuid == item.get('ref')), None)
        if host is None:
            raise TargetResolutionError(f'Хост {item.get("ref")} не найден в панели')
        return self._host_view(host, await self._all_nodes()).target

    async def _resolve_node(self, item: dict) -> Target:
        node = next((n for n in await self._all_nodes() if n.uuid == item.get('ref')), None)
        if node is None:
            raise TargetResolutionError(f'Нода {item.get("ref")} не найдена в панели')
        return target_from_node(node)

    async def _resolve_subscription_config(self, item: dict) -> Target:
        source = str(item.get('short_uuid') or item.get('url') or '')
        configs = (await self.subscription_configs(source)).configs
        index = item.get('index')
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(configs):
            raise TargetResolutionError(f'В подписке {source} нет конфига №{index}')
        target = configs[index]
        expected = str(item.get('target_key') or '').lower()
        if expected and expected != target.target_key:
            raise TargetResolutionError(f'Подписка изменилась: конфиг №{index} теперь другой — обновите список')
        return target

    async def _resolve_custom(self, item: dict) -> Target:
        value = str(item.get('value') or '')
        if not _is_config_link(value):
            return normalize_custom_target(value)
        parsed, rejected = parse_links(value)
        if not parsed:
            reason = rejected[0].reason if rejected else 'malformed'
            raise TargetResolutionError(f'Ссылка не годится для проверки ({reason})')
        return target_from_link(parsed[0], KIND_CUSTOM, {})

    async def _resolve_cidr(self, item: dict) -> Target:
        return target_from_cidr(str(item.get('value') or ''))

    async def _resolve_one(self, item: dict) -> Target:
        resolvers: dict[str, Callable[[dict], Awaitable[Target]]] = {
            KIND_HOST: self._resolve_host,
            KIND_NODE: self._resolve_node,
            KIND_SUBSCRIPTION_CONFIG: self._resolve_subscription_config,
            KIND_CUSTOM: self._resolve_custom,
            KIND_CIDR: self._resolve_cidr,
        }
        kind = item.get('kind')
        resolver = resolvers.get(str(kind))
        if resolver is None:
            raise TargetResolutionError(f'Неизвестный тип цели «{kind}»')
        return await resolver(item)

    async def resolve(self, items: list[dict]) -> list[Target]:
        """Цели в порядке ввода, без повторов по target_key (первая побеждает)."""
        seen: set[str] = set()
        targets: list[Target] = []
        for item in items:
            target = await self._resolve_one(item)
            if target.target_key not in seen:
                seen.add(target.target_key)
                targets.append(target)
        return targets
