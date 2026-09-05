# Доступность из РФ (bschekbot) — план реализации, бот, часть 2: задачи и API кабинета

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать поверх ядра (часть 1) сервис задач с фоновым исполнением, повторами тем же ключом и обходчиком, фасад сервиса и роуты `/admin/reachability` для кабинета, подключить фон в `main.py` и добавить живой детектор дрейфа контракта.

**Architecture:** `JobRunner` ведёт задачу по состояниям `pending → running(phase) → done|failed|cancelled`, все обращения к API идут через шлюз и с ключом задачи; результаты раскладываются в леги. `ReachabilityService` — фасад для роутов: статус, симки, цели, preview, запуск, история, отмена, сводка. Роуты тонкие: валидация pydantic, права, перевод исключений в HTTP.

**Tech Stack:** как в части 1 + FastAPI, pydantic v2.

**Spec:** `docs/superpowers/specs/2026-09-05-reachability-bschek-design.md` (разделы 8, 9, 11, приложение А). Часть 1 плана: `docs/superpowers/plans/2026-09-05-reachability-bot-core.md` — её интерфейсы считаются готовыми.

## Global Constraints

Те же, что в части 1 (см. её раздел Global Constraints). Дополнительно:
- Один ключ идемпотентности на задачу навсегда: любой повтор — `job.idempotency_key` + `job.request` без изменений.
- Статус асинхронной задачи читается только из GET; ответ на повторный submit статусом не считается.
- Фон получает свою сессию через `AsyncSessionLocal` (`app/database/database.py`), у задач держится сильная ссылка (образец: `_schedule_promo_notifications` в `app/cabinet/routes/admin_promo_offers.py`).
- Ключ API не попадает в логи; `webhook_secret` на фронт не отдаётся (клиент его уже отбросил).

## Карта файлов (часть 2)

| Файл | Ответственность |
|---|---|
| `app/services/reachability/resolver.py` | `TargetResolver`: хосты, ноды, конфиги подписки, ввод → `Target` |
| `app/services/reachability/legs.py` | `build_probe_legs`, `build_vless_legs`, `merge_skipped` |
| `app/services/reachability/jobs.py` | `JobRunner`, константы статусов и фаз, `RunnerConfig` |
| `app/services/reachability/requests.py` | тела запросов к API из целей и симок |
| `app/services/reachability/service.py` | `ReachabilityService` (фасад), исключения домена |
| `app/cabinet/schemas/reachability.py` | pydantic-схемы |
| `app/cabinet/routes/admin_reachability.py` | роуты `/admin/reachability` |
| `app/cabinet/routes/__init__.py` | регистрация роутера |
| `main.py` | запуск/остановка обходчика |
| `pyproject.toml` | маркер `bschek_live` |
| `tests/services/reachability/conftest.py` | `FakeClock`, `FakeAPI`, фабрика сессий на SQLite |
| `tests/services/reachability/test_resolver.py`, `test_legs.py`, `test_jobs.py`, `test_requests.py`, `test_service.py`, `tests/cabinet/test_admin_reachability.py`, `tests/live/test_bschek_live.py` | тесты |

---

### Task 11: Разрешение целей из источников

**Files:**
- Create: `app/services/reachability/resolver.py`
- Test: `tests/services/reachability/test_resolver.py`

**Interfaces:**
- Consumes: `RemnaWaveHost`, `RemnaWaveNode` (`app/external/remnawave_api.py`), `parse_links`, `MAX_CONFIGS_PER_TEST` (`links.py`), `Target`, `target_key`, `guess_purpose`, `normalize_custom_target`, `validate_cidr24`, `hosts_for_node`, константы `KIND_*`/`PURPOSE_*` (`targets.py`).
- Produces:
  - `class TargetResolutionError(ValueError)`.
  - `PrefsMap = dict[tuple[str, str], tuple[str, bool]]` — `(kind, ref) -> (purpose, excluded)`.
  - `@dataclass(frozen=True) HostView(host: RemnaWaveHost, target: Target, purpose_guessed: bool, excluded: bool, node_uuids: list[str])`.
  - `@dataclass(frozen=True) NodeView(node: RemnaWaveNode, target: Target, host_uuids: list[str])`.
  - `@dataclass(frozen=True) SubscriptionConfigs(short_uuid: str, configs: list[Target], rejected: list[RejectedLink])`.
  - `class TargetResolver(*, fetch_hosts, fetch_nodes, fetch_links, prefs: PrefsMap)` с `async hosts(include_disabled=False) -> list[HostView]`, `async nodes() -> list[NodeView]`, `async subscription_configs(short_uuid) -> SubscriptionConfigs`, `async resolve(items: list[dict]) -> list[Target]`.
  - `target_from_host(host, purpose) -> Target`, `target_from_node(node) -> Target`, `target_from_link(link, short_uuid, index) -> Target`, `target_from_cidr(value) -> Target`.

- [ ] **Step 1: Падающий тест**

```python
"""Цели из пяти источников приводятся к одному формату; ссылки — только через панель."""

from __future__ import annotations

import pytest

from app.external.remnawave_api import RemnaWaveHost, RemnaWaveNode
from app.services.reachability.resolver import TargetResolutionError, TargetResolver
from app.services.reachability.targets import KIND_CIDR, KIND_CUSTOM, KIND_HOST, KIND_NODE, KIND_SUBSCRIPTION_CONFIG


UUID = '00000000-0000-4000-8000-000000000001'
BS_LINK = f'vless://{UUID}@bs-host.example:9443?security=reality&sni=whitelisted.example#BS'
EU_LINK = f'vless://{UUID}@eu-host.example:443?security=reality&sni=eu-host.example#EU'
STUB = f'vless://{UUID}@0.0.0.0:1?security=none#stub'

HOSTS = [
    RemnaWaveHost(uuid='h-bs', remark='RU | LTE | БС', address='bs-host.example', port=9443, sni='whitelisted.example', config_profile_inbound_uuid='in-bs'),
    RemnaWaveHost(uuid='h-eu', remark='Germany', address='eu-host.example', port=443, sni='eu-host.example', config_profile_inbound_uuid='in-eu'),
    RemnaWaveHost(uuid='h-off', remark='Old', address='old.example', port=443, is_disabled=True),
]
NODES = [
    RemnaWaveNode(uuid='n-1', name='DE-1', address='192.0.2.142', country_code='DE', is_connected=True, is_disabled=False, users_online=0, traffic_used_bytes=0, traffic_limit_bytes=None, port=2222, active_inbounds=['in-eu']),
]


def _resolver(prefs: dict | None = None, links: list[str] | None = None) -> TargetResolver:
    async def fetch_hosts():
        return HOSTS

    async def fetch_nodes():
        return NODES

    async def fetch_links(short_uuid: str):
        assert short_uuid == 'sub-1'
        return links if links is not None else [BS_LINK, EU_LINK, STUB]

    return TargetResolver(fetch_hosts=fetch_hosts, fetch_nodes=fetch_nodes, fetch_links=fetch_links, prefs=prefs or {})


async def test_hosts_hide_disabled_by_default_and_guess_purpose() -> None:
    views = await _resolver().hosts()
    assert [v.host.uuid for v in views] == ['h-bs', 'h-eu']
    bs, eu = views
    assert (bs.target.kind, bs.target.target_key, bs.target.sni, bs.target.purpose, bs.purpose_guessed) == (KIND_HOST, 'bs-host.example:9443', 'whitelisted.example', 'bs', True)
    assert (eu.target.purpose, eu.node_uuids) == ('regular', ['n-1'])
    assert len(await _resolver().hosts(include_disabled=True)) == 3


async def test_prefs_override_guess_and_mark_excluded() -> None:
    views = await _resolver(prefs={('host', 'h-bs'): ('regular', True)}).hosts()
    assert (views[0].target.purpose, views[0].purpose_guessed, views[0].excluded) == ('regular', False, True)


async def test_nodes_expose_icmp_target_and_linked_hosts() -> None:
    views = await _resolver().nodes()
    assert views[0].target.kind == KIND_NODE
    assert (views[0].target.address, views[0].target.port, views[0].host_uuids) == ('192.0.2.142', None, ['h-eu'])


async def test_subscription_configs_parse_links_and_reject_stubs() -> None:
    configs = await _resolver().subscription_configs('sub-1')
    assert [c.label for c in configs.configs] == ['BS', 'EU']
    assert configs.configs[0].kind == KIND_SUBSCRIPTION_CONFIG
    assert configs.configs[0].raw_link == BS_LINK
    assert configs.configs[0].purpose == 'bs'
    assert [r.reason for r in configs.rejected] == ['stub']


async def test_resolve_mixed_items_dedups_by_target_key() -> None:
    targets = await _resolver().resolve(
        [
            {'kind': 'host', 'ref': 'h-bs'},
            {'kind': 'custom', 'value': 'BS-HOST.example:9443'},
            {'kind': 'node', 'ref': 'n-1'},
            {'kind': 'subscription_config', 'short_uuid': 'sub-1', 'index': 1},
            {'kind': 'cidr', 'value': '192.0.2.77/24'},
        ]
    )
    assert [t.kind for t in targets] == [KIND_HOST, KIND_NODE, KIND_SUBSCRIPTION_CONFIG, KIND_CIDR]
    assert targets[-1].target_key == '192.0.2.0/24'


async def test_resolve_custom_link_becomes_config_target() -> None:
    targets = await _resolver().resolve([{'kind': 'custom', 'value': EU_LINK}])
    assert (targets[0].kind, targets[0].raw_link, targets[0].target_key) == (KIND_CUSTOM, EU_LINK, 'eu-host.example:443')


@pytest.mark.parametrize(
    'item',
    [
        {'kind': 'host', 'ref': 'missing'},
        {'kind': 'node', 'ref': 'missing'},
        {'kind': 'subscription_config', 'short_uuid': 'sub-1', 'index': 7},
        {'kind': 'custom', 'value': '10.0.0.1'},
        {'kind': 'cidr', 'value': '192.0.2.0/23'},
        {'kind': 'teapot', 'value': 'x'},
    ],
)
async def test_resolve_reports_unknown_targets(item: dict) -> None:
    with pytest.raises((TargetResolutionError, ValueError)):
        await _resolver().resolve([item])
```

Если конструктор `RemnaWaveNode` требует другие обязательные поля — взять их из dataclass в `app/external/remnawave_api.py` (строки ~150–182) и дополнить.

- [ ] **Step 2: Убедиться, что падает** — `uv run pytest tests/services/reachability/test_resolver.py -q`.

- [ ] **Step 3: Реализация** `app/services/reachability/resolver.py`

```python
"""Разрешение целей: хосты и ноды панели, конфиги подписки, ввод админа, подсети.

Источники дёргаются лениво и один раз на вызов. Конфиги — только через API панели
(`/api/sub/{shortUuid}/info` → links[]), публичный sub-URL отдаёт заглушки.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.external.remnawave_api import RemnaWaveHost, RemnaWaveNode
from app.services.reachability.links import ParsedLink, RejectedLink, SUPPORTED_SCHEMES, parse_links
from app.services.reachability.targets import (
    KIND_CIDR,
    KIND_CUSTOM,
    KIND_HOST,
    KIND_NODE,
    KIND_SUBSCRIPTION_CONFIG,
    PURPOSE_UNKNOWN,
    Target,
    guess_purpose,
    hosts_for_node,
    normalize_custom_target,
    target_key,
    validate_cidr24,
)


PrefsMap = dict[tuple[str, str], tuple[str, bool]]


class TargetResolutionError(ValueError):
    """Цель не найдена в источнике — сообщение для админа."""


@dataclass(frozen=True)
class HostView:
    host: RemnaWaveHost
    target: Target
    purpose_guessed: bool
    excluded: bool
    node_uuids: list[str]


@dataclass(frozen=True)
class NodeView:
    node: RemnaWaveNode
    target: Target
    host_uuids: list[str]


@dataclass(frozen=True)
class SubscriptionConfigs:
    short_uuid: str
    configs: list[Target]
    rejected: list[RejectedLink]


def target_from_host(host: RemnaWaveHost, purpose: str) -> Target:
    sni = host.sni or host.host or host.address
    return Target(
        kind=KIND_HOST,
        label=host.remark or host.address,
        address=host.address.lower(),
        port=host.port,
        target_key=target_key(host.address, host.port),
        sni=sni,
        ref={'host_uuid': host.uuid},
        purpose=purpose,
    )


def target_from_node(node: RemnaWaveNode) -> Target:
    return Target(
        kind=KIND_NODE,
        label=node.name,
        address=node.address.lower(),
        port=None,
        target_key=target_key(node.address, None),
        sni=None,
        ref={'node_uuid': node.uuid},
        purpose=PURPOSE_UNKNOWN,
    )


def target_from_link(link: ParsedLink, kind: str, ref: dict) -> Target:
    return Target(
        kind=kind,
        label=link.name or f'{link.address}:{link.port}',
        address=link.address.lower(),
        port=link.port,
        target_key=target_key(link.address, link.port),
        sni=link.sni,
        ref=ref,
        purpose=guess_purpose(address=link.address, sni=link.sni, remark=link.name),
        raw_link=link.raw,
    )


def target_from_cidr(value: str) -> Target:
    cidr = validate_cidr24(value)
    network = ipaddress.ip_network(cidr)
    return Target(kind=KIND_CIDR, label=cidr, address=str(network.network_address), port=None, target_key=cidr, sni=None)


def _is_config_link(value: str) -> bool:
    return '://' in value and value.split('://', 1)[0].lower() in SUPPORTED_SCHEMES


class TargetResolver:
    def __init__(
        self,
        *,
        fetch_hosts: Callable[[], Awaitable[list[RemnaWaveHost]]],
        fetch_nodes: Callable[[], Awaitable[list[RemnaWaveNode]]],
        fetch_links: Callable[[str], Awaitable[list[str]]],
        prefs: PrefsMap,
    ) -> None:
        self._fetch_hosts = fetch_hosts
        self._fetch_nodes = fetch_nodes
        self._fetch_links = fetch_links
        self._prefs = prefs
        self._hosts: list[RemnaWaveHost] | None = None
        self._nodes: list[RemnaWaveNode] | None = None

    async def _all_hosts(self) -> list[RemnaWaveHost]:
        if self._hosts is None:
            self._hosts = list(await self._fetch_hosts())
        return self._hosts

    async def _all_nodes(self) -> list[RemnaWaveNode]:
        if self._nodes is None:
            self._nodes = list(await self._fetch_nodes())
        return self._nodes

    def _host_view(self, host: RemnaWaveHost, nodes: list[RemnaWaveNode]) -> HostView:
        pref = self._prefs.get((KIND_HOST, host.uuid))
        guessed = pref is None or pref[0] == PURPOSE_UNKNOWN
        purpose = guess_purpose(address=host.address, sni=host.sni, remark=host.remark, tag=host.tag) if guessed else pref[0]
        node_uuids = [
            node.uuid
            for node in nodes
            if host in hosts_for_node([host], node_active_inbounds=node.active_inbounds or [], node_address=node.address,
                                      node_ips=[str(ip.get('ip')) for ip in (node.ips or []) if ip.get('ip')])
        ]
        return HostView(host=host, target=target_from_host(host, purpose), purpose_guessed=guessed,
                        excluded=bool(pref and pref[1]), node_uuids=node_uuids)

    async def hosts(self, include_disabled: bool = False) -> list[HostView]:
        nodes = await self._all_nodes()
        hosts = [h for h in await self._all_hosts() if include_disabled or not h.is_disabled]
        return [self._host_view(host, nodes) for host in sorted(hosts, key=lambda h: h.view_position)]

    async def nodes(self) -> list[NodeView]:
        hosts = await self._all_hosts()
        views = []
        for node in await self._all_nodes():
            linked = hosts_for_node(hosts, node_active_inbounds=node.active_inbounds or [], node_address=node.address,
                                    node_ips=[str(ip.get('ip')) for ip in (node.ips or []) if ip.get('ip')])
            views.append(NodeView(node=node, target=target_from_node(node), host_uuids=[h.uuid for h in linked]))
        return views

    async def subscription_configs(self, short_uuid: str) -> SubscriptionConfigs:
        parsed, rejected = parse_links('\n'.join(await self._fetch_links(short_uuid)))
        configs = [
            target_from_link(link, KIND_SUBSCRIPTION_CONFIG, {'short_uuid': short_uuid, 'index': index})
            for index, link in enumerate(parsed)
        ]
        return SubscriptionConfigs(short_uuid=short_uuid, configs=configs, rejected=rejected)

    async def _resolve_one(self, item: dict, configs_cache: dict[str, SubscriptionConfigs]) -> Target:
        kind = item.get('kind')
        if kind == KIND_HOST:
            host = next((h for h in await self._all_hosts() if h.uuid == item.get('ref')), None)
            if host is None:
                raise TargetResolutionError(f'Хост {item.get("ref")} не найден в панели')
            nodes = await self._all_nodes()
            return self._host_view(host, nodes).target
        if kind == KIND_NODE:
            node = next((n for n in await self._all_nodes() if n.uuid == item.get('ref')), None)
            if node is None:
                raise TargetResolutionError(f'Нода {item.get("ref")} не найдена в панели')
            return target_from_node(node)
        if kind == KIND_SUBSCRIPTION_CONFIG:
            short_uuid = str(item.get('short_uuid') or '')
            if short_uuid not in configs_cache:
                configs_cache[short_uuid] = await self.subscription_configs(short_uuid)
            configs = configs_cache[short_uuid].configs
            index = item.get('index')
            if not isinstance(index, int) or not 0 <= index < len(configs):
                raise TargetResolutionError(f'В подписке {short_uuid} нет конфига №{index}')
            return configs[index]
        if kind == KIND_CUSTOM:
            value = str(item.get('value') or '')
            if _is_config_link(value):
                parsed, rejected = parse_links(value)
                if not parsed:
                    raise TargetResolutionError(rejected[0].reason if rejected else 'Ссылка не разобрана')
                return target_from_link(parsed[0], KIND_CUSTOM, {})
            return normalize_custom_target(value)
        if kind == KIND_CIDR:
            return target_from_cidr(str(item.get('value') or ''))
        raise TargetResolutionError(f'Неизвестный тип цели «{kind}»')

    async def resolve(self, items: list[dict]) -> list[Target]:
        configs_cache: dict[str, SubscriptionConfigs] = {}
        seen: set[str] = set()
        targets: list[Target] = []
        for item in items:
            target = await self._resolve_one(item, configs_cache)
            if target.target_key in seen:
                continue
            seen.add(target.target_key)
            targets.append(target)
        return targets
```

- [ ] **Step 4: Прогнать** — PASS.

- [ ] **Step 5: Коммит**

```bash
uv run ruff format app/services/reachability/resolver.py tests/services/reachability/test_resolver.py && uv run ruff check app/services/reachability/resolver.py tests/services/reachability/test_resolver.py
git checkout uv.lock 2>/dev/null
git add app/services/reachability/resolver.py tests/services/reachability/test_resolver.py
git commit -m "feat(reachability): разрешение целей из хостов, нод, подписок и ввода"
```

---

### Task 12: Леги из ответов и слияние пропусков

**Files:**
- Create: `app/services/reachability/legs.py`
- Test: `tests/services/reachability/test_legs.py`

**Interfaces:**
- Consumes: `Target`, `probe_api_target`, `is_reality_like`, `PURPOSE_BS` (`targets.py`); `probe_leg_verdict`, `vless_leg_verdict`, `matches_expectation` (`verdict.py`).
- Produces:
  - `build_probe_legs(targets: list[dict], request: dict, result: dict, *, checked_at: datetime) -> list[dict]` — словари под `ReachabilityLeg(**leg)`.
  - `build_vless_legs(targets: list[dict], legs_raw: list[dict], *, checked_at: datetime) -> list[dict]`.
  - `merge_skipped(existing: dict | None, response: dict) -> dict` — ключи `dpi_off`, `unavailable`, `unknown`, `blocked_targets`; всегда новый словарь.
  - `vless_op_key(leg: dict) -> str` — `оператор|округ|on/off` из лега VLESS (у него нет `op_key`).

- [ ] **Step 1: Падающий тест**

```python
"""Ответы API раскладываются в леги с вердиктом и ожиданием; пропуски сливаются без мутаций."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.reachability.legs import build_probe_legs, build_vless_legs, merge_skipped, vless_op_key
from app.services.reachability.targets import Target
from tests.fixtures.bschek_fixtures import load_bschek_fixture


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
BS = Target(kind='host', label='BS', address='bs-host.example', port=9443, target_key='bs-host.example:9443',
            sni='whitelisted.example', ref={'host_uuid': 'h-bs'}, purpose='bs').as_dict()
EU = Target(kind='host', label='EU', address='eu-host.example', port=None, target_key='eu-host.example',
            sni='eu-host.example', ref={'host_uuid': 'h-eu'}, purpose='regular').as_dict()


def test_probe_legs_from_recorded_full_response() -> None:
    fx = load_bschek_fixture('p2_replay')
    legs = build_probe_legs([EU, BS], fx['request'], fx['body'], checked_at=NOW)
    assert len(legs) == 10
    by = {(leg['target_key'], leg['op_key']): leg for leg in legs}
    tele2_bs = by[('bs-host.example:9443', 'tele2|цфо|on')]
    assert (tele2_bs['verdict'], tele2_bs['matches_expectation'], tele2_bs['target_ref'], tele2_bs['dpi']) == ('reachable', True, 'h-bs', 'on')
    tmobile_bs = by[('bs-host.example:9443', 't-mobile|цфо|on')]
    assert (tmobile_bs['verdict'], tmobile_bs['matches_expectation']) == ('down', False)
    eu_tele2 = by[('eu-host.example', 'tele2|цфо|on')]
    assert (eu_tele2['verdict'], eu_tele2['matches_expectation']) == ('blocked', None)  # обычный хост под БС — справка
    assert all(leg['checked_at'] == NOW and leg['kind'] == 'probe' and leg['raw'] for leg in legs)


def test_probe_legs_for_unknown_target_fall_back_to_api_key() -> None:
    fx = load_bschek_fixture('p1_probe')
    legs = build_probe_legs([], fx['request'], fx['body'], checked_at=NOW)
    assert legs[0]['target_key'] == 'eu-host.example' and legs[0]['target_kind'] == 'custom'


def test_vless_legs_match_by_server_addr_and_compose_op_key() -> None:
    fx = load_bschek_fixture('v1_poll_12')
    legs = build_vless_legs([BS], fx['body']['result'], checked_at=NOW)
    assert [leg['op_key'] for leg in legs] == ['tele2|цфо|on', 'dobro|цфо|on']
    assert all(leg['verdict'] == 'reachable' and leg['matches_expectation'] is True and leg['target_ref'] == 'h-bs' for leg in legs)


def test_vless_op_key_from_leg_fields() -> None:
    assert vless_op_key({'operator': 'mts', 'region': 'ЦФО', 'channel_state': 'DPI_OFF'}) == 'mts|цфо|off'
    assert vless_op_key({'operator': 'mts', 'region': 'ЦФО', 'channel_state': 'DOWN'}) == 'mts|цфо|?'


def test_merge_skipped_keeps_ours_and_adds_api_lists_without_mutation() -> None:
    ours = {'dpi_off': [{'op_key': 'a'}], 'unavailable': [], 'unknown': ['x'], 'blocked_targets': []}
    response = {'skipped_dpi_off': [{'op_key': 'b'}], 'skipped_unavailable': [{'op_key': 'c'}], 'skipped': [{'target': '10.0.0.1'}]}
    merged = merge_skipped(ours, response)
    assert merged == {
        'dpi_off': [{'op_key': 'a'}, {'op_key': 'b'}],
        'unavailable': [{'op_key': 'c'}],
        'unknown': ['x'],
        'blocked_targets': [{'target': '10.0.0.1'}],
    }
    assert ours['dpi_off'] == [{'op_key': 'a'}]
    assert merge_skipped(None, {}) == {'dpi_off': [], 'unavailable': [], 'unknown': [], 'blocked_targets': []}
```

- [ ] **Step 2: Падает.** **Step 3: Реализация** `app/services/reachability/legs.py`

```python
"""Раскладка ответов API в леги (цель × симка) и слияние пропусков."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.reachability.targets import KIND_CUSTOM, PURPOSE_BS, PURPOSE_UNKNOWN, Target, is_reality_like, probe_api_target
from app.services.reachability.verdict import matches_expectation, probe_leg_verdict, vless_leg_verdict


def _ref(target: Target | None) -> str | None:
    if target is None:
        return None
    return target.ref.get('host_uuid') or target.ref.get('node_uuid') or target.ref.get('short_uuid')


def _leg(kind: str, target: Target | None, fallback_key: str, op_key: str, raw: dict, verdict: str, dpi: str | None, checked_at: datetime) -> dict:
    purpose = target.purpose if target else PURPOSE_UNKNOWN
    return {
        'kind': kind,
        'target_key': target.target_key if target else fallback_key.lower(),
        'target_kind': target.kind if target else KIND_CUSTOM,
        'target_ref': _ref(target),
        'op_key': op_key,
        'operator': raw.get('operator'),
        'region': raw.get('region'),
        'dpi': dpi,
        'verdict': verdict,
        'matches_expectation': matches_expectation(verdict, purpose, dpi or ''),
        'raw': raw,
        'checked_at': checked_at,
    }


def build_probe_legs(targets: list[dict], request: dict, result: dict, *, checked_at: datetime) -> list[dict]:
    parsed = [Target.from_dict(t) for t in targets]
    by_api_target = {probe_api_target(t): t for t in parsed}
    sni_requested = bool((request.get('probes') or {}).get('sni'))
    legs: list[dict] = []
    for api_target, payload in (result.get('by_target') or {}).items():
        target = by_api_target.get(api_target) or by_api_target.get(api_target.lower())
        reality = target is not None and (target.purpose == PURPOSE_BS or is_reality_like(target.address, target.sni))
        for op_key, raw in (payload.get('by_operator') or {}).items():
            verdict = probe_leg_verdict(raw, sni_host=target.sni if (target and sni_requested) else None, reality=reality)
            legs.append(_leg('probe', target, api_target, op_key, raw, verdict, raw.get('dpi'), checked_at))
    return legs


def vless_op_key(leg: dict) -> str:
    dpi = {'DPI_ON': 'on', 'DPI_OFF': 'off'}.get(str(leg.get('channel_state') or ''), '?')
    return f'{leg.get("operator") or "?"}|{str(leg.get("region") or "?").lower()}|{dpi}'


def build_vless_legs(targets: list[dict], legs_raw: list[dict], *, checked_at: datetime) -> list[dict]:
    parsed = [Target.from_dict(t) for t in targets]
    by_key = {t.target_key: t for t in parsed}
    by_label = {t.label: t for t in parsed}
    legs: list[dict] = []
    for raw in legs_raw:
        target = by_key.get(str(raw.get('server_addr') or '').lower()) or by_label.get(str(raw.get('server_name') or ''))
        op_key = vless_op_key(raw)
        dpi = op_key.rsplit('|', 1)[1]
        legs.append(_leg('vless', target, str(raw.get('server_addr') or raw.get('server_name') or ''), op_key, raw,
                         vless_leg_verdict(raw), dpi if dpi in ('on', 'off') else None, checked_at))
    return legs


def merge_skipped(existing: dict | None, response: dict[str, Any]) -> dict:
    base = existing or {}
    return {
        'dpi_off': [*base.get('dpi_off', []), *(response.get('skipped_dpi_off') or [])],
        'unavailable': [*base.get('unavailable', []), *(response.get('skipped_unavailable') or [])],
        'unknown': list(base.get('unknown', [])),
        'blocked_targets': [*base.get('blocked_targets', []), *(response.get('skipped') or [])],
    }
```

- [ ] **Step 4: Прогнать** — PASS. Если в `p2_replay` целей две и 5 симок, легов 10 — иначе поправить ожидание под фикстуру.

- [ ] **Step 5: Коммит**

```bash
uv run ruff format app/services/reachability/legs.py tests/services/reachability/test_legs.py && uv run ruff check app/services/reachability/legs.py tests/services/reachability/test_legs.py
git checkout uv.lock 2>/dev/null
git add app/services/reachability/legs.py tests/services/reachability/test_legs.py
git commit -m "feat(reachability): раскладка ответов в леги и слияние пропусков"
```

---

### Task 13: Сервис задач (runner)

**Files:**
- Create: `app/services/reachability/jobs.py`
- Create: `tests/services/reachability/conftest.py`
- Test: `tests/services/reachability/test_jobs.py`

**Interfaces:**
- Consumes: `BschekAPI`, `BschekAPIError`, `BschekGatewayError`; `PaidCallGate`; CRUD `app/database/crud/reachability`; `build_probe_legs`, `build_vless_legs`, `merge_skipped`; `credits_to_kopeks`, `format_rubles`.
- Produces (в `jobs.py`):
  - константы `KIND_PROBE='probe'`, `KIND_VLESS='vless'`, `KIND_SCAN='scan'`; `STATUS_PENDING/RUNNING/DONE/FAILED/CANCELLED`; `PHASE_SUBMITTING='submitting'`, `PHASE_WAITING='waiting'`, `PHASE_RETRIEVING='retrieving'`, `PHASE_POLLING='polling'`, `PHASE_CANCELLING='cancelling'`.
  - `TRANSIENT_CODES`, `BUSY_CODES` (frozenset).
  - `@dataclass RunnerConfig` (поля и умолчания — ниже в коде).
  - `class JobNotCancellable(Exception)`.
  - `class JobRunner(*, client_factory: Callable[[], BschekAPI], gate: PaidCallGate, session_factory, cost_limit_kopeks: Callable[[], int], config: RunnerConfig | None = None, sleep=asyncio.sleep, clock=time.monotonic, now=lambda: datetime.now(UTC))`:
    `spawn(job_id) -> asyncio.Task`, `is_active(job_id) -> bool`, `async run(job_id)`, `async resume(job_id)`, `async cancel(db, job) -> ReachabilityJob` (только помечает `cancelling` и дёргает API; финал ставит поллер/обходчик), `async sweep()`, `async sweeper_loop()`, `stop()`.

- [ ] **Step 1: Общие фейки** `tests/services/reachability/conftest.py`

```python
"""Фейки для сервиса задач: часы, клиент API по сценарию, сессии на SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, ReachabilityJob, ReachabilityLeg, ReachabilityTargetPref, User
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeAPI:
    """Ответы по сценарию: dict возвращается, Exception поднимается. Считает вызовы."""

    def __init__(self, script: dict[str, list[Any]] | None = None) -> None:
        self.script = {name: list(items) for name, items in (script or {}).items()}
        self.calls: list[tuple[str, tuple]] = []

    async def __aenter__(self) -> FakeAPI:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def _next(self, name: str, *args: Any) -> Any:
        self.calls.append((name, args))
        queue = self.script.get(name) or []
        if not queue:
            raise AssertionError(f'FakeAPI: нет ответа для {name}{args}')
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def probe(self, body, key):
        return self._next('probe', key)

    async def start_vless(self, body, key):
        return self._next('start_vless', key)

    async def get_vless(self, test_id):
        return self._next('get_vless', test_id)

    async def cancel_vless(self, test_id):
        return self._next('cancel_vless', test_id)

    async def start_scan(self, body, key):
        return self._next('start_scan', key)

    async def get_scan(self, scan_id):
        return self._next('get_scan', scan_id)

    async def cancel_scan(self, scan_id):
        return self._next('cancel_scan', scan_id)


@pytest.fixture
async def session_factory(monkeypatch) -> AsyncIterator[async_sessionmaker]:
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    tables = [User.__table__, ReachabilityJob.__table__, ReachabilityLeg.__table__, ReachabilityTargetPref.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield maker
    finally:
        await engine.dispose()
```

Замечание: `:memory:` у aiosqlite — одна БД на соединение; `create_async_engine` с `:memory:` по умолчанию использует `StaticPool`/`SingletonThreadPool`, поэтому все сессии из одного `maker` видят те же таблицы. Если тесты увидят «no such table» — передать `poolclass=StaticPool` и `connect_args={'check_same_thread': False}`.

- [ ] **Step 2: Падающие тесты** `tests/services/reachability/test_jobs.py`

```python
"""Жизненный цикл задач на фейковом API: 524 → повтор ключом → 200; VLESS/скан с опросом;
потолок цены; занятость; отмена; таймаут опроса и обходчик."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.database.crud import reachability as crud
from app.database.models import User
from app.external.bschek_api import BschekAPIError, BschekGatewayError
from app.services.reachability.gate import PaidCallGate
from app.services.reachability.jobs import (
    KIND_PROBE,
    KIND_SCAN,
    KIND_VLESS,
    PHASE_CANCELLING,
    PHASE_RETRIEVING,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    JobNotCancellable,
    JobRunner,
    RunnerConfig,
)
from app.services.reachability.targets import Target
from tests.fixtures.bschek_fixtures import load_bschek_fixture
from tests.services.reachability.conftest import FakeAPI, FakeClock


def body(name: str) -> dict:
    return load_bschek_fixture(name)['body']


BS = Target(kind='host', label='BS', address='bs-host.example', port=9443, target_key='bs-host.example:9443',
            sni='whitelisted.example', ref={'host_uuid': 'h-bs'}, purpose='bs').as_dict()
EU = Target(kind='host', label='EU', address='eu-host.example', port=None, target_key='eu-host.example',
            sni='eu-host.example', ref={'host_uuid': 'h-eu'}, purpose='regular').as_dict()


async def make_job(session_factory, kind: str, request: dict, targets: list[dict], units: list[str], **extra) -> int:
    async with session_factory() as db:
        admin = User(telegram_id=1, username='admin', first_name='A', language='ru')
        db.add(admin)
        await db.flush()
        fields = dict(
            kind=kind, status='pending', trigger='manual', started_by_user_id=admin.id,
            idempotency_key=f'key-{kind}-{datetime.now(UTC).timestamp()}', request=request, targets=targets,
            units_requested=units, units_resolved=units, dpi=request.get('dpi', 'on'), estimated_kopeks=100,
            estimate_is_exact=True,
        )
        fields.update(extra)
        job = await crud.create_job(db, **fields)
        await db.commit()
        return job.id


def make_runner(session_factory, api: FakeAPI, clock: FakeClock, *, cost_limit: int = 0, config: RunnerConfig | None = None) -> JobRunner:
    gate = PaidCallGate(min_interval=0, clock=clock, sleep=clock.sleep)
    return JobRunner(client_factory=lambda: api, gate=gate, session_factory=session_factory,
                     cost_limit_kopeks=lambda: cost_limit, config=config, sleep=clock.sleep, clock=clock)


async def load(session_factory, job_id: int):
    async with session_factory() as db:
        return await crud.get_job(db, job_id)


# ---------------------------------------------------------------- probe

async def test_probe_happy_path_stores_result_cost_and_legs(session_factory) -> None:
    fx = load_bschek_fixture('p1_probe')
    api = FakeAPI({'probe': [fx['body']]})
    job_id = await make_job(session_factory, KIND_PROBE, fx['request'], [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.phase, job.cost_kopeks, job.refunded_kopeks) == (STATUS_DONE, None, 18, 0)
    assert job.units_effective == ['mts|пфо|on']
    assert [leg.verdict for leg in job.legs] == ['down']
    assert job.result['response']['outcome'] == 'done'
    assert job.attempts == 1


async def test_probe_gateway_timeout_then_in_progress_then_result(session_factory) -> None:
    fx = load_bschek_fixture('p2_replay')
    api = FakeAPI({'probe': [
        BschekGatewayError(code='http_524', message='cf', status=524, retryable=True),
        BschekAPIError(code='request_in_progress', message='wait', status=409),
        fx['body'],
    ]})
    clock = FakeClock()
    job_id = await make_job(session_factory, KIND_PROBE, fx['request'], [EU, BS], ['*|цфо|on'])
    await make_runner(session_factory, api, clock).run(job_id)

    job = await load(session_factory, job_id)
    assert job.status == STATUS_DONE and job.cost_kopeks == 260 and len(job.legs) == 10
    keys = [call[1][0] for call in api.calls if call[0] == 'probe']
    assert len(set(keys)) == 1  # все повторы — тем же ключом
    assert clock.sleeps[:2] == [15.0, 15.0]


async def test_probe_left_retrieving_when_result_never_comes(session_factory) -> None:
    api = FakeAPI({'probe': [BschekAPIError(code='request_in_progress', message='wait', status=409)]})
    clock = FakeClock()
    cfg = RunnerConfig(probe_retrieve_max=60.0, probe_retrieve_fast_interval=15.0)
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, clock, config=cfg).run(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.phase) == (STATUS_RUNNING, PHASE_RETRIEVING)


async def test_probe_no_dpi_on_race_fails_without_charge(session_factory) -> None:
    api = FakeAPI({'probe': [{'outcome': 'no_dpi_on', 'skipped_dpi_off': [{'operator': 'yota', 'name': 'Yota'}]}]})
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['yota|уфо|off'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.cost_kopeks, job.retryable) == (STATUS_FAILED, 'no_dpi_on', None, False)


async def test_probe_validation_error_fails_with_api_message(session_factory) -> None:
    api = FakeAPI({'probe': [BschekAPIError(code='no_probes', message='Не выбрано ни одной пробы', status=400)]})
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.error_message) == (STATUS_FAILED, 'no_probes', 'Не выбрано ни одной пробы')


async def test_probe_transient_503_is_retried_with_same_key(session_factory) -> None:
    fx = load_bschek_fixture('p1_probe')
    api = FakeAPI({'probe': [
        BschekAPIError(code='worker_unavailable', message='later', status=503, retryable=True, retry_after=5.0),
        fx['body'],
    ]})
    clock = FakeClock()
    job_id = await make_job(session_factory, KIND_PROBE, fx['request'], [EU], ['mts|пфо|on'])
    await make_runner(session_factory, api, clock).run(job_id)
    assert (await load(session_factory, job_id)).status == STATUS_DONE
    assert 5.0 in clock.sleeps


# ---------------------------------------------------------------- vless

async def test_vless_happy_path(session_factory) -> None:
    api = FakeAPI({'start_vless': [body('v1_submit')], 'get_vless': [body('v1_poll_00'), body('v1_poll_12')]})
    job_id = await make_job(session_factory, KIND_VLESS, load_bschek_fixture('v1_submit')['request'], [BS], ['tele2|цфо|on', 'dobro|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.external_id, job.cost_kopeks, job.estimate_is_exact) == (STATUS_DONE, 43300, 206, True)
    assert [leg.verdict for leg in job.legs] == ['reachable', 'reachable']
    assert job.result['submit']['test_id'] == 43300 and job.result['status']['state'] == 'done'


async def test_vless_over_cost_limit_is_cancelled_right_after_submit(session_factory) -> None:
    api = FakeAPI({'start_vless': [body('v1_submit')], 'cancel_vless': [body('v2_cancel')]})
    job_id = await make_job(session_factory, KIND_VLESS, {'raw_input': 'x'}, [BS], ['tele2|цфо|on'])
    await make_runner(session_factory, api, FakeClock(), cost_limit=100).run(job_id)

    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.external_id) == (STATUS_FAILED, 'cost_limit_exceeded', 43300)
    assert [c[0] for c in api.calls] == ['start_vless', 'cancel_vless']


async def test_vless_busy_fails_fast_and_retryable(session_factory) -> None:
    api = FakeAPI({'start_vless': [BschekAPIError(code='test_in_progress', message='busy', status=409, retryable=True)]})
    job_id = await make_job(session_factory, KIND_VLESS, {'raw_input': 'x'}, [BS], ['tele2|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.retryable, job.external_id) == (STATUS_FAILED, 'test_in_progress', True, None)


async def test_vless_cancel_marks_phase_and_resume_finalizes_as_cancelled(session_factory) -> None:
    """Отмена только дёргает API и ставит фазу; финал ставит поллер/обходчик по GET."""
    api = FakeAPI({'cancel_vless': [body('vC_cancel')], 'get_vless': [body('vC_after_cancel')]})
    runner = make_runner(session_factory, api, FakeClock())
    job_id = await make_job(
        session_factory, KIND_VLESS, {'raw_input': 'x'}, [EU], ['dobro|цфо|on'],
        status=STATUS_RUNNING, external_id=43306, result={'submit': body('vC_submit')}, cost_kopeks=206,
    )

    async with session_factory() as db:
        job = await crud.get_job(db, job_id)
        await runner.cancel(db, job)
        await db.commit()
        assert job.phase == PHASE_CANCELLING
    assert [call[0] for call in api.calls] == ['cancel_vless']

    await runner.resume(job_id)

    job = await load(session_factory, job_id)
    assert job.status == STATUS_CANCELLED
    assert [leg.verdict for leg in job.legs] == ['cancelled']
    assert job.cost_kopeks == 0 and job.estimate_is_exact is False


async def test_cancel_rejects_probe_and_finished_jobs(session_factory) -> None:
    runner = make_runner(session_factory, FakeAPI(), FakeClock())
    job_id = await make_job(session_factory, KIND_PROBE, {'target': 'x'}, [EU], ['mts|пфо|on'])
    async with session_factory() as db:
        job = await crud.get_job(db, job_id)
        with pytest.raises(JobNotCancellable):
            await runner.cancel(db, job)
        await crud.update_job(db, job, status=STATUS_DONE)
        with pytest.raises(JobNotCancellable):
            await runner.cancel(db, job)


# ---------------------------------------------------------------- scan

async def test_scan_happy_path(session_factory) -> None:
    api = FakeAPI({'start_scan': [body('s1_submit')], 'get_scan': [body('s1_poll_00'), body('s1_poll_03')]})
    job_id = await make_job(session_factory, KIND_SCAN, load_bschek_fixture('s1_submit')['request'], [{'kind': 'cidr', 'target_key': '192.0.2.0/24'}], ['dobro|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.external_id, job.cost_kopeks, job.units_effective) == (STATUS_DONE, 5355, 61, ['dobro|цфо|on'])
    assert job.result['status']['result']['up_n'] == 0 and job.legs == []


async def test_scan_cancelled_state_from_get(session_factory) -> None:
    api = FakeAPI({'start_scan': [body('sB_submit')], 'get_scan': [body('sB_after_0')]})
    job_id = await make_job(session_factory, KIND_SCAN, {'cidr': '192.0.2.0/24'}, [{'kind': 'cidr', 'target_key': '192.0.2.0/24'}], ['*|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.cost_kopeks) == (STATUS_CANCELLED, 0)


async def test_scan_failed_state_propagates_error_and_retryable(session_factory) -> None:
    api = FakeAPI({'start_scan': [body('s1_submit')],
                   'get_scan': [{'scan_id': 5355, 'state': 'failed', 'result_ready': False, 'error': 'lte_unavailable', 'retryable': True}]})
    job_id = await make_job(session_factory, KIND_SCAN, {'cidr': '192.0.2.0/24'}, [{'kind': 'cidr', 'target_key': '192.0.2.0/24'}], ['dobro|цфо|on'])
    await make_runner(session_factory, api, FakeClock()).run(job_id)
    job = await load(session_factory, job_id)
    assert (job.status, job.error_code, job.retryable) == (STATUS_FAILED, 'lte_unavailable', True)


# ---------------------------------------------------------------- timeout + sweeper

async def test_poll_timeout_leaves_job_running_and_sweep_resumes_it(session_factory) -> None:
    running = body('s1_poll_00')
    api = FakeAPI({'start_scan': [body('s1_submit')], 'get_scan': [running, running, body('s1_poll_03')]})
    clock = FakeClock()
    cfg = RunnerConfig(scan_poll_interval=4.0, scan_timeout_base=5.0, scan_timeout_per_unit=0.0, sweep_min_age_sec=0.0)
    runner = make_runner(session_factory, api, clock, config=cfg)
    job_id = await make_job(session_factory, KIND_SCAN, {'cidr': '192.0.2.0/24'}, [{'kind': 'cidr', 'target_key': '192.0.2.0/24'}], ['dobro|цфо|on'])
    await runner.run(job_id)
    assert (await load(session_factory, job_id)).status == STATUS_RUNNING

    await runner.sweep()
    for task in list(runner._tasks.values()):
        await task
    assert (await load(session_factory, job_id)).status == STATUS_DONE
```

- [ ] **Step 3: Убедиться, что падают** — `uv run pytest tests/services/reachability/test_jobs.py -q`.

- [ ] **Step 4: Реализация** `app/services/reachability/jobs.py`

```python
"""Сервис задач «Доступность из РФ»: фон, повторы тем же ключом, опрос, отмена, обходчик.

Состояния: pending → running(phase) → done | failed | cancelled. Фазы running:
submitting → waiting (probe идёт) / polling (VLESS, скан) / retrieving (probe оборвался,
забираем результат повтором ключа) / cancelling. Любой повтор к API — только с
``job.idempotency_key`` и ``job.request`` как есть.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from app.database.crud import reachability as crud
from app.database.models import ReachabilityJob
from app.external.bschek_api import BschekAPI, BschekAPIError, BschekGatewayError
from app.services.reachability.gate import PaidCallGate
from app.services.reachability.legs import build_probe_legs, build_vless_legs, merge_skipped
from app.services.reachability.pricing import credits_to_kopeks, format_rubles


logger = structlog.get_logger(__name__)

KIND_PROBE, KIND_VLESS, KIND_SCAN = 'probe', 'vless', 'scan'
STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED = crud.JOB_STATUSES
PHASE_SUBMITTING, PHASE_WAITING, PHASE_RETRIEVING, PHASE_POLLING, PHASE_CANCELLING = (
    'submitting', 'waiting', 'retrieving', 'polling', 'cancelling',
)

TRANSIENT_CODES = frozenset({'worker_unavailable', 'scanner_unavailable', 'lte_unavailable', 'maintenance', 'bot_not_ready', 'no_alive_modems'})
BUSY_CODES = frozenset({'test_in_progress', 'scan_in_progress', 'busy', 'too_many_active'})
CANCEL_OK_CODES = frozenset({'cannot_cancel_running', 'not_running', 'not_found'})


@dataclass
class RunnerConfig:
    probe_retrieve_fast_interval: float = 15.0
    probe_retrieve_fast_window: float = 120.0
    probe_retrieve_slow_interval: float = 30.0
    probe_retrieve_max: float = 1200.0
    vless_poll_interval: float = 5.0
    vless_timeout_base: float = 300.0
    vless_timeout_per_leg: float = 180.0
    vless_timeout_cap: float = 2700.0
    scan_poll_interval: float = 4.0
    scan_timeout_base: float = 180.0
    scan_timeout_per_unit: float = 60.0
    scan_timeout_cap: float = 2400.0
    transient_retries: int = 3
    transient_default_wait: float = 60.0
    internal_error_replay_wait: float = 60.0
    cancel_confirm_wait: float = 2.0
    sweep_interval: float = 60.0
    sweep_min_age_sec: float = 30.0


class JobNotCancellable(Exception):
    """Отменять нечего: синхронная проба, уже завершённая или ещё не отправленная задача."""


def _cancelled(leg: dict) -> bool:
    return bool(leg.get('cancelled')) or leg.get('stage') == 'cancelled'


class JobRunner:
    def __init__(
        self,
        *,
        client_factory: Callable[[], BschekAPI],
        gate: PaidCallGate,
        session_factory: Callable[[], Any],
        cost_limit_kopeks: Callable[[], int],
        config: RunnerConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client_factory = client_factory
        self._gate = gate
        self._session_factory = session_factory
        self._cost_limit = cost_limit_kopeks
        self.cfg = config or RunnerConfig()
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._tasks: dict[int, asyncio.Task] = {}
        self._running = False

    # ------------------------------------------------------------ фон

    def spawn(self, job_id: int) -> asyncio.Task:
        task = asyncio.create_task(self.run(job_id))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job_id, None))
        return task

    def is_active(self, job_id: int) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    async def run(self, job_id: int) -> None:
        async with self._session_factory() as db:
            job = await crud.get_job(db, job_id)
            if job is None or job.status in crud.TERMINAL_STATUSES:
                return
            try:
                if job.status == STATUS_PENDING:
                    await self._update(db, job, status=STATUS_RUNNING, phase=PHASE_SUBMITTING, started_at=self._now())
                if job.kind == KIND_PROBE:
                    await self._run_probe(db, job)
                else:
                    await self._run_async_kind(db, job)
            except BschekAPIError as exc:
                await self._fail(db, job, exc.code, exc.message, exc.retryable, request_id=exc.request_id)
            except Exception as exc:  # noqa: BLE001 — задача не должна умереть молча
                logger.exception('Задача проверки упала', job_id=job_id)
                await self._fail(db, job, 'internal_error', str(exc)[:500], False)

    async def resume(self, job_id: int) -> None:
        """Подхватить незавершённую задачу после таймаута опроса или перезапуска бота."""
        async with self._session_factory() as db:
            job = await crud.get_job(db, job_id)
            if job is None or job.status in crud.TERMINAL_STATUSES:
                return
            try:
                if job.kind == KIND_PROBE:
                    if job.status == STATUS_PENDING or job.phase == PHASE_SUBMITTING:
                        await self._run_probe(db, job)
                    else:
                        result = await self._retrieve_probe(db, job)
                        if result is not None:
                            await self._finish_probe(db, job, result)
                elif job.external_id is None:
                    await self._run_async_kind(db, job)
                else:
                    await self._poll(db, job)
            except BschekAPIError as exc:
                await self._fail(db, job, exc.code, exc.message, exc.retryable, request_id=exc.request_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception('Возобновление задачи упало', job_id=job_id)
                await self._fail(db, job, 'internal_error', str(exc)[:500], False)

    async def sweep(self) -> None:
        async with self._session_factory() as db:
            jobs = await crud.list_unfinished_jobs(db)
        threshold = self._now().timestamp() - self.cfg.sweep_min_age_sec
        for job in jobs:
            stamp = (job.updated_at or job.created_at)
            if self.is_active(job.id) or (stamp is not None and stamp.timestamp() > threshold):
                continue
            task = asyncio.create_task(self.resume(job.id))
            self._tasks[job.id] = task
            task.add_done_callback(lambda _t, jid=job.id: self._tasks.pop(jid, None))

    async def sweeper_loop(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001
                logger.exception('Обходчик задач проверки упал на итерации')
            await self._sleep(self.cfg.sweep_interval)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------ общие шаги

    async def _update(self, db, job: ReachabilityJob, **fields: Any) -> None:
        await crud.update_job(db, job, **fields)
        await db.commit()

    async def _fail(self, db, job, code: str, message: str, retryable: bool | None, *, request_id: str | None = None, result: dict | None = None) -> None:
        fields: dict[str, Any] = dict(status=STATUS_FAILED, phase=None, error_code=code, error_message=message,
                                      retryable=retryable, finished_at=self._now())
        if request_id:
            fields['last_request_id'] = request_id
        if result is not None:
            fields['result'] = result
        await self._update(db, job, **fields)

    async def _paid(self, db, job, fn: Callable[[BschekAPI], Awaitable[dict]], *, retry_gateway: bool) -> dict:
        attempt = 0
        while True:
            attempt += 1
            await self._update(db, job, attempts=(job.attempts or 0) + 1)
            try:
                async with self._client_factory() as api:
                    return await self._gate.run(lambda: fn(api))
            except BschekGatewayError:
                if retry_gateway and attempt <= self.cfg.transient_retries:
                    await self._sleep(self.cfg.transient_default_wait)
                    continue
                raise
            except BschekAPIError as exc:
                if exc.code in TRANSIENT_CODES and attempt <= self.cfg.transient_retries:
                    await self._sleep(exc.retry_after or self.cfg.transient_default_wait)
                    continue
                if exc.status == 500 and attempt == 1:
                    await self._sleep(self.cfg.internal_error_replay_wait)
                    continue
                raise

    # ------------------------------------------------------------ probe

    async def _run_probe(self, db, job) -> None:
        await self._update(db, job, phase=PHASE_WAITING)
        try:
            result = await self._paid(db, job, lambda api: api.probe(job.request, job.idempotency_key), retry_gateway=False)
        except BschekGatewayError:
            result = await self._retrieve_probe(db, job)
        except BschekAPIError as exc:
            if exc.code != 'request_in_progress':
                raise
            result = await self._retrieve_probe(db, job)
        if result is not None:
            await self._finish_probe(db, job, result)

    async def _retrieve_probe(self, db, job) -> dict | None:
        await self._update(db, job, phase=PHASE_RETRIEVING)
        started = self._clock()
        while self._clock() - started < self.cfg.probe_retrieve_max:
            elapsed = self._clock() - started
            fast = elapsed < self.cfg.probe_retrieve_fast_window
            await self._sleep(self.cfg.probe_retrieve_fast_interval if fast else self.cfg.probe_retrieve_slow_interval)
            try:
                async with self._client_factory() as api:
                    return await self._gate.run(lambda: api.probe(job.request, job.idempotency_key))
            except BschekGatewayError:
                continue
            except BschekAPIError as exc:
                if exc.code == 'request_in_progress':
                    continue
                raise
        return None

    async def _finish_probe(self, db, job, result: dict) -> None:
        if result.get('outcome') == 'no_dpi_on':
            await self._fail(db, job, 'no_dpi_on', 'Под фильтр Белого списка не попала ни одна симка', False, result={'response': result})
            return
        legs = build_probe_legs(job.targets or [], job.request or {}, result, checked_at=self._now())
        await crud.replace_legs(db, job.id, legs)
        await self._update(
            db, job, status=STATUS_DONE, phase=None, result={'response': result},
            cost_kopeks=credits_to_kopeks(result.get('cost_credits')), refunded_kopeks=credits_to_kopeks(result.get('refunded')),
            units_effective=list(result.get('operators') or []), skipped=merge_skipped(job.skipped, result), finished_at=self._now(),
        )

    # ------------------------------------------------------------ vless / scan

    async def _run_async_kind(self, db, job) -> None:
        is_vless = job.kind == KIND_VLESS
        submit = await self._paid(
            db, job,
            lambda api: api.start_vless(job.request, job.idempotency_key) if is_vless else api.start_scan(job.request, job.idempotency_key),
            retry_gateway=True,
        )
        if submit.get('outcome') == 'no_dpi_on':
            await self._fail(db, job, 'no_dpi_on', 'Под фильтр Белого списка не попала ни одна симка', False, result={'submit': submit})
            return
        external_id = submit.get('test_id' if is_vless else 'scan_id')
        if external_id is None:
            await self._fail(db, job, 'unexpected_response', 'API не вернул идентификатор задачи', False, result={'submit': submit})
            return
        fields: dict[str, Any] = dict(external_id=int(external_id), phase=PHASE_POLLING, result={'submit': submit},
                                      skipped=merge_skipped(job.skipped, submit))
        if is_vless:
            cost = credits_to_kopeks(submit.get('cost_credits'))
            fields.update(cost_kopeks=cost, estimated_kopeks=cost, estimate_is_exact=True, units_effective=list(job.units_resolved or []))
            limit = self._cost_limit()
            if limit and cost and cost > limit:
                await self._update(db, job, **fields)
                await self._try_cancel_remote(job.kind, int(external_id))
                await self._fail(db, job, 'cost_limit_exceeded',
                                 f'Цена задачи {format_rubles(cost)} выше потолка {format_rubles(limit)}; тест отменён, списания нет', False)
                return
        else:
            fields['units_effective'] = [unit.get('op_key') for unit in submit.get('units') or []]
        await self._update(db, job, **fields)
        await self._poll(db, job)

    def _timeout_for(self, job) -> float:
        if job.kind == KIND_VLESS:
            legs = max(1, len(job.targets or [])) * max(1, len(job.units_effective or job.units_resolved or []))
            return min(self.cfg.vless_timeout_cap, self.cfg.vless_timeout_base + self.cfg.vless_timeout_per_leg * legs)
        units = max(1, len(job.units_effective or job.units_resolved or []))
        return min(self.cfg.scan_timeout_cap, self.cfg.scan_timeout_base + self.cfg.scan_timeout_per_unit * units)

    async def _poll(self, db, job) -> None:
        is_vless = job.kind == KIND_VLESS
        interval = self.cfg.vless_poll_interval if is_vless else self.cfg.scan_poll_interval
        deadline = self._clock() + self._timeout_for(job)
        while self._clock() < deadline:
            await self._sleep(interval)
            try:
                async with self._client_factory() as api:
                    status = await (api.get_vless(job.external_id) if is_vless else api.get_scan(job.external_id))
            except BschekGatewayError:
                continue
            except BschekAPIError as exc:
                if not is_vless and exc.code == 'not_found':
                    await self._fail(db, job, 'not_found', 'Скан пропал на стороне сервиса', False)
                    return
                raise
            finished = await (self._handle_vless_status(db, job, status) if is_vless else self._handle_scan_status(db, job, status))
            if finished:
                return
        logger.warning('Опрос задачи проверки исчерпал таймаут, доберёт обходчик', job_id=job.id)

    async def _handle_vless_status(self, db, job, status: dict) -> bool:
        state = status.get('state')
        if state == 'unknown':
            return False
        if state == 'not_found':
            await self._fail(db, job, 'not_found', 'Тест пропал на стороне сервиса', False)
            return True
        if not (status.get('result_ready') or state in ('done', 'cancelled')):
            return False
        legs_raw = [leg for leg in (status.get('result') or []) if isinstance(leg, dict)]
        await crud.replace_legs(db, job.id, build_vless_legs(job.targets or [], legs_raw, checked_at=self._now()))
        cancelled = state == 'cancelled' or (bool(legs_raw) and all(_cancelled(leg) for leg in legs_raw)) or (
            job.phase == PHASE_CANCELLING and not legs_raw
        )
        fields: dict[str, Any] = dict(status=STATUS_CANCELLED if cancelled else STATUS_DONE, phase=None,
                                      result={**(job.result or {}), 'status': status}, finished_at=self._now())
        if cancelled:
            submit = (job.result or {}).get('submit') or {}
            total_legs = int(submit.get('n_servers') or 0) * int(submit.get('n_modems') or 0)
            per_leg = round(job.cost_kopeks / total_legs) if job.cost_kopeks and total_legs else 0
            completed = sum(1 for leg in legs_raw if not _cancelled(leg))
            fields.update(cost_kopeks=completed * per_leg, estimate_is_exact=False)
        await self._update(db, job, **fields)
        return True

    async def _handle_scan_status(self, db, job, status: dict) -> bool:
        state = status.get('state')
        if state == 'running':
            return False
        merged_result = {**(job.result or {}), 'status': status}
        if state == 'failed':
            await self._fail(db, job, str(status.get('error') or 'scan_failed'), 'Скан завершился ошибкой на стороне сервиса',
                             bool(status.get('retryable')), result=merged_result)
            return True
        result = status.get('result') or {}
        await self._update(
            db, job, status=STATUS_CANCELLED if state == 'cancelled' else STATUS_DONE, phase=None, result=merged_result,
            cost_kopeks=credits_to_kopeks(result.get('cost_credits')), finished_at=self._now(),
        )
        return True

    # ------------------------------------------------------------ отмена

    async def _try_cancel_remote(self, kind: str, external_id: int) -> None:
        try:
            async with self._client_factory() as api:
                await (api.cancel_vless(external_id) if kind == KIND_VLESS else api.cancel_scan(external_id))
        except BschekAPIError as exc:
            if exc.code not in CANCEL_OK_CODES:
                raise

    async def cancel(self, db, job: ReachabilityJob) -> ReachabilityJob:
        if job.status not in crud.ACTIVE_STATUSES:
            raise JobNotCancellable('Задача уже завершена')
        if job.kind == KIND_PROBE:
            raise JobNotCancellable('Синхронную пробу нельзя отменить: у API нет такой операции')
        if job.external_id is None:
            raise JobNotCancellable('Задача ещё не отправлена, подождите пару секунд')
        await self._update(db, job, phase=PHASE_CANCELLING)
        await self._try_cancel_remote(job.kind, job.external_id)
        return job
```

Порядок в `_finish_probe`/`_handle_*`: сначала леги, потом статус, одним коммитом (`_update` коммитит).

- [ ] **Step 5: Прогнать** — `uv run pytest tests/services/reachability/test_jobs.py -q`. Ожидаемые подводные камни:
  - `test_vless_cancel_...`: поллер после отмены получает `vC_after_cancel` (state done, лег cancelled) → статус `cancelled`, `cost_kopeks = 0` (completed = 0). Если тест гонок нестабилен — заменить ожидание «первый get_vless уже был» на явное событие: в `FakeAPI.get_vless` выставлять `asyncio.Event`.
  - SQLite `:memory:` и несколько сессий — см. замечание в conftest.

- [ ] **Step 6: Коммит**

```bash
uv run ruff format app/services/reachability/jobs.py tests/services/reachability/conftest.py tests/services/reachability/test_jobs.py && uv run ruff check app/services/reachability/jobs.py tests/services/reachability
git checkout uv.lock 2>/dev/null
git add app/services/reachability/jobs.py tests/services/reachability/conftest.py tests/services/reachability/test_jobs.py
git commit -m "feat(reachability): сервис задач — фон, повтор ключом, опрос, отмена, обходчик"
```

---

### Task 14: Тела запросов и фасад сервиса

**Files:**
- Create: `app/services/reachability/requests.py`
- Create: `app/services/reachability/service.py`
- Test: `tests/services/reachability/test_requests.py`, `tests/services/reachability/test_service.py`

**Interfaces:**
- `requests.py`:
  - `build_probe_request(targets: list[Target], units: list[str], dpi: str, probes: dict[str, bool]) -> dict` — `{'targets': [...], 'operators': units, 'probes': {...}, 'dpi': dpi}` + `sni_hosts` (уникальные SNI целей, только при `probes['sni']`). Цели с `kind == cidr` игнорируются. Пустые probes → ошибка `RequestBuildError`.
  - `build_vless_request(targets: list[Target], units: list[str], dpi: str, core: str) -> dict` — `{'raw_input': '\n'.join(raw_link), 'selected_modems': units, 'dpi': dpi, 'core': core}`; цели без `raw_link` → `RequestBuildError`; больше 20 → `RequestBuildError`.
  - `build_scan_request(target: Target, units: list[str], dpi: str, probes: dict, sni_hosts: list[str]) -> dict`.
  - `class RequestBuildError(ValueError)`.
- `service.py`:
  - исключения `ReachabilityDisabled(Exception)` (выключено/не настроено; `.reason: str`), `ReachabilityUnhealthy(Exception)` (`.until: datetime`, `.reason`), `ReachabilityBusy(Exception)` (`.job: ReachabilityJob`), `JobNotFound(Exception)`.
  - `@dataclass PreviewResult(kind, targets: list[Target], units_resolved: list[str], skipped: dict, cost_kopeks: int | None, estimate_is_exact: bool, warnings: list[str], balance_kopeks: int | None, request: dict)`.
  - `class ReachabilityService(*, settings_obj=settings, session_factory=AsyncSessionLocal, remnawave_factory=None, runner: JobRunner | None = None, clock=time.monotonic)`:
    `client() -> BschekAPI` (бросает `ReachabilityDisabled`), `async status(db) -> dict`, `async units(*, dpi=None, operator=None, region=None) -> list[Unit]`, `async resolver(db) -> TargetResolver`, `async hosts(db, include_disabled=False) -> list[HostView]`, `async nodes(db) -> list[NodeView]`, `async subscription_configs(db, short_uuid=None, user_id=None) -> SubscriptionConfigs`, `async preview(db, payload: dict) -> PreviewResult`, `async create_job(db, payload: dict, admin_id: int) -> ReachabilityJob`, `async list_jobs(db, **filters)`, `async get_job(db, job_id) -> ReachabilityJob`, `async cancel_job(db, job_id) -> ReachabilityJob`, `async retrieve_job(db, job_id) -> ReachabilityJob`, `async summary(db, dpi='on') -> dict`, `async update_pref(db, *, target_kind, target_ref, purpose, excluded, note, admin_id)`, `start_background() -> None`, `async stop_background() -> None`, `mark_unhealthy(reason)`.
  - Модульный синглтон `reachability_service = ReachabilityService()`.
  - `payload` — словарь формы `JobCreateRequest` (Task 15): `{'kind','targets':[{kind,ref,value,short_uuid,index}], 'units':[...], 'dpi', 'probes':{icmp,tcp,sni}, 'core'}`.

- [ ] **Step 1: Падающие тесты тел запросов** `tests/services/reachability/test_requests.py`

```python
"""Тела запросов к API из целей и симок: строгий селектор проб, SNI парой, лимит 20 конфигов."""

import pytest

from app.services.reachability.requests import RequestBuildError, build_probe_request, build_scan_request, build_vless_request
from app.services.reachability.targets import Target


def _t(address: str, port: int | None, sni: str | None, raw: str | None = None, kind: str = 'host') -> Target:
    key = f'{address}:{port}' if port else address
    return Target(kind=kind, label=address, address=address, port=port, target_key=key, sni=sni, raw_link=raw)


def test_probe_request_has_targets_units_probes_and_sni_hosts() -> None:
    body = build_probe_request([_t('bs-host.example', 9443, 'whitelisted.example'), _t('eu-host.example', None, 'eu-host.example')],
                               ['mts|цфо|on'], 'on', {'icmp': False, 'tcp': True, 'sni': True})
    assert body == {'targets': ['bs-host.example:9443', 'eu-host.example'], 'operators': ['mts|цфо|on'],
                    'probes': {'icmp': False, 'tcp': True, 'sni': True}, 'dpi': 'on',
                    'sni_hosts': ['eu-host.example', 'whitelisted.example']}


def test_probe_request_without_sni_omits_sni_hosts_and_skips_cidr_targets() -> None:
    body = build_probe_request([_t('eu-host.example', None, 'eu-host.example'), _t('192.0.2.0', None, None, kind='cidr')],
                               [], 'off', {'icmp': True, 'tcp': True, 'sni': False})
    assert body['targets'] == ['eu-host.example'] and 'sni_hosts' not in body and body['operators'] == []


def test_probe_request_rejects_no_probes_and_no_targets() -> None:
    with pytest.raises(RequestBuildError):
        build_probe_request([_t('a.example', None, None)], [], 'on', {'icmp': False, 'tcp': False, 'sni': False})
    with pytest.raises(RequestBuildError):
        build_probe_request([], [], 'on', {'tcp': True})


def test_vless_request_joins_raw_links_and_limits_20() -> None:
    links = [_t(f's{i}.example', 443, None, raw=f'vless://u@s{i}.example:443#s{i}', kind='subscription_config') for i in range(3)]
    body = build_vless_request(links, ['mts|*|off'], 'any', 'stable')
    assert body == {'raw_input': '\n'.join(t.raw_link for t in links), 'selected_modems': ['mts|*|off'], 'dpi': 'any', 'core': 'stable'}
    with pytest.raises(RequestBuildError):
        build_vless_request(links * 7, [], 'on', '')
    with pytest.raises(RequestBuildError):
        build_vless_request([_t('a.example', 443, None)], [], 'on', '')


def test_scan_request() -> None:
    body = build_scan_request(_t('192.0.2.0', None, None, kind='cidr'), ['dobro|цфо|on'], 'on', {'icmp': True, 'tcp': True, 'sni': False}, [])
    assert body == {'cidr': '192.0.2.0', 'operators': ['dobro|цфо|on'], 'probes': {'icmp': True, 'tcp': True, 'sni': False}, 'dpi': 'on'}
```

`_t` для cidr передаёт `target_key='192.0.2.0'` — в реальности это `192.0.2.0/24`; `build_scan_request` берёт `target.target_key`, тест это и проверяет.

- [ ] **Step 2: Реализация** `app/services/reachability/requests.py`

```python
"""Тела запросов bschekbot из целей и раскрытых симок. Правила — строгий селектор проб,
SNI только парой probes.sni + sni_hosts, не больше 20 конфигов на VLESS-тест."""

from __future__ import annotations

from app.services.reachability.links import MAX_CONFIGS_PER_TEST
from app.services.reachability.targets import KIND_CIDR, Target, probe_api_target


class RequestBuildError(ValueError):
    """Запрос не собрать — сообщение для админа."""


def _probes(probes: dict[str, bool]) -> dict[str, bool]:
    clean = {name: bool(probes.get(name, False)) for name in ('icmp', 'tcp', 'sni')}
    if not any(clean.values()):
        raise RequestBuildError('Не выбрано ни одной пробы (ICMP, TCP или SNI)')
    return clean


def build_probe_request(targets: list[Target], units: list[str], dpi: str, probes: dict[str, bool]) -> dict:
    hosts = [t for t in targets if t.kind != KIND_CIDR]
    if not hosts:
        raise RequestBuildError('Нет целей для пробы')
    body: dict = {'targets': [probe_api_target(t) for t in hosts], 'operators': list(units), 'probes': _probes(probes), 'dpi': dpi}
    if body['probes']['sni']:
        sni_hosts = sorted({(t.sni or t.address).lower() for t in hosts})
        body['sni_hosts'] = sni_hosts
    return body


def build_vless_request(targets: list[Target], units: list[str], dpi: str, core: str) -> dict:
    links = [t.raw_link for t in targets if t.raw_link]
    if len(links) != len(targets):
        raise RequestBuildError('Для VLESS-теста нужны конфиги (ссылки), а не адреса')
    if not links:
        raise RequestBuildError('Нет конфигов для теста')
    if len(links) > MAX_CONFIGS_PER_TEST:
        raise RequestBuildError(f'API принимает не больше {MAX_CONFIGS_PER_TEST} конфигов за тест, выбрано {len(links)}')
    return {'raw_input': '\n'.join(links), 'selected_modems': list(units), 'dpi': dpi, 'core': core or ''}


def build_scan_request(target: Target, units: list[str], dpi: str, probes: dict[str, bool], sni_hosts: list[str]) -> dict:
    if target.kind != KIND_CIDR:
        raise RequestBuildError('Скан принимает только подсеть /24')
    body: dict = {'cidr': target.target_key, 'operators': list(units), 'probes': _probes(probes), 'dpi': dpi}
    if body['probes']['sni']:
        if not sni_hosts:
            raise RequestBuildError('Для SNI-пробы скана укажите имена (sni_hosts)')
        body['sni_hosts'] = list(sni_hosts)
    return body
```

- [ ] **Step 3: Прогнать тесты запросов** — PASS.

- [ ] **Step 4: Падающие тесты фасада** `tests/services/reachability/test_service.py` (фасад собирается на фейках: клиент, панель, runner)

```python
"""Фасад: выключено → ReachabilityDisabled; preview считает симки, пропуски и цену до денег;
create_job проверяет занятость и потолок, пишет задачу и запускает фон."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.database.crud import reachability as crud
from app.database.models import User
from app.external.remnawave_api import RemnaWaveHost
from app.services.reachability.jobs import JobRunner, RunnerConfig
from app.services.reachability.gate import PaidCallGate
from app.services.reachability.service import ReachabilityBusy, ReachabilityDisabled, ReachabilityService
from app.services.reachability.units import SelectorError
from tests.fixtures.bschek_fixtures import load_bschek_fixture
from tests.services.reachability.conftest import FakeAPI, FakeClock


HOSTS = [RemnaWaveHost(uuid='h-bs', remark='RU | БС', address='bs-host.example', port=9443, sni='whitelisted.example')]


class FakePanel:
    is_configured = True
    configuration_error = None

    def get_api_client(self):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                return outer

            async def __aexit__(self_inner, *exc):
                return None

        return _Ctx()

    async def get_all_hosts(self):
        return HOSTS

    async def get_all_nodes(self):
        return []

    async def get_subscription_info(self, short_uuid):
        return SimpleNamespace(links=['vless://00000000-0000-4000-8000-000000000001@bs-host.example:9443?security=reality&sni=whitelisted.example#BS'])


class FakeClient(FakeAPI):
    async def get_operators(self, **kwargs):
        return load_bschek_fixture('operators')['body']

    async def get_account(self):
        return {k: v for k, v in load_bschek_fixture('account')['body'].items() if k != 'webhook_secret'}

    async def preview_probe(self, body):
        return load_bschek_fixture('pv_bare_mts')['body']

    async def preview_scan(self, body):
        return load_bschek_fixture('sv_one_unit')['body']


def make_service(session_factory, *, enabled=True, key='bsk_live_test', limit=0, client: FakeClient | None = None) -> ReachabilityService:
    settings_obj = SimpleNamespace(
        BSCHEK_ENABLED=enabled, BSCHEK_API_KEY=key, BSCHEK_REQUEST_TIMEOUT=200, BSCHEK_REFERENCE_SUBSCRIPTION='ref-1',
        BSCHEK_JOB_COST_LIMIT_KOPEKS=limit,
        is_bschek_enabled=lambda: enabled, is_bschek_configured=lambda: bool(key), get_bschek_api_url=lambda: 'https://bsbord.com/v1',
    )
    clock = FakeClock()
    api = client or FakeClient()
    runner = JobRunner(client_factory=lambda: api, gate=PaidCallGate(min_interval=0, clock=clock, sleep=clock.sleep),
                       session_factory=session_factory, cost_limit_kopeks=lambda: limit, config=RunnerConfig(),
                       sleep=clock.sleep, clock=clock)
    service = ReachabilityService(settings_obj=settings_obj, session_factory=session_factory, remnawave_factory=lambda: FakePanel(), runner=runner, clock=clock)
    service._client_factory = lambda: api  # noqa: SLF001 — тестовая подмена клиента
    return service


async def _admin(db) -> User:
    user = User(telegram_id=1, username='admin', first_name='A', language='ru')
    db.add(user)
    await db.flush()
    return user


async def test_disabled_integration_raises(session_factory) -> None:
    service = make_service(session_factory, enabled=False)
    async with session_factory() as db:
        with pytest.raises(ReachabilityDisabled):
            await service.preview(db, {'kind': 'probe', 'targets': [{'kind': 'custom', 'value': '1.1.1.1'}], 'units': [], 'dpi': 'on', 'probes': {'tcp': True}})


async def test_status_reports_balance_without_secret_and_reference(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        status = await service.status(db)
    assert status['enabled'] and status['configured'] and status['healthy']
    assert status['balance_kopeks'] == 100018 and 'webhook_secret' not in str(status)
    assert status['reference']['short_uuid'] == 'ref-1' and status['reference']['configs'] == 1


async def test_preview_probe_expands_units_reports_skipped_and_exact_price(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        preview = await service.preview(db, {'kind': 'probe', 'targets': [{'kind': 'host', 'ref': 'h-bs'}], 'units': ['mts'], 'dpi': 'on', 'probes': {'tcp': True, 'sni': True}})
    assert preview.units_resolved == ['mts|пфо|on']
    assert [u['op_key'] for u in preview.skipped['dpi_off']] == ['mts|цфо|off', 'mts|дфо|off']
    assert (preview.cost_kopeks, preview.estimate_is_exact) == (18, True)
    assert preview.request['sni_hosts'] == ['whitelisted.example']


async def test_preview_unknown_selector_is_rejected_before_api(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        with pytest.raises(SelectorError):
            await service.preview(db, {'kind': 'probe', 'targets': [{'kind': 'host', 'ref': 'h-bs'}], 'units': ['nokia|цфо|on'], 'dpi': 'on', 'probes': {'tcp': True}})


async def test_preview_vless_is_an_estimate(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        preview = await service.preview(db, {'kind': 'vless', 'targets': [{'kind': 'subscription_config', 'short_uuid': 'ref-1', 'index': 0}], 'units': ['*|цфо|on'], 'dpi': 'on', 'probes': {}, 'core': ''})
    assert preview.estimate_is_exact is False and preview.cost_kopeks == 5 * 110


async def test_create_job_writes_row_and_spawns_runner(session_factory) -> None:
    client = FakeClient({'probe': [load_bschek_fixture('p1_probe')['body']]})
    service = make_service(session_factory, client=client)
    async with session_factory() as db:
        admin = await _admin(db)
        await db.commit()
        job = await service.create_job(db, {'kind': 'probe', 'targets': [{'kind': 'host', 'ref': 'h-bs'}], 'units': ['mts'], 'dpi': 'on', 'probes': {'tcp': True}}, admin.id)
        assert job.status == 'pending' and job.idempotency_key
    for task in list(service.runner._tasks.values()):  # noqa: SLF001
        await task
    async with session_factory() as db:
        assert (await crud.get_job(db, job.id)).status == 'done'


async def test_create_job_refuses_second_active_vless(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        admin = await _admin(db)
        await crud.create_job(db, kind='vless', status='running', trigger='manual', started_by_user_id=admin.id, idempotency_key='busy',
                              request={}, targets=[], dpi='on')
        await db.commit()
        with pytest.raises(ReachabilityBusy):
            await service.create_job(db, {'kind': 'vless', 'targets': [{'kind': 'subscription_config', 'short_uuid': 'ref-1', 'index': 0}], 'units': [], 'dpi': 'on', 'probes': {}, 'core': ''}, admin.id)


async def test_summary_builds_matrix_from_latest_legs(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        admin = await _admin(db)
        job = await crud.create_job(db, kind='probe', status='done', trigger='manual', started_by_user_id=admin.id, idempotency_key='s', request={}, targets=[], dpi='on')
        from datetime import UTC, datetime
        await crud.replace_legs(db, job.id, [{'kind': 'probe', 'target_key': 'bs-host.example:9443', 'target_kind': 'host', 'target_ref': 'h-bs', 'op_key': 'mts|пфо|on',
                                              'operator': 'mts', 'region': 'ПФО', 'dpi': 'on', 'verdict': 'reachable', 'matches_expectation': True, 'raw': {}, 'checked_at': datetime.now(UTC)}])
        await db.commit()
        summary = await service.summary(db, dpi='on')
    row = summary['rows'][0]
    assert (row['target_key'], row['purpose'], row['cells']['mts|пфо|on']['verdict']) == ('bs-host.example:9443', 'bs', 'reachable')
    assert 'mts|пфо|on' in [u['op_key'] for u in summary['units']]
```

- [ ] **Step 5: Реализация фасада** `app/services/reachability/service.py`

```python
"""Фасад раздела «Доступность из РФ» для роутов кабинета.

Собирает клиент, каталог симок, резолвер целей, цены и сервис задач. Ничего
платного не делает сам — только preview и запуск задач через JobRunner.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import reachability as crud
from app.database.database import AsyncSessionLocal
from app.database.models import ReachabilityJob, Subscription
from app.external.bschek_api import BschekAPI, BschekAPIError
from app.services.reachability.gate import PaidCallGate
from app.services.reachability.jobs import KIND_PROBE, KIND_SCAN, KIND_VLESS, JobNotCancellable, JobRunner
from app.services.reachability.pricing import credits_to_kopeks, enforce_cost_limit, estimate_vless_kopeks
from app.services.reachability.requests import build_probe_request, build_scan_request, build_vless_request
from app.services.reachability.resolver import HostView, NodeView, PrefsMap, SubscriptionConfigs, TargetResolver
from app.services.reachability.targets import KIND_CIDR, KIND_HOST, PURPOSE_BS, Target
from app.services.reachability.units import Unit, UnitsCache, UnitsCatalog


logger = structlog.get_logger(__name__)

AUTH_CODES = frozenset({'unauthenticated', 'api_not_available', 'tier_too_low', 'subscription_required'})
UNHEALTHY_FOR = timedelta(minutes=5)


class ReachabilityDisabled(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ReachabilityUnhealthy(Exception):
    def __init__(self, reason: str, until: datetime) -> None:
        self.reason, self.until = reason, until
        super().__init__(reason)


class ReachabilityBusy(Exception):
    def __init__(self, job: ReachabilityJob) -> None:
        self.job = job
        super().__init__(f'Уже идёт задача #{job.id}')


class JobNotFound(Exception):
    pass


@dataclass
class PreviewResult:
    kind: str
    targets: list[Target]
    units_resolved: list[str]
    skipped: dict
    cost_kopeks: int | None
    estimate_is_exact: bool
    warnings: list[str] = field(default_factory=list)
    balance_kopeks: int | None = None
    request: dict = field(default_factory=dict)


class ReachabilityService:
    def __init__(
        self,
        *,
        settings_obj: Any = settings,
        session_factory: Callable[[], Any] = AsyncSessionLocal,
        remnawave_factory: Callable[[], Any] | None = None,
        runner: JobRunner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings_obj
        self._session_factory = session_factory
        self._remnawave_factory = remnawave_factory
        self._clock = clock
        self._client_factory: Callable[[], BschekAPI] = self._make_client
        self._gate = PaidCallGate()
        self.runner = runner or JobRunner(
            client_factory=lambda: self._client_factory(), gate=self._gate, session_factory=session_factory,
            cost_limit_kopeks=lambda: int(self._settings.BSCHEK_JOB_COST_LIMIT_KOPEKS or 0),
        )
        self._units = UnitsCache(self._fetch_operators, clock=clock)
        self._unhealthy_until: datetime | None = None
        self._unhealthy_reason: str | None = None
        self._background: asyncio.Task | None = None

    # ------------------------------------------------------------ доступ

    def _make_client(self) -> BschekAPI:
        return BschekAPI(api_key=self._settings.BSCHEK_API_KEY, base_url=self._settings.get_bschek_api_url(),
                         timeout=float(self._settings.BSCHEK_REQUEST_TIMEOUT))

    def _ensure_enabled(self) -> None:
        if not self._settings.is_bschek_enabled():
            raise ReachabilityDisabled('Интеграция bschekbot выключена (BSCHEK_ENABLED)')
        if not self._settings.is_bschek_configured():
            raise ReachabilityDisabled('Не задан ключ API bschekbot (BSCHEK_API_KEY)')
        if self._unhealthy_until and datetime.now(UTC) < self._unhealthy_until:
            raise ReachabilityUnhealthy(self._unhealthy_reason or 'API недоступен', self._unhealthy_until)

    def mark_unhealthy(self, reason: str) -> None:
        self._unhealthy_until = datetime.now(UTC) + UNHEALTHY_FOR
        self._unhealthy_reason = reason

    async def _call(self, fn: Callable[[BschekAPI], Any]) -> Any:
        self._ensure_enabled()
        try:
            async with self._client_factory() as api:
                return await fn(api)
        except BschekAPIError as exc:
            if exc.code in AUTH_CODES:
                self.mark_unhealthy(exc.message)
            raise

    async def _fetch_operators(self) -> dict:
        return await self._call(lambda api: api.get_operators())

    def _panel(self):
        if self._remnawave_factory is not None:
            return self._remnawave_factory()
        from app.services.remnawave_service import RemnaWaveService

        return RemnaWaveService()

    # ------------------------------------------------------------ чтение

    async def status(self, db: AsyncSession) -> dict:
        enabled, configured = self._settings.is_bschek_enabled(), self._settings.is_bschek_configured()
        healthy = not (self._unhealthy_until and datetime.now(UTC) < self._unhealthy_until)
        account: dict = {}
        if enabled and configured and healthy:
            try:
                account = await self._call(lambda api: api.get_account())
            except BschekAPIError as exc:
                healthy, self._unhealthy_reason = False, exc.message
        active = [job for kind in (KIND_VLESS, KIND_SCAN) if (job := await crud.get_active_job(db, kind))]
        reference = await self._reference_status(db) if enabled and configured else None
        return {
            'enabled': enabled, 'configured': configured, 'healthy': healthy, 'health_message': None if healthy else self._unhealthy_reason,
            'balance_kopeks': credits_to_kopeks(account.get('balance_total')), 'bonus_kopeks': credits_to_kopeks(account.get('bonus_credits')),
            'tier': account.get('tier'), 'tier_expires_at': account.get('tier_expires_at'), 'min_interval_sec': account.get('min_interval_sec'),
            'active_jobs': [{'id': j.id, 'kind': j.kind, 'phase': j.phase, 'started_by_user_id': j.started_by_user_id, 'started_at': j.started_at} for j in active],
            'reference': reference, 'cost_limit_kopeks': int(self._settings.BSCHEK_JOB_COST_LIMIT_KOPEKS or 0),
        }

    async def _reference_status(self, db: AsyncSession) -> dict | None:
        short_uuid = self._settings.BSCHEK_REFERENCE_SUBSCRIPTION
        if not short_uuid:
            return {'short_uuid': None, 'configs': 0, 'error': 'Эталонная подписка не задана'}
        try:
            configs = await self.subscription_configs(db, short_uuid=short_uuid)
        except Exception as exc:  # noqa: BLE001 — статус не должен падать из-за панели
            return {'short_uuid': short_uuid, 'configs': 0, 'error': str(exc)[:200]}
        return {'short_uuid': short_uuid, 'configs': len(configs.configs), 'error': None if configs.configs else 'В подписке нет пригодных конфигов'}

    async def units(self, *, dpi: str | None = None, operator: list[str] | None = None, region: list[str] | None = None) -> list[Unit]:
        self._ensure_enabled()
        catalog = await self._units.get()
        units = catalog.units
        if dpi and dpi != 'any':
            units = [u for u in units if u.dpi == dpi]
        if operator:
            wanted = {o.lower() for o in operator}
            units = [u for u in units if u.operator.lower() in wanted]
        if region:
            wanted = {r.lower() for r in region}
            units = [u for u in units if u.region.lower() in wanted or u.region_code.lower() in wanted]
        return units

    async def _prefs(self, db: AsyncSession) -> PrefsMap:
        return {(p.target_kind, p.target_ref): (p.purpose, p.excluded) for p in await crud.list_prefs(db)}

    async def resolver(self, db: AsyncSession) -> TargetResolver:
        panel = self._panel()

        async def fetch_hosts():
            async with panel.get_api_client() as api:
                return await api.get_all_hosts()

        async def fetch_nodes():
            async with panel.get_api_client() as api:
                return await api.get_all_nodes()

        async def fetch_links(short_uuid: str):
            async with panel.get_api_client() as api:
                info = await api.get_subscription_info(short_uuid)
                return list(info.links or [])

        return TargetResolver(fetch_hosts=fetch_hosts, fetch_nodes=fetch_nodes, fetch_links=fetch_links, prefs=await self._prefs(db))

    async def hosts(self, db: AsyncSession, include_disabled: bool = False) -> list[HostView]:
        return await (await self.resolver(db)).hosts(include_disabled=include_disabled)

    async def nodes(self, db: AsyncSession) -> list[NodeView]:
        return await (await self.resolver(db)).nodes()

    async def subscription_configs(self, db: AsyncSession, *, short_uuid: str | None = None, user_id: int | None = None) -> SubscriptionConfigs:
        if not short_uuid and user_id is not None:
            short_uuid = await self._short_uuid_for_user(db, user_id)
        if not short_uuid:
            short_uuid = self._settings.BSCHEK_REFERENCE_SUBSCRIPTION
        if not short_uuid:
            raise ReachabilityDisabled('Не задана эталонная подписка панели (BSCHEK_REFERENCE_SUBSCRIPTION)')
        return await (await self.resolver(db)).subscription_configs(short_uuid)

    async def _short_uuid_for_user(self, db: AsyncSession, user_id: int) -> str | None:
        from sqlalchemy import select

        rows = await db.execute(select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.created_at.desc()))
        for subscription in rows.scalars():
            if subscription.remnawave_short_uuid:
                return subscription.remnawave_short_uuid
        return None

    # ------------------------------------------------------------ preview / запуск

    async def preview(self, db: AsyncSession, payload: dict) -> PreviewResult:
        self._ensure_enabled()
        kind = payload['kind']
        targets = await (await self.resolver(db)).resolve(payload.get('targets') or [])
        catalog: UnitsCatalog = await self._units.get()
        expansion = catalog.expand(list(payload.get('units') or []), payload.get('dpi', 'on'))
        if expansion.unknown:
            from app.services.reachability.units import SelectorError

            raise SelectorError(f'Неизвестные симки: {", ".join(expansion.unknown)} — обновите список')
        skipped = {'dpi_off': [u.as_dict() for u in expansion.skipped_dpi_off], 'unavailable': [u.as_dict() for u in expansion.skipped_unavailable],
                   'unknown': [], 'blocked_targets': []}
        warnings: list[str] = []
        probes = payload.get('probes') or {}
        if kind == KIND_PROBE:
            request = build_probe_request(targets, expansion.resolved, payload.get('dpi', 'on'), probes)
            if any(t.purpose == PURPOSE_BS for t in targets) and not request['probes'].get('sni'):
                warnings.append('У хостов под Белый список без SNI-пробы вердикт ненадёжен (Reality даёт ложный blocked)')
            price = await self._call(lambda api: api.preview_probe(request))
            cost, exact = credits_to_kopeks(price.get('cost_credits')), True
        elif kind == KIND_VLESS:
            request = build_vless_request(targets, expansion.resolved, payload.get('dpi', 'on'), payload.get('core') or '')
            leg = await crud.last_vless_leg_price_kopeks(db)
            cost, exact = estimate_vless_kopeks(len(targets), len(expansion.resolved), leg), False
            warnings.append('Точная цена VLESS-теста известна только после запуска')
        elif kind == KIND_SCAN:
            cidr = next((t for t in targets if t.kind == KIND_CIDR), None)
            if cidr is None:
                raise ValueError('Для скана нужна подсеть /24')
            sni_hosts = sorted({(t.sni or t.address).lower() for t in targets if t.kind != KIND_CIDR})
            request = build_scan_request(cidr, expansion.resolved, payload.get('dpi', 'on'), probes, sni_hosts)
            price = await self._call(lambda api: api.preview_scan(request))
            cost, exact = credits_to_kopeks(price.get('cost_credits')), True
        else:
            raise ValueError(f'Неизвестный вид задачи «{kind}»')
        if not expansion.resolved:
            warnings.append('Под фильтр Белого списка не попала ни одна симка')
        balance = None
        try:
            account = await self._call(lambda api: api.get_account())
            balance = credits_to_kopeks(account.get('balance_total'))
        except BschekAPIError:
            pass
        return PreviewResult(kind, targets, expansion.resolved, skipped, cost, exact, warnings, balance, request)

    async def create_job(self, db: AsyncSession, payload: dict, admin_id: int) -> ReachabilityJob:
        preview = await self.preview(db, payload)
        if not preview.units_resolved:
            raise ValueError('Под фильтр Белого списка не попала ни одна симка — выберите dpi:any или другие симки')
        enforce_cost_limit(preview.cost_kopeks, int(self._settings.BSCHEK_JOB_COST_LIMIT_KOPEKS or 0))
        if preview.balance_kopeks is not None and preview.cost_kopeks is not None and preview.cost_kopeks > preview.balance_kopeks:
            raise ValueError('На балансе bschekbot не хватает средств на эту задачу')
        if preview.kind in (KIND_VLESS, KIND_SCAN):
            active = await crud.get_active_job(db, preview.kind)
            if active is not None:
                raise ReachabilityBusy(active)
        job = await crud.create_job(
            db, kind=preview.kind, status='pending', trigger='manual', started_by_user_id=admin_id, idempotency_key=str(uuid.uuid4()),
            request=preview.request, targets=[t.as_dict() for t in preview.targets], units_requested=list(payload.get('units') or []),
            units_resolved=preview.units_resolved, skipped=preview.skipped, dpi=payload.get('dpi', 'on'),
            estimated_kopeks=preview.cost_kopeks, estimate_is_exact=preview.estimate_is_exact,
        )
        await db.commit()
        self.runner.spawn(job.id)
        return job

    # ------------------------------------------------------------ история и управление

    async def list_jobs(self, db: AsyncSession, **filters: Any) -> tuple[list[ReachabilityJob], int]:
        return await crud.list_jobs(db, **filters)

    async def get_job(self, db: AsyncSession, job_id: int) -> ReachabilityJob:
        job = await crud.get_job(db, job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job

    async def cancel_job(self, db: AsyncSession, job_id: int) -> ReachabilityJob:
        self._ensure_enabled()
        job = await self.get_job(db, job_id)
        await self.runner.cancel(db, job)
        if not self.runner.is_active(job.id):
            asyncio.create_task(self.runner.resume(job.id))
        return job

    async def retrieve_job(self, db: AsyncSession, job_id: int) -> ReachabilityJob:
        self._ensure_enabled()
        job = await self.get_job(db, job_id)
        if job.status not in crud.ACTIVE_STATUSES:
            raise JobNotCancellable('Задача уже завершена, забирать нечего')
        if not self.runner.is_active(job.id):
            asyncio.create_task(self.runner.resume(job.id))
        return job

    async def summary(self, db: AsyncSession, dpi: str = 'on') -> dict:
        legs = await crud.latest_legs(db, target_kind=KIND_HOST, dpi=None if dpi == 'any' else dpi)
        prefs = await self._prefs(db)
        rows: dict[str, dict] = {}
        for leg in legs:
            purpose, excluded = prefs.get((KIND_HOST, leg.target_ref or ''), ('unknown', False))
            if excluded:
                continue
            row = rows.setdefault(leg.target_key, {'target_key': leg.target_key, 'kind': leg.target_kind, 'ref': leg.target_ref, 'purpose': purpose, 'cells': {}})
            row['cells'][leg.op_key] = {'verdict': leg.verdict, 'matches_expectation': leg.matches_expectation, 'checked_at': leg.checked_at, 'job_id': leg.job_id}
        units: list[dict] = []
        try:
            units = [u.as_dict() for u in await self.units(dpi=None if dpi == 'any' else dpi)]
        except (ReachabilityDisabled, ReachabilityUnhealthy, BschekAPIError):
            seen = sorted({leg.op_key for leg in legs})
            units = [{'op_key': key} for key in seen]
        return {'units': units, 'rows': list(rows.values())}

    async def update_pref(self, db: AsyncSession, *, target_kind: str, target_ref: str, purpose: str | None, excluded: bool | None, note: str | None, admin_id: int):
        pref = await crud.upsert_pref(db, target_kind=target_kind, target_ref=target_ref, purpose=purpose, excluded=excluded, note=note, user_id=admin_id)
        await db.commit()
        return pref

    # ------------------------------------------------------------ фон

    def start_background(self) -> None:
        if self._background is None or self._background.done():
            self._background = asyncio.create_task(self.runner.sweeper_loop())

    async def stop_background(self) -> None:
        self.runner.stop()
        if self._background is not None:
            self._background.cancel()
            self._background = None


reachability_service = ReachabilityService()
```

Файл должен уложиться в 400 строк; если нет — вынести `status()` и `_reference_status()` в `service_status.py`.

- [ ] **Step 6: Прогнать** — `uv run pytest tests/services/reachability/test_service.py tests/services/reachability/test_requests.py -q` → PASS. В `test_summary_...` покрытие симок берётся из `units()` (фейковый клиент отдаёт каталог), поэтому `mts|пфо|on` в списке.

- [ ] **Step 7: Коммит**

```bash
uv run ruff format app/services/reachability tests/services/reachability && uv run ruff check app/services/reachability tests/services/reachability
git checkout uv.lock 2>/dev/null
git add app/services/reachability/requests.py app/services/reachability/service.py tests/services/reachability/test_requests.py tests/services/reachability/test_service.py
git commit -m "feat(reachability): тела запросов и фасад сервиса — статус, preview, запуск, сводка"
```

---

### Task 15: Схемы и роуты кабинета

**Files:**
- Create: `app/cabinet/schemas/reachability.py`
- Create: `app/cabinet/routes/admin_reachability.py`
- Modify: `app/cabinet/routes/__init__.py` (import рядом с `admin_ban_system`, include после `admin_ban_system_router`)
- Test: `tests/cabinet/test_admin_reachability.py`

**Interfaces (контракт для кабинета):** префикс `/cabinet/admin/reachability`.

| Метод и путь | Право | Ответ |
|---|---|---|
| `GET /status` | `reachability:read` | `StatusResponse` |
| `GET /units?dpi=&operator=&region=` | read | `UnitsResponse{units: UnitOut[]}` |
| `GET /targets/hosts?include_disabled=` | read | `HostsResponse{items: HostTargetOut[]}` |
| `GET /targets/nodes` | read | `NodesResponse{items: NodeTargetOut[]}` |
| `GET /targets/subscription?short_uuid=&user_id=` | read | `SubscriptionConfigsResponse` |
| `PUT /targets/prefs` | run | `PrefOut` |
| `POST /jobs/preview` | read | `PreviewResponse` |
| `POST /jobs` | run | `JobOut` (201) |
| `GET /jobs?kind=&status=&target_key=&user_id=&offset=&limit=` | read | `JobListResponse` |
| `GET /jobs/{job_id}` | read | `JobOut` |
| `POST /jobs/{job_id}/cancel` | run | `JobOut` |
| `POST /jobs/{job_id}/retrieve` | run | `JobOut` |
| `GET /summary/hosts?dpi=on` | read | `SummaryResponse` |

Схемы (pydantic v2, `from_attributes` где нужно):

```python
"""Схемы раздела «Доступность из РФ» (bschekbot) для кабинета."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Kind = Literal['probe', 'vless', 'scan']
Dpi = Literal['on', 'off', 'any']
Purpose = Literal['bs', 'regular', 'unknown']


class TargetIn(BaseModel):
    kind: Literal['host', 'node', 'subscription_config', 'custom', 'cidr']
    ref: str | None = None
    value: str | None = None
    short_uuid: str | None = None
    index: int | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def _required_fields(self):
        if self.kind in ('host', 'node') and not self.ref:
            raise ValueError('для host/node нужен ref (uuid)')
        if self.kind == 'subscription_config' and (not self.short_uuid or self.index is None):
            raise ValueError('для subscription_config нужны short_uuid и index')
        if self.kind in ('custom', 'cidr') and not (self.value or '').strip():
            raise ValueError('для custom/cidr нужно value')
        return self


class ProbesIn(BaseModel):
    icmp: bool = False
    tcp: bool = True
    sni: bool = True


class JobCreateRequest(BaseModel):
    kind: Kind
    targets: list[TargetIn] = Field(min_length=1, max_length=20)
    units: list[str] = Field(default_factory=list, max_length=64)
    dpi: Dpi = 'on'
    probes: ProbesIn = Field(default_factory=ProbesIn)
    core: Literal['', 'stable', 'prerelease'] = ''


class UnitOut(BaseModel):
    op_key: str
    operator: str = ''
    name: str = ''
    region: str = ''
    region_code: str = ''
    dpi: str = ''
    channel_state: str = ''
    probeable: bool = False


class UnitsResponse(BaseModel):
    units: list[UnitOut]


class SkippedOut(BaseModel):
    dpi_off: list[dict[str, Any]] = Field(default_factory=list)
    unavailable: list[dict[str, Any]] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    blocked_targets: list[dict[str, Any]] = Field(default_factory=list)


class TargetOut(BaseModel):
    kind: str
    label: str
    address: str
    port: int | None
    target_key: str
    sni: str | None
    ref: dict[str, Any] = Field(default_factory=dict)
    purpose: str = 'unknown'


class PreviewResponse(BaseModel):
    kind: Kind
    targets: list[TargetOut]
    units_resolved: list[str]
    skipped: SkippedOut
    cost_kopeks: int | None
    estimate_is_exact: bool
    warnings: list[str]
    balance_kopeks: int | None


class LegOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    target_key: str
    target_kind: str | None
    target_ref: str | None
    op_key: str
    operator: str | None
    region: str | None
    dpi: str | None
    verdict: str
    matches_expectation: bool | None
    raw: dict[str, Any] | None
    checked_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    status: str
    phase: str | None
    trigger: str
    started_by_user_id: int | None
    external_id: int | None
    targets: list[dict[str, Any]]
    units_requested: list[str] | None
    units_resolved: list[str] | None
    units_effective: list[str] | None
    skipped: dict[str, Any] | None
    dpi: str
    estimated_kopeks: int | None
    estimate_is_exact: bool
    cost_kopeks: int | None
    refunded_kopeks: int | None
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    retryable: bool | None
    attempts: int
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    legs: list[LegOut] = Field(default_factory=list)


class JobListResponse(BaseModel):
    items: list[JobOut]
    total: int
    offset: int
    limit: int


class ActiveJobOut(BaseModel):
    id: int
    kind: str
    phase: str | None
    started_by_user_id: int | None
    started_at: datetime | None


class ReferenceOut(BaseModel):
    short_uuid: str | None
    configs: int
    error: str | None


class StatusResponse(BaseModel):
    enabled: bool
    configured: bool
    healthy: bool
    health_message: str | None = None
    balance_kopeks: int | None = None
    bonus_kopeks: int | None = None
    tier: str | None = None
    tier_expires_at: str | None = None
    min_interval_sec: float | None = None
    active_jobs: list[ActiveJobOut] = Field(default_factory=list)
    reference: ReferenceOut | None = None
    cost_limit_kopeks: int = 0


class HostTargetOut(BaseModel):
    uuid: str
    remark: str
    address: str
    port: int | None
    sni: str | None
    is_disabled: bool
    tag: str | None
    purpose: str
    purpose_guessed: bool
    excluded: bool
    node_uuids: list[str]
    target_key: str


class HostsResponse(BaseModel):
    items: list[HostTargetOut]


class NodeTargetOut(BaseModel):
    uuid: str
    name: str
    address: str
    is_connected: bool
    is_disabled: bool
    host_uuids: list[str]
    target_key: str


class NodesResponse(BaseModel):
    items: list[NodeTargetOut]


class ConfigOut(BaseModel):
    index: int
    protocol: str | None
    label: str
    address: str
    port: int | None
    sni: str | None
    target_key: str
    purpose: str


class RejectedOut(BaseModel):
    reason: str
    preview: str  # первые 60 символов ссылки без учётных данных


class SubscriptionConfigsResponse(BaseModel):
    short_uuid: str
    configs: list[ConfigOut]
    rejected: list[RejectedOut]


class PrefUpdateRequest(BaseModel):
    target_kind: Literal['host', 'node']
    target_ref: str = Field(min_length=1, max_length=255)
    purpose: Purpose | None = None
    excluded: bool | None = None
    note: str | None = Field(default=None, max_length=500)


class PrefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    target_kind: str
    target_ref: str
    purpose: str
    excluded: bool
    note: str | None


class CellOut(BaseModel):
    verdict: str
    matches_expectation: bool | None
    checked_at: datetime
    job_id: int


class SummaryRow(BaseModel):
    target_key: str
    kind: str | None
    ref: str | None
    purpose: str
    cells: dict[str, CellOut]


class SummaryResponse(BaseModel):
    units: list[UnitOut]
    rows: list[SummaryRow]
```

- [ ] **Step 1: Падающие тесты роутов** `tests/cabinet/test_admin_reachability.py`

```python
"""Роуты /admin/reachability: регистрация, права, 503 при выключенной интеграции,
статус без секретов, ошибки домена → HTTP, аудит запуска и отмены."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_reachability
from app.cabinet.schemas.reachability import JobCreateRequest, PrefUpdateRequest, TargetIn
from app.services.reachability.service import ReachabilityBusy, ReachabilityDisabled, JobNotFound
from app.services.reachability.jobs import JobNotCancellable
from app.services.reachability.units import SelectorError


ADMIN = SimpleNamespace(id=7, telegram_id=1)


@pytest.fixture
def service(monkeypatch):
    fake = SimpleNamespace()
    monkeypatch.setattr(admin_reachability, '_service', lambda: fake)
    monkeypatch.setattr(admin_reachability.PermissionService, 'log_action', AsyncMock())
    return fake


def test_routes_are_registered(registered_paths) -> None:
    base = '/cabinet/admin/reachability'
    assert 'GET' in registered_paths[f'{base}/status']
    assert 'GET' in registered_paths[f'{base}/units']
    assert 'GET' in registered_paths[f'{base}/targets/hosts']
    assert 'GET' in registered_paths[f'{base}/targets/nodes']
    assert 'GET' in registered_paths[f'{base}/targets/subscription']
    assert 'PUT' in registered_paths[f'{base}/targets/prefs']
    assert 'POST' in registered_paths[f'{base}/jobs/preview']
    assert {'GET', 'POST'} <= registered_paths[f'{base}/jobs']
    assert 'GET' in registered_paths[f'{base}/jobs/{{job_id}}']
    assert 'POST' in registered_paths[f'{base}/jobs/{{job_id}}/cancel']
    assert 'POST' in registered_paths[f'{base}/jobs/{{job_id}}/retrieve']
    assert 'GET' in registered_paths[f'{base}/summary/hosts']


@pytest.mark.parametrize(
    ('endpoint_name', 'permission'),
    [
        ('get_status', 'reachability:read'),
        ('get_units', 'reachability:read'),
        ('get_hosts', 'reachability:read'),
        ('preview_job', 'reachability:read'),
        ('create_job', 'reachability:run'),
        ('cancel_job', 'reachability:run'),
        ('retrieve_job', 'reachability:run'),
        ('update_pref', 'reachability:run'),
        ('get_summary', 'reachability:read'),
    ],
)
def test_routes_require_expected_permission(endpoint_name: str, permission: str) -> None:
    endpoint = getattr(admin_reachability, endpoint_name)
    route = next(route for route in admin_reachability.router.routes if route.endpoint is endpoint)
    dependency = route.dependant.dependencies[0].call
    closure_values = [cell.cell_contents for cell in dependency.__closure__ or ()]
    assert (permission,) in closure_values


def test_target_in_validation() -> None:
    with pytest.raises(ValueError):
        TargetIn(kind='host')
    with pytest.raises(ValueError):
        TargetIn(kind='subscription_config', short_uuid='x')
    assert TargetIn(kind='custom', value='1.1.1.1').value == '1.1.1.1'
    with pytest.raises(ValueError):
        JobCreateRequest(kind='probe', targets=[])


async def test_status_maps_service_dict(service) -> None:
    service.status = AsyncMock(return_value={'enabled': True, 'configured': True, 'healthy': True, 'balance_kopeks': 100018,
                                             'tier': 'gold', 'active_jobs': [], 'reference': {'short_uuid': 'r', 'configs': 3, 'error': None}, 'cost_limit_kopeks': 0})
    response = await admin_reachability.get_status(admin=ADMIN, db=None)
    assert (response.balance_kopeks, response.tier, response.reference.configs) == (100018, 'gold', 3)
    assert 'webhook_secret' not in response.model_dump()


async def test_disabled_integration_is_503(service) -> None:
    service.preview = AsyncMock(side_effect=ReachabilityDisabled('выключено'))
    body = JobCreateRequest(kind='probe', targets=[TargetIn(kind='custom', value='1.1.1.1')])
    with pytest.raises(HTTPException) as exc:
        await admin_reachability.preview_job(body, admin=ADMIN, db=None)
    assert exc.value.status_code == 503 and 'выключено' in exc.value.detail


async def test_selector_error_is_400(service) -> None:
    service.preview = AsyncMock(side_effect=SelectorError('Неизвестные симки: nokia'))
    body = JobCreateRequest(kind='probe', targets=[TargetIn(kind='custom', value='1.1.1.1')], units=['nokia'])
    with pytest.raises(HTTPException) as exc:
        await admin_reachability.preview_job(body, admin=ADMIN, db=None)
    assert exc.value.status_code == 400 and 'nokia' in exc.value.detail


async def test_busy_is_409_with_job_reference(service) -> None:
    active = SimpleNamespace(id=42, kind='vless', started_by_user_id=3, started_at=None)
    service.create_job = AsyncMock(side_effect=ReachabilityBusy(active))
    body = JobCreateRequest(kind='vless', targets=[TargetIn(kind='subscription_config', short_uuid='s', index=0)])
    with pytest.raises(HTTPException) as exc:
        await admin_reachability.create_job(body, admin=ADMIN, db=None)
    assert exc.value.status_code == 409 and '#42' in exc.value.detail


async def test_create_job_logs_audit(service) -> None:
    job = SimpleNamespace(id=5, kind='probe', status='pending', phase=None, trigger='manual', started_by_user_id=7, external_id=None,
                          targets=[], units_requested=[], units_resolved=['mts|пфо|on'], units_effective=None, skipped=None, dpi='on',
                          estimated_kopeks=18, estimate_is_exact=True, cost_kopeks=None, refunded_kopeks=None, result=None, error_code=None,
                          error_message=None, retryable=None, attempts=0, created_at=None, started_at=None, finished_at=None, legs=[])
    service.create_job = AsyncMock(return_value=job)
    body = JobCreateRequest(kind='probe', targets=[TargetIn(kind='custom', value='1.1.1.1')])
    response = await admin_reachability.create_job(body, admin=ADMIN, db=AsyncMock())
    assert response.id == 5
    admin_reachability.PermissionService.log_action.assert_awaited_once()
    kwargs = admin_reachability.PermissionService.log_action.await_args.kwargs
    assert (kwargs['action'], kwargs['resource_type'], kwargs['resource_id']) == ('reachability_job_create', 'reachability_job', '5')


async def test_cancel_not_cancellable_is_409_and_not_found_is_404(service) -> None:
    service.cancel_job = AsyncMock(side_effect=JobNotCancellable('нельзя'))
    with pytest.raises(HTTPException) as exc:
        await admin_reachability.cancel_job(1, admin=ADMIN, db=None)
    assert exc.value.status_code == 409
    service.get_job = AsyncMock(side_effect=JobNotFound(9))
    with pytest.raises(HTTPException) as exc:
        await admin_reachability.get_job(9, admin=ADMIN, db=None)
    assert exc.value.status_code == 404


async def test_update_pref_calls_service_with_admin(service) -> None:
    service.update_pref = AsyncMock(return_value=SimpleNamespace(target_kind='host', target_ref='h', purpose='bs', excluded=False, note=None))
    response = await admin_reachability.update_pref(PrefUpdateRequest(target_kind='host', target_ref='h', purpose='bs'), admin=ADMIN, db=AsyncMock())
    assert response.purpose == 'bs'
    assert service.update_pref.await_args.kwargs['admin_id'] == 7
```

- [ ] **Step 2: Убедиться, что падает.** **Step 3: Роуты** `app/cabinet/routes/admin_reachability.py`

```python
"""Роуты раздела «Доступность из РФ» (bschekbot) в кабинете: тонкий слой над фасадом."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.external.bschek_api import BschekAPIError
from app.services.permission_service import PermissionService
from app.services.reachability.jobs import JobNotCancellable
from app.services.reachability.pricing import CostLimitExceeded
from app.services.reachability.requests import RequestBuildError
from app.services.reachability.resolver import TargetResolutionError
from app.services.reachability.service import (
    JobNotFound,
    ReachabilityBusy,
    ReachabilityDisabled,
    ReachabilityService,
    ReachabilityUnhealthy,
    reachability_service,
)
from app.services.reachability.targets import TargetValidationError
from app.services.reachability.units import SelectorError

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.reachability import (
    ConfigOut,
    HostsResponse,
    HostTargetOut,
    JobCreateRequest,
    JobListResponse,
    JobOut,
    NodesResponse,
    NodeTargetOut,
    PrefOut,
    PrefUpdateRequest,
    PreviewResponse,
    RejectedOut,
    SkippedOut,
    StatusResponse,
    SubscriptionConfigsResponse,
    SummaryResponse,
    TargetOut,
    UnitOut,
    UnitsResponse,
)


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/admin/reachability', tags=['Cabinet Admin Reachability'])

BAD_REQUEST_ERRORS = (SelectorError, TargetValidationError, TargetResolutionError, RequestBuildError, CostLimitExceeded, ValueError)


def _service() -> ReachabilityService:
    return reachability_service


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, ReachabilityDisabled):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.reason)
    if isinstance(exc, ReachabilityUnhealthy):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f'{exc.reason} (повтор после {exc.until:%H:%M} UTC)')
    if isinstance(exc, ReachabilityBusy):
        job = exc.job
        return HTTPException(status.HTTP_409_CONFLICT, f'Уже идёт задача #{job.id} ({job.kind}), запустил пользователь {job.started_by_user_id}')
    if isinstance(exc, JobNotCancellable):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, JobNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, 'Задача не найдена')
    if isinstance(exc, BAD_REQUEST_ERRORS):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if isinstance(exc, BschekAPIError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, f'bschekbot: {exc.message} [{exc.code}]')
    logger.error('Неожиданная ошибка раздела reachability', error=exc)
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, 'Внутренняя ошибка')


def _job_out(job: Any) -> JobOut:
    return JobOut.model_validate(job, from_attributes=True)


@router.get('/status', response_model=StatusResponse)
async def get_status(admin: User = Depends(require_permission('reachability:read')), db: AsyncSession = Depends(get_cabinet_db)) -> StatusResponse:
    try:
        return StatusResponse(**await _service().status(db))
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc


@router.get('/units', response_model=UnitsResponse)
async def get_units(
    dpi: str | None = Query(default=None),
    operator: str | None = Query(default=None, description='через запятую'),
    region: str | None = Query(default=None, description='через запятую, кириллица или код'),
    admin: User = Depends(require_permission('reachability:read')),
) -> UnitsResponse:
    try:
        units = await _service().units(dpi=dpi, operator=operator.split(',') if operator else None, region=region.split(',') if region else None)
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc
    return UnitsResponse(units=[UnitOut(**u.as_dict()) for u in units])


@router.get('/targets/hosts', response_model=HostsResponse)
async def get_hosts(
    include_disabled: bool = Query(default=False),
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> HostsResponse:
    try:
        views = await _service().hosts(db, include_disabled=include_disabled)
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc
    return HostsResponse(items=[
        HostTargetOut(uuid=v.host.uuid, remark=v.host.remark, address=v.host.address, port=v.host.port, sni=v.target.sni, is_disabled=v.host.is_disabled,
                      tag=v.host.tag, purpose=v.target.purpose, purpose_guessed=v.purpose_guessed, excluded=v.excluded, node_uuids=v.node_uuids,
                      target_key=v.target.target_key)
        for v in views
    ])


@router.get('/targets/nodes', response_model=NodesResponse)
async def get_nodes(admin: User = Depends(require_permission('reachability:read')), db: AsyncSession = Depends(get_cabinet_db)) -> NodesResponse:
    try:
        views = await _service().nodes(db)
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc
    return NodesResponse(items=[
        NodeTargetOut(uuid=v.node.uuid, name=v.node.name, address=v.node.address, is_connected=v.node.is_connected, is_disabled=v.node.is_disabled,
                      host_uuids=v.host_uuids, target_key=v.target.target_key)
        for v in views
    ])


@router.get('/targets/subscription', response_model=SubscriptionConfigsResponse)
async def get_subscription_configs(
    short_uuid: str | None = Query(default=None),
    user_id: int | None = Query(default=None),
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SubscriptionConfigsResponse:
    try:
        configs = await _service().subscription_configs(db, short_uuid=short_uuid, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc
    return SubscriptionConfigsResponse(
        short_uuid=configs.short_uuid,
        configs=[
            ConfigOut(index=t.ref.get('index', i), protocol=(t.raw_link or '').split('://', 1)[0] or None, label=t.label, address=t.address, port=t.port,
                      sni=t.sni, target_key=t.target_key, purpose=t.purpose)
            for i, t in enumerate(configs.configs)
        ],
        rejected=[RejectedOut(reason=r.reason, preview=r.raw.split('@')[-1][:60]) for r in configs.rejected],
    )


@router.put('/targets/prefs', response_model=PrefOut)
async def update_pref(body: PrefUpdateRequest, admin: User = Depends(require_permission('reachability:run')), db: AsyncSession = Depends(get_cabinet_db)) -> PrefOut:
    pref = await _service().update_pref(db, target_kind=body.target_kind, target_ref=body.target_ref, purpose=body.purpose, excluded=body.excluded,
                                        note=body.note, admin_id=admin.id)
    return PrefOut.model_validate(pref, from_attributes=True)


@router.post('/jobs/preview', response_model=PreviewResponse)
async def preview_job(body: JobCreateRequest, admin: User = Depends(require_permission('reachability:read')), db: AsyncSession = Depends(get_cabinet_db)) -> PreviewResponse:
    try:
        preview = await _service().preview(db, body.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc
    return PreviewResponse(kind=preview.kind, targets=[TargetOut(**t.as_dict()) for t in preview.targets], units_resolved=preview.units_resolved,
                           skipped=SkippedOut(**preview.skipped), cost_kopeks=preview.cost_kopeks, estimate_is_exact=preview.estimate_is_exact,
                           warnings=preview.warnings, balance_kopeks=preview.balance_kopeks)


@router.post('/jobs', response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(body: JobCreateRequest, admin: User = Depends(require_permission('reachability:run')), db: AsyncSession = Depends(get_cabinet_db)) -> JobOut:
    try:
        job = await _service().create_job(db, body.model_dump(), admin.id)
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc
    await PermissionService.log_action(
        db, user_id=admin.id, action='reachability_job_create', resource_type='reachability_job', resource_id=str(job.id),
        details={'kind': job.kind, 'units': job.units_resolved, 'targets': [t.get('target_key') for t in job.targets or []],
                 'estimated_kopeks': job.estimated_kopeks},
    )
    await db.commit()
    return _job_out(job)


@router.get('/jobs', response_model=JobListResponse)
async def list_jobs(
    kind: str | None = Query(default=None),
    job_status: str | None = Query(default=None, alias='status'),
    target_key: str | None = Query(default=None),
    user_id: int | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> JobListResponse:
    items, total = await _service().list_jobs(db, kind=kind, status=job_status, target_key=target_key, user_id=user_id, offset=offset, limit=limit)
    return JobListResponse(items=[_job_out(j) for j in items], total=total, offset=offset, limit=limit)


@router.get('/jobs/{job_id}', response_model=JobOut)
async def get_job(job_id: int, admin: User = Depends(require_permission('reachability:read')), db: AsyncSession = Depends(get_cabinet_db)) -> JobOut:
    try:
        return _job_out(await _service().get_job(db, job_id))
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc


@router.post('/jobs/{job_id}/cancel', response_model=JobOut)
async def cancel_job(job_id: int, admin: User = Depends(require_permission('reachability:run')), db: AsyncSession = Depends(get_cabinet_db)) -> JobOut:
    try:
        job = await _service().cancel_job(db, job_id)
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc
    await PermissionService.log_action(db, user_id=admin.id, action='reachability_job_cancel', resource_type='reachability_job', resource_id=str(job.id))
    await db.commit()
    return _job_out(job)


@router.post('/jobs/{job_id}/retrieve', response_model=JobOut)
async def retrieve_job(job_id: int, admin: User = Depends(require_permission('reachability:run')), db: AsyncSession = Depends(get_cabinet_db)) -> JobOut:
    try:
        return _job_out(await _service().retrieve_job(db, job_id))
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc


@router.get('/summary/hosts', response_model=SummaryResponse)
async def get_summary(dpi: str = Query(default='on'), admin: User = Depends(require_permission('reachability:read')), db: AsyncSession = Depends(get_cabinet_db)) -> SummaryResponse:
    try:
        return SummaryResponse(**await _service().summary(db, dpi=dpi))
    except Exception as exc:  # noqa: BLE001
        raise _http(exc) from exc
```

Регистрация в `app/cabinet/routes/__init__.py`: рядом с `from .admin_ban_system import router as admin_ban_system_router` добавить `from .admin_reachability import router as admin_reachability_router`; после `router.include_router(admin_ban_system_router)` — `router.include_router(admin_reachability_router)`.

- [ ] **Step 4: Прогнать** — `uv run pytest tests/cabinet/test_admin_reachability.py tests/cabinet/test_admin_remnawave_geocheck.py -q` → PASS. Если тест прав падает из-за порядка `dependencies[0]` (в роуте есть `Query`-параметры) — искать зависимость `require_permission` среди всех `route.dependant.dependencies`, как делает тест GeoCheck; при необходимости так же поправить тест.

- [ ] **Step 5: Коммит**

```bash
uv run ruff format app/cabinet/schemas/reachability.py app/cabinet/routes/admin_reachability.py app/cabinet/routes/__init__.py tests/cabinet/test_admin_reachability.py && uv run ruff check app/cabinet tests/cabinet/test_admin_reachability.py
git checkout uv.lock 2>/dev/null
git add app/cabinet/schemas/reachability.py app/cabinet/routes/admin_reachability.py app/cabinet/routes/__init__.py tests/cabinet/test_admin_reachability.py
git commit -m "feat(reachability): роуты и схемы /admin/reachability для кабинета"
```

---

### Task 16: Фон в `main.py`

**Files:**
- Modify: `main.py` (после стадии «Служба мониторинга», ~строка 656; остановка рядом с `monitoring_task.cancel()`, ~строка 888)
- Test: `tests/services/reachability/test_background.py`

- [ ] **Step 1: Падающий тест**

```python
"""Фон сервиса: запуск идемпотентен, остановка гасит обходчик."""

import asyncio

from app.services.reachability.jobs import JobRunner, RunnerConfig
from app.services.reachability.gate import PaidCallGate
from app.services.reachability.service import ReachabilityService
from tests.services.reachability.conftest import FakeAPI, FakeClock


async def test_start_background_is_idempotent_and_stop_cancels(session_factory) -> None:
    clock = FakeClock()
    runner = JobRunner(client_factory=lambda: FakeAPI(), gate=PaidCallGate(min_interval=0, clock=clock, sleep=clock.sleep),
                       session_factory=session_factory, cost_limit_kopeks=lambda: 0, config=RunnerConfig(sweep_interval=0.01),
                       sleep=asyncio.sleep, clock=clock)
    service = ReachabilityService(session_factory=session_factory, runner=runner)
    service.start_background()
    first = service._background  # noqa: SLF001
    service.start_background()
    assert service._background is first  # noqa: SLF001
    await asyncio.sleep(0.05)
    await service.stop_background()
    assert first.cancelled() or first.done()
```

- [ ] **Step 2: Реализация в `main.py`**

После стадии мониторинга:

```python
        async with timeline.stage(
            'Доступность из РФ (bschekbot)',
            '📶',
            success_message='Обходчик задач проверки запущен',
        ) as stage:
            if settings.is_bschek_enabled() and settings.is_bschek_configured():
                from app.services.reachability.service import reachability_service

                reachability_service.start_background()
                stage.log('Незавершённые задачи будут подхвачены обходчиком')
            else:
                stage.skip('Интеграция bschekbot выключена или без ключа')
```

В блоке остановки рядом с `monitoring_task.cancel()`:

```python
        try:
            from app.services.reachability.service import reachability_service

            await reachability_service.stop_background()
        except Exception as exc:  # noqa: BLE001
            logger.warning('Не удалось остановить обходчик задач проверки', error=exc)
```

Проверить, что имена `timeline`, `stage`, `settings`, `logger` в этих местах уже используются (см. соседние стадии), а импорт `reachability_service` внутри стадии не ломает `tests/test_no_undefined_names.py`.

- [ ] **Step 3: Прогнать** — `uv run pytest tests/services/reachability/test_background.py tests/test_no_undefined_names.py -q` → PASS. Запуск приложения не требуется.

- [ ] **Step 4: Коммит**

```bash
uv run ruff format main.py tests/services/reachability/test_background.py && uv run ruff check main.py tests/services/reachability/test_background.py
git checkout uv.lock 2>/dev/null
git add main.py tests/services/reachability/test_background.py
git commit -m "feat(reachability): запуск и остановка обходчика задач в main"
```

---

### Task 17: Живой детектор дрейфа контракта

**Files:**
- Modify: `pyproject.toml` (`markers = [...]`, ~строка 336: добавить `'bschek_live: живой bschekbot API, нужен BSCHEK_LIVE_API_KEY; только бесплатные ручки'`)
- Create: `tests/live/__init__.py` (пустой), `tests/live/test_bschek_live.py`

- [ ] **Step 1: Тест**

```python
"""Живой детектор дрейфа контракта bschekbot. Только бесплатные ручки, денег не тратит.

Запуск: BSCHEK_LIVE_API_KEY=bsk_live_… uv run pytest -m bschek_live tests/live -q
Без ключа в окружении — пропускается целиком.
"""

from __future__ import annotations

import os

import pytest

from app.external.bschek_api import BschekAPI, BschekAPIError


pytestmark = pytest.mark.bschek_live

KEY = os.environ.get('BSCHEK_LIVE_API_KEY')


@pytest.fixture
def api_key() -> str:
    if not KEY:
        pytest.skip('BSCHEK_LIVE_API_KEY не задан')
    return KEY


async def test_operators_shape(api_key: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        payload = await api.get_operators()
    assert {'units', 'n_units', 'n_probeable', 'filters', 'n_total'} <= set(payload)
    unit = payload['units'][0]
    assert {'op_key', 'operator', 'name', 'region', 'region_code', 'dpi', 'channel_state', 'probeable'} <= set(unit)
    assert unit['op_key'].count('|') == 2


async def test_account_shape_without_secret(api_key: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        account = await api.get_account()
    assert {'balance_credits', 'bonus_credits', 'balance_total', 'tier'} <= set(account)
    assert 'webhook_secret' not in account


async def test_probe_preview_breakdown(api_key: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        preview = await api.preview_probe({'target': 'example.com', 'operators': ['mts'], 'probes': {'tcp': True}})
    assert preview['cost_credits'] > 0
    assert {'base', 'sni_addon', 'multi_scan_factor', 'pre_discount', 'discount_pct', 'total'} <= set(preview['breakdown'])
    assert isinstance(preview['selected_units'], list)


@pytest.mark.parametrize(
    ('body', 'code'),
    [
        ({'target': 'example.com', 'operators': ['ufo1:mts']}, 'unknown_operator'),
        ({'target': 'example.com', 'operators': ['mts'], 'probes': {'icmp': False, 'tcp': False}}, 'no_probes'),
        ({'operators': ['mts']}, 'invalid_request'),
    ],
)
async def test_validation_codes_still_the_same(api_key: str, body: dict, code: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        with pytest.raises(BschekAPIError) as exc:
            await api.preview_probe(body)
    assert exc.value.code == code


async def test_scan_preview_rejects_non_24(api_key: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        with pytest.raises(BschekAPIError) as exc:
            await api.preview_scan({'cidr': '192.0.2.0/25', 'operators': ['mts']})
    assert exc.value.code == 'cidr_not_24'
```

- [ ] **Step 2: Маркер и прогон**

В `pyproject.toml` в список `markers` добавить строку. Прогнать без ключа: `uv run pytest tests/live -q` → все `skipped`. С ключом (только локально, по желанию): `BSCHEK_LIVE_API_KEY=… uv run pytest -m bschek_live tests/live -q`.

- [ ] **Step 3: Коммит**

```bash
uv run ruff format tests/live && uv run ruff check tests/live
git checkout uv.lock 2>/dev/null
git add pyproject.toml tests/live
git commit -m "test(reachability): живой детектор дрейфа контракта bschekbot (бесплатные ручки)"
```

---

### Task 18: Финальная проверка части 2

- [ ] Полный прогон: `uv run pytest -q` (все тесты бота) — зелёный; `uv run ruff check app tests main.py` — чисто; `uv run ruff format --check app tests main.py` — чисто.
- [ ] Миграция: на пустой SQLite/PostgreSQL `uv run alembic -c alembic.ini upgrade head` проходит (для PostgreSQL — через `make pg-test-up` и `TEST_DATABASE_URL`, см. Makefile).
- [ ] Ручная проверка API через `uv run python -c` не требуется; контракт зафиксирован тестами роутов и схемами. Кабинет — по плану `docs/superpowers/plans/2026-09-05-reachability-cabinet.md`.
- [ ] `git status` чист, `uv.lock` без постороннего бампа версии.
