"""Public tariff-route contracts for mandatory panel squad synchronization."""

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_tariffs
from app.cabinet.schemas.tariffs import TariffUpdateRequest
from app.services.admin_panel_sync import PanelSyncFailed, PanelSyncReason, PanelSyncSkipped
from tests.cabinet.admin_panel_sync_case_manifest import (
    TARIFF_FAILED_CASES,
    TARIFF_SKIPPED_CASES,
    TARIFF_SUCCESS_CASES,
)


@pytest.fixture
def tariff():
    return SimpleNamespace(id=81, name='Starter', allowed_squads=['old'], external_squad_uuid=None)


@pytest.fixture
def db():
    return AsyncMock()


async def _call_public_tariff_route(route: str, tariff, db):
    if route == 'manual-sync':
        return await admin_tariffs.sync_tariff_squads(81, SimpleNamespace(id=1), db)
    return await admin_tariffs.update_existing_tariff(
        81,
        TariffUpdateRequest(allowed_squads=['new']),
        SimpleNamespace(id=1),
        db,
    )


def _configure_tariff_route(monkeypatch, tariff, route: str):
    monkeypatch.setattr(admin_tariffs, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    if route == 'update':
        monkeypatch.setattr(admin_tariffs, 'update_tariff', AsyncMock())
        monkeypatch.setattr(admin_tariffs, 'load_period_prices_from_db', AsyncMock())
        monkeypatch.setattr(admin_tariffs, 'get_tariff', AsyncMock(return_value=SimpleNamespace(id=tariff.id)))


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'route'), TARIFF_SUCCESS_CASES)
async def test_tariff_squad_routes_commit_only_after_successful_panel_sync(monkeypatch, tariff, db, case_key, route):
    _configure_tariff_route(monkeypatch, tariff, route)
    sync = AsyncMock(return_value=3)
    monkeypatch.setattr(admin_tariffs, '_sync_tariff_squads_atomically', sync)

    result = await _call_public_tariff_route(route, tariff, db)

    expected_action = 'tariff_update_sync_squads' if route == 'update' else 'sync_tariff_squads'
    sync.assert_awaited_once_with(db, ANY, action=expected_action)
    db.commit.assert_awaited_once()
    assert result is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'route'), TARIFF_SKIPPED_CASES)
async def test_tariff_squad_routes_rollback_and_return_safe_error_when_panel_sync_is_skipped(
    monkeypatch, tariff, db, case_key, route
):
    _configure_tariff_route(monkeypatch, tariff, route)
    monkeypatch.setattr(
        admin_tariffs,
        '_sync_tariff_squads_atomically',
        AsyncMock(side_effect=PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED)),
    )

    with pytest.raises(HTTPException) as raised:
        await _call_public_tariff_route(route, tariff, db)

    assert raised.value.status_code == 503
    assert 'not saved' in raised.value.detail.lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'route'), TARIFF_FAILED_CASES)
async def test_tariff_squad_routes_rollback_and_return_safe_error_when_panel_sync_fails(
    monkeypatch, tariff, db, case_key, route
):
    _configure_tariff_route(monkeypatch, tariff, route)
    monkeypatch.setattr(
        admin_tariffs,
        '_sync_tariff_squads_atomically',
        AsyncMock(side_effect=PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)),
    )

    with pytest.raises(HTTPException) as raised:
        await _call_public_tariff_route(route, tariff, db)

    assert raised.value.status_code == 503
    assert 'not saved' in raised.value.detail.lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
