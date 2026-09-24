"""Сырые ``results`` DPI//CHECKER → компактный вид для кабинета.

Строка сервиса = ресурс × точка, плюс по одной строке ``is_direct`` на ресурс — «из-за границы».
VPN-проверка на 5 ключей × 97 точек весит 550 КБ — кабинету отдаётся только то, что он показывает.
Ключи VPN и ссылки MTProto наружу не уходят: у ресурса — имя, а ``value`` только у IP/домена.
"""

from __future__ import annotations

from typing import Any


OK_FIELD = {'vpn': 'connected', 'ip': 'accessible', 'mtproto': 'reached'}
CHECK_FIELDS = ('id', 'status', 'check_type', 'location', 'usd_cost', 'created_at', 'started_at', 'completed_at')
MTPROTO_NAME = 'MTProto'
VPN_NAME = 'VPN'


def _resource_id(check_type: str, row: dict[str, Any]) -> str:
    return str((row.get('uri') if check_type == 'vpn' else row.get('resource')) or '')


def _name(check_type: str, raw: str, row: dict[str, Any], names: dict[str, str]) -> str:
    if raw in names:
        return names[raw]
    if check_type == 'vpn':
        return str(row.get('host') or VPN_NAME)
    if check_type == 'ip':
        return raw
    return MTPROTO_NAME


def _speeds(row: dict[str, Any]) -> list[dict[str, Any]]:
    sites = (row.get('speed_test') or {}).get('sites') or []
    return [{'host': site.get('host'), 'mbps': site.get('speed_mbps')} for site in sites if site.get('ok')]


def _row(check_type: str, row: dict[str, Any]) -> dict[str, Any]:
    control = row.get('control_check')
    reason = row.get('untestable_reason') or (row.get('status') if check_type == 'mtproto' else None)
    return {
        'pop_id': row.get('pop_id'),
        'region': row.get('pop_name') or '',
        'ok': bool(row.get(OK_FIELD[check_type])),
        'latency_ms': row.get('latency_ms') or row.get('ping_avg_ms'),
        'speeds': _speeds(row),
        'error': row.get('error') or None,
        'reason': reason or None,
        'verdict': row.get('verdict'),
        'error_code': row.get('error_code'),
        'port_story': row.get('port_story'),
        'mode': row.get('mode'),
        'internet_ok': control.get('accessible') if isinstance(control, dict) else None,
    }


def _direct(check_type: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        'ok': bool(row.get(OK_FIELD[check_type])),
        'latency_ms': row.get('latency_ms') or row.get('tcp_latency_ms'),
        'error_code': row.get('error_code') or row.get('error') or None,
    }


def summarize(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Как сводка сайта: ресурс доступен со всех точек / с части / ни с одной; средняя задержка удачных."""
    latencies = [
        row['latency_ms'] for res in resources for row in res['rows'] if row['ok'] and (row['latency_ms'] or 0) > 0
    ]
    return {
        'available': sum(1 for res in resources if res['total'] and res['ok_count'] == res['total']),
        'partial': sum(1 for res in resources if 0 < res['ok_count'] < res['total']),
        'unavailable': sum(1 for res in resources if res['total'] and res['ok_count'] == 0),
        'avg_latency_ms': round(sum(latencies) / len(latencies)) if latencies else None,
    }


def present_check(check: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    check_type = str(check.get('check_type') or 'ip')
    if check_type not in OK_FIELD:
        check_type = 'ip'
    order: list[str] = []
    grouped: dict[str, dict[str, Any]] = {}
    for row in check.get('results') or []:
        raw = _resource_id(check_type, row)
        if raw not in grouped:
            order.append(raw)
            grouped[raw] = {
                'index': len(order) - 1,
                'name': _name(check_type, raw, row, names),
                'value': raw if check_type == 'ip' else None,
                'server_ip': row.get('server_ip') or None,
                'direct': None,
                'rows': [],
            }
        if row.get('is_direct'):
            grouped[raw]['direct'] = _direct(check_type, row)
        else:
            grouped[raw]['rows'].append(_row(check_type, row))
    resources = []
    for raw in order:
        res = grouped[raw]
        rows = sorted(res['rows'], key=lambda item: item['region'].casefold())
        resources.append({**res, 'rows': rows, 'total': len(rows), 'ok_count': sum(1 for item in rows if item['ok'])})
    return {
        **{key: check.get(key) for key in CHECK_FIELDS},
        'progress': check.get('progress') or {},
        'summary': summarize(resources),
        'resources': resources,
    }
