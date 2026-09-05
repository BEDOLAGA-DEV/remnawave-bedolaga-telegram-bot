# Доступность из РФ (bschekbot) — план реализации, бот, часть 1: ядро

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать в боте фундамент интеграции bschekbot: настройки, права, HTTP-клиент, каталог симок, модели БД, разбор ссылок и целей, вердикты, цены и шлюз платных вызовов — всё с тестами, без роутов и фона (они в части 2).

**Architecture:** Клиент `app/external/bschek_api.py` разбирает конверт ошибок API и отличает «ответ без конверта» (524 Cloudflare) от ошибок API. Домен живёт пакетом `app/services/reachability/` из маленьких чистых модулей (каталог симок, ссылки, цели, вердикт, цены, шлюз), которые часть 2 соберёт в сервис задач. Данные — три таблицы (`reachability_jobs`, `reachability_legs`, `reachability_target_prefs`) через SQLAlchemy `JSON`, совместимо с SQLite и PostgreSQL.

**Tech Stack:** Python 3.13, aiohttp, SQLAlchemy 2 async, Alembic, pydantic-settings, pytest + pytest-asyncio, ruff (single quotes, line 120).

**Spec:** `docs/superpowers/specs/2026-09-05-reachability-bschek-design.md` (читать целиком; приложение А — поведение живого API).

## Global Constraints

- Домен в коде — `reachability` (пакет, таблицы, права, роуты); всё, что принадлежит провайдеру, — `bschek` (клиент, префикс настроек `BSCHEK_`).
- Ключ API никогда не логируется и не возвращается клиентам; `webhook_secret` из `/account` отбрасывается в клиенте.
- Никаких зашитых списков операторов/округов: флот меняется, всё берётся из `GET /operators`.
- Один Idempotency-Key на задачу навсегда; автоматический повтор всегда тем же ключом и тем же телом.
- Файлы ≤ 400 строк; функции ≤ 50 строк; JSON-колонки — `sqlalchemy.JSON`; перечисления — строковые константы.
- Стиль: ruff `quote-style = 'single'`, `line-length = 120`, docstrings и комментарии по-русски, как в соседних модулях.
- Перед каждым коммитом: `uv run ruff format <файлы> && uv run ruff check <файлы>`; после `uv run` проверить `git diff uv.lock` и откатить (`git checkout uv.lock`), если он поменял версию.
- Коммиты: `<type>(reachability): <описание>` без строк Co-Authored-By / Claude-Session / «Generated with».
- Тесты запускаются `uv run pytest <путь> -q`. Тесты на реальной БД — через `tests/fixtures/sqlite_memory.py::memory_session`.
- Фикстуры API уже лежат в `tests/fixtures/bschek/*.json` (см. README там): `{"name","status","elapsed_sec","headers","request","idempotency_key","body"}`. Не редактировать.

## Карта файлов (часть 1)

| Файл | Ответственность |
|---|---|
| `app/config.py` | поля `BSCHEK_*`, хелперы `is_bschek_enabled/is_bschek_configured/get_bschek_api_url` |
| `app/services/system_settings_service.py` | категория `BSCHEK` в реестре настроек |
| `app/services/permission_service.py`, `app/services/rbac_bootstrap_service.py` | секция `reachability`, `reachability:*` у Admin |
| `.env.example` | закомментированный блок `BSCHEK_*` |
| `app/external/bschek_api.py` | HTTP-клиент, `BschekAPIError`, `BschekGatewayError` |
| `app/external/remnawave_api.py` | `RemnaWaveHost`, `get_all_hosts()` |
| `app/database/models.py` | `ReachabilityJob`, `ReachabilityLeg`, `ReachabilityTargetPref` |
| `migrations/alembic/versions/0115_create_reachability_tables.py` | миграция |
| `app/database/crud/reachability.py` | CRUD, запрос сводки, активные и незавершённые задачи |
| `app/services/reachability/__init__.py` | пустой пакет с docstring |
| `app/services/reachability/units.py` | `Unit`, `parse_selector`, `UnitsCatalog.expand`, `UnitsCache` |
| `app/services/reachability/links.py` | разбор ссылок vless/vmess/trojan/ss/hysteria2 |
| `app/services/reachability/targets.py` | нормализация цели, `target_key`, назначение, /24 |
| `app/services/reachability/verdict.py` | вердикт лега и соответствие ожиданию |
| `app/services/reachability/pricing.py` | кредиты → копейки, оценка VLESS, потолок |
| `app/services/reachability/gate.py` | `PaidCallGate` |
| `tests/fixtures/bschek_fixtures.py` | `load_bschek_fixture(name)` |
| `tests/services/test_reachability_registries.py`, `tests/external/test_bschek_api.py`, `tests/external/test_remnawave_hosts.py`, `tests/database/test_reachability_crud.py`, `tests/services/reachability/test_units.py`, `test_links.py`, `test_targets.py`, `test_verdict.py`, `test_pricing.py`, `test_gate.py` | тесты |

---

### Task 1: Настройки, категория реестра, права

**Files:**
- Modify: `app/config.py` (блок после `BAN_SYSTEM_REQUEST_TIMEOUT: int = 30`, ~строка 1505; хелперы после `get_ban_system_request_timeout`, ~строка 4300)
- Modify: `app/services/system_settings_service.py` (`CATEGORY_TITLES` после `'GRACE_ACCESS'` ~207; `CATEGORY_DESCRIPTIONS` после блока `'GRACE_ACCESS'` ~285–290; `CATEGORY_PREFIX_OVERRIDES` после `'GRACE_ACCESS_': 'GRACE_ACCESS'` ~527)
- Modify: `app/services/permission_service.py` (после `'ban_system': ['read', 'edit', 'ban', 'unban'],` ~80)
- Modify: `app/services/rbac_bootstrap_service.py` (роль Admin, после `'ban_system:*',` ~189)
- Modify: `.env.example` (после блока `BAN_SYSTEM_*`, ~1212)
- Test: `tests/services/test_reachability_registries.py`

**Interfaces:**
- Produces: `settings.BSCHEK_ENABLED: bool`, `settings.BSCHEK_API_URL: str`, `settings.BSCHEK_API_KEY: str | None`, `settings.BSCHEK_REQUEST_TIMEOUT: int`, `settings.BSCHEK_REFERENCE_SUBSCRIPTION: str | None`, `settings.BSCHEK_JOB_COST_LIMIT_KOPEKS: int`; методы `settings.is_bschek_enabled() -> bool`, `settings.is_bschek_configured() -> bool`, `settings.get_bschek_api_url() -> str`; права `reachability:read`, `reachability:run`.

- [ ] **Step 1: Написать падающий тест**

```python
"""Реестры, без которых раздел «Доступность из РФ» не соберётся.

Право, которого нет в PERMISSION_REGISTRY, редактор ролей отвергает с 400, а
настройка без категории не показывается в кабинете. Здесь закреплены имена и
дефолты из спецификации (раздел 5).
"""

import pytest

from app.cabinet.routes.admin_roles import _validate_permissions
from app.config import settings
from app.services.permission_service import PERMISSION_REGISTRY, get_all_permissions
from app.services.rbac_bootstrap_service import _PRESET_ROLES
from app.services.system_settings_service import BotConfigurationService as registry


BSCHEK_KEYS = (
    'BSCHEK_ENABLED',
    'BSCHEK_API_URL',
    'BSCHEK_API_KEY',
    'BSCHEK_REQUEST_TIMEOUT',
    'BSCHEK_REFERENCE_SUBSCRIPTION',
    'BSCHEK_JOB_COST_LIMIT_KOPEKS',
)


def test_permission_section_registered() -> None:
    assert PERMISSION_REGISTRY['reachability'] == ['read', 'run']


@pytest.mark.parametrize('permission', ['reachability:read', 'reachability:run'])
def test_permission_is_grantable(permission: str) -> None:
    assert permission in get_all_permissions()
    _validate_permissions([permission])


def test_wildcard_from_bootstrap_survives_a_role_save() -> None:
    _validate_permissions(['reachability:*'])


def test_admin_preset_gets_wildcard() -> None:
    admin = next(role for role in _PRESET_ROLES if role['name'] == 'Admin')
    assert 'reachability:*' in admin['permissions']


def test_settings_defaults() -> None:
    assert settings.BSCHEK_REQUEST_TIMEOUT == 200
    assert settings.BSCHEK_JOB_COST_LIMIT_KOPEKS == 0
    assert settings.get_bschek_api_url() == 'https://bsbord.com/v1'
    assert settings.is_bschek_configured() is bool(settings.BSCHEK_API_KEY)


@pytest.mark.parametrize('key', BSCHEK_KEYS)
def test_settings_land_in_bschek_category(key: str) -> None:
    assert registry.get_definition(key).category_key == 'BSCHEK'


def test_category_has_title_and_description() -> None:
    assert 'BSCHEK' in registry.CATEGORY_TITLES
    assert 'BSCHEK' in registry.CATEGORY_DESCRIPTIONS


def test_api_key_is_masked_and_numbers_are_not() -> None:
    assert registry.is_masked_secret('BSCHEK_API_KEY', 'bsk_live_x') is True
    assert registry.is_masked_secret('BSCHEK_JOB_COST_LIMIT_KOPEKS', 0) is False
    assert registry.is_masked_secret('BSCHEK_REQUEST_TIMEOUT', 200) is False


def test_env_example_block_is_commented_out() -> None:
    """Раскомментированный BSCHEK_* в .env затеняет значение из кабинета."""
    text = open('.env.example', encoding='utf-8').read()
    for key in BSCHEK_KEYS:
        assert f'# {key}=' in text, key
        assert f'\n{key}=' not in text, key
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/services/test_reachability_registries.py -q`
Expected: FAIL — `KeyError: 'reachability'`, `AttributeError: ... BSCHEK_REQUEST_TIMEOUT`.

- [ ] **Step 3: Добавить поля и хелперы в `app/config.py`**

После строки `BAN_SYSTEM_REQUEST_TIMEOUT: int = 30`:

```python
    # bschekbot — «Доступность из РФ»: проверка хостов глазами мобильных операторов
    BSCHEK_ENABLED: bool = False
    BSCHEK_API_URL: str = 'https://bsbord.com/v1'
    BSCHEK_API_KEY: str | None = None  # bsk_live_…, выпускается вручную в кабинете bschekbot
    BSCHEK_REQUEST_TIMEOUT: int = 200  # синхронный probe идёт до нескольких минут
    BSCHEK_REFERENCE_SUBSCRIPTION: str | None = None  # shortUuid эталонной подписки панели
    BSCHEK_JOB_COST_LIMIT_KOPEKS: int = 0  # потолок цены одной задачи, 0 — без потолка
```

После метода `get_ban_system_request_timeout`:

```python
    # bschekbot helpers
    def is_bschek_enabled(self) -> bool:
        return bool(self.BSCHEK_ENABLED)

    def is_bschek_configured(self) -> bool:
        return bool(self.BSCHEK_API_KEY)

    def get_bschek_api_url(self) -> str:
        return (self.BSCHEK_API_URL or 'https://bsbord.com/v1').rstrip('/')
```

- [ ] **Step 4: Категория в реестре настроек**

В `CATEGORY_TITLES` после `'GRACE_ACCESS': '🛟 Grace-доступ',`:

```python
        'BSCHEK': '📶 Доступность из РФ (bschekbot)',
```

В `CATEGORY_DESCRIPTIONS` после записи `'GRACE_ACCESS'`:

```python
        'BSCHEK': (
            'Проверка хостов и конфигов глазами мобильных операторов РФ через bschekbot API: '
            'ключ, эталонная подписка панели, потолок цены одной задачи.'
        ),
```

В `CATEGORY_PREFIX_OVERRIDES` после `'GRACE_ACCESS_': 'GRACE_ACCESS',`:

```python
        'BSCHEK_': 'BSCHEK',
```

- [ ] **Step 5: Права**

`app/services/permission_service.py`, после `'ban_system': [...]`:

```python
    'reachability': ['read', 'run'],
```

`app/services/rbac_bootstrap_service.py`, роль `Admin`, после `'ban_system:*',`:

```python
            'reachability:*',
```

- [ ] **Step 6: `.env.example`**

После блока `BAN_SYSTEM_*`:

```
# ---- bschekbot: «Доступность из РФ» (Кабинет → Система → Доступность из РФ) ----
# Настройки редактируются в кабинете; раскомментированная строка здесь перекроет их.
# BSCHEK_ENABLED=false
# BSCHEK_API_URL=https://bsbord.com/v1
# BSCHEK_API_KEY=
# BSCHEK_REQUEST_TIMEOUT=200
# BSCHEK_REFERENCE_SUBSCRIPTION=
# BSCHEK_JOB_COST_LIMIT_KOPEKS=0
```

- [ ] **Step 7: Прогнать тесты**

Run: `uv run pytest tests/services/test_reachability_registries.py tests/cabinet/test_system_errors_permissions.py -q`
Expected: PASS.

- [ ] **Step 8: Коммит**

```bash
uv run ruff format app/config.py app/services/system_settings_service.py app/services/permission_service.py app/services/rbac_bootstrap_service.py tests/services/test_reachability_registries.py && uv run ruff check app/config.py app/services tests/services/test_reachability_registries.py
git checkout uv.lock 2>/dev/null
git add app/config.py app/services/system_settings_service.py app/services/permission_service.py app/services/rbac_bootstrap_service.py .env.example tests/services/test_reachability_registries.py
git commit -m "feat(reachability): настройки BSCHEK_*, категория реестра и права раздела"
```

---

### Task 2: Клиент bschekbot API

**Files:**
- Create: `app/external/bschek_api.py`
- Create: `tests/fixtures/bschek_fixtures.py`
- Test: `tests/external/test_bschek_api.py`

**Interfaces:**
- Produces:
  - `class BschekAPIError(Exception)`: поля `code: str`, `message: str`, `status: int | None`, `retryable: bool`, `retry_after: float | None`, `request_id: str | None`, `details: dict`.
  - `class BschekGatewayError(BschekAPIError)`: ответ без конверта (524/502, таймаут, обрыв).
  - `class BschekAPI(api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT)`, async context manager; методы `get_operators(*, dpi=None, operator=None, region=None, probeable=None) -> dict`, `get_account() -> dict` (без `webhook_secret`), `preview_probe(body) -> dict`, `probe(body, idempotency_key) -> dict`, `preview_scan(body) -> dict`, `start_scan(body, idempotency_key) -> dict`, `get_scan(scan_id) -> dict`, `cancel_scan(scan_id) -> dict`, `start_vless(body, idempotency_key) -> dict`, `get_vless(test_id) -> dict`, `cancel_vless(test_id) -> dict`.
  - `BschekAPI.parse_response(status: int, text: str, headers: Mapping[str, str]) -> dict` — чистая функция разбора (для тестов и для клиента).
  - `build_operators_params(...) -> dict[str, str]`.
  - `load_bschek_fixture(name: str) -> dict` в `tests/fixtures/bschek_fixtures.py`.

- [ ] **Step 1: Загрузчик фикстур**

`tests/fixtures/bschek_fixtures.py`:

```python
"""Записанные ответы bschekbot API (см. tests/fixtures/bschek/README.md)."""

from __future__ import annotations

import json
from pathlib import Path


FIXTURES_DIR = Path(__file__).parent / 'bschek'


def load_bschek_fixture(name: str) -> dict:
    """Возвращает фикстуру целиком: status, headers, request, idempotency_key, body."""
    return json.loads((FIXTURES_DIR / f'{name}.json').read_text(encoding='utf-8'))


def iter_bschek_fixtures():
    for path in sorted(FIXTURES_DIR.glob('*.json')):
        yield path.stem, json.loads(path.read_text(encoding='utf-8'))
```

- [ ] **Step 2: Написать падающие тесты**

`tests/external/test_bschek_api.py`:

```python
"""Клиент bschekbot API v1 на записанных ответах живого сервиса.

Что закреплено: единый конверт ошибок (включая коды, которых нет в контракте),
ответ без конверта = отдельный класс ошибки (524 за Cloudflare — деньги списаны,
результат надо забирать повтором ключа), сборка query для /operators без потери
кириллицы и сокрытие webhook_secret.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.external.bschek_api import (
    BschekAPI,
    BschekAPIError,
    BschekGatewayError,
    build_operators_params,
)
from tests.fixtures.bschek_fixtures import iter_bschek_fixtures, load_bschek_fixture


def _parse(name: str) -> dict:
    fx = load_bschek_fixture(name)
    return BschekAPI.parse_response(fx['status'], json.dumps(fx['body'], ensure_ascii=False), fx['headers'])


@pytest.mark.parametrize(
    ('name', 'code', 'status', 'retryable'),
    [
        ('auth_bad', 'unauthenticated', 401, False),
        ('auth_none', 'unauthenticated', 401, False),
        ('method_405', 'method_not_allowed', 405, False),
        ('pv_conflict', 'no_dpi_on', 400, False),
        ('pv_unknown_op', 'worker_unavailable', 503, True),
        ('pv_garbage', 'invalid_request', 422, False),
        ('pv_old_format', 'unknown_operator', 400, False),
        ('pv_no_probes', 'no_probes', 400, False),
        ('pv_11_targets', 'too_many_targets', 400, False),
        ('rl2_b', 'rate_limited', 429, True),
        ('v1_second', 'test_in_progress', 409, True),
        ('s1_second', 'scan_in_progress', 409, True),
        ('pF_same_key_while_running', 'request_in_progress', 409, False),
        ('p1_reused', 'idempotency_key_reused', 409, False),
        ('p_noidem', 'idempotency_key_required', 400, False),
        ('p_blocked', 'blocked_target', 400, False),
        ('v_noconfigs', 'parse_failed', 400, False),
        ('v_suburl', 'subscription_not_supported', 400, False),
        ('v_too_many', 'too_many_configs', 400, False),
        ('v_too_large', 'input_too_large', 400, False),
        ('s_notfound', 'not_found', 404, False),
        ('v_cancel_done', 'not_found', 404, False),
        ('sB_cancel_again', 'not_running', 409, False),
        ('v2_cancel_again', 'cannot_cancel_running', 409, True),
        ('sv_not24', 'cidr_too_wide', 400, False),
        ('sv_webhook', 'webhooks_disabled', 400, False),
    ],
)
def test_error_envelope_is_mapped(name: str, code: str, status: int, retryable: bool) -> None:
    with pytest.raises(BschekAPIError) as exc:
        _parse(name)
    assert not isinstance(exc.value, BschekGatewayError)
    assert (exc.value.code, exc.value.status, exc.value.retryable) == (code, status, retryable)
    assert exc.value.message


def test_no_dpi_on_carries_skipped_units_in_details() -> None:
    with pytest.raises(BschekAPIError) as exc:
        _parse('pv_conflict')
    assert exc.value.details['skipped_dpi_off'][0]['op_key'] == 'yota|уфо|off'


def test_rate_limited_exposes_retry_after() -> None:
    with pytest.raises(BschekAPIError) as exc:
        _parse('rl2_b')
    assert exc.value.retry_after == pytest.approx(1.0)


def test_validation_422_keeps_fields() -> None:
    with pytest.raises(BschekAPIError) as exc:
        _parse('pv_garbage')
    assert exc.value.details['fields'][0]['type'] == 'json_invalid'


def test_cloudflare_524_without_body_is_gateway_error() -> None:
    fx = load_bschek_fixture('pF_fleet')
    with pytest.raises(BschekGatewayError) as exc:
        BschekAPI.parse_response(fx['status'], '', fx['headers'])
    assert exc.value.status == 524
    assert exc.value.retryable is True


def test_html_502_is_gateway_error() -> None:
    with pytest.raises(BschekGatewayError):
        BschekAPI.parse_response(502, '<html>bad gateway</html>', {'Content-Type': 'text/html'})


def test_success_body_is_returned_as_is() -> None:
    body = _parse('p2_replay')
    assert body['outcome'] == 'done'
    assert body['cost_credits'] == 260
    assert set(body['by_target']) == {'eu-host.example', 'bs-host.example:9443'}


def test_no_dpi_on_race_with_200_is_not_an_error() -> None:
    body = BschekAPI.parse_response(200, json.dumps({'outcome': 'no_dpi_on', 'skipped_dpi_off': []}), {})
    assert body['outcome'] == 'no_dpi_on'


def test_every_recorded_error_fixture_parses_to_a_code() -> None:
    """Сторож: новый записанный ответ с конвертом ошибки обязан разбираться."""
    seen = 0
    for _name, fx in iter_bschek_fixtures():
        if isinstance(fx['body'], dict) and 'error' in fx['body']:
            with pytest.raises(BschekAPIError) as exc:
                BschekAPI.parse_response(fx['status'], json.dumps(fx['body'], ensure_ascii=False), fx['headers'])
            assert exc.value.code
            seen += 1
    assert seen >= 25


def test_operators_params_join_lists_and_keep_cyrillic() -> None:
    params = build_operators_params(dpi='on', operator=['mts', 'beeline'], region=['цфо'], probeable=True)
    assert params == {'dpi': 'on', 'operator': 'mts,beeline', 'region': 'цфо', 'probeable': 'true'}
    assert build_operators_params() == {}


async def test_account_hides_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    api = BschekAPI(api_key='bsk_live_test')
    fx = load_bschek_fixture('account')

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict:
        assert (method, path) == ('GET', '/account')
        return dict(fx['body'])

    monkeypatch.setattr(api, '_request', fake_request)
    account = await api.get_account()
    assert 'webhook_secret' not in account
    assert account['balance_total'] == fx['body']['balance_total']


@pytest.mark.parametrize(
    ('method_name', 'args', 'expected'),
    [
        ('probe', ({'target': 'x'}, 'k1'), ('POST', '/probe', {'target': 'x'}, 'k1')),
        ('preview_probe', ({'target': 'x'},), ('POST', '/probe/preview', {'target': 'x'}, None)),
        ('start_scan', ({'cidr': 'c'}, 'k2'), ('POST', '/scans', {'cidr': 'c'}, 'k2')),
        ('preview_scan', ({'cidr': 'c'},), ('POST', '/scans/preview', {'cidr': 'c'}, None)),
        ('get_scan', (5,), ('GET', '/scans/5', None, None)),
        ('cancel_scan', (5,), ('POST', '/scans/5/cancel', None, None)),
        ('start_vless', ({'raw_input': 'v'}, 'k3'), ('POST', '/vless', {'raw_input': 'v'}, 'k3')),
        ('get_vless', (7,), ('GET', '/vless/7', None, None)),
        ('cancel_vless', (7,), ('POST', '/vless/7/cancel', None, None)),
    ],
)
async def test_methods_hit_expected_paths(monkeypatch: pytest.MonkeyPatch, method_name, args, expected) -> None:
    api = BschekAPI(api_key='bsk_live_test')
    calls: list[tuple] = []

    async def fake_request(method: str, path: str, *, params=None, json_body=None, idempotency_key=None) -> dict:
        calls.append((method, path, json_body, idempotency_key))
        return {}

    monkeypatch.setattr(api, '_request', fake_request)
    await getattr(api, method_name)(*args)
    assert calls == [expected]


def test_api_key_never_appears_in_repr() -> None:
    api = BschekAPI(api_key='bsk_live_secret')
    assert 'bsk_live_secret' not in repr(api)
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `uv run pytest tests/external/test_bschek_api.py -q`
Expected: FAIL — `ModuleNotFoundError: app.external.bschek_api`.

- [ ] **Step 4: Реализовать клиент**

`app/external/bschek_api.py`:

```python
"""Клиент bschekbot API v1 — проверка достижимости из мобильных сетей РФ.

Живое поведение API описано в docs/superpowers/specs/2026-09-05-reachability-bschek-design.md
(приложение А). Здесь важны три вещи:

* единый конверт ошибок ``{"error": {code, message, details}}`` на любых статусах,
  включая коды, которых нет в контракте — они сохраняются как есть;
* ответ без конверта (524/502 от Cloudflare, таймаут, обрыв) — это НЕ ошибка API:
  платный запрос мог отработать и списать деньги, результат достаётся повтором
  с тем же Idempotency-Key. Для этого отдельный класс :class:`BschekGatewayError`;
* ``/account`` отдаёт ``webhook_secret`` — он отбрасывается на этой границе.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import structlog


logger = structlog.get_logger(__name__)

DEFAULT_BASE_URL = 'https://bsbord.com/v1'
DEFAULT_TIMEOUT = 200.0


@dataclass
class BschekAPIError(Exception):
    """Ошибка API в едином конверте."""

    code: str
    message: str
    status: int | None = None
    retryable: bool = False
    retry_after: float | None = None
    request_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f'{self.code}: {self.message}')


class BschekGatewayError(BschekAPIError):
    """Ответ без конверта API: шлюз, таймаут или сеть. Результат надо переспросить тем же ключом."""


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def build_operators_params(
    *,
    dpi: str | None = None,
    operator: list[str] | None = None,
    region: list[str] | None = None,
    probeable: bool | None = None,
) -> dict[str, str]:
    """Query для GET /operators. aiohttp сам percent-encode'ит кириллицу."""
    params: dict[str, str] = {}
    if dpi:
        params['dpi'] = dpi
    if operator:
        params['operator'] = ','.join(operator)
    if region:
        params['region'] = ','.join(region)
    if probeable is not None:
        params['probeable'] = 'true' if probeable else 'false'
    return params


class BschekAPI:
    """Тонкий HTTP-клиент: один метод на эндпоинт, без бизнес-логики."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip('/')
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def __repr__(self) -> str:
        return f'BschekAPI(base_url={self.base_url!r})'

    async def __aenter__(self) -> BschekAPI:
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers={'Authorization': f'Bearer {self._api_key}', 'Accept': 'application/json'},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ разбор

    @staticmethod
    def parse_response(status: int, text: str, headers: Mapping[str, str]) -> dict:
        """Единая точка разбора: конверт ошибки → BschekAPIError, без конверта → BschekGatewayError."""
        request_id = _header(headers, 'X-Request-Id')
        try:
            body: Any = json.loads(text) if text else {}
        except json.JSONDecodeError:
            body = None

        if isinstance(body, dict) and isinstance(body.get('error'), dict):
            err = body['error']
            details = dict(err.get('details') or {})
            raise BschekAPIError(
                code=str(err.get('code') or 'unknown_error'),
                message=str(err.get('message') or ''),
                status=status,
                retryable=bool(details.get('retryable', False)),
                retry_after=_float_or_none(details.get('retry_after')) or _float_or_none(_header(headers, 'Retry-After')),
                request_id=details.get('request_id') or request_id,
                details=details,
            )

        if body is None or status >= 500:
            raise BschekGatewayError(
                code=f'http_{status}',
                message=f'Ответ без конверта API (HTTP {status})',
                status=status,
                retryable=True,
                request_id=request_id,
            )
        if status >= 400:
            raise BschekAPIError(code=f'http_{status}', message=text[:200], status=status, request_id=request_id)
        return body if isinstance(body, dict) else {'_raw': body}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        if self._session is None:
            raise RuntimeError('BschekAPI используется только как async context manager')
        headers = {'Idempotency-Key': idempotency_key} if idempotency_key else None
        try:
            async with self._session.request(
                method, f'{self.base_url}{path}', params=params or None, json=json_body, headers=headers
            ) as response:
                text = await response.text()
                return self.parse_response(response.status, text, response.headers)
        except TimeoutError as exc:
            raise BschekGatewayError(code='timeout', message='Таймаут запроса к bschekbot', retryable=True) from exc
        except aiohttp.ClientError as exc:
            raise BschekGatewayError(code='network_error', message=str(exc), retryable=True) from exc

    # ------------------------------------------------------------------ ручки

    async def get_operators(
        self,
        *,
        dpi: str | None = None,
        operator: list[str] | None = None,
        region: list[str] | None = None,
        probeable: bool | None = None,
    ) -> dict:
        params = build_operators_params(dpi=dpi, operator=operator, region=region, probeable=probeable)
        return await self._request('GET', '/operators', params=params)

    async def get_account(self) -> dict:
        account = await self._request('GET', '/account')
        return {key: value for key, value in account.items() if key != 'webhook_secret'}

    async def preview_probe(self, body: dict) -> dict:
        return await self._request('POST', '/probe/preview', json_body=body)

    async def probe(self, body: dict, idempotency_key: str) -> dict:
        return await self._request('POST', '/probe', json_body=body, idempotency_key=idempotency_key)

    async def preview_scan(self, body: dict) -> dict:
        return await self._request('POST', '/scans/preview', json_body=body)

    async def start_scan(self, body: dict, idempotency_key: str) -> dict:
        return await self._request('POST', '/scans', json_body=body, idempotency_key=idempotency_key)

    async def get_scan(self, scan_id: int) -> dict:
        return await self._request('GET', f'/scans/{scan_id}')

    async def cancel_scan(self, scan_id: int) -> dict:
        return await self._request('POST', f'/scans/{scan_id}/cancel')

    async def start_vless(self, body: dict, idempotency_key: str) -> dict:
        return await self._request('POST', '/vless', json_body=body, idempotency_key=idempotency_key)

    async def get_vless(self, test_id: int) -> dict:
        return await self._request('GET', f'/vless/{test_id}')

    async def cancel_vless(self, test_id: int) -> dict:
        return await self._request('POST', f'/vless/{test_id}/cancel')
```

- [ ] **Step 5: Прогнать тесты**

Run: `uv run pytest tests/external/test_bschek_api.py -q`
Expected: PASS (все параметризованные случаи).

- [ ] **Step 6: Коммит**

```bash
uv run ruff format app/external/bschek_api.py tests/external/test_bschek_api.py tests/fixtures/bschek_fixtures.py && uv run ruff check app/external/bschek_api.py tests/external/test_bschek_api.py tests/fixtures/bschek_fixtures.py
git checkout uv.lock 2>/dev/null
git add app/external/bschek_api.py tests/external/test_bschek_api.py tests/fixtures/bschek_fixtures.py tests/fixtures/bschek
git commit -m "feat(reachability): клиент bschekbot API с разбором конверта ошибок и фикстуры живых ответов"
```

---

### Task 3: Хосты панели Remnawave

**Files:**
- Modify: `app/external/remnawave_api.py` (dataclass рядом с `RemnaWaveAccessibleNode` ~строка 137; парсер рядом с `_parse_node` ~2045; метод рядом с `get_all_nodes` ~1236)
- Test: `tests/external/test_remnawave_hosts.py`

**Interfaces:**
- Produces: `@dataclass RemnaWaveHost(uuid, remark, address, port: int | None, sni, host, is_disabled, is_hidden, tag, security_layer, config_profile_uuid, config_profile_inbound_uuid, view_position)`; `RemnaWaveAPI.get_all_hosts() -> list[RemnaWaveHost]`; `RemnaWaveAPI._parse_host(data: dict) -> RemnaWaveHost`.

- [ ] **Step 1: Падающий тест**

```python
"""GET /api/hosts — то, куда подключаются пользователи (адрес, порт, SNI, инбаунд).

У ноды address/port — канал «панель → нода» (порт агента), поэтому проверять
достижимость надо по хостам. Форма объекта хоста взята из схемы панели.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveHost


HOST_PAYLOAD = {
    'uuid': 'h-1',
    'viewPosition': 2,
    'remark': '🇩🇪 Germany',
    'address': 'eu-host.example',
    'port': 443,
    'path': None,
    'sni': 'eu-host.example',
    'host': None,
    'alpn': None,
    'fingerprint': 'firefox',
    'isDisabled': False,
    'isHidden': False,
    'securityLayer': 'DEFAULT',
    'tag': 'EU',
    'inbound': {'configProfileUuid': 'cp-1', 'configProfileInboundUuid': 'in-1'},
}


def test_parse_host_maps_panel_fields() -> None:
    host = RemnaWaveAPI._parse_host(HOST_PAYLOAD)
    assert host == RemnaWaveHost(
        uuid='h-1',
        remark='🇩🇪 Germany',
        address='eu-host.example',
        port=443,
        sni='eu-host.example',
        host=None,
        is_disabled=False,
        is_hidden=False,
        tag='EU',
        security_layer='DEFAULT',
        config_profile_uuid='cp-1',
        config_profile_inbound_uuid='in-1',
        view_position=2,
    )


def test_parse_host_tolerates_missing_optional_fields() -> None:
    host = RemnaWaveAPI._parse_host({'uuid': 'h-2', 'remark': 'x', 'address': 'a.example'})
    assert (host.port, host.sni, host.config_profile_inbound_uuid, host.is_disabled) == (None, None, None, False)


async def test_get_all_hosts_calls_hosts_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    api = RemnaWaveAPI(base_url='https://panel.example', api_key='k')
    calls: list[tuple[str, str]] = []

    async def fake_make_request(method: str, endpoint: str, data: dict | None = None, params: dict | None = None) -> Any:
        calls.append((method, endpoint))
        return {'response': [HOST_PAYLOAD]}

    monkeypatch.setattr(api, '_make_request', fake_make_request)
    hosts = await api.get_all_hosts()
    assert calls == [('GET', '/api/hosts')]
    assert [h.uuid for h in hosts] == ['h-1']
```

Если конструктор `RemnaWaveAPI` требует других аргументов — посмотреть `tests/external/test_remnawave_3_0_0.py`, как там создают клиент, и повторить.

- [ ] **Step 2: Убедиться, что падает**

Run: `uv run pytest tests/external/test_remnawave_hosts.py -q`
Expected: FAIL — `ImportError: RemnaWaveHost`.

- [ ] **Step 3: Реализация**

Рядом с `RemnaWaveAccessibleNode`:

```python
@dataclass
class RemnaWaveHost:
    """Хост панели — то, куда подключаются пользователи: адрес, порт, SNI, инбаунд."""

    uuid: str
    remark: str
    address: str
    port: int | None = None
    sni: str | None = None
    host: str | None = None
    is_disabled: bool = False
    is_hidden: bool = False
    tag: str | None = None
    security_layer: str | None = None
    config_profile_uuid: str | None = None
    config_profile_inbound_uuid: str | None = None
    view_position: int = 0
```

Рядом с `get_all_nodes`:

```python
    async def get_all_hosts(self) -> list[RemnaWaveHost]:
        """GET /api/hosts — все хосты панели (включая отключённые и скрытые)."""
        response = await self._make_request('GET', '/api/hosts')
        return [self._parse_host(host) for host in response.get('response') or []]
```

Рядом с `_parse_node` (staticmethod, чтобы тестировать без сессии):

```python
    @staticmethod
    def _parse_host(data: dict) -> RemnaWaveHost:
        inbound = data.get('inbound') or {}
        port = data.get('port')
        return RemnaWaveHost(
            uuid=data['uuid'],
            remark=data.get('remark') or '',
            address=data.get('address') or '',
            port=int(port) if port is not None else None,
            sni=data.get('sni') or None,
            host=data.get('host') or None,
            is_disabled=bool(data.get('isDisabled', False)),
            is_hidden=bool(data.get('isHidden', False)),
            tag=data.get('tag') or None,
            security_layer=data.get('securityLayer') or None,
            config_profile_uuid=inbound.get('configProfileUuid') or None,
            config_profile_inbound_uuid=inbound.get('configProfileInboundUuid') or None,
            view_position=int(data.get('viewPosition') or 0),
        )
```

- [ ] **Step 4: Прогнать**

Run: `uv run pytest tests/external/test_remnawave_hosts.py tests/external/test_remnawave_3_0_0.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
uv run ruff format app/external/remnawave_api.py tests/external/test_remnawave_hosts.py && uv run ruff check app/external/remnawave_api.py tests/external/test_remnawave_hosts.py
git checkout uv.lock 2>/dev/null
git add app/external/remnawave_api.py tests/external/test_remnawave_hosts.py
git commit -m "feat(remnawave): чтение хостов панели (GET /api/hosts)"
```

---

### Task 4: Модели, миграция, CRUD

**Files:**
- Modify: `app/database/models.py` (в конец файла)
- Create: `migrations/alembic/versions/0115_create_reachability_tables.py`
- Create: `app/database/crud/reachability.py`
- Test: `tests/database/test_reachability_crud.py`

**Interfaces:**
- Produces модели `ReachabilityJob`, `ReachabilityLeg`, `ReachabilityTargetPref` (поля — см. спец 6.1–6.3) и CRUD:
  - `async create_job(db, **fields) -> ReachabilityJob`
  - `async get_job(db, job_id) -> ReachabilityJob | None` (с `legs`)
  - `async update_job(db, job, **fields) -> ReachabilityJob` (присваивает поля, `updated_at`, flush)
  - `async list_jobs(db, *, kind=None, status=None, target_key=None, user_id=None, offset=0, limit=50) -> tuple[list[ReachabilityJob], int]`
  - `async get_active_job(db, kind) -> ReachabilityJob | None` — статус `pending`/`running`
  - `async list_unfinished_jobs(db) -> list[ReachabilityJob]`
  - `async replace_legs(db, job_id, legs: list[dict]) -> list[ReachabilityLeg]`
  - `async latest_legs(db, *, target_kind=None, dpi=None) -> list[ReachabilityLeg]` — последний лег на пару (target_key, op_key)
  - `async get_pref(db, target_kind, target_ref) -> ReachabilityTargetPref | None`, `async upsert_pref(db, *, target_kind, target_ref, purpose=None, excluded=None, note=None, user_id=None)`, `async list_prefs(db) -> list[ReachabilityTargetPref]`
  - `async last_vless_leg_price_kopeks(db) -> int | None`
- Константы статусов в `app/services/reachability/states.py`? Нет — здесь, в CRUD-модуле: `JOB_STATUSES = ('pending', 'running', 'done', 'failed', 'cancelled')`, `ACTIVE_STATUSES = ('pending', 'running')`.

- [ ] **Step 1: Падающие тесты на реальном SQLite**

```python
"""CRUD раздела «Доступность из РФ» на настоящем SQLite.

Главное — запрос сводки: последний лег на пару (хост, симка), а не «все леги»,
и учёт активных задач по виду (один VLESS и один скан на аккаунт).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud import reachability as crud
from app.database.models import ReachabilityJob, ReachabilityLeg, ReachabilityTargetPref, User
from tests.fixtures.sqlite_memory import memory_session


_TABLES = (User.__table__, ReachabilityJob.__table__, ReachabilityLeg.__table__, ReachabilityTargetPref.__table__)


async def _admin(db) -> User:
    user = User(telegram_id=1, username='admin', first_name='A', language='ru')
    db.add(user)
    await db.flush()
    return user


def _job_fields(user_id: int, **overrides) -> dict:
    fields = {
        'kind': 'probe',
        'status': 'pending',
        'trigger': 'manual',
        'started_by_user_id': user_id,
        'idempotency_key': overrides.pop('idempotency_key', 'key-1'),
        'request': {'target': 'bs-host.example:9443'},
        'targets': [{'kind': 'host', 'target_key': 'bs-host.example:9443'}],
        'units_requested': ['mts|цфо|on'],
        'units_resolved': ['mts|цфо|on'],
        'dpi': 'on',
        'estimated_kopeks': 18,
        'estimate_is_exact': True,
    }
    fields.update(overrides)
    return fields


async def test_create_get_and_update_job(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        job = await crud.create_job(db, **_job_fields(admin.id))
        await db.commit()

        loaded = await crud.get_job(db, job.id)
        assert loaded is not None
        assert (loaded.kind, loaded.status, loaded.idempotency_key) == ('probe', 'pending', 'key-1')
        assert loaded.request == {'target': 'bs-host.example:9443'}

        await crud.update_job(db, loaded, status='done', cost_kopeks=18, phase=None)
        await db.commit()
        assert (await crud.get_job(db, job.id)).cost_kopeks == 18


async def test_idempotency_key_is_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.exc import IntegrityError

    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        await crud.create_job(db, **_job_fields(admin.id))
        with pytest.raises(IntegrityError):
            await crud.create_job(db, **_job_fields(admin.id))


async def test_active_job_is_found_per_kind_only_while_running(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        vless = await crud.create_job(db, **_job_fields(admin.id, kind='vless', status='running', idempotency_key='v'))
        await crud.create_job(db, **_job_fields(admin.id, kind='scan', status='done', idempotency_key='s'))
        await db.commit()

        assert (await crud.get_active_job(db, 'vless')).id == vless.id
        assert await crud.get_active_job(db, 'scan') is None
        assert [j.id for j in await crud.list_unfinished_jobs(db)] == [vless.id]


async def test_list_jobs_filters_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        for i, kind in enumerate(('probe', 'probe', 'scan')):
            await crud.create_job(db, **_job_fields(admin.id, kind=kind, idempotency_key=f'k{i}'))
        await db.commit()

        items, total = await crud.list_jobs(db, kind='probe', limit=1)
        assert (len(items), total) == (1, 2)
        items, total = await crud.list_jobs(db, target_key='bs-host.example:9443')
        assert total == 3
        items, total = await crud.list_jobs(db, user_id=admin.id + 1)
        assert total == 0


async def test_latest_legs_returns_newest_per_target_and_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        old = await crud.create_job(db, **_job_fields(admin.id, status='done', idempotency_key='old'))
        new = await crud.create_job(db, **_job_fields(admin.id, status='done', idempotency_key='new'))
        t0 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
        leg = {
            'kind': 'probe',
            'target_key': 'bs-host.example:9443',
            'target_kind': 'host',
            'target_ref': 'h-1',
            'op_key': 'mts|цфо|on',
            'operator': 'mts',
            'region': 'ЦФО',
            'dpi': 'on',
            'raw': {},
        }
        await crud.replace_legs(db, old.id, [{**leg, 'verdict': 'down', 'matches_expectation': False, 'checked_at': t0}])
        await crud.replace_legs(
            db,
            new.id,
            [
                {**leg, 'verdict': 'reachable', 'matches_expectation': True, 'checked_at': t0 + timedelta(hours=1)},
                {**leg, 'op_key': 'tele2|цфо|on', 'operator': 'tele2', 'verdict': 'blocked', 'matches_expectation': False, 'checked_at': t0},
            ],
        )
        await db.commit()

        latest = await crud.latest_legs(db, target_kind='host', dpi='on')
        by_unit = {leg.op_key: leg.verdict for leg in latest}
        assert by_unit == {'mts|цфо|on': 'reachable', 'tele2|цфо|on': 'blocked'}
        assert all(leg.job_id == new.id for leg in latest)


async def test_replace_legs_drops_previous_legs_of_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        job = await crud.create_job(db, **_job_fields(admin.id))
        base = {'kind': 'probe', 'target_key': 'a:1', 'op_key': 'mts|цфо|on', 'verdict': 'down', 'raw': {}, 'checked_at': datetime.now(UTC)}
        await crud.replace_legs(db, job.id, [base])
        await crud.replace_legs(db, job.id, [{**base, 'verdict': 'reachable'}])
        await db.commit()
        loaded = await crud.get_job(db, job.id)
        assert [leg.verdict for leg in loaded.legs] == ['reachable']


async def test_prefs_upsert_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        await crud.upsert_pref(db, target_kind='host', target_ref='h-1', purpose='bs', user_id=admin.id)
        await crud.upsert_pref(db, target_kind='host', target_ref='h-1', excluded=True, user_id=admin.id)
        await db.commit()

        pref = await crud.get_pref(db, 'host', 'h-1')
        assert (pref.purpose, pref.excluded) == ('bs', True)
        assert len(await crud.list_prefs(db)) == 1


async def test_last_vless_leg_price_uses_latest_done_vless_job(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        assert await crud.last_vless_leg_price_kopeks(db) is None
        await crud.create_job(
            db,
            **_job_fields(admin.id, kind='vless', status='done', idempotency_key='v1', cost_kopeks=206,
                          result={'n_servers': 1, 'n_modems': 2}),
        )
        await db.commit()
        assert await crud.last_vless_leg_price_kopeks(db) == 103
```

Если `User(...)` требует других обязательных полей — посмотреть, как создают пользователя в `tests/database/test_guest_purchase_gift_idempotency.py`, и повторить.

- [ ] **Step 2: Убедиться, что падают**

Run: `uv run pytest tests/database/test_reachability_crud.py -q`
Expected: FAIL — `ImportError` моделей/CRUD.

- [ ] **Step 3: Модели** (в конец `app/database/models.py`)

```python
class ReachabilityJob(Base):
    """Задача проверки достижимости через bschekbot (probe / vless / scan).

    Хранит запрос байт в байт и ключ идемпотентности: любой повтор к API идёт
    только с ними (иначе списание повторится). ``result`` — сырой итоговый ответ.
    """

    __tablename__ = 'reachability_jobs'
    __table_args__ = (Index('ix_reachability_jobs_kind_created', 'kind', 'created_at'),)

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(16), nullable=False)  # probe | vless | scan
    status = Column(String(16), nullable=False, default='pending', index=True)
    phase = Column(String(32), nullable=True)  # submitting | waiting | retrieving | polling | cancelling
    trigger = Column(String(16), nullable=False, default='manual')  # manual | scheduled (v2)
    started_by_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)

    idempotency_key = Column(String(64), unique=True, nullable=False)
    external_id = Column(Integer, nullable=True, index=True)  # scan_id / test_id
    last_request_id = Column(String(64), nullable=True)

    request = Column(JSON, nullable=False)
    targets = Column(JSON, nullable=False)
    units_requested = Column(JSON, nullable=True)
    units_resolved = Column(JSON, nullable=True)
    units_effective = Column(JSON, nullable=True)
    skipped = Column(JSON, nullable=True)
    dpi = Column(String(8), nullable=False, default='on')

    estimated_kopeks = Column(Integer, nullable=True)
    estimate_is_exact = Column(Boolean, nullable=False, default=True)
    cost_kopeks = Column(Integer, nullable=True)
    refunded_kopeks = Column(Integer, nullable=True)

    result = Column(JSON, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    retryable = Column(Boolean, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)

    created_at = Column(AwareDateTime(), default=func.now())
    started_at = Column(AwareDateTime(), nullable=True)
    finished_at = Column(AwareDateTime(), nullable=True)
    updated_at = Column(AwareDateTime(), default=func.now(), onupdate=func.now())

    legs = relationship('ReachabilityLeg', back_populates='job', cascade='all, delete-orphan', order_by='ReachabilityLeg.id')
    started_by = relationship('User', backref='reachability_jobs')


class ReachabilityLeg(Base):
    """Пара цель × симка с вердиктом — из неё строится сводка. Только probe и vless."""

    __tablename__ = 'reachability_legs'
    __table_args__ = (Index('ix_reachability_legs_target_unit_time', 'target_key', 'op_key', 'checked_at'),)

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey('reachability_jobs.id', ondelete='CASCADE'), nullable=False, index=True)
    kind = Column(String(16), nullable=False)
    target_key = Column(String(255), nullable=False)  # адрес:порт в нижнем регистре
    target_kind = Column(String(32), nullable=True)  # host | node | subscription_config | custom
    target_ref = Column(String(255), nullable=True)  # uuid хоста / uuid ноды / shortUuid
    op_key = Column(String(64), nullable=False)
    operator = Column(String(32), nullable=True)
    region = Column(String(32), nullable=True)
    dpi = Column(String(8), nullable=True)
    verdict = Column(String(16), nullable=False)  # reachable | blocked | down | unknown | cancelled
    matches_expectation = Column(Boolean, nullable=True)
    raw = Column(JSON, nullable=True)
    checked_at = Column(AwareDateTime(), nullable=False)

    job = relationship('ReachabilityJob', back_populates='legs')


class ReachabilityTargetPref(Base):
    """Назначение цели (под Белый список / обычный) и её исключение из сводки — решение админа."""

    __tablename__ = 'reachability_target_prefs'
    __table_args__ = (UniqueConstraint('target_kind', 'target_ref', name='uq_reachability_target_prefs_target'),)

    id = Column(Integer, primary_key=True, index=True)
    target_kind = Column(String(32), nullable=False)  # host | node
    target_ref = Column(String(255), nullable=False)
    purpose = Column(String(16), nullable=False, default='unknown')  # bs | regular | unknown
    excluded = Column(Boolean, nullable=False, default=False)
    note = Column(Text, nullable=True)
    updated_by_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    updated_at = Column(AwareDateTime(), default=func.now(), onupdate=func.now())
```

Проверить, что `Index`, `UniqueConstraint`, `JSON`, `Text`, `Boolean` уже импортированы в шапке `models.py` (строки 13–32); чего нет — добавить в тот же импорт из `sqlalchemy`.

- [ ] **Step 4: Миграция** `migrations/alembic/versions/0115_create_reachability_tables.py`

```python
"""create reachability tables (bschekbot integration)

Revision ID: 0115
Revises: 0114
Create Date: 2026-09-05

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0115'
down_revision: Union[str, None] = '0114'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'reachability_jobs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('phase', sa.String(32), nullable=True),
        sa.Column('trigger', sa.String(16), nullable=False, server_default='manual'),
        sa.Column('started_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('idempotency_key', sa.String(64), nullable=False, unique=True),
        sa.Column('external_id', sa.Integer(), nullable=True),
        sa.Column('last_request_id', sa.String(64), nullable=True),
        sa.Column('request', sa.JSON(), nullable=False),
        sa.Column('targets', sa.JSON(), nullable=False),
        sa.Column('units_requested', sa.JSON(), nullable=True),
        sa.Column('units_resolved', sa.JSON(), nullable=True),
        sa.Column('units_effective', sa.JSON(), nullable=True),
        sa.Column('skipped', sa.JSON(), nullable=True),
        sa.Column('dpi', sa.String(8), nullable=False, server_default='on'),
        sa.Column('estimated_kopeks', sa.Integer(), nullable=True),
        sa.Column('estimate_is_exact', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('cost_kopeks', sa.Integer(), nullable=True),
        sa.Column('refunded_kopeks', sa.Integer(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('retryable', sa.Boolean(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_reachability_jobs_id', 'reachability_jobs', ['id'])
    op.create_index('ix_reachability_jobs_status', 'reachability_jobs', ['status'])
    op.create_index('ix_reachability_jobs_started_by_user_id', 'reachability_jobs', ['started_by_user_id'])
    op.create_index('ix_reachability_jobs_external_id', 'reachability_jobs', ['external_id'])
    op.create_index('ix_reachability_jobs_kind_created', 'reachability_jobs', ['kind', 'created_at'])

    op.create_table(
        'reachability_legs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('reachability_jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('target_key', sa.String(255), nullable=False),
        sa.Column('target_kind', sa.String(32), nullable=True),
        sa.Column('target_ref', sa.String(255), nullable=True),
        sa.Column('op_key', sa.String(64), nullable=False),
        sa.Column('operator', sa.String(32), nullable=True),
        sa.Column('region', sa.String(32), nullable=True),
        sa.Column('dpi', sa.String(8), nullable=True),
        sa.Column('verdict', sa.String(16), nullable=False),
        sa.Column('matches_expectation', sa.Boolean(), nullable=True),
        sa.Column('raw', sa.JSON(), nullable=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_reachability_legs_id', 'reachability_legs', ['id'])
    op.create_index('ix_reachability_legs_job_id', 'reachability_legs', ['job_id'])
    op.create_index('ix_reachability_legs_target_unit_time', 'reachability_legs', ['target_key', 'op_key', 'checked_at'])

    op.create_table(
        'reachability_target_prefs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('target_kind', sa.String(32), nullable=False),
        sa.Column('target_ref', sa.String(255), nullable=False),
        sa.Column('purpose', sa.String(16), nullable=False, server_default='unknown'),
        sa.Column('excluded', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('updated_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('target_kind', 'target_ref', name='uq_reachability_target_prefs_target'),
    )
    op.create_index('ix_reachability_target_prefs_id', 'reachability_target_prefs', ['id'])


def downgrade() -> None:
    op.drop_table('reachability_target_prefs')
    op.drop_table('reachability_legs')
    op.drop_table('reachability_jobs')
```

- [ ] **Step 5: CRUD** `app/database/crud/reachability.py`

```python
"""CRUD раздела «Доступность из РФ»: задачи, леги, предпочтения по целям."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import ReachabilityJob, ReachabilityLeg, ReachabilityTargetPref


JOB_STATUSES = ('pending', 'running', 'done', 'failed', 'cancelled')
ACTIVE_STATUSES = ('pending', 'running')
TERMINAL_STATUSES = ('done', 'failed', 'cancelled')


async def create_job(db: AsyncSession, **fields: Any) -> ReachabilityJob:
    job = ReachabilityJob(**fields)
    db.add(job)
    await db.flush()
    return job


async def get_job(db: AsyncSession, job_id: int) -> ReachabilityJob | None:
    result = await db.execute(
        select(ReachabilityJob).options(selectinload(ReachabilityJob.legs)).where(ReachabilityJob.id == job_id)
    )
    return result.scalar_one_or_none()


async def update_job(db: AsyncSession, job: ReachabilityJob, **fields: Any) -> ReachabilityJob:
    for name, value in fields.items():
        setattr(job, name, value)
    job.updated_at = datetime.now(UTC)
    await db.flush()
    return job


async def list_jobs(
    db: AsyncSession,
    *,
    kind: str | None = None,
    status: str | None = None,
    target_key: str | None = None,
    user_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[ReachabilityJob], int]:
    conditions = []
    if kind:
        conditions.append(ReachabilityJob.kind == kind)
    if status:
        conditions.append(ReachabilityJob.status == status)
    if user_id is not None:
        conditions.append(ReachabilityJob.started_by_user_id == user_id)
    if target_key:
        # targets — JSON-список; ищем подстроку "target_key":"…" в его текстовом виде,
        # это работает одинаково в SQLite и PostgreSQL без JSON-операторов.
        conditions.append(func.cast(ReachabilityJob.targets, _text_type()).like(f'%"target_key": "{target_key}"%'))
    where = and_(*conditions) if conditions else True

    total = (await db.execute(select(func.count()).select_from(ReachabilityJob).where(where))).scalar_one()
    rows = await db.execute(
        select(ReachabilityJob)
        .options(selectinload(ReachabilityJob.legs))
        .where(where)
        .order_by(ReachabilityJob.created_at.desc(), ReachabilityJob.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), int(total)


def _text_type():
    from sqlalchemy import Text

    return Text


async def get_active_job(db: AsyncSession, kind: str) -> ReachabilityJob | None:
    result = await db.execute(
        select(ReachabilityJob)
        .where(ReachabilityJob.kind == kind, ReachabilityJob.status.in_(ACTIVE_STATUSES))
        .order_by(ReachabilityJob.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_unfinished_jobs(db: AsyncSession) -> list[ReachabilityJob]:
    result = await db.execute(
        select(ReachabilityJob).where(ReachabilityJob.status.in_(ACTIVE_STATUSES)).order_by(ReachabilityJob.id)
    )
    return list(result.scalars().all())


async def replace_legs(db: AsyncSession, job_id: int, legs: list[dict[str, Any]]) -> list[ReachabilityLeg]:
    await db.execute(delete(ReachabilityLeg).where(ReachabilityLeg.job_id == job_id))
    rows = [ReachabilityLeg(job_id=job_id, **leg) for leg in legs]
    db.add_all(rows)
    await db.flush()
    return rows


async def latest_legs(
    db: AsyncSession, *, target_kind: str | None = None, dpi: str | None = None
) -> list[ReachabilityLeg]:
    """Последний лег на каждую пару (target_key, op_key)."""
    newest = (
        select(
            ReachabilityLeg.target_key.label('target_key'),
            ReachabilityLeg.op_key.label('op_key'),
            func.max(ReachabilityLeg.checked_at).label('checked_at'),
        )
        .group_by(ReachabilityLeg.target_key, ReachabilityLeg.op_key)
        .subquery()
    )
    query = select(ReachabilityLeg).join(
        newest,
        and_(
            ReachabilityLeg.target_key == newest.c.target_key,
            ReachabilityLeg.op_key == newest.c.op_key,
            ReachabilityLeg.checked_at == newest.c.checked_at,
        ),
    )
    if target_kind:
        query = query.where(ReachabilityLeg.target_kind == target_kind)
    if dpi:
        query = query.where(ReachabilityLeg.dpi == dpi)
    result = await db.execute(query.order_by(ReachabilityLeg.target_key, ReachabilityLeg.op_key))
    return list(result.scalars().all())


async def get_pref(db: AsyncSession, target_kind: str, target_ref: str) -> ReachabilityTargetPref | None:
    result = await db.execute(
        select(ReachabilityTargetPref).where(
            ReachabilityTargetPref.target_kind == target_kind, ReachabilityTargetPref.target_ref == target_ref
        )
    )
    return result.scalar_one_or_none()


async def upsert_pref(
    db: AsyncSession,
    *,
    target_kind: str,
    target_ref: str,
    purpose: str | None = None,
    excluded: bool | None = None,
    note: str | None = None,
    user_id: int | None = None,
) -> ReachabilityTargetPref:
    pref = await get_pref(db, target_kind, target_ref)
    if pref is None:
        pref = ReachabilityTargetPref(target_kind=target_kind, target_ref=target_ref)
        db.add(pref)
    if purpose is not None:
        pref.purpose = purpose
    if excluded is not None:
        pref.excluded = excluded
    if note is not None:
        pref.note = note
    pref.updated_by_user_id = user_id
    pref.updated_at = datetime.now(UTC)
    await db.flush()
    return pref


async def list_prefs(db: AsyncSession) -> list[ReachabilityTargetPref]:
    result = await db.execute(select(ReachabilityTargetPref).order_by(ReachabilityTargetPref.id))
    return list(result.scalars().all())


async def last_vless_leg_price_kopeks(db: AsyncSession) -> int | None:
    """Цена одного лега VLESS по последней завершённой задаче (cost / (серверы × симки))."""
    result = await db.execute(
        select(ReachabilityJob)
        .where(ReachabilityJob.kind == 'vless', ReachabilityJob.status == 'done', ReachabilityJob.cost_kopeks.is_not(None))
        .order_by(ReachabilityJob.finished_at.desc().nullslast(), ReachabilityJob.id.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job is None or not job.result:
        return None
    # Сервис задач хранит ответ на запуск под ключом "submit"; допускаем и плоскую форму.
    submit = job.result.get('submit') or job.result
    legs = int(submit.get('n_servers') or 0) * int(submit.get('n_modems') or 0)
    return round(job.cost_kopeks / legs) if legs else None
```

Примечание: `nullslast()` в SQLite поддерживается начиная с 3.30; если тест на SQLite упадёт на нём — заменить порядок на `ReachabilityJob.id.desc()`.

- [ ] **Step 6: Прогнать**

Run: `uv run pytest tests/database/test_reachability_crud.py -q`
Expected: PASS. Затем проверить миграцию: `uv run alembic -c alembic.ini heads` показывает `0115`.

- [ ] **Step 7: Коммит**

```bash
uv run ruff format app/database/models.py app/database/crud/reachability.py migrations/alembic/versions/0115_create_reachability_tables.py tests/database/test_reachability_crud.py && uv run ruff check app/database/crud/reachability.py migrations/alembic/versions/0115_create_reachability_tables.py tests/database/test_reachability_crud.py
git checkout uv.lock 2>/dev/null
git add app/database/models.py app/database/crud/reachability.py migrations/alembic/versions/0115_create_reachability_tables.py tests/database/test_reachability_crud.py
git commit -m "feat(reachability): таблицы задач, легов и предпочтений целей с CRUD и сводкой"
```

---

### Task 5: Разбор ссылок конфигов

**Files:**
- Create: `app/services/reachability/__init__.py` (docstring «Доступность из РФ: домен интеграции bschekbot»)
- Create: `app/services/reachability/links.py`
- Test: `tests/services/reachability/__init__.py` (пустой), `tests/services/reachability/test_links.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) ParsedLink(protocol: str, address: str, port: int, sni: str | None, name: str, raw: str)`; `@dataclass(frozen=True) RejectedLink(raw: str, reason: str)` с причинами `'stub' | 'unsupported_scheme' | 'malformed'`; `parse_links(text: str) -> tuple[list[ParsedLink], list[RejectedLink]]`; `MAX_CONFIGS_PER_TEST = 20`.

- [ ] **Step 1: Падающий тест**

```python
"""Разбор ссылок конфигов из подписки панели.

API принимает vless/vmess/trojan/ss/hysteria2 и не больше 20 серверов; подписка
неизвестному клиенту отдаёт заглушки 0.0.0.0:1 — их надо отсеивать до отправки.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.services.reachability.links import MAX_CONFIGS_PER_TEST, ParsedLink, parse_links


UUID = '00000000-0000-4000-8000-000000000001'
VLESS = (
    f'vless://{UUID}@bs-host.example:9443?encryption=none&flow=xtls-rprx-vision&type=tcp'
    '&security=reality&sni=whitelisted.example&fp=firefox&pbk=PUBKEY&sid=def012#%F0%9F%87%B7%F0%9F%87%BA%20Russia'
)
TROJAN = 'trojan://pass@eu-host.example:443?security=tls&sni=eu-host.example#trojan-test'
HY2 = 'hysteria2://pass@eu-host.example:443/?sni=eu-host.example&insecure=0#hy2-test'
SS_SIP002 = f'ss://{base64.b64encode(b"chacha20-ietf-poly1305:pw").decode()}@eu-host.example:8388#ss-test'
SS_LEGACY = 'ss://' + base64.b64encode(b'aes-256-gcm:pw@eu-host.example:8388').decode() + '#ss-legacy'
VMESS = 'vmess://' + base64.b64encode(
    json.dumps({'v': '2', 'ps': 'vmess-test', 'add': 'eu-host.example', 'port': '443', 'id': UUID, 'sni': 'eu-host.example'}).encode()
).decode()
STUB = f'vless://{UUID}@0.0.0.0:1?encryption=none&type=tcp&security=none#%E2%9D%8C%20stub'


def test_parses_vless_with_sni_and_decoded_name() -> None:
    parsed, rejected = parse_links(VLESS)
    assert rejected == []
    assert parsed == [ParsedLink(protocol='vless', address='bs-host.example', port=9443, sni='whitelisted.example', name='🇷🇺 Russia', raw=VLESS)]


@pytest.mark.parametrize(
    ('link', 'protocol', 'port', 'sni', 'name'),
    [
        (TROJAN, 'trojan', 443, 'eu-host.example', 'trojan-test'),
        (HY2, 'hysteria2', 443, 'eu-host.example', 'hy2-test'),
        (SS_SIP002, 'ss', 8388, None, 'ss-test'),
        (SS_LEGACY, 'ss', 8388, None, 'ss-legacy'),
        (VMESS, 'vmess', 443, 'eu-host.example', 'vmess-test'),
    ],
)
def test_parses_other_protocols(link: str, protocol: str, port: int, sni: str | None, name: str) -> None:
    parsed, rejected = parse_links(link)
    assert rejected == []
    assert (parsed[0].protocol, parsed[0].address, parsed[0].port, parsed[0].sni, parsed[0].name) == (
        protocol, 'eu-host.example', port, sni, name,
    )


def test_stub_links_from_subscription_page_are_rejected() -> None:
    parsed, rejected = parse_links(STUB)
    assert parsed == []
    assert rejected[0].reason == 'stub'


def test_unknown_scheme_and_garbage_are_rejected_with_reason() -> None:
    parsed, rejected = parse_links('https://sub.example/abc\nhello world\nvless://broken')
    assert parsed == []
    assert [r.reason for r in rejected] == ['unsupported_scheme', 'unsupported_scheme', 'malformed']


def test_multiple_lines_keep_order_and_skip_blank_lines() -> None:
    parsed, _ = parse_links(f'{VLESS}\n\n{TROJAN}\n')
    assert [p.protocol for p in parsed] == ['vless', 'trojan']


def test_max_configs_constant_matches_api_limit() -> None:
    assert MAX_CONFIGS_PER_TEST == 20
```

- [ ] **Step 2: Убедиться, что падает** — `uv run pytest tests/services/reachability/test_links.py -q` → `ModuleNotFoundError`.

- [ ] **Step 3: Реализация** `app/services/reachability/links.py`

```python
"""Разбор ссылок конфигов (vless/vmess/trojan/ss/hysteria2) из подписки панели.

Только то, что нужно для проверки: протокол, адрес, порт, SNI и имя. Сырая строка
сохраняется — в API уезжает она, а не наш разбор.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit


MAX_CONFIGS_PER_TEST = 20
SUPPORTED_SCHEMES = ('vless', 'vmess', 'trojan', 'ss', 'hysteria2', 'hy2')
STUB_HOSTS = frozenset({'0.0.0.0', '127.0.0.1', 'localhost', ''})


@dataclass(frozen=True)
class ParsedLink:
    protocol: str
    address: str
    port: int
    sni: str | None
    name: str
    raw: str


@dataclass(frozen=True)
class RejectedLink:
    raw: str
    reason: str  # stub | unsupported_scheme | malformed


def parse_links(text: str) -> tuple[list[ParsedLink], list[RejectedLink]]:
    parsed: list[ParsedLink] = []
    rejected: list[RejectedLink] = []
    for line in (raw.strip() for raw in text.splitlines()):
        if not line:
            continue
        scheme = line.split('://', 1)[0].lower() if '://' in line else ''
        if scheme not in SUPPORTED_SCHEMES:
            rejected.append(RejectedLink(line, 'unsupported_scheme'))
            continue
        link = _parse_one(scheme, line)
        if link is None:
            rejected.append(RejectedLink(line, 'malformed'))
        elif link.address.lower() in STUB_HOSTS or link.port <= 1:
            rejected.append(RejectedLink(line, 'stub'))
        else:
            parsed.append(link)
    return parsed, rejected


def _parse_one(scheme: str, raw: str) -> ParsedLink | None:
    if scheme == 'vmess':
        return _parse_vmess(raw)
    if scheme == 'ss':
        return _parse_ss(raw)
    return _parse_url_like('hysteria2' if scheme == 'hy2' else scheme, raw)


def _parse_url_like(protocol: str, raw: str) -> ParsedLink | None:
    parts = urlsplit(raw)
    if not parts.hostname or parts.port is None:
        return None
    query = parse_qs(parts.query)
    sni = (query.get('sni') or query.get('peer') or query.get('host') or [None])[0]
    return ParsedLink(protocol, parts.hostname, parts.port, sni or None, unquote(parts.fragment), raw)


def _b64(value: str) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None


def _parse_vmess(raw: str) -> ParsedLink | None:
    payload = _b64(raw.split('://', 1)[1].split('#', 1)[0])
    try:
        data = json.loads(payload or b'')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or not data.get('add'):
        return None
    try:
        port = int(data.get('port'))
    except (TypeError, ValueError):
        return None
    return ParsedLink('vmess', str(data['add']), port, data.get('sni') or data.get('host') or None, str(data.get('ps') or ''), raw)


def _parse_ss(raw: str) -> ParsedLink | None:
    body, _, fragment = raw.split('://', 1)[1].partition('#')
    if '@' not in body:
        decoded = _b64(body.split('?', 1)[0])
        if decoded is None:
            return None
        body = decoded.decode('utf-8', errors='replace')
    if '@' not in body:
        return None
    hostport = body.rsplit('@', 1)[1].split('?', 1)[0].split('/', 1)[0]
    host, _, port_text = hostport.rpartition(':')
    if not host or not port_text.isdigit():
        return None
    return ParsedLink('ss', host, int(port_text), None, unquote(fragment), raw)
```

`__init__.py` пакета:

```python
"""«Доступность из РФ» — домен интеграции bschekbot API (проверки через симки операторов РФ)."""
```

- [ ] **Step 4: Прогнать** — `uv run pytest tests/services/reachability/test_links.py -q` → PASS.

- [ ] **Step 5: Коммит**

```bash
uv run ruff format app/services/reachability tests/services/reachability && uv run ruff check app/services/reachability tests/services/reachability
git checkout uv.lock 2>/dev/null
git add app/services/reachability/__init__.py app/services/reachability/links.py tests/services/reachability
git commit -m "feat(reachability): разбор ссылок vless/vmess/trojan/ss/hysteria2"
```

---

### Task 6: Нормализация целей, назначение, подсети

**Files:**
- Create: `app/services/reachability/targets.py`
- Test: `tests/services/reachability/test_targets.py`

**Interfaces:**
- Produces:
  - константы `KIND_HOST='host'`, `KIND_NODE='node'`, `KIND_SUBSCRIPTION_CONFIG='subscription_config'`, `KIND_CUSTOM='custom'`, `KIND_CIDR='cidr'`; `PURPOSE_BS='bs'`, `PURPOSE_REGULAR='regular'`, `PURPOSE_UNKNOWN='unknown'`.
  - `class TargetValidationError(ValueError)` с русским сообщением.
  - `@dataclass(frozen=True) Target(kind, label, address, port: int | None, target_key, sni, ref: dict, purpose, raw_link: str | None = None)` + метод `as_dict() -> dict` и `Target.from_dict(d)`.
  - `target_key(address: str, port: int | None) -> str` — нижний регистр, `адрес:порт` или `адрес`.
  - `normalize_custom_target(value: str) -> Target` — принимает IP, домен, `адрес:порт`, `http(s)://…`; отбрасывает схему, режет приватные/loopback/link-local/multicast/reserved; порт 1..65535.
  - `is_reality_like(address: str, sni: str | None) -> bool` — SNI задан, не равен адресу и не его поддомен.
  - `guess_purpose(*, address, sni, remark=None, tag=None) -> str`.
  - `validate_cidr24(value: str) -> str`, `cidr24_for_ip(ip: str) -> str`.
  - `hosts_for_node(hosts: list[RemnaWaveHost], node_active_inbounds: list[str], node_address: str, node_ips: list[str]) -> list[RemnaWaveHost]`.
  - `probe_api_target(target: Target) -> str` — строка цели для API (`адрес:порт` либо `адрес`).

- [ ] **Step 1: Падающий тест**

```python
"""Цели проверки: нормализация ввода, ключ цели, назначение, /24, связь хост → нода."""

from __future__ import annotations

import pytest

from app.external.remnawave_api import RemnaWaveHost
from app.services.reachability.targets import (
    PURPOSE_BS,
    PURPOSE_REGULAR,
    Target,
    TargetValidationError,
    cidr24_for_ip,
    guess_purpose,
    hosts_for_node,
    is_reality_like,
    normalize_custom_target,
    probe_api_target,
    target_key,
    validate_cidr24,
)


@pytest.mark.parametrize(
    ('value', 'address', 'port'),
    [
        ('BS-Host.Example:9443', 'bs-host.example', 9443),
        ('eu-host.example', 'eu-host.example', None),
        ('https://eu-host.example/', 'eu-host.example', 443),
        ('http://eu-host.example:8080/path', 'eu-host.example', 8080),
        ('192.0.2.142:443', '192.0.2.142', 443),
        ('  198.51.100.44  ', '198.51.100.44', None),
        ('[2001:db8::1]:443', '2001:db8::1', 443),
    ],
)
def test_normalize_custom_target(value: str, address: str, port: int | None) -> None:
    target = normalize_custom_target(value)
    assert (target.kind, target.address, target.port) == ('custom', address, port)
    assert target.target_key == target_key(address, port)


@pytest.mark.parametrize('value', ['127.0.0.1', '10.0.0.1:443', 'localhost', '169.254.1.1', '224.0.0.1', '0.0.0.0', 'host:99999', 'host:0', '', 'a b.example', 'vless://x@y:1'])
def test_normalize_rejects_private_and_malformed(value: str) -> None:
    with pytest.raises(TargetValidationError):
        normalize_custom_target(value)


def test_target_key_is_lowercase_with_optional_port() -> None:
    assert target_key('BS-Host.Example', 9443) == 'bs-host.example:9443'
    assert target_key('EU.example', None) == 'eu.example'


def test_probe_api_target_keeps_port() -> None:
    assert probe_api_target(normalize_custom_target('bs-host.example:9443')) == 'bs-host.example:9443'
    assert probe_api_target(normalize_custom_target('eu-host.example')) == 'eu-host.example'


@pytest.mark.parametrize(
    ('address', 'sni', 'expected'),
    [
        ('bs-host.example', 'whitelisted.example', True),
        ('eu-host.example', 'eu-host.example', False),
        ('eu-host.example', 'cdn.eu-host.example', False),
        ('eu-host.example', None, False),
        ('192.0.2.1', 'whitelisted.example', True),
    ],
)
def test_is_reality_like(address: str, sni: str | None, expected: bool) -> None:
    assert is_reality_like(address, sni) is expected


@pytest.mark.parametrize(
    ('kwargs', 'expected'),
    [
        ({'address': 'bs-host.example', 'sni': 'whitelisted.example'}, PURPOSE_BS),
        ({'address': 'eu-host.example', 'sni': 'eu-host.example', 'remark': '🇩🇪 Germany'}, PURPOSE_REGULAR),
        ({'address': 'eu-host.example', 'sni': 'eu-host.example', 'remark': 'Russia | LTE | БС'}, PURPOSE_BS),
        ({'address': 'eu-host.example', 'sni': None, 'tag': 'BS'}, PURPOSE_BS),
    ],
)
def test_guess_purpose(kwargs: dict, expected: str) -> None:
    assert guess_purpose(**kwargs) == expected


def test_cidr_helpers() -> None:
    assert validate_cidr24('192.0.2.0/24') == '192.0.2.0/24'
    assert validate_cidr24('192.0.2.77/24') == '192.0.2.0/24'
    assert cidr24_for_ip('192.0.2.142') == '192.0.2.0/24'
    for bad in ('192.0.2.0/23', '192.0.2.0/25', '10.0.0.0/24', 'nope', '2001:db8::/24'):
        with pytest.raises(TargetValidationError):
            validate_cidr24(bad)


def _host(uuid: str, address: str, inbound: str | None) -> RemnaWaveHost:
    return RemnaWaveHost(uuid=uuid, remark=uuid, address=address, port=443, config_profile_inbound_uuid=inbound)


def test_hosts_for_node_matches_by_inbound_then_by_address() -> None:
    hosts = [_host('a', 'a.example', 'in-1'), _host('b', 'b.example', 'in-9'), _host('c', '192.0.2.5', None)]
    matched = hosts_for_node(hosts, node_active_inbounds=['in-1'], node_address='192.0.2.5', node_ips=[])
    assert [h.uuid for h in matched] == ['a', 'c']
    matched = hosts_for_node(hosts, node_active_inbounds=[], node_address='x', node_ips=['192.0.2.5'])
    assert [h.uuid for h in matched] == ['c']


def test_target_round_trips_through_dict() -> None:
    target = normalize_custom_target('bs-host.example:9443')
    assert Target.from_dict(target.as_dict()) == target
```

- [ ] **Step 2: Убедиться, что падает** — `uv run pytest tests/services/reachability/test_targets.py -q`.

- [ ] **Step 3: Реализация** `app/services/reachability/targets.py`

```python
"""Цели проверки: единый формат, нормализация ввода, назначение, подсети /24.

Любой источник (хост панели, нода, конфиг подписки, произвольный ввод, подсеть)
приводится к :class:`Target`, дальше сервису безразлично, откуда цель пришла.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from app.external.remnawave_api import RemnaWaveHost


KIND_HOST = 'host'
KIND_NODE = 'node'
KIND_SUBSCRIPTION_CONFIG = 'subscription_config'
KIND_CUSTOM = 'custom'
KIND_CIDR = 'cidr'

PURPOSE_BS = 'bs'
PURPOSE_REGULAR = 'regular'
PURPOSE_UNKNOWN = 'unknown'
PURPOSES = (PURPOSE_BS, PURPOSE_REGULAR, PURPOSE_UNKNOWN)

_BS_MARKERS = ('бс', 'bs', 'lte', 'whitelist', 'белый')
_HOSTNAME_RE = re.compile(r'^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')


class TargetValidationError(ValueError):
    """Цель не годится для проверки; сообщение — для админа, по-русски."""


@dataclass(frozen=True)
class Target:
    kind: str
    label: str
    address: str
    port: int | None
    target_key: str
    sni: str | None
    ref: dict = field(default_factory=dict)
    purpose: str = PURPOSE_UNKNOWN
    raw_link: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Target:
        return cls(**{key: data.get(key) for key in cls.__dataclass_fields__})


def target_key(address: str, port: int | None) -> str:
    address = address.lower()
    return f'{address}:{port}' if port else address


def probe_api_target(target: Target) -> str:
    """Строка цели для API: IP/домен с портом либо без."""
    return target_key(target.address, target.port)


def _check_public(address: str) -> None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        if address == 'localhost' or not _HOSTNAME_RE.match(address):
            raise TargetValidationError(f'«{address}» не похоже на IP-адрес или домен') from None
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise TargetValidationError(f'{address} — служебный адрес, такие цели API не проверяет')


def _port(value: str | int | None) -> int | None:
    if value is None or value == '':
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise TargetValidationError(f'Порт «{value}» не число') from None
    if not 1 <= port <= 65535:
        raise TargetValidationError(f'Порт {port} вне диапазона 1–65535')
    return port


def normalize_custom_target(value: str) -> Target:
    """IP, домен, адрес:порт или URL → цель. Схема отбрасывается (HTTP-проба у API с http:// не работает)."""
    text = (value or '').strip()
    if not text:
        raise TargetValidationError('Пустая цель')
    if '://' in text:
        scheme = text.split('://', 1)[0].lower()
        if scheme not in ('http', 'https'):
            raise TargetValidationError('Ссылки конфигов проверяются VLESS-тестом, а не probe')
        parts = urlsplit(text)
        host, port = parts.hostname, parts.port or (443 if scheme == 'https' else 80)
    else:
        parts = urlsplit(f'//{text}')
        try:
            host, port = parts.hostname, parts.port
        except ValueError:
            raise TargetValidationError(f'Не удалось разобрать порт в «{text}»') from None
        if parts.path or parts.query:
            raise TargetValidationError(f'«{text}» содержит лишнее: нужен адрес и, при необходимости, порт')
    if not host:
        raise TargetValidationError(f'В «{text}» нет адреса')
    host = host.lower()
    _check_public(host)
    port = _port(port)
    return Target(kind=KIND_CUSTOM, label=text, address=host, port=port, target_key=target_key(host, port), sni=None)


def is_reality_like(address: str, sni: str | None) -> bool:
    """SNI чужого домена — признак Reality с dest на «белом» сайте."""
    if not sni:
        return False
    address, sni = address.lower(), sni.lower()
    return sni != address and not sni.endswith(f'.{address}') and not address.endswith(f'.{sni}')


def guess_purpose(*, address: str, sni: str | None, remark: str | None = None, tag: str | None = None) -> str:
    text = f'{remark or ""} {tag or ""}'.lower()
    if is_reality_like(address, sni) or any(marker in text for marker in _BS_MARKERS):
        return PURPOSE_BS
    return PURPOSE_REGULAR


def validate_cidr24(value: str) -> str:
    try:
        network = ipaddress.ip_network((value or '').strip(), strict=False)
    except ValueError:
        raise TargetValidationError(f'«{value}» не похоже на подсеть') from None
    if network.version != 4 or network.prefixlen != 24:
        raise TargetValidationError('API сканирует ровно одну подсеть /24 (IPv4)')
    if not network.is_global:
        raise TargetValidationError(f'{network} — служебная подсеть')
    return str(network)


def cidr24_for_ip(ip: str) -> str:
    return validate_cidr24(f'{ip}/24')


def hosts_for_node(
    hosts: list[RemnaWaveHost], *, node_active_inbounds: list[str], node_address: str, node_ips: list[str]
) -> list[RemnaWaveHost]:
    """Хосты ноды: по инбаунду, а без него — по совпадению адреса с адресом/IP ноды."""
    inbounds = set(node_active_inbounds or [])
    addresses = {node_address.lower(), *(ip.lower() for ip in node_ips or [])}
    return [
        host
        for host in hosts
        if (host.config_profile_inbound_uuid and host.config_profile_inbound_uuid in inbounds)
        or host.address.lower() in addresses
    ]
```

- [ ] **Step 4: Прогнать** — PASS. Если `urlsplit('//host:99999').port` кидает `ValueError` — тест `host:99999` ожидает `TargetValidationError`, это уже учтено `try/except`.

- [ ] **Step 5: Коммит**

```bash
uv run ruff format app/services/reachability/targets.py tests/services/reachability/test_targets.py && uv run ruff check app/services/reachability/targets.py tests/services/reachability/test_targets.py
git checkout uv.lock 2>/dev/null
git add app/services/reachability/targets.py tests/services/reachability/test_targets.py
git commit -m "feat(reachability): нормализация целей, назначение хоста и подсети /24"
```

---

### Task 7: Вердикт и ожидание

**Files:**
- Create: `app/services/reachability/verdict.py`
- Test: `tests/services/reachability/test_verdict.py`

**Interfaces:**
- Produces: константы `REACHABLE='reachable'`, `BLOCKED='blocked'`, `DOWN='down'`, `UNKNOWN='unknown'`, `CANCELLED='cancelled'`; `probe_leg_verdict(leg: dict, *, sni_host: str | None = None, reality: bool = False) -> str`; `vless_leg_verdict(leg: dict) -> str`; `matches_expectation(verdict: str, purpose: str, dpi: str) -> bool | None`.

- [ ] **Step 1: Падающий тест на легах из фикстур**

```python
"""Вердикт лега — чистая функция на записанных легах живого API.

Ключевые случаи: Reality-хост даёт ложный «blocked» на cert-validated TCP (решает
SNI-проба с настоящим SNI); у одной симки TCP проходит, а SNI режется; провал VLESS
с поднятым туннелем — это «режется», без TCP — «недоступен».
"""

from __future__ import annotations

import pytest

from app.services.reachability.verdict import (
    BLOCKED,
    CANCELLED,
    DOWN,
    REACHABLE,
    UNKNOWN,
    matches_expectation,
    probe_leg_verdict,
    vless_leg_verdict,
)
from tests.fixtures.bschek_fixtures import load_bschek_fixture


def _probe_leg(fixture: str, target: str, op_key: str) -> dict:
    return load_bschek_fixture(fixture)['body']['by_target'][target]['by_operator'][op_key]


BS = 'bs-host.example:9443'
EU = 'eu-host.example'


@pytest.mark.parametrize(
    ('fixture', 'target', 'op_key', 'sni_host', 'reality', 'expected'),
    [
        ('p2_replay', BS, 'tele2|цфо|on', 'whitelisted.example', True, REACHABLE),  # tcp blocked, sni alive
        ('p2_replay', BS, 'sberm|цфо|on', 'whitelisted.example', True, REACHABLE),  # чужое sni режется, своё живо
        ('p2_replay', BS, 't-mobile|цфо|on', 'whitelisted.example', True, DOWN),  # сам IP недоступен
        ('p2_replay', EU, 'tele2|цфо|on', 'eu-host.example', False, BLOCKED),  # handshake timeout
        ('p2_replay', EU, 'sberm|цфо|on', 'eu-host.example', False, BLOCKED),  # refused
        ('p2_replay', EU, 'dobro|цфо|on', 'eu-host.example', False, DOWN),
        ('pF_replay_late', BS, 'yota|цфо|on', 'whitelisted.example', True, BLOCKED),  # tcp ok, sni blocked
        ('pF_replay_late', BS, 'rtk|пфо|on', 'whitelisted.example', True, REACHABLE),
        ('pF_replay_late', BS, 'mts|пфо|on', 'whitelisted.example', True, DOWN),
        ('p1_probe', EU, 'mts|пфо|on', None, False, DOWN),  # tls verdict down, без sni
        ('p4_bare_mts_any', '1.1.1.1', 'mts|цфо|off', None, False, REACHABLE),
        ('p4_bare_mts_any', '1.1.1.1', 'mts|дфо|off', None, False, REACHABLE),
        ('p4_bare_mts_any', '1.1.1.1', 'mts|пфо|on', None, False, DOWN),
    ],
)
def test_probe_leg_verdict_on_recorded_legs(fixture, target, op_key, sni_host, reality, expected) -> None:
    assert probe_leg_verdict(_probe_leg(fixture, target, op_key), sni_host=sni_host, reality=reality) == expected


def test_reality_tls_blocked_without_sni_probe_is_unknown() -> None:
    leg = {'ok': True, 'tcp_is_tls': True, 'tcp': {'ok': False, 'verdict': 'blocked', 'cert_names': ['CN=*.whitelisted.example']}, 'sni': None}
    assert probe_leg_verdict(leg, reality=True) == UNKNOWN
    assert probe_leg_verdict(leg, reality=False) == BLOCKED


def test_probe_leg_not_executed_is_unknown() -> None:
    assert probe_leg_verdict({'ok': False, 'error': 'modem lost'}) == UNKNOWN


def test_icmp_only_probe() -> None:
    assert probe_leg_verdict({'ok': True, 'icmp': {'ok': True}, 'tcp': None, 'sni': None}) == REACHABLE
    assert probe_leg_verdict({'ok': True, 'icmp': {'ok': False}, 'tcp': None, 'sni': None}) == DOWN


def _vless_leg(fixture: str, index: int = 0) -> dict:
    return load_bschek_fixture(fixture)['body']['result'][index]


def test_vless_verdicts_on_recorded_legs() -> None:
    assert vless_leg_verdict(_vless_leg('v1_poll_12')) == REACHABLE
    vb = load_bschek_fixture('vB_poll_34')['body']['result'][0]
    assert vb['fail_reason'] == 'zombie_tcp' and vless_leg_verdict(vb) == BLOCKED
    assert vless_leg_verdict(_vless_leg('vC_after_cancel')) == CANCELLED


def test_vless_other_protocol_fail_reasons() -> None:
    legs = {leg['protocol']: leg for leg in load_bschek_fixture('vD_poll_19')['body']['result']}
    assert vless_leg_verdict(legs['vmess']) == DOWN  # tcp_timeout
    assert vless_leg_verdict(legs['hysteria2']) == BLOCKED  # dataplane_dead, tunnel_up


@pytest.mark.parametrize(
    ('verdict', 'purpose', 'dpi', 'expected'),
    [
        (REACHABLE, 'bs', 'on', True),
        (BLOCKED, 'bs', 'on', False),
        (DOWN, 'bs', 'on', False),
        (UNKNOWN, 'bs', 'on', False),
        (REACHABLE, 'bs', 'off', None),
        (REACHABLE, 'regular', 'off', True),
        (BLOCKED, 'regular', 'off', False),
        (BLOCKED, 'regular', 'on', None),
        (REACHABLE, 'unknown', 'on', None),
        (CANCELLED, 'bs', 'on', None),
    ],
)
def test_matches_expectation(verdict, purpose, dpi, expected) -> None:
    assert matches_expectation(verdict, purpose, dpi) is expected
```

Имена фикстур `vB_poll_34` и `vD_poll_19` — проверить фактические имена в `tests/fixtures/bschek/` (`ls tests/fixtures/bschek | grep -E 'vB_poll|vD_poll'`) и подставить существующие.

- [ ] **Step 2: Убедиться, что падает.**

- [ ] **Step 3: Реализация** `app/services/reachability/verdict.py`

```python
"""Вердикт лега и его соответствие ожиданию.

Правила выведены из живых ответов (спец, разделы 3 и 6.4). Для хостов под Белый
список решает SNI-проба с настоящим SNI хоста: cert-validated TCP у Reality
проверяет сертификат dest и даёт ложный «blocked».
"""

from __future__ import annotations

from app.services.reachability.targets import PURPOSE_BS, PURPOSE_REGULAR


REACHABLE = 'reachable'
BLOCKED = 'blocked'
DOWN = 'down'
UNKNOWN = 'unknown'
CANCELLED = 'cancelled'


def _pick_sni(entries: list[dict] | None, sni_host: str | None) -> dict | None:
    if not entries:
        return None
    if sni_host:
        for entry in entries:
            if str(entry.get('host', '')).lower() == sni_host.lower():
                return entry
    return entries[0] if sni_host is None else None


def probe_leg_verdict(leg: dict, *, sni_host: str | None = None, reality: bool = False) -> str:
    if not leg.get('ok'):
        return UNKNOWN

    sni = _pick_sni(leg.get('sni'), sni_host)
    if sni is not None:
        if sni.get('ok') or sni.get('verdict') == 'alive':
            return REACHABLE
        if sni.get('verdict') in ('blocked', 'refused'):
            return BLOCKED

    tcp = leg.get('tcp')
    if tcp:
        if tcp.get('ok'):
            return REACHABLE
        verdict = tcp.get('verdict')
        if verdict == 'refused':
            return BLOCKED
        if verdict == 'blocked':
            if leg.get('tcp_is_tls') and reality:
                return DOWN if sni is not None else UNKNOWN
            return BLOCKED
        return DOWN

    icmp = leg.get('icmp')
    if icmp:
        return REACHABLE if icmp.get('ok') else DOWN
    return UNKNOWN


def vless_leg_verdict(leg: dict) -> str:
    if leg.get('cancelled') or leg.get('stage') == 'cancelled':
        return CANCELLED
    targets = leg.get('targets') or []
    any_target_ok = any(target.get('ok') for target in targets)
    if leg.get('ok') and leg.get('tunnel_up') and (any_target_ok or not targets):
        return REACHABLE
    if leg.get('tunnel_up'):
        return BLOCKED
    if leg.get('tcp_ok') is False:
        return DOWN
    return UNKNOWN


def matches_expectation(verdict: str, purpose: str, dpi: str) -> bool | None:
    """True/False, когда ожидание есть; None — справочная строка без ожидания."""
    if verdict == CANCELLED:
        return None
    expected_dpi = {PURPOSE_BS: 'on', PURPOSE_REGULAR: 'off'}.get(purpose)
    if expected_dpi is None or dpi != expected_dpi:
        return None
    return verdict == REACHABLE
```

- [ ] **Step 4: Прогнать** — PASS.

- [ ] **Step 5: Коммит**

```bash
uv run ruff format app/services/reachability/verdict.py tests/services/reachability/test_verdict.py && uv run ruff check app/services/reachability/verdict.py tests/services/reachability/test_verdict.py
git checkout uv.lock 2>/dev/null
git add app/services/reachability/verdict.py tests/services/reachability/test_verdict.py
git commit -m "feat(reachability): вердикт лега и соответствие ожиданию по назначению хоста"
```

---

### Task 8: Каталог симок: селекторы, раскрытие, пропуски, кэш

**Files:**
- Create: `app/services/reachability/units.py`
- Test: `tests/services/reachability/test_units.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Unit(op_key, operator, name, region, region_code, dpi, channel_state, probeable)`; `Unit.as_dict()`.
  - `class SelectorError(ValueError)`.
  - `@dataclass(frozen=True) Selector(operator: str | None, region: str | None, dpi: str | None)`; `parse_selector(raw: str) -> Selector`.
  - `@dataclass Expansion(resolved: list[str], skipped_dpi_off: list[Unit], skipped_unavailable: list[Unit], unknown: list[str])`.
  - `class UnitsCatalog(units: list[Unit], fetched_at: float)`: `from_response(payload, fetched_at)`, `expand(selectors: list[str], dpi: str) -> Expansion`, `by_key: dict[str, Unit]`.
  - `class UnitsCache(fetch: Callable[[], Awaitable[dict]], ttl: float = 60.0, clock=time.monotonic)`: `async get(force=False) -> UnitsCatalog`.

- [ ] **Step 1: Падающий тест**

```python
"""Каталог симок: раскрытие селекторов по живому списку и расчёт пропусков.

Preview API не отдаёт skipped_*, а голый оператор в probe не даёт skipped_dpi_off,
поэтому «что заказано, но не пошло» считаем сами. Неизвестный оператор у API — 503,
у нас — ошибка валидации ДО траты денег.
"""

from __future__ import annotations

import pytest

from app.services.reachability.units import Selector, SelectorError, UnitsCache, UnitsCatalog, parse_selector
from tests.fixtures.bschek_fixtures import load_bschek_fixture


@pytest.fixture
def catalog() -> UnitsCatalog:
    return UnitsCatalog.from_response(load_bschek_fixture('operators')['body'], fetched_at=0.0)


def test_catalog_from_response(catalog: UnitsCatalog) -> None:
    assert len(catalog.units) == 30
    unit = catalog.by_key['mts|цфо|off']
    assert (unit.operator, unit.region, unit.region_code, unit.dpi, unit.probeable) == ('mts', 'ЦФО', 'cfo', 'off', True)


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('mts', Selector('mts', None, None)),
        ('mts|цфо|off', Selector('mts', 'цфо', 'off')),
        ('mts|*|on', Selector('mts', None, 'on')),
        ('mts||on', Selector('mts', None, 'on')),
        ('*|цфо|on', Selector(None, 'цфо', 'on')),
        ('|цфо|on', Selector(None, 'цфо', 'on')),
        ('MTS|CFO|OFF', Selector('mts', 'cfo', 'off')),
        ('*|*|off', Selector(None, None, 'off')),
    ],
)
def test_parse_selector(raw: str, expected: Selector) -> None:
    assert parse_selector(raw) == expected


@pytest.mark.parametrize('raw', ['', '*|*|*', 'mts|цфо|off|extra', 'ufo1:mts', 'mts|цфо|maybe'])
def test_parse_selector_rejects(raw: str) -> None:
    with pytest.raises(SelectorError):
        parse_selector(raw)


def test_expand_bare_operator_with_dpi_on_skips_off_units(catalog: UnitsCatalog) -> None:
    result = catalog.expand(['mts'], dpi='on')
    assert result.resolved == ['mts|пфо|on']
    assert sorted(u.op_key for u in result.skipped_dpi_off) == ['mts|дфо|off', 'mts|цфо|off']
    assert result.unknown == []


def test_expand_region_selector_and_latin_code(catalog: UnitsCatalog) -> None:
    assert catalog.expand(['*|цфо|on'], dpi='on').resolved == ['megafon|цфо|on', 'tele2|цфо|on', 't-mobile|цфо|on', 'dobro|цфо|on', 'sberm|цфо|on']
    assert catalog.expand(['mts|cfo|off'], dpi='off').resolved == ['mts|цфо|off']


def test_expand_any_keeps_both_groups_and_dedups(catalog: UnitsCatalog) -> None:
    result = catalog.expand(['mts', 'mts|цфо|off'], dpi='any')
    assert result.resolved == ['mts|цфо|off', 'mts|дфо|off', 'mts|пфо|on']


def test_expand_empty_means_whole_fleet_by_dpi(catalog: UnitsCatalog) -> None:
    assert len(catalog.expand([], dpi='on').resolved) == 15
    assert len(catalog.expand([], dpi='any').resolved) == 30


def test_expand_reports_unknown_selectors_instead_of_dropping_them(catalog: UnitsCatalog) -> None:
    result = catalog.expand(['nokia|цфо|on', 'mts|пфо|on'], dpi='on')
    assert result.unknown == ['nokia|цфо|on']
    assert result.resolved == ['mts|пфо|on']


def test_expand_marks_non_probeable_units_unavailable() -> None:
    payload = load_bschek_fixture('operators')['body']
    payload['units'][0]['probeable'] = False
    catalog = UnitsCatalog.from_response(payload, fetched_at=0.0)
    key = payload['units'][0]['op_key']
    result = catalog.expand([key], dpi='any')
    assert result.resolved == [] and [u.op_key for u in result.skipped_unavailable] == [key]


async def test_cache_refetches_after_ttl_and_on_force() -> None:
    calls = 0
    now = [0.0]

    async def fetch() -> dict:
        nonlocal calls
        calls += 1
        return load_bschek_fixture('operators')['body']

    cache = UnitsCache(fetch, ttl=60.0, clock=lambda: now[0])
    await cache.get()
    await cache.get()
    assert calls == 1
    now[0] = 61.0
    await cache.get()
    assert calls == 2
    await cache.get(force=True)
    assert calls == 3
```

- [ ] **Step 2: Убедиться, что падает.**

- [ ] **Step 3: Реализация** `app/services/reachability/units.py`

```python
"""Симки bschekbot: каталог из GET /operators, селекторы и раскрытие с пропусками."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field


DPI_MODES = ('on', 'off', 'any')


class SelectorError(ValueError):
    """Ключ симки не разобрался — сообщение для админа."""


@dataclass(frozen=True)
class Unit:
    op_key: str
    operator: str
    name: str
    region: str
    region_code: str
    dpi: str
    channel_state: str
    probeable: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Selector:
    operator: str | None
    region: str | None
    dpi: str | None


@dataclass
class Expansion:
    resolved: list[str] = field(default_factory=list)
    skipped_dpi_off: list[Unit] = field(default_factory=list)
    skipped_unavailable: list[Unit] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def parse_selector(raw: str) -> Selector:
    text = (raw or '').strip()
    if not text:
        raise SelectorError('Пустой ключ симки')
    if ':' in text:
        raise SelectorError(f'«{text}» — старый формат ключа; нужен вид «оператор|округ|бс»')
    parts = [part.strip().lower() for part in text.split('|')]
    if len(parts) > 3:
        raise SelectorError(f'«{text}» — ключ симки состоит максимум из трёх частей')
    parts += [''] * (3 - len(parts))
    operator, region, dpi = (None if part in ('', '*') else part for part in parts)
    if dpi is not None and dpi not in ('on', 'off'):
        raise SelectorError(f'Третья часть ключа «{text}» — on, off или *')
    if operator is None and region is None and dpi is None:
        raise SelectorError('Ключ «все|все|все» не имеет смысла: оставьте список симок пустым')
    return Selector(operator, region, dpi)


def _matches(selector: Selector, unit: Unit) -> bool:
    if selector.operator and selector.operator != unit.operator.lower():
        return False
    if selector.region and selector.region not in (unit.region.lower(), unit.region_code.lower()):
        return False
    return not selector.dpi or selector.dpi == unit.dpi


class UnitsCatalog:
    def __init__(self, units: list[Unit], fetched_at: float) -> None:
        self.units = units
        self.fetched_at = fetched_at
        self.by_key = {unit.op_key: unit for unit in units}

    @classmethod
    def from_response(cls, payload: dict, fetched_at: float) -> UnitsCatalog:
        units = [
            Unit(
                op_key=str(item['op_key']),
                operator=str(item.get('operator') or ''),
                name=str(item.get('name') or item.get('operator') or ''),
                region=str(item.get('region') or ''),
                region_code=str(item.get('region_code') or ''),
                dpi=str(item.get('dpi') or ''),
                channel_state=str(item.get('channel_state') or ''),
                probeable=bool(item.get('probeable', False)),
            )
            for item in payload.get('units') or []
        ]
        return cls(units, fetched_at)

    def expand(self, selectors: list[str], dpi: str) -> Expansion:
        """Раскрыть селекторы по каталогу и отделить пропуски. Порядок — как в каталоге."""
        if dpi not in DPI_MODES:
            raise SelectorError(f'Режим Белого списка «{dpi}» — on, off или any')
        result = Expansion()
        matched: list[Unit] = []
        if selectors:
            parsed = [(raw, parse_selector(raw)) for raw in selectors]
            for raw, selector in parsed:
                hits = [unit for unit in self.units if _matches(selector, unit)]
                if not hits:
                    result.unknown.append(raw)
                matched.extend(hits)
        else:
            matched = list(self.units)

        seen: set[str] = set()
        for unit in sorted(matched, key=lambda u: self.units.index(u)):
            if unit.op_key in seen:
                continue
            seen.add(unit.op_key)
            if dpi != 'any' and unit.dpi != dpi:
                result.skipped_dpi_off.append(unit)
            elif not unit.probeable:
                result.skipped_unavailable.append(unit)
            else:
                result.resolved.append(unit.op_key)
        return result


class UnitsCache:
    """Кэш каталога: флот меняется в течение часа, поэтому TTL короткий (60 с)."""

    def __init__(self, fetch: Callable[[], Awaitable[dict]], ttl: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._catalog: UnitsCatalog | None = None

    async def get(self, force: bool = False) -> UnitsCatalog:
        now = self._clock()
        if force or self._catalog is None or now - self._catalog.fetched_at >= self._ttl:
            payload = await self._fetch()
            self._catalog = UnitsCatalog.from_response(payload, fetched_at=now)
        return self._catalog
```

- [ ] **Step 4: Прогнать** — PASS. Если порядок в `test_expand_region_selector_and_latin_code` отличается — проверить порядок `units` в фикстуре `operators.json` и поправить ожидание под каталог (порядок должен совпадать с каталогом, не с алфавитом).

- [ ] **Step 5: Коммит**

```bash
uv run ruff format app/services/reachability/units.py tests/services/reachability/test_units.py && uv run ruff check app/services/reachability/units.py tests/services/reachability/test_units.py
git checkout uv.lock 2>/dev/null
git add app/services/reachability/units.py tests/services/reachability/test_units.py
git commit -m "feat(reachability): каталог симок, селекторы и расчёт пропусков"
```

---

### Task 9: Цены

**Files:**
- Create: `app/services/reachability/pricing.py`
- Test: `tests/services/reachability/test_pricing.py`

**Interfaces:**
- Produces: `DEFAULT_VLESS_LEG_KOPEKS = 110`; `class CostLimitExceeded(Exception)` с полями `cost_kopeks`, `limit_kopeks`; `credits_to_kopeks(credits: int | None) -> int | None`; `estimate_vless_kopeks(n_servers: int, n_units: int, leg_kopeks: int | None) -> int`; `enforce_cost_limit(cost_kopeks: int | None, limit_kopeks: int) -> None`; `format_rubles(kopeks: int) -> str` («2,79 ₽»).

- [ ] **Step 1: Падающий тест**

```python
"""Деньги: 1 кредит = 1 копейка; VLESS без preview оценивается по цене лега; потолок задачи."""

import pytest

from app.services.reachability.pricing import (
    DEFAULT_VLESS_LEG_KOPEKS,
    CostLimitExceeded,
    credits_to_kopeks,
    enforce_cost_limit,
    estimate_vless_kopeks,
    format_rubles,
)


def test_credits_are_kopeks() -> None:
    assert credits_to_kopeks(279) == 279
    assert credits_to_kopeks(None) is None


def test_vless_estimate_uses_last_leg_price_or_default() -> None:
    assert estimate_vless_kopeks(2, 3, 103) == 618
    assert estimate_vless_kopeks(1, 1, None) == DEFAULT_VLESS_LEG_KOPEKS


def test_cost_limit_zero_means_unlimited() -> None:
    enforce_cost_limit(10_000_000, 0)
    enforce_cost_limit(None, 500)


def test_cost_limit_exceeded_carries_numbers() -> None:
    with pytest.raises(CostLimitExceeded) as exc:
        enforce_cost_limit(501, 500)
    assert (exc.value.cost_kopeks, exc.value.limit_kopeks) == (501, 500)
    enforce_cost_limit(500, 500)


def test_format_rubles() -> None:
    assert format_rubles(279) == '2,79 ₽'
    assert format_rubles(100000) == '1000,00 ₽'
```

- [ ] **Step 2: Падает.** **Step 3: Реализация**

```python
"""Деньги интеграции: кредиты API = копейки, оценка VLESS, потолок цены задачи."""

from __future__ import annotations


DEFAULT_VLESS_LEG_KOPEKS = 110  # наблюдалось 103 на gold (−7 %); без скидки ≈110


class CostLimitExceeded(Exception):
    def __init__(self, cost_kopeks: int, limit_kopeks: int) -> None:
        self.cost_kopeks = cost_kopeks
        self.limit_kopeks = limit_kopeks
        super().__init__(f'Цена задачи {format_rubles(cost_kopeks)} выше потолка {format_rubles(limit_kopeks)}')


def credits_to_kopeks(credits: int | None) -> int | None:
    return None if credits is None else int(credits)


def estimate_vless_kopeks(n_servers: int, n_units: int, leg_kopeks: int | None) -> int:
    return max(0, n_servers) * max(0, n_units) * (leg_kopeks or DEFAULT_VLESS_LEG_KOPEKS)


def enforce_cost_limit(cost_kopeks: int | None, limit_kopeks: int) -> None:
    if limit_kopeks > 0 and cost_kopeks is not None and cost_kopeks > limit_kopeks:
        raise CostLimitExceeded(cost_kopeks, limit_kopeks)


def format_rubles(kopeks: int) -> str:
    return f'{kopeks // 100},{kopeks % 100:02d} ₽'
```

- [ ] **Step 4: Прогнать** — PASS. **Step 5: Коммит**

```bash
uv run ruff format app/services/reachability/pricing.py tests/services/reachability/test_pricing.py && uv run ruff check app/services/reachability/pricing.py tests/services/reachability/test_pricing.py
git checkout uv.lock 2>/dev/null
git add app/services/reachability/pricing.py tests/services/reachability/test_pricing.py
git commit -m "feat(reachability): цены — кредиты в копейки, оценка VLESS, потолок задачи"
```

---

### Task 10: Шлюз платных вызовов

**Files:**
- Create: `app/services/reachability/gate.py`
- Test: `tests/services/reachability/test_gate.py`

**Interfaces:**
- Produces: `class PaidCallGate(*, min_interval: float = 1.1, max_rate_limit_retries: int = 5, clock=time.monotonic, sleep=asyncio.sleep)` с `async run(call: Callable[[], Awaitable[T]]) -> T`. Гарантии: между стартами платных вызовов ≥ `min_interval`; на `BschekAPIError(code='rate_limited')` ждёт `retry_after` (или 1 с) и повторяет тот же `call`; замок держится только на время расчёта паузы, не на время вызова.

- [ ] **Step 1: Падающий тест**

```python
"""Шлюз платных вызовов: 1 запрос в секунду на аккаунт, 429 повторяется тем же вызовом.

Замок не держится на время самого вызова: проба по всему флоту идёт минуты, и
она не должна блокировать запуск VLESS.
"""

from __future__ import annotations

import asyncio

import pytest

from app.external.bschek_api import BschekAPIError
from app.services.reachability.gate import PaidCallGate


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_spaces_calls_by_min_interval() -> None:
    clock = FakeClock()
    gate = PaidCallGate(min_interval=1.1, clock=clock, sleep=clock.sleep)
    starts: list[float] = []

    async def call() -> str:
        starts.append(clock.now)
        return 'ok'

    assert await gate.run(call) == 'ok'
    assert await gate.run(call) == 'ok'
    assert starts == [0.0, 1.1]


async def test_retries_rate_limited_with_retry_after_and_same_call() -> None:
    clock = FakeClock()
    gate = PaidCallGate(min_interval=1.1, clock=clock, sleep=clock.sleep)
    attempts = 0

    async def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BschekAPIError(code='rate_limited', message='slow down', status=429, retryable=True, retry_after=0.98)
        return 'done'

    assert await gate.run(call) == 'done'
    assert attempts == 2
    assert 0.98 in clock.sleeps


async def test_gives_up_after_max_rate_limit_retries() -> None:
    clock = FakeClock()
    gate = PaidCallGate(min_interval=0, max_rate_limit_retries=2, clock=clock, sleep=clock.sleep)

    async def call() -> str:
        raise BschekAPIError(code='rate_limited', message='x', status=429, retryable=True, retry_after=None)

    with pytest.raises(BschekAPIError) as exc:
        await gate.run(call)
    assert exc.value.code == 'rate_limited'


async def test_other_errors_pass_through_immediately() -> None:
    clock = FakeClock()
    gate = PaidCallGate(clock=clock, sleep=clock.sleep)

    async def call() -> str:
        raise BschekAPIError(code='no_dpi_on', message='x', status=400)

    with pytest.raises(BschekAPIError):
        await gate.run(call)
    assert clock.sleeps == []


async def test_lock_is_not_held_during_the_call() -> None:
    clock = FakeClock()
    gate = PaidCallGate(min_interval=0, clock=clock, sleep=clock.sleep)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def slow() -> str:
        first_started.set()
        await release_first.wait()
        return 'slow'

    async def fast() -> str:
        return 'fast'

    slow_task = asyncio.create_task(gate.run(slow))
    await first_started.wait()
    assert await asyncio.wait_for(gate.run(fast), timeout=1.0) == 'fast'
    release_first.set()
    assert await slow_task == 'slow'
```

- [ ] **Step 2: Падает.** **Step 3: Реализация** `app/services/reachability/gate.py`

```python
"""Шлюз платных POST-вызовов bschekbot: не чаще 1 запроса в секунду на аккаунт.

429 гасится здесь же: пауза по retry_after и повтор ТОГО ЖЕ вызова (тот же
Idempotency-Key внутри), админ этого не видит. Замок держится только пока
считается пауза — сам вызов может идти минуты.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.external.bschek_api import BschekAPIError


T = TypeVar('T')


class PaidCallGate:
    def __init__(
        self,
        *,
        min_interval: float = 1.1,
        max_rate_limit_retries: int = 5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._max_retries = max_rate_limit_retries
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_start: float | None = None

    async def _wait_for_slot(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._last_start is not None:
                pause = self._last_start + self._min_interval - now
                if pause > 0:
                    await self._sleep(pause)
                    now = self._clock()
            self._last_start = now

    async def run(self, call: Callable[[], Awaitable[T]]) -> T:
        attempt = 0
        while True:
            await self._wait_for_slot()
            try:
                return await call()
            except BschekAPIError as exc:
                if exc.code != 'rate_limited' or attempt >= self._max_retries:
                    raise
                attempt += 1
                await self._sleep(exc.retry_after or 1.0)
```

- [ ] **Step 4: Прогнать** — PASS. **Step 5: Коммит**

```bash
uv run ruff format app/services/reachability/gate.py tests/services/reachability/test_gate.py && uv run ruff check app/services/reachability/gate.py tests/services/reachability/test_gate.py
git checkout uv.lock 2>/dev/null
git add app/services/reachability/gate.py tests/services/reachability/test_gate.py
git commit -m "feat(reachability): шлюз платных вызовов с интервалом и повтором 429"
```

---

### Итог части 1

После Task 10 выполнить весь прогон: `uv run pytest tests/services/test_reachability_registries.py tests/external/test_bschek_api.py tests/external/test_remnawave_hosts.py tests/database/test_reachability_crud.py tests/services/reachability -q` и `uv run ruff check app tests` — всё зелёное. Дальше — `docs/superpowers/plans/2026-09-05-reachability-bot-jobs-api.md` (сервис задач, роуты, обходчик, живой детектор дрейфа).
