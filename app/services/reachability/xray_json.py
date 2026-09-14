"""Подписка в формате xray-json (Happ, v2rayNG с HWID): конфиги → ссылки.

Панель отдаёт клиентам список JSON-конфигов; конфиг-балансировщик («⚡️ АВТО») держит
несколько outbound — оригинал bsbord показывает каждый как отдельный сервер с подписью
«ремарка · tag». Собираем из outbound обычные ссылки vless/trojan/ss, дальше их разбирает
``links.parse_links`` как любые другие.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode


def _first(items: Any) -> dict:
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def _stream_params(stream: dict) -> dict[str, str]:
    params: dict[str, str] = {}
    network = str(stream.get('network') or 'tcp')
    security = str(stream.get('security') or 'none')
    params['type'] = network
    params['security'] = security
    if security == 'reality':
        reality = stream.get('realitySettings') or {}
        for src, dst in (
            ('serverName', 'sni'),
            ('publicKey', 'pbk'),
            ('shortId', 'sid'),
            ('fingerprint', 'fp'),
            ('spiderX', 'spx'),
        ):
            if reality.get(src):
                params[dst] = str(reality[src])
    elif security == 'tls':
        tls = stream.get('tlsSettings') or {}
        if tls.get('serverName'):
            params['sni'] = str(tls['serverName'])
        if tls.get('fingerprint'):
            params['fp'] = str(tls['fingerprint'])
        if isinstance(tls.get('alpn'), list) and tls['alpn']:
            params['alpn'] = ','.join(str(item) for item in tls['alpn'])
    transport = stream.get(f'{network}Settings') or {}
    if network in ('ws', 'xhttp', 'httpupgrade', 'splithttp'):
        if transport.get('path'):
            params['path'] = str(transport['path'])
        host = transport.get('host') or (transport.get('headers') or {}).get('Host')
        if host:
            params['host'] = str(host)
    elif network == 'grpc' and transport.get('serviceName'):
        params['serviceName'] = str(transport['serviceName'])
    return params


def _vless(outbound: dict, label: str) -> str | None:
    vnext = _first((outbound.get('settings') or {}).get('vnext'))
    user = _first(vnext.get('users'))
    address, port, uuid = vnext.get('address'), vnext.get('port'), user.get('id')
    if not (address and port and uuid):
        return None
    params = {'encryption': str(user.get('encryption') or 'none')}
    if user.get('flow'):
        params['flow'] = str(user['flow'])
    params.update(_stream_params(outbound.get('streamSettings') or {}))
    return f'vless://{uuid}@{address}:{port}?{urlencode(params, quote_via=quote)}#{quote(label)}'


def _trojan(outbound: dict, label: str) -> str | None:
    server = _first((outbound.get('settings') or {}).get('servers'))
    address, port, password = server.get('address'), server.get('port'), server.get('password')
    if not (address and port and password):
        return None
    params = _stream_params(outbound.get('streamSettings') or {})
    return f'trojan://{quote(str(password))}@{address}:{port}?{urlencode(params, quote_via=quote)}#{quote(label)}'


def _shadowsocks(outbound: dict, label: str) -> str | None:
    server = _first((outbound.get('settings') or {}).get('servers'))
    address, port = server.get('address'), server.get('port')
    method, password = server.get('method'), server.get('password')
    if not (address and port and method and password):
        return None
    userinfo = base64.urlsafe_b64encode(f'{method}:{password}'.encode()).decode().rstrip('=')
    return f'ss://{userinfo}@{address}:{port}#{quote(label)}'


_BUILDERS: dict[str, Callable[[dict, str], str | None]] = {
    'vless': _vless,
    'trojan': _trojan,
    'shadowsocks': _shadowsocks,
}


def links_from_xray_json(text: str) -> list[str]:
    """Ссылки из JSON-подписки; не JSON или без прокси-outbound — пусто."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    configs = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    links: list[str] = []
    for config in configs:
        if not isinstance(config, dict):
            continue
        remark = str(config.get('remarks') or config.get('remark') or '').strip()
        proxies = [
            outbound
            for outbound in config.get('outbounds') or []
            if isinstance(outbound, dict) and outbound.get('protocol') in _BUILDERS
        ]
        multiple = len(proxies) > 1
        for outbound in proxies:
            tag = str(outbound.get('tag') or '').strip()
            label = f'{remark} · {tag}' if multiple and tag else remark or tag
            link = _BUILDERS[str(outbound['protocol'])](outbound, label)
            if link:
                links.append(link)
    return links
