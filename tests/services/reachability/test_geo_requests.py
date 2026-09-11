"""Тело POST /v1/geo/runs из целей и блока «откуда»: правила сервиса ловим у себя, словами."""

from __future__ import annotations

import pytest

from app.services.reachability.geo_requests import GeoOptions, GeoScope, build_geo_request, parse_geo_options
from app.services.reachability.kinds import KIND_GEO
from app.services.reachability.requests import RequestBuildError
from app.services.reachability.targets import Target


LINK = 'vless://00000000-0000-4000-8000-000000000001@srv.example:443?security=reality&sni=srv.example#S'


def site(address: str, port: int | None = 443, kind: str = 'custom') -> Target:
    return Target(kind=kind, label=address, address=address, port=port, target_key=f'{address}:{port}', sni=None)


def tunnel(link: str = LINK) -> Target:
    return Target(
        kind='subscription_config',
        label='S',
        address='srv.example',
        port=443,
        target_key='srv.example:443',
        sni='srv.example',
        raw_link=link,
    )


def options(**kw) -> GeoOptions:
    base = dict(network='res', scope=GeoScope(kind='all'), isp=None, city_limit=0, probe_mode='tls', heavy=False)
    base.update(kw)
    return GeoOptions(**base)


def test_kind_geo_exists() -> None:
    assert KIND_GEO == 'geo'


def test_sites_go_as_host_port_and_tunnel_as_its_link() -> None:
    body = build_geo_request([site('example.com'), tunnel()], options(), '')
    assert body['targets'] == ['example.com:443', LINK]
    assert body['network'] == 'res' and body['probe_mode'] == 'tls' and body['heavy'] is False
    assert body['core'] == ''
    assert 'district' not in body and 'region' not in body and 'cities' not in body and 'isp' not in body


def test_site_without_port_goes_with_443_by_contract() -> None:
    # «example.com» из поля адресов приходит без порта; у сервиса порт по умолчанию 443.
    body = build_geo_request([site('example.com', port=None), site('203.0.113.7', port=8443)], options(), '')
    assert body['targets'] == ['example.com:443', '203.0.113.7:8443']


def test_scope_district_region_cities_and_isp_are_passed_through() -> None:
    district = build_geo_request([site('a.example')], options(scope=GeoScope(kind='district', district='cfo')), '')
    region = build_geo_request([site('a.example')], options(scope=GeoScope(kind='region', region='moscow')), '')
    cities = build_geo_request(
        [site('a.example')],
        options(
            scope=GeoScope(kind='cities', cities=({'region': 'moscow', 'city': 'moscow', 'isp': 'mts'},)),
            isp='__ALL__',
            city_limit=30,
        ),
        'stable',
    )
    assert district['district'] == 'cfo' and 'region' not in district
    assert region['region'] == 'moscow'
    assert cities['cities'] == [{'region': 'moscow', 'city': 'moscow', 'isp': 'mts'}]
    assert cities['isp'] == '__ALL__' and cities['city_limit'] == 30 and cities['core'] == 'stable'


def test_more_than_twenty_targets_is_refused_in_words() -> None:
    with pytest.raises(RequestBuildError, match='не больше 20 целей'):
        build_geo_request([site(f's{i}.example') for i in range(21)], options(), '')


def test_second_tunnel_is_refused() -> None:
    with pytest.raises(RequestBuildError, match='один конфиг'):
        build_geo_request([tunnel(), tunnel(LINK.replace('srv', 'other'))], options(), '')


def test_hy2_link_is_normalized_to_hysteria2() -> None:
    body = build_geo_request([tunnel('hy2://pass@h.example:443/?sni=h.example#H')], options(), '')
    assert body['targets'] == ['hysteria2://pass@h.example:443/?sni=h.example#H']


def test_cidr_is_refused_with_a_pointer_to_the_scan_tab() -> None:
    cidr = Target(
        kind='cidr', label='192.0.2.0/24', address='192.0.2.0/24', port=None, target_key='192.0.2.0/24', sni=None
    )
    with pytest.raises(RequestBuildError, match='CIDR'):
        build_geo_request([cidr], options(), '')


def test_heavy_needs_a_domain_site_target_and_is_dropped_in_tcp() -> None:
    with pytest.raises(RequestBuildError, match='доменн'):
        build_geo_request([site('192.0.2.1')], options(heavy=True), '')
    body = build_geo_request([site('example.com')], options(heavy=True, probe_mode='tcp'), '')
    assert body['heavy'] is False
    body = build_geo_request([tunnel()], options(heavy=True), '')
    assert body['heavy'] is True, 'у туннеля heavy разрешён без доменных целей'


def test_all_isps_needs_a_scope() -> None:
    with pytest.raises(RequestBuildError, match='каждого провайдера'):
        build_geo_request([site('a.example')], options(isp='__ALL__'), '')


def test_no_targets_is_refused() -> None:
    with pytest.raises(RequestBuildError, match='Нет целей'):
        build_geo_request([], options(), '')


def test_parse_geo_options_defaults_and_normalization() -> None:
    parsed = parse_geo_options(None)
    assert parsed == options()
    parsed = parse_geo_options(
        {
            'network': 'mob',
            'scope': {'kind': 'district', 'district': 'ЦФО'},
            'isp': 'mts',
            'city_limit': 10,
            'probe_mode': 'tcp',
            'heavy': True,
        }
    )
    assert parsed.network == 'mob' and parsed.scope.district == 'cfo' and parsed.isp == 'mts'
    assert parsed.city_limit == 10 and parsed.probe_mode == 'tcp' and parsed.heavy is True


def test_parse_geo_options_rejects_garbage() -> None:
    with pytest.raises(RequestBuildError, match='сеть'):
        parse_geo_options({'network': 'wifi'})
    with pytest.raises(RequestBuildError, match='округ'):
        parse_geo_options({'scope': {'kind': 'district', 'district': 'krym'}})
    with pytest.raises(RequestBuildError, match='метод'):
        parse_geo_options({'probe_mode': 'udp'})
    with pytest.raises(RequestBuildError, match='регион'):
        parse_geo_options({'scope': {'kind': 'region'}})
    with pytest.raises(RequestBuildError, match='город'):
        parse_geo_options({'scope': {'kind': 'cities', 'cities': []}})
    with pytest.raises(RequestBuildError, match='отрицательн'):
        parse_geo_options({'city_limit': -3})
