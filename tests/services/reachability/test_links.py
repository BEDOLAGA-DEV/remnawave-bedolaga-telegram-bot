"""Разбор ссылок конфигов из подписки панели.

API принимает vless/vmess/trojan/ss/hysteria2 и не больше 20 серверов; подписка
неизвестному клиенту отдаёт заглушки 0.0.0.0:1 — их надо отсеивать до отправки.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.services.reachability.links import MAX_CONFIGS_PER_TEST, ParsedLink, parse_links


UUID = '00000000-0000-4000-8000-000000000001'
VLESS = (
    f'vless://{UUID}@bs-host.example:9443?encryption=none&flow=xtls-rprx-vision&type=tcp'
    '&security=reality&sni=whitelisted.example&fp=firefox&pbk=PUBKEY&sid=def012#%F0%9F%87%B7%F0%9F%87%BA%20Russia'
)
TROJAN = 'trojan://pass@eu-host.example:443?security=tls&sni=eu-host.example#trojan-test'
HY2 = 'hysteria2://pass@eu-host.example:443/?sni=eu-host.example&insecure=0#hy2-test'
SS_SIP002 = f'ss://{base64.b64encode(b"chacha20-ietf-poly1305:pw").decode()}@eu-host.example:8388#ss-test'
SS_LEGACY = 'ss://' + base64.b64encode(b'aes-256-gcm:pw@eu-host.example:8388').decode() + '#ss-legacy'
VMESS = (
    'vmess://'
    + base64.b64encode(
        json.dumps(
            {
                'v': '2',
                'ps': 'vmess-test',
                'add': 'eu-host.example',
                'port': '443',
                'id': UUID,
                'sni': 'eu-host.example',
            }
        ).encode()
    ).decode()
)
STUB = f'vless://{UUID}@0.0.0.0:1?encryption=none&type=tcp&security=none#%E2%9D%8C%20stub'


def test_parses_vless_with_sni_and_decoded_name() -> None:
    parsed, rejected = parse_links(VLESS)
    assert rejected == []
    assert parsed == [
        ParsedLink(
            protocol='vless',
            address='bs-host.example',
            port=9443,
            sni='whitelisted.example',
            name='🇷🇺 Russia',
            raw=VLESS,
        )
    ]


@pytest.mark.parametrize(
    ('link', 'protocol', 'port', 'sni', 'name'),
    [
        (TROJAN, 'trojan', 443, 'eu-host.example', 'trojan-test'),
        (HY2, 'hysteria2', 443, 'eu-host.example', 'hy2-test'),
        (SS_SIP002, 'ss', 8388, None, 'ss-test'),
        (SS_LEGACY, 'ss', 8388, None, 'ss-legacy'),
        (VMESS, 'vmess', 443, 'eu-host.example', 'vmess-test'),
    ],
)
def test_parses_other_protocols(link: str, protocol: str, port: int, sni: str | None, name: str) -> None:
    parsed, rejected = parse_links(link)
    assert rejected == []
    assert (parsed[0].protocol, parsed[0].address, parsed[0].port, parsed[0].sni, parsed[0].name) == (
        protocol,
        'eu-host.example',
        port,
        sni,
        name,
    )


def test_stub_links_from_subscription_page_are_rejected() -> None:
    parsed, rejected = parse_links(STUB)
    assert parsed == []
    assert rejected[0].reason == 'stub'


def test_unknown_scheme_and_garbage_are_rejected_with_reason() -> None:
    parsed, rejected = parse_links('https://sub.example/abc\nhello world\nvless://broken')
    assert parsed == []
    assert [r.reason for r in rejected] == ['unsupported_scheme', 'unsupported_scheme', 'malformed']


def test_multiple_lines_keep_order_and_skip_blank_lines() -> None:
    parsed, _ = parse_links(f'{VLESS}\n\n{TROJAN}\n')
    assert [p.protocol for p in parsed] == ['vless', 'trojan']


def test_max_configs_constant_matches_api_limit() -> None:
    assert MAX_CONFIGS_PER_TEST == 20


# ---------------------------------------------------------------- вставленный текст (как поле «Конфиг или подписка»)


def test_expand_raw_input_splits_lines_decodes_base64_and_keeps_urls() -> None:
    from app.services.reachability.links import expand_raw_input

    blob = base64.b64encode(f'{VLESS}\n{TROJAN}'.encode()).decode()
    text = f'  {HY2}\nhttps://sub.example/abc\n\n{blob}\n'
    assert expand_raw_input(text) == [HY2, 'https://sub.example/abc', VLESS, TROJAN]
    assert expand_raw_input('') == []
    # Не base64 и не ссылка — остаётся строкой, дальше её отвергнет разбор.
    assert expand_raw_input('just words') == ['just words']
