"""Строки прогона GEO — в вид для кабинета: регион словами, вердикт-результат, задержка, цели, туннель."""

from __future__ import annotations

from app.services.reachability.geo_result import (
    NOISE_VERDICTS,
    RESULT_VERDICTS,
    geo_summary,
    is_result_verdict,
    merge_recheck,
    name_rows,
    normalize_rows,
    recheck_request,
    row_key,
    scope_label,
)


REGIONS = {
    'regions': {'voronezh_oblast': {'name': 'Воронежская область', 'district': 'ЦФО'}},
    'cities': {'voronezh_oblast|voronezh': 'Воронеж'},
}


def row(**kw) -> dict:
    base = {
        'region': 'voronezh_oblast',
        'city': 'voronezh',
        'req_isp': '',
        'provider': 'rostelecom',
        'exit_ip': '203.0.113.7',
        'verdict': 'ok',
        'status': 'ok',
        'mb': 0.12,
        'mb_bill': 0.12,
        'targets': {
            'example.com:443': {'ok': True, 'ms': 140, 'kind': 'tls'},
            'ya.ru:443': {'ok': True, 'ms': 60, 'kind': 'tls'},
        },
    }
    base.update(kw)
    return base


def test_verdict_sets_cover_the_contract() -> None:
    assert frozenset({'ok', 'partial', 'throttled', 'blocked', 'unconfirmed', 'target_error'}) == RESULT_VERDICTS
    assert frozenset({'port_blocked', 'exit_bad', 'no_ru_node', 'no_udp'}) == NOISE_VERDICTS
    assert is_result_verdict('blocked') and not is_result_verdict('exit_bad') and not is_result_verdict('чушь')


def test_row_gets_region_name_district_latency_and_targets() -> None:
    [out] = normalize_rows([row()], REGIONS)
    assert out['region'] == 'voronezh_oblast' and out['region_ru'] == 'Воронежская область'
    assert out['district'] == 'ЦФО'
    assert out['city'] == 'voronezh' and out['city_ru'] == 'Воронеж'
    assert out['provider'] == 'rostelecom' and out['verdict'] == 'ok'
    assert out['is_result'] is True
    assert out['latency_ms'] == 100, 'медиана по ответившим целям'
    assert out['targets'] == [
        {'key': 'example.com:443', 'ok': True, 'ms': 140, 'kind': 'tls', 'err': None},
        {'key': 'ya.ru:443', 'ok': True, 'ms': 60, 'kind': 'tls', 'err': None},
    ]
    assert out['mb_bill'] == 0.12 and out['err'] is None
    assert out['tunnel'] is None and out['heavy'] is None


def test_unknown_region_and_missing_fields_do_not_break() -> None:
    [out] = normalize_rows(
        [{'region': 'nowhere', 'city': 'x', 'verdict': 'no_ru_node', 'err': 'run-timeout'}, 'мусор', None], REGIONS
    )
    assert out['region_ru'] == 'nowhere' and out['district'] == '' and out['city_ru'] == 'x'
    assert out['is_result'] is False and out['latency_ms'] is None and out['targets'] == []
    assert out['err'] == 'run-timeout' and out['mb_bill'] is None


def test_tunnel_checks_and_heavy_fields_are_kept() -> None:
    [out] = normalize_rows(
        [
            row(
                targets={
                    'IP-проверка': {'ok': True, 'ms': 300},
                    'Google': {'ok': False, 'ms': None, 'err': 'timeout'},
                },
                used_core='stable',
                kbps=1200,
                froze=True,
                hv_measured=True,
            )
        ],
        REGIONS,
    )
    assert out['tunnel'] == {
        'used_core': 'stable',
        'checks': [{'name': 'IP-проверка', 'ok': True, 'ms': 300}, {'name': 'Google', 'ok': False, 'ms': None}],
    }
    assert out['targets'] == [], 'подпроверки туннеля не считаются сайт-целями'
    assert out['latency_ms'] == 300
    assert out['heavy'] == {'kbps': 1200, 'froze': True, 'hv_measured': True, 'hv_small': False}


def test_summary_counts_result_and_noise_rows_and_keeps_service_fields() -> None:
    rows = normalize_rows([row(), row(verdict='blocked'), row(verdict='exit_bad')], REGIONS)
    summary = geo_summary(
        {
            'by_verdict': {'ok': 1, 'blocked': 1, 'exit_bad': 1},
            'conclusion': {'code': 'mixed', 'text': 'Смешанная картина'},
            'progress': {'done': 3, 'total': 3},
        },
        rows,
    )
    assert summary['by_verdict'] == {'ok': 1, 'blocked': 1, 'exit_bad': 1}
    assert summary['result_rows'] == 2 and summary['noise_rows'] == 1
    assert summary['conclusion'] == {'code': 'mixed', 'text': 'Смешанная картина'}
    assert summary['progress'] == {'done': 3, 'total': 3}


def test_scope_label_reads_like_the_original() -> None:
    assert scope_label({'network': 'res', 'targets': ['a.example:443']}) == 'сайты · проводной · вся РФ'
    assert (
        scope_label({'network': 'mob', 'targets': ['vless://x@h:443'], 'district': 'cfo', 'isp': 'mts'})
        == 'туннель · мобильный · округ ЦФО · МТС'
    )
    assert (
        scope_label(
            {'network': 'res', 'targets': ['a.example:443'], 'region': 'moscow', 'isp': '__ALL__', 'city_limit': 30}
        )
        == 'сайты · проводной · регион moscow · все провайдеры · до 30 городов'
    )
    assert (
        scope_label(
            {
                'network': 'res',
                'targets': ['a.example:443'],
                'cities': [{'region': 'moscow', 'city': 'moscow'}, {'region': 'spb', 'city': 'spb'}],
            }
        )
        == 'сайты · проводной · 2 города'
    )
    assert scope_label({'targets': ['vless://x@h:443', 'a.example:443']}).startswith('туннель и сайты')
    assert scope_label({'cities': [{}] * 11}).endswith('11 городов')
    assert scope_label({'cities': [{}] * 21}).endswith('21 город')


def test_rows_without_index_keep_tokens_and_name_rows_fills_them_later() -> None:
    [out] = normalize_rows([row()])
    assert out['region_ru'] == 'voronezh_oblast' and out['city_ru'] == 'voronezh' and out['district'] == ''
    [named, stranger] = name_rows([out, {**out, 'region': 'nowhere', 'city': 'x', 'region_ru': 'nowhere'}], REGIONS)
    assert named['region_ru'] == 'Воронежская область' and named['district'] == 'ЦФО' and named['city_ru'] == 'Воронеж'
    assert stranger['region_ru'] == 'nowhere', 'неизвестный регион остаётся токеном'
    assert name_rows([out], {}) == [out], 'пустой индекс — строки как есть'


def test_session_fields_survive_for_a_same_exit_repeat() -> None:
    [out] = normalize_rows([row(sid='s-1', sid_hold_s=287, exit_changed=True)])
    assert out['sid'] == 's-1' and out['sid_hold_s'] == 287 and out['exit_changed'] is True
    [plain] = normalize_rows([row()])
    assert plain['sid'] is None and plain['sid_hold_s'] is None and plain['exit_changed'] is False


PARENT_REQUEST = {
    'targets': ['example.com:443'],
    'network': 'res',
    'probe_mode': 'tls',
    'heavy': False,
    'core': '',
    'isp': '__ALL__',
    'district': 'cfo',
    'city_limit': 30,
}


def test_recheck_request_keeps_targets_and_method_and_narrows_scope_to_the_city() -> None:
    row = {'region': 'moscow', 'city': 'moscow', 'req_isp': 'mts', 'sid': 's-1', 'exit_ip': '203.0.113.7'}
    fresh = recheck_request(PARENT_REQUEST, row, same_exit=False)
    assert fresh == {
        'targets': ['example.com:443'],
        'network': 'res',
        'probe_mode': 'tls',
        'heavy': False,
        'core': '',
        'cities': [{'region': 'moscow', 'city': 'moscow', 'isp': 'mts'}],
    }, 'охват, потолок и «каждый провайдер» родителя не уходят — только этот город и его провайдер'
    same = recheck_request(PARENT_REQUEST, row, same_exit=True)
    assert same['session'] == 's-1' and same['expect_exit_ip'] == '203.0.113.7'
    no_sid = recheck_request(PARENT_REQUEST, {**row, 'sid': None, 'req_isp': None}, same_exit=True)
    assert 'session' not in no_sid and no_sid['cities'] == [{'region': 'moscow', 'city': 'moscow'}]
    assert row_key(row) == ('moscow', 'moscow', 'mts')


def _prow(city: str, verdict: str, exit_ip: str | None, **extra) -> dict:
    return {
        'region': 'r',
        'city': city,
        'req_isp': None,
        'provider': 'p',
        'exit_ip': exit_ip,
        'verdict': verdict,
        'is_result': verdict != 'no_ru_node',
        **extra,
    }


def test_merge_recheck_follows_the_original_rules() -> None:
    parent = {
        'rows': [_prow('a', 'blocked', '1.1.1.1'), _prow('b', 'ok', '2.2.2.2'), _prow('c', 'no_ru_node', None)],
        'summary': {'by_verdict': {'blocked': 1, 'ok': 1, 'no_ru_node': 1}, 'conclusion': {'text': 'старое'}},
        'geo': {'n_nodes': 3},
    }
    # Тот же выход — прежняя строка заменяется свежей (переезжает в конец).
    merged = merge_recheck(parent, [_prow('a', 'ok', '1.1.1.1')], child_id=9)
    assert [row['city'] for row in merged['rows']] == ['b', 'c', 'a']
    assert merged['rows'][-1] == {**_prow('a', 'ok', '1.1.1.1'), 'recheck_job_id': 9}
    assert merged['summary']['by_verdict'] == {'ok': 2, 'no_ru_node': 1}
    assert merged['summary']['conclusion'] is None and merged['geo'] == {'n_nodes': 3}
    # Другой выход — строка добавляется с «новый выход», прежняя остаётся и помечается «перепроверено».
    merged = merge_recheck(parent, [_prow('a', 'ok', '9.9.9.9')], child_id=10)
    rows = merged['rows']
    assert rows[0] == {**_prow('a', 'blocked', '1.1.1.1'), 'rechecked': True}
    assert rows[-1] == {**_prow('a', 'ok', '9.9.9.9'), 'recheck_job_id': 10, 'new_exit': True}
    assert merged['summary']['by_verdict'] == {'blocked': 1, 'ok': 2, 'no_ru_node': 1}
    assert merged['summary']['result_rows'] == 3 and merged['summary']['noise_rows'] == 1
    # Строка без выхода («нет RU-ноды») — заменяется на месте.
    merged = merge_recheck(parent, [_prow('c', 'ok', '3.3.3.3')], child_id=11)
    assert [row['city'] for row in merged['rows']] == ['a', 'b', 'c']
    assert merged['rows'][2]['exit_ip'] == '3.3.3.3' and 'new_exit' not in merged['rows'][2]
    # Мусор в строках повтора пропускается, родитель без строк не ломается.
    assert merge_recheck({}, ['x', None], child_id=1)['rows'] == []
