"""Тело запуска GEO-РФ (`POST /v1/geo/runs`) из целей и блока «откуда».

Правила сервиса (контракт 2026-09-11) проверяются здесь, до денег и словами:
не больше 20 целей, один туннель, CIDR не проверяется, «тяжёлая» проба только по
доменам, «каждый провайдер» только с охватом. Округ уходит латинским кодом.
Сайт-цель без порта уходит с портом 443 — так по умолчанию считает и сервис.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from app.services.reachability.requests import RequestBuildError
from app.services.reachability.targets import KIND_CIDR, Target, is_hostname


MAX_GEO_TARGETS = 20
DEFAULT_SITE_PORT = 443
NETWORKS = ('res', 'mob')
PROBE_MODES = ('tls', 'tcp')
SCOPE_KINDS = ('all', 'district', 'region', 'cities')
ALL_ISPS = '__ALL__'
TUNNEL_SCHEMES = ('vless://', 'hysteria2://', 'hy2://')

#: Восемь округов GEO: кириллица → латинский код (в JSON-теле принимаются оба, в query — только латиница).
DISTRICT_CODES = {
    'цфо': 'cfo',
    'сзфо': 'szfo',
    'юфо': 'yufo',
    'скфо': 'skfo',
    'пфо': 'pfo',
    'уфо': 'urfo',
    'сфо': 'sfo',
    'дфо': 'dfo',
}


@dataclass(frozen=True)
class GeoScope:
    kind: str = 'all'
    district: str | None = None
    region: str | None = None
    cities: tuple[dict, ...] = ()


@dataclass(frozen=True)
class GeoOptions:
    network: str = 'res'
    scope: GeoScope = field(default_factory=GeoScope)
    isp: str | None = None
    city_limit: int = 0
    probe_mode: str = 'tls'
    heavy: bool = False


def normalize_district(value: str) -> str:
    code = value.strip().lower()
    code = DISTRICT_CODES.get(code, code)
    if code not in DISTRICT_CODES.values():
        raise RequestBuildError(
            f'Неизвестный округ «{value}»: в GEO их восемь — ЦФО, СЗФО, ЮФО, СКФО, ПФО, УФО, СФО, ДФО'
        )
    return code


def _city(raw: dict) -> dict:
    region, city = str(raw.get('region') or '').strip(), str(raw.get('city') or '').strip()
    if not region or not city:
        raise RequestBuildError('У города в списке нужны регион и город (токены из справочника)')
    item = {'region': region, 'city': city}
    if raw.get('isp'):
        item['isp'] = str(raw['isp']).strip()
    return item


def _parse_scope(raw: dict) -> GeoScope:
    kind = str(raw.get('kind') or 'all')
    if kind not in SCOPE_KINDS:
        raise RequestBuildError('Неизвестный охват: вся РФ, округ, регион или список городов')
    scope = GeoScope(
        kind=kind,
        district=normalize_district(str(raw.get('district') or '')) if kind == 'district' else None,
        region=(str(raw.get('region') or '').strip() or None) if kind == 'region' else None,
        cities=tuple(_city(item) for item in (raw.get('cities') or [])) if kind == 'cities' else (),
    )
    if kind == 'region' and not scope.region:
        raise RequestBuildError('Выберите регион из справочника')
    if kind == 'cities' and not scope.cities:
        raise RequestBuildError('Выберите хотя бы один город')
    return scope


def parse_geo_options(raw: dict | None) -> GeoOptions:
    """Блок «откуда» из запроса кабинета → нормализованные параметры; мусор — отказ словами."""
    data = dict(raw or {})
    network = str(data.get('network') or 'res')
    if network not in NETWORKS:
        raise RequestBuildError('Неизвестная сеть: нужна «res» (домашний интернет) или «mob» (мобильный)')
    probe_mode = str(data.get('probe_mode') or 'tls')
    if probe_mode not in PROBE_MODES:
        raise RequestBuildError('Неизвестный метод пробы: нужен «tls» или «tcp»')
    scope = _parse_scope(dict(data.get('scope') or {}))
    isp = str(data.get('isp') or '').strip() or None
    city_limit = int(data.get('city_limit') or 0)
    if city_limit < 0:
        raise RequestBuildError('Потолок городов не может быть отрицательным')
    return GeoOptions(
        network=network,
        scope=scope,
        isp=isp,
        city_limit=city_limit,
        probe_mode=probe_mode,
        heavy=bool(data.get('heavy')),
    )


def _is_tunnel(target: Target) -> bool:
    return bool(target.raw_link) and str(target.raw_link).lower().startswith(TUNNEL_SCHEMES)


def _tunnel_link(target: Target) -> str:
    link = str(target.raw_link)
    return 'hysteria2://' + link[len('hy2://') :] if link.lower().startswith('hy2://') else link


def _is_domain(address: str) -> bool:
    """Доменное имя, а не IP-литерал: `is_hostname` пропускает «192.0.2.1» (цифры и точки проходят)."""
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return is_hostname(address)
    return False


def _site_target(target: Target) -> str:
    """Сайт-цель для сервиса: `host:port`; без порта — 443, как по умолчанию у сервиса."""
    return f'{target.address.lower()}:{target.port or DEFAULT_SITE_PORT}'


def _check_targets(targets: list[Target]) -> tuple[list[Target], list[Target]]:
    if any(target.kind == KIND_CIDR for target in targets):
        raise RequestBuildError('GEO подсети не проверяет — CIDR сканируется на вкладке «Скан CIDR»')
    if not targets:
        raise RequestBuildError('Нет целей для проверки')
    if len(targets) > MAX_GEO_TARGETS:
        raise RequestBuildError(f'GEO проверяет не больше {MAX_GEO_TARGETS} целей за запуск, выбрано {len(targets)}')
    tunnels = [target for target in targets if _is_tunnel(target)]
    if len(tunnels) > 1:
        raise RequestBuildError('В один запуск GEO входит только один конфиг туннеля')
    sites = [target for target in targets if not _is_tunnel(target)]
    return sites, tunnels


def build_geo_request(targets: list[Target], options: GeoOptions, core: str) -> dict:
    """Тело `RunBody`. Сайт-цели уходят как host:port, туннель — своей ссылкой."""
    sites, tunnels = _check_targets(targets)
    heavy = options.heavy and options.probe_mode != 'tcp'
    if heavy and not tunnels and not any(_is_domain(target.address) for target in sites):
        raise RequestBuildError('«Тяжёлой» пробе нужна хотя бы одна доменная цель: по голому IP троттлинг не меряется')
    if options.isp == ALL_ISPS and options.scope.kind == 'all':
        raise RequestBuildError(
            '«По пробе на каждого провайдера» работает только с округом, регионом или списком городов'
        )
    body: dict = {
        'targets': [_site_target(target) for target in sites] + [_tunnel_link(target) for target in tunnels],
        'network': options.network,
        'probe_mode': options.probe_mode,
        'heavy': heavy,
        'core': core or '',
    }
    if options.isp:
        body['isp'] = options.isp
    if options.city_limit:
        body['city_limit'] = options.city_limit
    scope = options.scope
    if scope.kind == 'district':
        body['district'] = scope.district
    elif scope.kind == 'region':
        body['region'] = scope.region
    elif scope.kind == 'cities':
        body['cities'] = list(scope.cities)
    return body
