"""Тег панели и дни триала у тарифа — через настоящий роутер кабинета.

Тег нормализуется к верхнему регистру и проверяется по правилам панели (16 символов,
A–Z, 0–9, _). Пустая строка на правке снимает тег; без поля в запросе он не трогается.
Дни триала на тарифе: раньше телеграм-редактор писал их в несуществующую колонку.
"""

import pytest

from app.database.models import Tariff
from tests.cabinet.test_admin_create_update_schema_parity import _app, _tariff_payload


@pytest.mark.asyncio
async def test_create_normalizes_panel_tag_to_upper_case(monkeypatch):
    async with _app(monkeypatch) as (http, db):
        response = http.post('/cabinet/admin/tariffs', json=_tariff_payload(panel_tag='vip_1'))

        assert response.status_code == 200, response.text
        assert response.json()['panel_tag'] == 'VIP_1'
        stored = await db.get(Tariff, response.json()['id'])
        assert stored.panel_tag == 'VIP_1'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['v-i-p', 'a' * 17, 'тег', 'my tag'])
async def test_create_rejects_tags_the_panel_would_reject(monkeypatch, bad):
    async with _app(monkeypatch) as (http, _db):
        response = http.post('/cabinet/admin/tariffs', json=_tariff_payload(panel_tag=bad))

        assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_create_without_tag_stores_null(monkeypatch):
    async with _app(monkeypatch) as (http, db):
        response = http.post('/cabinet/admin/tariffs', json=_tariff_payload(panel_tag=''))

        assert response.status_code == 200, response.text
        assert response.json()['panel_tag'] is None
        assert (await db.get(Tariff, response.json()['id'])).panel_tag is None


@pytest.mark.asyncio
async def test_update_empty_string_clears_tag_and_missing_field_keeps_it(monkeypatch):
    async with _app(monkeypatch) as (http, db):
        tariff_id = http.post('/cabinet/admin/tariffs', json=_tariff_payload(panel_tag='VIP')).json()['id']

        untouched = http.put(f'/cabinet/admin/tariffs/{tariff_id}', json={'name': 'Другое имя'})
        assert untouched.status_code == 200, untouched.text
        assert untouched.json()['panel_tag'] == 'VIP'

        cleared = http.put(f'/cabinet/admin/tariffs/{tariff_id}', json={'panel_tag': ''})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()['panel_tag'] is None
        assert (await db.get(Tariff, tariff_id)).panel_tag is None


@pytest.mark.asyncio
async def test_list_exposes_panel_tag(monkeypatch):
    async with _app(monkeypatch) as (http, _db):
        http.post('/cabinet/admin/tariffs', json=_tariff_payload(panel_tag='gold'))

        response = http.get('/cabinet/admin/tariffs')

        assert response.status_code == 200, response.text
        assert [item['panel_tag'] for item in response.json()['tariffs']] == ['GOLD']


@pytest.mark.asyncio
async def test_trial_duration_days_round_trips_and_null_means_global(monkeypatch):
    async with _app(monkeypatch) as (http, db):
        created = http.post('/cabinet/admin/tariffs', json=_tariff_payload(trial_duration_days=5))
        assert created.status_code == 200, created.text
        assert created.json()['trial_duration_days'] == 5
        tariff_id = created.json()['id']
        assert (await db.get(Tariff, tariff_id)).trial_duration_days == 5

        cleared = http.put(f'/cabinet/admin/tariffs/{tariff_id}', json={'trial_duration_days': None})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()['trial_duration_days'] is None


@pytest.mark.asyncio
async def test_trial_duration_days_must_be_positive(monkeypatch):
    async with _app(monkeypatch) as (http, _db):
        assert http.post('/cabinet/admin/tariffs', json=_tariff_payload(trial_duration_days=0)).status_code == 422
