"""Хосты и ноды панели для тестов блока «Локации».

Используется тестами сведения устройств и HTTP-маршрутами локаций.
"""

from __future__ import annotations

from app.external.remnawave_api import RemnaWaveHost, RemnaWaveNode


# UUID инбаундов (configProfileInboundUuid).
U_RU = 'in-ru'
U_AUTO = 'in-auto'
U_DE = 'in-de'
U_NL = 'in-nl'
U_ADS = 'in-ads'
U_SE = 'in-se'

# Теги инбаундов на нодах.
TAG_RU = 'vless-ru'
TAG_AUTO = 'vless-auto'
TAG_DE = 'vless-de'
TAG_NL = 'vless-nl'
TAG_ADS = 'vless-nl-ads'
TAG_SE = 'vless-se'

OVERRIDES: dict[str, str] = {'ru-b.example.com': 'RU-B'}
# Разделитель списка в подписке — не локация.
SKIP_MARKERS = '👇'


def make_node(
    name: str,
    address: str,
    tags: dict[str, str],
    users_online: int,
) -> RemnaWaveNode:
    """Нода с минимальным набором полей; tags — uuid инбаунда -> тег."""
    return RemnaWaveNode(
        uuid=f'node-{name}',
        name=name,
        address=address,
        country_code='',
        is_connected=True,
        is_disabled=False,
        users_online=users_online,
        traffic_used_bytes=None,
        traffic_limit_bytes=None,
        active_inbound_uuids=list(tags),
        active_inbound_tags=dict(tags),
    )


def make_host(
    remark: str,
    address: str,
    inbound_uuid: str,
    view_position: int,
    *,
    is_disabled: bool = False,
    is_hidden: bool = False,
) -> RemnaWaveHost:
    return RemnaWaveHost(
        uuid=f'host-{view_position}-{remark}',
        remark=remark,
        address=address,
        is_disabled=is_disabled,
        is_hidden=is_hidden,
        config_profile_inbound_uuid=inbound_uuid,
        view_position=view_position,
    )


def sample_nodes() -> list[RemnaWaveNode]:
    return [
        make_node('RU-A', 'ru-a.example.com', {U_RU: TAG_RU}, 1),
        make_node('RU-B', 'remnanode', {U_RU: TAG_RU}, 2),
        make_node('HUB', 'hub.example.com', {U_AUTO: TAG_AUTO, U_DE: TAG_DE}, 29),
        make_node('NL-A', 'nl.example.com', {U_NL: TAG_NL, U_ADS: TAG_ADS}, 34),
        make_node('SE-A', '203.0.113.10', {U_SE: TAG_SE}, 8),
    ]


def sample_hosts() -> list[RemnaWaveHost]:
    # Намеренно не по порядку view_position, чтобы проверить сортировку.
    return [
        make_host('🇩🇪 Germany', 'hub.example.com', U_DE, 3),
        make_host('🇪🇺 Europe (AUTO)', 'hub.example.com', U_AUTO, 1),
        make_host('🎬 Streaming — Germany', 'hub.example.com', U_DE, 9, is_disabled=True),
        make_host('👇 Gaming 👇', 'ru-a.example.com', U_RU, 4),
        make_host('🇷🇺 Russia #1', 'ru-a.example.com', U_RU, 5),
        make_host('🇷🇺 Russia #2', 'ru-b.example.com', U_RU, 6),
        make_host('🇳🇱 Netherlands (ads)', 'nl.example.com', U_ADS, 7),
        make_host('🇳🇱 Netherlands', 'nl.example.com', U_NL, 2),
        make_host('🇸🇪 Sweden', 'se.example.com', U_SE, 8),
    ]
