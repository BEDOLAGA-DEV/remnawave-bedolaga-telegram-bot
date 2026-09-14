"""Тела запросов bschekbot из целей и раскрытых симок.

Правила из живого API: строгий селектор проб (хотя бы одна), SNI только парой
``probes.sni`` + ``sni_hosts``, не больше 10 целей на probe (``too_many_targets``),
не больше 20 конфигов на VLESS-тест, скан — ровно одна подсеть /24.
"""

from __future__ import annotations

import ipaddress

from app.services.reachability.links import MAX_CONFIGS_PER_TEST
from app.services.reachability.targets import KIND_CIDR, Target, is_hostname, probe_api_target


PROBE_NAMES = ('icmp', 'tcp', 'sni')
MAX_PROBE_TARGETS = 10
MAX_SNI_HOSTS = 5  # как Multi-SNI в оригинале: до 5 имён за прогон
# Белый домен по умолчанию для TLS-SNI, когда имя не ввели и у целей его нет (плейсхолдер оригинала).
DEFAULT_SNI_HOST = 'ads.x5.ru'


class RequestBuildError(ValueError):
    """Запрос не собрать — сообщение для админа."""


def normalize_probes(probes: dict[str, bool] | None) -> dict[str, bool]:
    clean = {name: bool((probes or {}).get(name, False)) for name in PROBE_NAMES}
    if not any(clean.values()):
        raise RequestBuildError('Не выбрано ни одной пробы (ICMP, TCP или SNI)')
    return clean


def _is_ip_literal(address: str) -> bool:
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return False
    return True


def sni_name_for(target: Target) -> str | None:
    """Имя для TLS-SNI: SNI цели, а без него — её адрес, если это домен. У голого IP имени нет."""
    sni = (target.sni or '').strip().lower()
    if sni:
        return sni
    address = target.address.strip().lower()
    return None if not address or _is_ip_literal(address) else address


def sni_hosts_for(targets: list[Target]) -> list[str]:
    """Имена для SNI-пробы по всем целям: уникальные, по алфавиту, без IP-адресов."""
    return sorted({name for name in map(sni_name_for, targets) if name})


def normalize_sni_hosts(names: list[str] | None) -> list[str]:
    """Свои имена для TLS-SNI: домены в нижнем регистре, без повторов, не больше пяти."""
    clean: list[str] = []
    for raw in names or []:
        name = str(raw or '').strip().lower().rstrip('.')
        if not name:
            continue
        if _is_ip_literal(name) or not is_hostname(name):
            raise RequestBuildError(f'«{name}» не похоже на домен для SNI')
        if name not in clean:
            clean.append(name)
    if len(clean) > MAX_SNI_HOSTS:
        raise RequestBuildError(f'API принимает не больше {MAX_SNI_HOSTS} имён SNI за проверку')
    return clean


def resolve_sni_hosts(
    targets: list[Target], explicit: list[str] | None, default_sni: str | None = DEFAULT_SNI_HOST
) -> list[str]:
    """Имена для SNI-пробы: свои → из целей → белый домен по умолчанию."""
    names = normalize_sni_hosts(explicit)
    if names:
        return names
    auto = sni_hosts_for(targets)
    if auto:
        return auto
    return normalize_sni_hosts([default_sni]) if default_sni else []


def build_probe_request(
    targets: list[Target],
    units: list[str],
    dpi: str,
    probes: dict[str, bool],
    sni_hosts: list[str] | None = None,
    default_sni: str | None = DEFAULT_SNI_HOST,
) -> dict:
    hosts = [target for target in targets if target.kind != KIND_CIDR]
    if not hosts:
        raise RequestBuildError('Нет целей для пробы')
    if len(hosts) > MAX_PROBE_TARGETS:
        raise RequestBuildError(f'API проверяет не больше {MAX_PROBE_TARGETS} целей за раз, выбрано {len(hosts)}')
    clean_probes = normalize_probes(probes)
    body = {
        'targets': [probe_api_target(target) for target in hosts],
        'operators': list(units),
        'probes': clean_probes,
        'dpi': dpi,
    }
    if not clean_probes['sni']:
        return body
    names = resolve_sni_hosts(hosts, sni_hosts, default_sni)
    if not names:
        raise RequestBuildError('Для TLS-SNI укажите SNI-хост или добавьте домен среди целей: у IP-адреса нет имени')
    return {**body, 'sni_hosts': names}


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
    target: Target,
    units: list[str],
    dpi: str,
    probes: dict[str, bool],
    sni_hosts: list[str],
    default_sni: str | None = DEFAULT_SNI_HOST,
) -> dict:
    if target.kind != KIND_CIDR:
        raise RequestBuildError('Скан принимает только подсеть /24')
    clean_probes = normalize_probes(probes)
    body = {'cidr': target.target_key, 'operators': list(units), 'probes': clean_probes, 'dpi': dpi}
    if not clean_probes['sni']:
        return body
    names = normalize_sni_hosts(sni_hosts) or (normalize_sni_hosts([default_sni]) if default_sni else [])
    if not names:
        raise RequestBuildError('Для SNI-пробы скана укажите SNI-хост')
    return {**body, 'sni_hosts': names}
