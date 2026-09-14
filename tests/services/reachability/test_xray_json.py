"""JSON-подписка (Happ / v2rayNG с HWID) → ссылки; балансировщик даёт по ссылке на outbound
с подписью «ремарка · tag», как показывает оригинал bsbord."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlsplit

from app.services.reachability.links import parse_links
from app.services.reachability.xray_json import links_from_xray_json


UUID = '00000000-0000-4000-8000-000000000001'


def _vless_outbound(tag: str, address: str, sni: str, port: int = 443) -> dict:
    return {
        'tag': tag,
        'protocol': 'vless',
        'settings': {
            'vnext': [
                {
                    'address': address,
                    'port': port,
                    'users': [{'id': UUID, 'flow': 'xtls-rprx-vision', 'encryption': 'none'}],
                }
            ]
        },
        'streamSettings': {
            'network': 'tcp',
            'security': 'reality',
            'realitySettings': {
                'serverName': sni,
                'publicKey': 'PUBKEY',
                'shortId': 'def012',
                'fingerprint': 'chrome',
                'spiderX': '/',
            },
        },
    }


def test_balancer_config_expands_into_one_link_per_outbound_with_tag_labels() -> None:
    payload = [
        {
            'remarks': '🇪🇺 ⚡️ АВТО | WI-FI',
            'outbounds': [
                _vless_outbound('proxy', 'de2.example', 'de2.example'),
                _vless_outbound('proxy-2', 'fi1.example', 'fi1.example'),
                {'tag': 'direct', 'protocol': 'freedom'},
            ],
            'routing': {'balancers': [{'tag': 'Balancer', 'selector': ['proxy']}]},
        },
        {'remarks': '🇩🇪 Germany', 'outbounds': [_vless_outbound('proxy', 'de2.example', 'de2.example')]},
        {'remarks': '🇷🇺 Russia | LTE | БС', 'outbounds': [_vless_outbound('proxy', 'ru1.example', 'ads.x5.ru', 9443)]},
    ]
    links = links_from_xray_json(json.dumps(payload))
    assert len(links) == 4
    parsed, rejected = parse_links('\n'.join(links))
    assert rejected == []
    assert [item.name for item in parsed] == [
        '🇪🇺 ⚡️ АВТО | WI-FI · proxy',
        '🇪🇺 ⚡️ АВТО | WI-FI · proxy-2',
        '🇩🇪 Germany',
        '🇷🇺 Russia | LTE | БС',
    ]
    assert [(item.address, item.port, item.sni) for item in parsed] == [
        ('de2.example', 443, 'de2.example'),
        ('fi1.example', 443, 'fi1.example'),
        ('de2.example', 443, 'de2.example'),
        ('ru1.example', 9443, 'ads.x5.ru'),
    ]
    query = parse_qs(urlsplit(links[0]).query)
    assert query['security'] == ['reality'] and query['pbk'] == ['PUBKEY'] and query['sid'] == ['def012']
    assert query['flow'] == ['xtls-rprx-vision'] and query['fp'] == ['chrome'] and query['type'] == ['tcp']
    assert unquote(links[0].split('#', 1)[1]) == '🇪🇺 ⚡️ АВТО | WI-FI · proxy'


def test_trojan_and_shadowsocks_outbounds_and_ws_transport() -> None:
    payload = {
        'remarks': 'Mixed',
        'outbounds': [
            {
                'tag': 'proxy',
                'protocol': 'trojan',
                'settings': {'servers': [{'address': 't.example', 'port': 443, 'password': 'p@ss'}]},
                'streamSettings': {
                    'network': 'ws',
                    'security': 'tls',
                    'tlsSettings': {'serverName': 't.example', 'alpn': ['h2', 'http/1.1']},
                    'wsSettings': {'path': '/ws', 'headers': {'Host': 't.example'}},
                },
            },
            {
                'tag': 'proxy-2',
                'protocol': 'shadowsocks',
                'settings': {
                    'servers': [{'address': 's.example', 'port': 8388, 'method': 'aes-256-gcm', 'password': 'pw'}]
                },
            },
        ],
    }
    links = links_from_xray_json(json.dumps(payload))
    parsed, rejected = parse_links('\n'.join(links))
    assert rejected == [] and [item.protocol for item in parsed] == ['trojan', 'ss']
    assert parsed[0].sni == 't.example' and 'path=%2Fws' in links[0] and 'alpn=h2%2Chttp%2F1.1' in links[0]
    assert parsed[1].address == 's.example' and parsed[1].port == 8388


def test_not_json_or_no_proxies_gives_empty() -> None:
    assert links_from_xray_json('vless://x') == []
    assert links_from_xray_json('{"outbounds": [{"protocol": "freedom"}]}') == []
    assert links_from_xray_json('[1, 2]') == []
    assert links_from_xray_json('{"outbounds": [{"protocol": "vless", "settings": {}}]}') == []
