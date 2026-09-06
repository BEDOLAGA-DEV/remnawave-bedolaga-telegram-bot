"""Цели проверки: единый формат, нормализация ввода, назначение, подсети /24.

Любой источник (хост панели, нода, конфиг подписки, произвольный ввод, подсеть)
приводится к :class:`Target`, дальше сервису безразлично, откуда цель пришла.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from app.external.remnawave_api import RemnaWaveHost


KIND_HOST = 'host'
KIND_NODE = 'node'
KIND_SUBSCRIPTION_CONFIG = 'subscription_config'
KIND_CUSTOM = 'custom'
KIND_CIDR = 'cidr'

PURPOSE_BS = 'bs'
PURPOSE_REGULAR = 'regular'
PURPOSE_UNKNOWN = 'unknown'
PURPOSES = (PURPOSE_BS, PURPOSE_REGULAR, PURPOSE_UNKNOWN)

_BS_MARKERS = ('бс', 'bs', 'lte', 'whitelist', 'белый')
_HOSTNAME_RE = re.compile(r'^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')


class TargetValidationError(ValueError):
    """Цель не годится для проверки; сообщение — для админа, по-русски."""


def is_hostname(value: str) -> bool:
    """Синтаксически корректное доменное имя (строчные буквы, без localhost)."""
    return value != 'localhost' and bool(_HOSTNAME_RE.match(value))


@dataclass(frozen=True)
class Target:
    kind: str
    label: str
    address: str
    port: int | None
    target_key: str
    sni: str | None
    ref: dict = field(default_factory=dict)
    purpose: str = PURPOSE_UNKNOWN
    raw_link: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Target:
        return cls(**{key: data.get(key) for key in cls.__dataclass_fields__})


def target_key(address: str, port: int | None) -> str:
    address = address.lower()
    return f'{address}:{port}' if port else address


def probe_api_target(target: Target) -> str:
    """Строка цели для API: IP/домен с портом либо без."""
    return target_key(target.address, target.port)


def _check_public(address: str) -> None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        if address == 'localhost' or not _HOSTNAME_RE.match(address):
            raise TargetValidationError(f'«{address}» не похоже на IP-адрес или домен') from None
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise TargetValidationError(f'{address} — служебный адрес, такие цели API не проверяет')


def _port(value: str | int | None) -> int | None:
    if value is None or value == '':
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise TargetValidationError(f'Порт «{value}» не число') from None
    if not 1 <= port <= 65535:
        raise TargetValidationError(f'Порт {port} вне диапазона 1–65535')
    return port


def normalize_custom_target(value: str) -> Target:
    """IP, домен, адрес:порт или URL → цель. Схема отбрасывается (HTTP-проба у API с http:// не работает)."""
    text = (value or '').strip()
    if not text:
        raise TargetValidationError('Пустая цель')
    if '://' in text:
        scheme = text.split('://', 1)[0].lower()
        if scheme not in ('http', 'https'):
            raise TargetValidationError('Ссылки конфигов проверяются VLESS-тестом, а не probe')
        parts = urlsplit(text)
        try:
            host, port = parts.hostname, parts.port or (443 if scheme == 'https' else 80)
        except ValueError:
            raise TargetValidationError(f'Не удалось разобрать порт в «{text}»') from None
    else:
        parts = urlsplit(f'//{text}')
        try:
            host, port = parts.hostname, parts.port
        except ValueError:
            raise TargetValidationError(f'Не удалось разобрать порт в «{text}»') from None
        if parts.path or parts.query:
            raise TargetValidationError(f'«{text}» содержит лишнее: нужен адрес и, при необходимости, порт')
    if not host:
        raise TargetValidationError(f'В «{text}» нет адреса')
    host = host.lower()
    _check_public(host)
    port = _port(port)
    return Target(kind=KIND_CUSTOM, label=text, address=host, port=port, target_key=target_key(host, port), sni=None)


def is_reality_like(address: str, sni: str | None) -> bool:
    """SNI чужого домена — признак Reality с dest на «белом» сайте."""
    if not sni:
        return False
    address, sni = address.lower(), sni.lower()
    return sni != address and not sni.endswith(f'.{address}') and not address.endswith(f'.{sni}')


def guess_purpose(*, address: str, sni: str | None, remark: str | None = None, tag: str | None = None) -> str:
    text = f'{remark or ""} {tag or ""}'.lower()
    if is_reality_like(address, sni) or any(marker in text for marker in _BS_MARKERS):
        return PURPOSE_BS
    return PURPOSE_REGULAR


def validate_cidr24(value: str) -> str:
    try:
        network = ipaddress.ip_network((value or '').strip(), strict=False)
    except ValueError:
        raise TargetValidationError(f'«{value}» не похоже на подсеть') from None
    if network.version != 4 or network.prefixlen != 24:
        raise TargetValidationError('API сканирует ровно одну подсеть /24 (IPv4)')
    if not network.is_global:
        raise TargetValidationError(f'{network} — служебная подсеть')
    return str(network)


def cidr24_for_ip(ip: str) -> str:
    return validate_cidr24(f'{ip}/24')


def hosts_for_node(
    hosts: list[RemnaWaveHost], *, node_active_inbounds: list[str], node_address: str, node_ips: list[str]
) -> list[RemnaWaveHost]:
    """Хосты ноды: по инбаунду, а без него — по совпадению адреса с адресом/IP ноды."""
    inbounds = set(node_active_inbounds or [])
    addresses = {node_address.lower(), *(ip.lower() for ip in node_ips or [])}
    return [
        host
        for host in hosts
        if (host.config_profile_inbound_uuid and host.config_profile_inbound_uuid in inbounds)
        or host.address.lower() in addresses
    ]
