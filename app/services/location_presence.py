"""Сведение устройств, о которых сообщили счётчики на нодах, и карточки локаций.

Чистая логика без ввода-вывода: отчёты нод (`devices_from_report`) сводятся в
«кто где сейчас» (`merge_presence`), а хосты и ноды панели — в карточки
(`build_location_cards`).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.external.remnawave_api import RemnaWaveHost, RemnaWaveNode


WINDOW = timedelta(seconds=300)


@dataclass(frozen=True)
class Device:
    user_key: str
    device_key: str
    last_seen: datetime
    node: str
    inbound: str
    exit: str | None


@dataclass
class Presence:
    users: int
    exits: dict[str, int] = field(default_factory=dict)


@dataclass
class LocationCard:
    title: str
    node_name: str | None
    inbound_tag: str | None
    users: int | None
    exits: list[tuple[str, int]]
    source: str | None  # 'counter' | 'panel' | None
    reported_at: datetime | None


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def devices_from_report(report: dict) -> list[Device]:
    node = str(report['node'])
    return [
        Device(
            user_key=str(item['u']),
            device_key=str(item['d']),
            last_seen=_parse_utc(item['t']),
            node=node,
            inbound=str(item['inbound']),
            exit=item.get('exit') or None,
        )
        for item in report.get('devices') or []
    ]


def merge_presence(devices: list[Device], now: datetime) -> dict[tuple[str, str], Presence]:
    """Свести устройства со всех нод: устройство считается там, где его видели последним."""
    latest: dict[str, Device] = {}
    for device in devices:
        current = latest.get(device.device_key)
        if current is None or device.last_seen > current.last_seen:
            latest[device.device_key] = device

    threshold = now - WINDOW
    users: dict[tuple[str, str], set[str]] = defaultdict(set)
    exit_users: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for device in latest.values():
        if device.last_seen < threshold:
            continue
        key = (device.node, device.inbound)
        users[key].add(device.user_key)
        if device.exit:
            exit_users[key][device.exit].add(device.user_key)

    return {
        key: Presence(users=len(members), exits={name: len(who) for name, who in exit_users[key].items()})
        for key, members in users.items()
    }


def _resolve_node(host: RemnaWaveHost, nodes: list[RemnaWaveNode], overrides: dict[str, str]) -> RemnaWaveNode | None:
    candidates = [n for n in nodes if host.config_profile_inbound_uuid in n.active_inbound_uuids]
    if len(candidates) > 1:
        host_address = host.address.lower()
        by_address = [n for n in candidates if n.address.lower() == host_address]
        if len(by_address) == 1:
            candidates = by_address
        else:
            name = overrides.get(host_address)
            candidates = [n for n in candidates if n.name == name]
    return candidates[0] if len(candidates) == 1 else None


def build_location_cards(
    hosts: list[RemnaWaveHost],
    nodes: list[RemnaWaveNode],
    presence: dict[tuple[str, str], Presence],
    reports_at: dict[str, datetime],
    overrides: dict[str, str],
    exit_breakdown_inbounds: set[str],
    skip_host_markers: tuple[str, ...] = (),
) -> list[LocationCard]:
    """Карточка на каждый видимый в подписке хост, в порядке подписки.

    Хосты, в названии которых есть одна из `skip_host_markers` (например, разделители списка),
    пропускаются.
    """
    visible = sorted(
        (
            h
            for h in hosts
            if not h.is_disabled and not h.is_hidden and not any(marker in h.remark for marker in skip_host_markers)
        ),
        key=lambda h: h.view_position,
    )

    resolved: list[tuple[RemnaWaveHost, RemnaWaveNode | None, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for host in visible:
        node = _resolve_node(host, nodes, overrides) if host.config_profile_inbound_uuid else None
        tag = node.active_inbound_tags.get(host.config_profile_inbound_uuid or '') if node else None
        if node is not None and tag is not None:
            if (node.name, tag) in seen:
                continue  # дубль (нода, inbound) — остаётся карточка первого хоста
            seen.add((node.name, tag))
        resolved.append((host, node, tag))

    cards_per_node: dict[str, int] = defaultdict(int)
    for _, node, _ in resolved:
        if node is not None:
            cards_per_node[node.name] += 1

    cards: list[LocationCard] = []
    for host, node, tag in resolved:
        users: int | None = None
        exits: list[tuple[str, int]] = []
        source: str | None = None
        reported_at: datetime | None = None
        if node is not None:
            if node.name in reports_at:
                found = presence.get((node.name, tag)) if tag is not None else None
                users = found.users if found else 0
                if found and tag in exit_breakdown_inbounds:
                    exits = sorted(found.exits.items(), key=lambda item: (-item[1], item[0]))
                source = 'counter'
                reported_at = reports_at[node.name]
            elif cards_per_node[node.name] == 1:
                users = node.users_online
                source = 'panel'
        cards.append(
            LocationCard(
                title=host.remark,
                node_name=node.name if node else None,
                inbound_tag=tag,
                users=users,
                exits=exits,
                source=source,
                reported_at=reported_at,
            )
        )
    return cards
