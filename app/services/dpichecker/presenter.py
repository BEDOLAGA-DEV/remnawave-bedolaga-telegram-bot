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
    given = names.get(raw)
    # Сам ключ или ссылка прокси именем не бывают — это секрет пользователя.
    if given and (check_type == 'ip' or (given != raw and '://' not in given)):
        return given
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
        'proxy_dead': bool(row.get('proxy_dead')),
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


# Колонка отчёта с самим ресурсом: у VPN это ключ, у MTProto — ссылка прокси (секреты), у IP — адрес.
REPORT_KEY_COLUMN = {'vpn': 'uri', 'ip': 'resource', 'mtproto': 'resource'}
REPORT_ROW_FLAG = 'is_direct'
LOCATION_NAMES = {'россия': 'russia', 'китай': 'china', 'иран': 'iran', 'туркменистан': 'turkmenistan'}


def normalize_location(value: Any) -> str | None:
    """Страна сервиса бывает кодом («russia») и словом («Россия») — в кабинет только кодом."""
    if not value:
        return None
    text = str(value).strip()
    return LOCATION_NAMES.get(text.casefold(), text)


def _cell(value: Any) -> Any:
    """Ячейка таблицы — число, строка, да/нет; вложенное — одной строкой. Контрольная проверка точки
    (``{target, accessible}``) — просто «есть ли у точки интернет»."""
    if isinstance(value, dict) and 'accessible' in value:
        return bool(value['accessible'])
    if isinstance(value, dict):
        return ', '.join(f'{key}: {_cell(item)}' for key, item in value.items())
    if isinstance(value, list):
        return ', '.join(str(_cell(item)) for item in value)
    return value


def present_report(report: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """Построчный отчёт сервиса (все поля строки ресурс × точка) → таблица для кабинета.

    Первая колонка — имя ресурса, как в результате; ключ VPN и ссылка MTProto наружу не уходят,
    адрес IP остаётся своей колонкой. ``is_direct`` — признак строки «из-за границы», не колонка.
    """
    check_type = str(report.get('check_type') or 'ip')
    if check_type not in OK_FIELD:
        check_type = 'ip'
    key_column = REPORT_KEY_COLUMN[check_type]
    secret = check_type != 'ip'
    hidden = {REPORT_ROW_FLAG, *((key_column,) if secret else ())}
    columns = [str(column) for column in report.get('columns') or [] if column not in hidden]
    rows = []
    for row in report.get('rows') or []:
        raw = str(row.get(key_column) or '')
        name = _name(check_type, raw, row, names)
        cells = {column: _cell(row.get(column)) for column in columns}
        if secret and raw:
            # Текст ошибки сервиса может процитировать ключ — он заменяется именем.
            cells = {key: value.replace(raw, name) if isinstance(value, str) else value for key, value in cells.items()}
        rows.append({'name': name, **cells, REPORT_ROW_FLAG: bool(row.get(REPORT_ROW_FLAG))})
    return {'id': report.get('id'), 'check_type': check_type, 'columns': ['name', *columns], 'rows': rows}
