"""Результат прогона GEO-РФ в виде для кабинета.

Строка сервиса (`rows[]`) богата и сырая; кабинету нужны регион словами, вердикт с
пометкой «это результат или шум сервиса», задержка одним числом, цели списком и,
у туннеля, его подпроверки. Правило «результат ли» — из контракта: exit_bad,
no_ru_node, no_udp, port_blocked статистикой не считаются.
"""

from __future__ import annotations

from statistics import median
from typing import Any


RESULT_VERDICTS = frozenset({'ok', 'partial', 'throttled', 'blocked', 'unconfirmed', 'target_error'})
NOISE_VERDICTS = frozenset({'port_blocked', 'exit_bad', 'no_ru_node', 'no_udp'})
#: Подпроверки туннеля в `targets{}` вместо сайт-целей (контракт 2026-09-11).
TUNNEL_CHECKS = ('IP-проверка', 'Google', 'YouTube')
TUNNEL_SCHEMES = ('vless://', 'hysteria2://')
ISP_NAMES = {'mts': 'МТС', 'beeline': 'Билайн', 'megafon': 'МегаФон', 'rostelecom': 'Ростелеком', 'tele2': 'Tele2'}
DISTRICT_NAMES = {
    'cfo': 'ЦФО',
    'szfo': 'СЗФО',
    'yufo': 'ЮФО',
    'skfo': 'СКФО',
    'pfo': 'ПФО',
    'urfo': 'УФО',
    'sfo': 'СФО',
    'dfo': 'ДФО',
}


def is_result_verdict(verdict: Any) -> bool:
    return verdict in RESULT_VERDICTS


def _targets(raw: Any) -> list[dict]:
    if not isinstance(raw, dict):
        return []
    return [
        {
            'key': str(key),
            'ok': bool(value.get('ok')),
            'ms': value.get('ms'),
            'kind': value.get('kind'),
            'err': value.get('err'),
        }
        for key, value in raw.items()
        if isinstance(value, dict)
    ]


def _latency(targets: list[dict]) -> int | None:
    answered = [int(target['ms']) for target in targets if target['ok'] and isinstance(target.get('ms'), int | float)]
    return int(median(answered)) if answered else None


def _tunnel(raw: dict, targets: list[dict]) -> dict | None:
    checks = [
        {'name': target['key'], 'ok': target['ok'], 'ms': target['ms']}
        for target in targets
        if target['key'] in TUNNEL_CHECKS
    ]
    if not checks and not raw.get('used_core'):
        return None
    return {'used_core': raw.get('used_core'), 'checks': checks}


def _heavy(raw: dict) -> dict | None:
    if 'kbps' not in raw and 'froze' not in raw:
        return None
    return {
        'kbps': raw.get('kbps'),
        'froze': bool(raw.get('froze')),
        'hv_measured': bool(raw.get('hv_measured')),
        'hv_small': bool(raw.get('hv_small')),
    }


def _row(raw: dict, regions: dict[str, dict]) -> dict:
    region = str(raw.get('region') or '')
    known = regions.get(region) or {}
    all_targets = _targets(raw.get('targets'))
    verdict = str(raw.get('verdict') or '')
    return {
        'region': region,
        'region_ru': known.get('name') or region,
        'district': known.get('district') or '',
        'city': str(raw.get('city') or ''),
        'city_ru': str(raw.get('city_ru') or raw.get('city') or ''),
        'req_isp': raw.get('req_isp') or None,
        'provider': raw.get('provider') or None,
        'exit_ip': raw.get('exit_ip') or None,
        'verdict': verdict,
        'is_result': is_result_verdict(verdict),
        'latency_ms': _latency(all_targets),
        'targets': [target for target in all_targets if target['key'] not in TUNNEL_CHECKS],
        'tunnel': _tunnel(raw, all_targets),
        'heavy': _heavy(raw),
        'mb_bill': raw.get('mb_bill'),
        'err': raw.get('err') or None,
        'flaky': bool(raw.get('flaky')),
        'retries': raw.get('retries'),
    }


def normalize_rows(rows: list, regions: dict[str, dict]) -> list[dict]:
    """Строки сервиса → строки кабинета; не-словарь в списке пропускается, а не роняет задачу."""
    return [_row(raw, regions) for raw in rows if isinstance(raw, dict)]


def geo_summary(status: dict, rows: list[dict]) -> dict:
    return {
        'by_verdict': dict(status.get('by_verdict') or {}),
        'result_rows': sum(1 for row in rows if row.get('is_result')),
        'noise_rows': sum(1 for row in rows if not row.get('is_result')),
        'conclusion': status.get('conclusion') or None,
        'progress': status.get('progress') or None,
    }


def _cities_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return 'город'
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return 'города'
    return 'городов'


def _what(targets: list[str]) -> str:
    has_tunnel = any(target.lower().startswith(TUNNEL_SCHEMES) for target in targets)
    if not has_tunnel:
        return 'сайты'
    return 'туннель и сайты' if len(targets) > 1 else 'туннель'


def _where(request: dict) -> str:
    if request.get('district'):
        code = str(request['district']).lower()
        return f'округ {DISTRICT_NAMES.get(code, request["district"])}'
    if request.get('region'):
        return f'регион {request["region"]}'
    if request.get('cities'):
        count = len(request['cities'])
        return f'{count} {_cities_word(count)}'
    return 'вся РФ'


def scope_label(request: dict) -> str:
    """Заголовок для истории: «сайты · проводной · округ ЦФО · МТС · до 30 городов»."""
    targets = [str(target) for target in request.get('targets') or []]
    network = 'мобильный' if request.get('network') == 'mob' else 'проводной'
    parts = [_what(targets), network, _where(request)]
    isp = request.get('isp')
    if isp == '__ALL__':
        parts.append('все провайдеры')
    elif isp:
        parts.append(ISP_NAMES.get(str(isp), str(isp)))
    if request.get('city_limit'):
        parts.append(f'до {request["city_limit"]} городов')
    return ' · '.join(parts)
