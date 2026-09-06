"""Разбор ссылок конфигов (vless/vmess/trojan/ss/hysteria2) из подписки панели.

Только то, что нужно для проверки: протокол, адрес, порт, SNI и имя. Сырая строка
сохраняется — в API уезжает она, а не наш разбор.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from app.services.reachability.panel_links import decode_subscription_body


MAX_CONFIGS_PER_TEST = 20
SUPPORTED_SCHEMES = ('vless', 'vmess', 'trojan', 'ss', 'hysteria2', 'hy2')
STUB_HOSTS = frozenset({'0.0.0.0', '127.0.0.1', 'localhost', ''})


@dataclass(frozen=True)
class ParsedLink:
    protocol: str
    address: str
    port: int
    sni: str | None
    name: str
    raw: str


@dataclass(frozen=True)
class RejectedLink:
    raw: str
    reason: str  # stub | unsupported_scheme | malformed


def parse_links(text: str) -> tuple[list[ParsedLink], list[RejectedLink]]:
    parsed: list[ParsedLink] = []
    rejected: list[RejectedLink] = []
    for line in (raw.strip() for raw in text.splitlines()):
        if not line:
            continue
        scheme = line.split('://', 1)[0].lower() if '://' in line else ''
        if scheme not in SUPPORTED_SCHEMES:
            rejected.append(RejectedLink(line, 'unsupported_scheme'))
            continue
        link = _parse_one(scheme, line)
        if link is None:
            rejected.append(RejectedLink(line, 'malformed'))
        elif link.address.lower() in STUB_HOSTS or link.port <= 1:
            rejected.append(RejectedLink(line, 'stub'))
        else:
            parsed.append(link)
    return parsed, rejected


def _parse_one(scheme: str, raw: str) -> ParsedLink | None:
    if scheme == 'vmess':
        return _parse_vmess(raw)
    if scheme == 'ss':
        return _parse_ss(raw)
    return _parse_url_like('hysteria2' if scheme == 'hy2' else scheme, raw)


def _parse_url_like(protocol: str, raw: str) -> ParsedLink | None:
    parts = urlsplit(raw)
    try:
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not hostname or port is None:
        return None
    query = parse_qs(parts.query)
    sni = (query.get('sni') or query.get('peer') or query.get('host') or [None])[0]
    return ParsedLink(protocol, hostname, port, sni or None, unquote(parts.fragment), raw)


def _b64(value: str) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None


def _parse_vmess(raw: str) -> ParsedLink | None:
    payload = _b64(raw.split('://', 1)[1].split('#', 1)[0])
    try:
        data = json.loads(payload or b'')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or not data.get('add'):
        return None
    try:
        port = int(data.get('port'))
    except (TypeError, ValueError):
        return None
    sni = data.get('sni') or data.get('host') or None
    return ParsedLink('vmess', str(data['add']), port, sni, str(data.get('ps') or ''), raw)


def _parse_ss(raw: str) -> ParsedLink | None:
    body, _, fragment = raw.split('://', 1)[1].partition('#')
    if '@' not in body:
        decoded = _b64(body.split('?', 1)[0])
        if decoded is None:
            return None
        body = decoded.decode('utf-8', errors='replace')
    if '@' not in body:
        return None
    hostport = body.rsplit('@', 1)[1].split('?', 1)[0].split('/', 1)[0]
    host, _, port_text = hostport.rpartition(':')
    if not host or not port_text.isdigit():
        return None
    return ParsedLink('ss', host, int(port_text), None, unquote(fragment), raw)


def expand_raw_input(text: str) -> list[str]:
    """Строки поля «Конфиг или подписка»: ссылки и URL как есть, base64-блоб — в ссылки."""
    lines: list[str] = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if '://' in line:
            lines.append(line)
            continue
        decoded = decode_subscription_body(line)
        lines.extend(decoded if decoded else [line])
    return lines
