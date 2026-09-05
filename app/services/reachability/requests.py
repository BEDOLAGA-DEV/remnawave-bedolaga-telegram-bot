"""Тела запросов bschekbot из целей и раскрытых симок.

Правила из живого API: строгий селектор проб (хотя бы одна), SNI только парой
``probes.sni`` + ``sni_hosts``, не больше 20 конфигов на VLESS-тест, скан — ровно
одна подсеть /24.
"""

from __future__ import annotations

from app.services.reachability.links import MAX_CONFIGS_PER_TEST
from app.services.reachability.targets import KIND_CIDR, Target, probe_api_target


PROBE_NAMES = ('icmp', 'tcp', 'sni')


class RequestBuildError(ValueError):
    """Запрос не собрать — сообщение для админа."""


def normalize_probes(probes: dict[str, bool] | None) -> dict[str, bool]:
    clean = {name: bool((probes or {}).get(name, False)) for name in PROBE_NAMES}
    if not any(clean.values()):
        raise RequestBuildError('Не выбрано ни одной пробы (ICMP, TCP или SNI)')
    return clean


def sni_hosts_for(targets: list[Target]) -> list[str]:
    """Имена для SNI-пробы: SNI цели, а без него — её адрес. Уникальные, по алфавиту."""
    return sorted({(target.sni or target.address).lower() for target in targets})


def build_probe_request(targets: list[Target], units: list[str], dpi: str, probes: dict[str, bool]) -> dict:
    hosts = [target for target in targets if target.kind != KIND_CIDR]
    if not hosts:
        raise RequestBuildError('Нет целей для пробы')
    clean_probes = normalize_probes(probes)
    body = {
        'targets': [probe_api_target(target) for target in hosts],
        'operators': list(units),
        'probes': clean_probes,
        'dpi': dpi,
    }
    if clean_probes['sni']:
        return {**body, 'sni_hosts': sni_hosts_for(hosts)}
    return body


def build_vless_request(targets: list[Target], units: list[str], dpi: str, core: str) -> dict:
    links = [target.raw_link for target in targets if target.raw_link]
    if len(links) != len(targets):
        raise RequestBuildError('Для VLESS-теста нужны конфиги (ссылки), а не адреса')
    if not links:
        raise RequestBuildError('Нет конфигов для теста')
    if len(links) > MAX_CONFIGS_PER_TEST:
        raise RequestBuildError(
            f'API принимает не больше {MAX_CONFIGS_PER_TEST} конфигов за тест, выбрано {len(links)}'
        )
    return {'raw_input': '\n'.join(links), 'selected_modems': list(units), 'dpi': dpi, 'core': core or ''}


def build_scan_request(
    target: Target, units: list[str], dpi: str, probes: dict[str, bool], sni_hosts: list[str]
) -> dict:
    if target.kind != KIND_CIDR:
        raise RequestBuildError('Скан принимает только подсеть /24')
    clean_probes = normalize_probes(probes)
    body = {'cidr': target.target_key, 'operators': list(units), 'probes': clean_probes, 'dpi': dpi}
    if not clean_probes['sni']:
        return body
    if not sni_hosts:
        raise RequestBuildError('Для SNI-пробы скана укажите имена (sni_hosts)')
    return {**body, 'sni_hosts': list(sni_hosts)}
