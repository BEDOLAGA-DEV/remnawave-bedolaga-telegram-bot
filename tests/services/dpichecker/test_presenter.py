"""Сырые results DPI//CHECKER → вид для кабинета: по ресурсу, с «из-за границы», именами из панели,
пояснениями и сводкой «доступно / частично / недоступно»."""

from app.services.dpichecker.presenter import present_check
from tests.fixtures.dpichecker_fixtures import load_dpichecker_fixture


def _fx(name: str) -> dict:
    return load_dpichecker_fixture(name)['body']


def test_vpn_check_grouped_per_key_with_abroad_row_and_names():
    check = _fx('check_vpn')
    first_uri = check['results'][0]['uri']
    view = present_check(check, {first_uri: 'Finland · fi1'})
    assert len(view['resources']) == 2
    key = view['resources'][0]
    assert key['name'] == 'Finland · fi1'
    assert key['value'] is None
    assert key['total'] == 5
    assert key['direct'] is not None and key['direct']['ok'] is True
    assert all(row['pop_id'] is not None for row in key['rows'])
    ok_row = next(row for row in key['rows'] if row['ok'])
    assert {s['host'] for s in ok_row['speeds']} == {'instagram.com', 'telegram.org'}
    assert sum(view['summary'][k] for k in ('available', 'partial', 'unavailable')) == 2


def test_vpn_key_without_panel_name_uses_host_label():
    check = _fx('check_vpn')
    view = present_check(check, {})
    assert view['resources'][0]['name'] == check['results'][0]['host']
    assert 'vless://' not in str(view)


def test_failed_vpn_row_says_whether_point_had_internet():
    rows = [row for res in present_check(_fx('check_vpn'), {})['resources'] for row in res['rows'] if not row['ok']]
    assert rows
    assert rows[0]['internet_ok'] is True and rows[0]['error']


def test_rows_sorted_by_region_and_counts_match():
    res = present_check(_fx('check_vpn'), {})['resources'][0]
    regions = [row['region'] for row in res['rows']]
    assert regions == sorted(regions, key=str.casefold)
    assert res['ok_count'] == sum(1 for row in res['rows'] if row['ok'])


def test_ip_check_shows_address_and_verdict():
    view = present_check(_fx('check_ip_server'), {})
    res = view['resources'][0]
    assert res['value'] == 'google.com' and res['name'] == 'google.com'
    assert res['ok_count'] == res['total'] == 10
    assert res['rows'][0]['mode'] == 'server' and res['rows'][0]['verdict'] == 'clean'
    assert view['summary']['available'] == 1
    assert view['summary']['avg_latency_ms'] is not None


def test_ip_abroad_row_keeps_error_code():
    res = present_check(_fx('check_ip_noserver'), {})['resources'][0]
    assert res['direct'] == {'ok': False, 'latency_ms': None, 'error_code': 'https_upgrade_stub'}


def test_mtproto_row_reason_is_status_and_link_hidden():
    view = present_check(_fx('check_mtproto'), {})
    res = view['resources'][0]
    assert res['rows'][0]['ok'] is False and res['rows'][0]['reason'] == 'tls_handshake_failed'
    assert res['value'] is None and res['name'] == 'MTProto'
    assert 'tg://' not in str(view)
    assert view['summary']['unavailable'] == 1


def test_pending_check_without_results_is_empty():
    view = present_check({'id': 1, 'status': 'pending', 'check_type': 'ip', 'results': None, 'progress': {}}, {})
    assert view['resources'] == []
    assert view['summary'] == {'available': 0, 'partial': 0, 'unavailable': 0, 'avg_latency_ms': None}


def test_row_says_when_point_proxy_was_dead():
    """Карта различает «недоступно» и «прокси точки не поднялся» — как подсказка региона на сайте."""
    check = _fx('check_vpn')
    dead = {**check['results'][0], 'connected': False, 'proxy_dead': True}
    view = present_check({**check, 'results': [dead, *check['results'][1:]]}, {})
    rows = view['resources'][0]['rows']
    assert any(row['proxy_dead'] and not row['ok'] for row in rows)
    assert all(row['proxy_dead'] is False for row in view['resources'][1]['rows'])
