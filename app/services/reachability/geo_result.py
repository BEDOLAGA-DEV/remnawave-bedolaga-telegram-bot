"""Результат прогона GEO-РФ в виде для кабинета.

Строка сервиса (`rows[]`) богата и сырая; кабинету нужны регион словами, вердикт с
пометкой «это результат или шум сервиса», задержка одним числом, цели списком и,
у туннеля, его подпроверки. Правило «результат ли» — из контракта: exit_bad,
no_ru_node, no_udp, port_blocked статистикой не считаются.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from statistics import median
from typing import Any

from app.services.reachability.geo_catalog import city_name_key


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


def _row(raw: dict, names: dict) -> dict:
    region = str(raw.get('region') or '')
    city = str(raw.get('city') or '')
    known = (names.get('regions') or {}).get(region) or {}
    city_ru = raw.get('city_ru') or (names.get('cities') or {}).get(city_name_key(region, city)) or city
    all_targets = _targets(raw.get('targets'))
    verdict = str(raw.get('verdict') or '')
    return {
        'region': region,
        'region_ru': known.get('name') or region,
        'district': known.get('district') or '',
        'city': city,
        'city_ru': str(city_ru),
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
        # Повтор через тот же выход из кабинета: sid и остаток удержания; «выход сменился» — пометка повтора.
        'sid': str(raw['sid']) if raw.get('sid') else None,
        'sid_hold_s': raw.get('sid_hold_s') if isinstance(raw.get('sid_hold_s'), int | float) else None,
        'exit_changed': bool(raw.get('exit_changed')),
    }


def normalize_rows(rows: list, names: dict | None = None) -> list[dict]:
    """Строки сервиса → строки кабинета; не-словарь в списке пропускается, а не роняет задачу.

    `names` — индекс `names_from_catalog`: регион и город словами; без него остаются токены.
    """
    return [_row(raw, names or {}) for raw in rows if isinstance(raw, dict)]


def name_rows(rows: list, names: dict) -> list[dict]:
    """Уже нормализованные строки — с именами из индекса (старые задачи в базе лежат токенами)."""
    if not names.get('regions') and not names.get('cities'):
        return list(rows)
    named = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        region, city = str(row.get('region') or ''), str(row.get('city') or '')
        known = (names.get('regions') or {}).get(region)
        city_ru = (names.get('cities') or {}).get(city_name_key(region, city))
        patch = {}
        if known:
            patch.update(region_ru=known['name'], district=known['district'])
        if city_ru:
            patch['city_ru'] = city_ru
        named.append({**row, **patch} if patch else row)
    return named


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


# ---------------------------------------------------------------- перепроверка города из отчёта

#: Ключи охвата родительского запроса — у повтора охват один: этот город.
_SCOPE_KEYS = ('district', 'region', 'cities', 'city_limit', 'isp', 'session', 'expect_exit_ip')


def row_key(row: dict) -> tuple[str, str, str]:
    """Город × заказанный провайдер — по этому ключу оригинал ищет строки при повторе."""
    return (str(row.get('region') or ''), str(row.get('city') or ''), str(row.get('req_isp') or ''))


def recheck_request(parent_request: dict, row: dict, *, same_exit: bool) -> dict:
    """Тело повтора: цели, сеть, метод, ядро родителя; охват — один город; при `same_exit` — его выход."""
    city = {'region': str(row.get('region') or ''), 'city': str(row.get('city') or '')}
    if row.get('req_isp'):
        city['isp'] = str(row['req_isp'])
    body = {key: value for key, value in parent_request.items() if key not in _SCOPE_KEYS}
    body['cities'] = [city]
    if same_exit and row.get('sid'):
        body['session'] = str(row['sid'])
        body['expect_exit_ip'] = str(row.get('exit_ip') or '')
    return body


def _same_key(row: dict, key: tuple[str, str, str]) -> bool:
    return row_key(row) == key


def merge_recheck(parent_result: dict, new_rows: list[dict], *, run_id: int | None) -> dict:
    """Строки повтора — в отчёт родителя по правилам оригинала.

    Тот же выход (совпал exit_ip) — повтор наблюдения: прежняя строка заменяется свежей.
    Строка без выхода — заменяется. Другой выход — ДОБАВЛЯЕТСЯ с пометкой «новый выход»,
    прежние строки города остаются, но помечаются «перепроверено» и кнопок больше не получают.
    Сводка пересчитывается по строкам; фраза-вывод сервиса после слияния устаревает и снимается.
    """
    rows = [dict(row) for row in parent_result.get('rows') or [] if isinstance(row, dict)]
    for raw in new_rows:
        if not isinstance(raw, dict):
            continue
        fresh = {**raw, 'recheck_run_id': run_id}
        key = row_key(fresh)
        same = next(
            (
                i
                for i, row in enumerate(rows)
                if _same_key(row, key) and row.get('exit_ip') and row.get('exit_ip') == fresh.get('exit_ip')
            ),
            None,
        )
        if same is not None:
            rows.pop(same)
            rows.append(fresh)
            continue
        stub = next((i for i, row in enumerate(rows) if _same_key(row, key) and not row.get('exit_ip')), None)
        if stub is not None:
            rows[stub] = fresh
            continue
        siblings = [i for i, row in enumerate(rows) if _same_key(row, key)]
        if siblings:
            fresh['new_exit'] = True
            for i in siblings:
                rows[i] = {**rows[i], 'rechecked': True}
        rows.append(fresh)
    by_verdict: dict[str, int] = {}
    for row in rows:
        verdict = str(row.get('verdict') or '')
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
    summary = {
        **(parent_result.get('summary') or {}),
        'by_verdict': by_verdict,
        'result_rows': sum(1 for row in rows if row.get('is_result')),
        'noise_rows': sum(1 for row in rows if not row.get('is_result')),
        'conclusion': None,
    }
    return {**parent_result, 'rows': rows, 'summary': summary}


# ---------------------------------------------------------------- идущие повторы в отчёте родителя

#: Повтор города пишется в тот же тест: запись ``result.rechecks[ключ]`` живёт, пока идёт прогон.
RECHECK_RUNNING = 'running'
RECHECK_FAILED = 'failed'
RECHECK_STALE_MESSAGE = 'Повтор прерван перезапуском бота — итог не получен'


def recheck_key_str(key: tuple[str, str, str]) -> str:
    """Ключ записи повтора — «регион|город|провайдер», тот же, что у строки в кабинете."""
    return '|'.join(key)


def _rechecks(result: dict) -> dict[str, dict]:
    raw = result.get('rechecks')
    if not isinstance(raw, dict):
        return {}
    return {str(key): dict(entry) for key, entry in raw.items() if isinstance(entry, dict)}


def _with_rechecks(result: dict, rechecks: dict[str, dict]) -> dict:
    return {**result, 'rechecks': rechecks}


def recheck_started(
    result: dict,
    key: str,
    *,
    same_exit: bool,
    reserve_kopeks: int | None,
    started_at: str,
    admin_id: int | None,
) -> dict:
    """Новый result с записью только что заказанного повтора; прогон у сервиса ещё не запущен."""
    entry = {
        'status': RECHECK_RUNNING,
        'same_exit': bool(same_exit),
        'reserve_kopeks': reserve_kopeks,
        'started_at': started_at,
        'admin_id': admin_id,
        'run_id': None,
    }
    return _with_rechecks(result, {**_rechecks(result), key: entry})


def recheck_updated(result: dict, key: str, **fields: Any) -> dict:
    """Новый result с полями, дописанными в запись повтора (номер прогона, уточнённый резерв)."""
    rechecks = _rechecks(result)
    return _with_rechecks(result, {**rechecks, key: {**rechecks.get(key, {}), **fields}})


def recheck_failed(result: dict, key: str, message: str, *, finished_at: str) -> dict:
    """Повтор не удался: запись остаётся с причиной словами, кнопки у строки возвращаются."""
    return recheck_updated(result, key, status=RECHECK_FAILED, error=message, finished_at=finished_at)


def recheck_finished(result: dict, key: str) -> dict:
    """Итог влит в строки — запись снимается."""
    rechecks = _rechecks(result)
    rechecks.pop(key, None)
    return _with_rechecks(result, rechecks)


def running_rechecks(result: dict) -> dict[str, dict]:
    return {key: entry for key, entry in _rechecks(result).items() if entry.get('status') == RECHECK_RUNNING}


def _age_sec(started_at: Any, now: datetime) -> float:
    try:
        started = datetime.fromisoformat(str(started_at))
    except (TypeError, ValueError):
        return float('inf')
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (now - started).total_seconds()


def expire_rechecks(result: dict, *, now: datetime, is_active: Callable[[str], bool], grace_sec: float) -> dict | None:
    """Записи «идёт» без живой фоновой задачи дольше окна запуска — сироты после перезапуска бота.

    Они падают с причиной словами, иначе кабинет ждал бы итог вечно. Нечего менять — None.
    """
    changed: dict | None = None
    for key, entry in running_rechecks(result).items():
        if is_active(key) or _age_sec(entry.get('started_at'), now) < grace_sec:
            continue
        changed = recheck_failed(changed or result, key, RECHECK_STALE_MESSAGE, finished_at=now.isoformat())
    return changed


def recheck_money(job: Any, *, reserve_kopeks: int, charged_kopeks: int) -> dict[str, int]:
    """Деньги повтора — в родителя: резерв к оценке, факт к списанию, разница к возврату."""
    return {
        'cost_kopeks': (job.cost_kopeks or 0) + charged_kopeks,
        'estimated_kopeks': (job.estimated_kopeks or 0) + reserve_kopeks,
        'refunded_kopeks': (job.refunded_kopeks or 0) + max(0, reserve_kopeks - charged_kopeks),
    }
