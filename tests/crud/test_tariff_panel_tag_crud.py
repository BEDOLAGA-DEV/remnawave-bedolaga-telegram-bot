"""CRUD тарифа нормализует тег панели сам: телеграм-редактор идёт мимо схем кабинета."""

import pytest

from app.database.crud.tariff import create_tariff, update_tariff
from app.database.models import PromoGroup, Tariff, tariff_promo_groups
from tests.fixtures.sqlite_memory import memory_session


TABLES = (PromoGroup.__table__, Tariff.__table__, tariff_promo_groups)


@pytest.mark.asyncio
async def test_create_upper_cases_tag(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        tariff = await create_tariff(db, 'Базовый', panel_tag='vip')
        assert tariff.panel_tag == 'VIP'


@pytest.mark.asyncio
async def test_update_blank_clears_and_missing_keeps(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        tariff = await create_tariff(db, 'Базовый', panel_tag='VIP')
        tariff = await update_tariff(db, tariff, name='Иное')
        assert tariff.panel_tag == 'VIP'
        tariff = await update_tariff(db, tariff, panel_tag='')
        assert tariff.panel_tag is None


@pytest.mark.asyncio
async def test_invalid_tag_is_rejected_before_write(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        with pytest.raises(ValueError):
            await create_tariff(db, 'Базовый', panel_tag='v-i-p')


@pytest.mark.asyncio
async def test_trial_duration_days_persists(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        tariff = await create_tariff(db, 'Триал', trial_duration_days=5)
        assert tariff.trial_duration_days == 5
        await db.refresh(tariff)
        assert tariff.trial_duration_days == 5
