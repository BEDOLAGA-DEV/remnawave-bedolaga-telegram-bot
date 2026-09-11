"""Строки прогона GEO — в вид для кабинета: регион словами, вердикт-результат, задержка, цели, туннель."""

from __future__ import annotations

from app.services.reachability.geo_result import (
    NOISE_VERDICTS,
    RESULT_VERDICTS,
    geo_summary,
    is_result_verdict,
    name_rows,
    normalize_rows,
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
