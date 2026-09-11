"""Создание и правка в админке принимают одно и то же.

Кабинет собирает ОДИН объект и шлёт его и на создание, и на правку. Любое
расхождение ограничений между парой схем ``XCreateRequest`` / ``XUpdateRequest``
ломает одну из ручек молча: тариф не создавался вовсе, потому что «ничего не
выделено» кабинет кодирует нулём, а создание требовало ``>= 1``. Та же болезнь —
закреплённое сообщение из одной картинки (создание требовало текст, хотя роут
сам проверяет «текст или медиа») и тег новости без предела длины на правке
(колонка ``String(50)``: правка пускала то, что создание отсекало).

Сторож держит инвариант на всех парах сразу: общее поле — общие ограничения.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import pkgutil
from types import SimpleNamespace

import pytest
from annotated_types import Ge, Gt, Le, Lt, MaxLen, MinLen
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

import app.cabinet.schemas as schemas_pkg
from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes import admin_tariffs as route
from app.cabinet.schemas.news import NewsUpdateRequest
from app.cabinet.schemas.pinned_messages import PinnedMessageCreateRequest
from app.database.models import PromoGroup, ServerSquad, Subscription, Tariff, User, tariff_promo_groups
from tests.fixtures.sqlite_memory import memory_session


# ---------------------------------------------------------------------------
# Сторож: ограничения общих полей у пары Create/Update совпадают
# ---------------------------------------------------------------------------

# Классы annotated_types — датаклассы со slots, у них нет __dict__.
_BOUND_ATTRS = {Ge: 'ge', Gt: 'gt', Le: 'le', Lt: 'lt', MinLen: 'min_length', MaxLen: 'max_length'}


def _constraints(field) -> dict[str, object]:
    return {
        attr: getattr(item, attr)
        for item in field.metadata
        for kind, attr in _BOUND_ATTRS.items()
        if isinstance(item, kind)
    }


def _request_pairs() -> list[tuple[str, type[BaseModel], type[BaseModel]]]:
    pairs = []
    for module_info in pkgutil.iter_modules(schemas_pkg.__path__):
        module = importlib.import_module(f'{schemas_pkg.__name__}.{module_info.name}')
        models = {
            name: obj
            for name, obj in vars(module).items()
            if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj.__module__ == module.__name__
        }
        for name, create in models.items():
            if not name.endswith('CreateRequest'):
                continue
            update = models.get(name.replace('CreateRequest', 'UpdateRequest'))
            if update is not None:
                pairs.append((f'{module_info.name}.{name}', create, update))
    return pairs


PAIRS = _request_pairs()


def test_schema_pairs_are_discovered():
    """Пустой список сделал бы сторож ниже бессмысленно зелёным."""
    assert len(PAIRS) >= 5, [name for name, _, _ in PAIRS]


@pytest.mark.parametrize(('name', 'create', 'update'), PAIRS, ids=[pair[0] for pair in PAIRS])
def test_shared_fields_share_constraints(name, create, update):
    mismatches = {
        field: (_constraints(create_field), _constraints(update.model_fields[field]))
        for field, create_field in create.model_fields.items()
        if field in update.model_fields and _constraints(create_field) != _constraints(update.model_fields[field])
    }
    assert not mismatches, f'{name}: поле у создания и правки ограничено по-разному: {mismatches}'


# ---------------------------------------------------------------------------
# Тариф: ноль в highlight_period_days — «ничего не выделено», а не ошибка
# ---------------------------------------------------------------------------

TABLES = (
    User.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    Tariff.__table__,
    Subscription.__table__,
    ServerSquad.__table__,
)
ADMIN = SimpleNamespace(id=1, telegram_id=777)


def _override_permission_dependencies(app: FastAPI) -> None:
    """Пропустить RBAC, оставив всё остальное настоящим (см. test_admin_grace_access_http)."""
    for candidate in route.router.routes:
        for dependant in candidate.dependant.dependencies:
            call = dependant.call
            if getattr(call, '__name__', '') == 'dependency' and getattr(call, '__module__', '').endswith(
                'cabinet.dependencies'
            ):
                app.dependency_overrides[call] = lambda: ADMIN


@contextlib.asynccontextmanager
async def _app(monkeypatch):
    async def _no_reload(_db):
        return None

    monkeypatch.setattr(route, 'load_period_prices_from_db', _no_reload)

    async with memory_session(monkeypatch, TABLES) as db:
        app = FastAPI()
        app.include_router(route.router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = lambda: db
        _override_permission_dependencies(app)
        with TestClient(app) as http:
            yield http, db


def _tariff_payload(**overrides) -> dict:
    """Ровно то, что шлёт форма кабинета, когда оператор ничего не выделил."""
    payload = {
        'name': 'Базовый',
        'period_prices': [{'days': 30, 'price_kopeks': 39900}, {'days': 365, 'price_kopeks': 199900}],
        'highlight_period_days': 0,
        'traffic_limit_gb': 100,
        'device_limit': 1,
        'tier_level': 1,
    }
    return {**payload, **overrides}


@pytest.mark.asyncio
async def test_create_tariff_accepts_zero_as_no_highlight(monkeypatch):
    async with _app(monkeypatch) as (http, db):
        response = http.post('/cabinet/admin/tariffs', json=_tariff_payload())

        assert response.status_code == 200, response.text
        assert response.json()['highlight_period_days'] is None
        stored = await db.get(Tariff, response.json()['id'])
        assert stored.highlight_period_days is None


@pytest.mark.asyncio
async def test_create_tariff_keeps_marked_period(monkeypatch):
    async with _app(monkeypatch) as (http, _db):
        response = http.post('/cabinet/admin/tariffs', json=_tariff_payload(highlight_period_days=365))

        assert response.status_code == 200, response.text
        assert response.json()['highlight_period_days'] == 365


@pytest.mark.asyncio
async def test_create_tariff_rejects_negative_highlight(monkeypatch):
    async with _app(monkeypatch) as (http, _db):
        response = http.post('/cabinet/admin/tariffs', json=_tariff_payload(highlight_period_days=-1))

        assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Закреплённое сообщение: одна картинка без текста — допустимо
# ---------------------------------------------------------------------------


def test_pinned_message_can_be_media_only():
    request = PinnedMessageCreateRequest(
        content='',
        media={'type': 'photo', 'file_id': 'AgAC'},
        send_before_menu=False,
        send_on_every_start=False,
        broadcast=False,
    )
    assert request.content == ''
    assert request.media is not None


# ---------------------------------------------------------------------------
# Новость: правка не пускает тег длиннее колонки
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(('field', 'value'), [('tag', 'x' * 51), ('excerpt', 'x' * 1001)])
def test_news_update_enforces_same_lengths_as_create(field, value):
    with pytest.raises(ValidationError):
        NewsUpdateRequest(**{field: value})


# ---------------------------------------------------------------------------
# Тариф, правка: тот же объект с нулём снимает выделение, без поля — не трогает
# ---------------------------------------------------------------------------


async def _create_highlighted(http) -> int:
    response = http.post('/cabinet/admin/tariffs', json=_tariff_payload(highlight_period_days=365))
    assert response.status_code == 200, response.text
    assert response.json()['highlight_period_days'] == 365
    return response.json()['id']


@pytest.mark.asyncio
async def test_update_tariff_zero_clears_highlight(monkeypatch):
    async with _app(monkeypatch) as (http, db):
        tariff_id = await _create_highlighted(http)

        response = http.put(f'/cabinet/admin/tariffs/{tariff_id}', json={'highlight_period_days': 0})

        assert response.status_code == 200, response.text
        assert response.json()['highlight_period_days'] is None
        stored = await db.get(Tariff, tariff_id)
        assert stored.highlight_period_days is None


@pytest.mark.asyncio
async def test_update_tariff_moves_highlight_to_another_period(monkeypatch):
    async with _app(monkeypatch) as (http, _db):
        tariff_id = await _create_highlighted(http)

        response = http.put(f'/cabinet/admin/tariffs/{tariff_id}', json={'highlight_period_days': 30})

        assert response.status_code == 200, response.text
        assert response.json()['highlight_period_days'] == 30


@pytest.mark.asyncio
async def test_update_tariff_without_the_field_keeps_highlight(monkeypatch):
    async with _app(monkeypatch) as (http, _db):
        tariff_id = await _create_highlighted(http)

        response = http.put(f'/cabinet/admin/tariffs/{tariff_id}', json={'name': 'Базовый+'})

        assert response.status_code == 200, response.text
        assert response.json()['name'] == 'Базовый+'
        assert response.json()['highlight_period_days'] == 365
