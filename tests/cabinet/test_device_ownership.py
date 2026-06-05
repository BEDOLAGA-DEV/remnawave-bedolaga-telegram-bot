"""Tests for `app.cabinet.utils.device_ownership.verify_hwid_belongs_to_user`.

Regression cover for the multi-tariff false-negative reported in code
review: previously, the helper picked the FIRST non-null panel UUID and
queried only that one — devices on a non-primary subscription's panel
returned 404 even though the user legitimately owned them.

Also covers the degrade-open contract: RemnaWave outage must not block
rename writes (the alias is per-user-id, no auth concern from accepting
a write during a partial outage).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cabinet.utils.device_ownership import _collect_panel_uuids, verify_hwid_belongs_to_user


def _user(uuid: str | None, sub_uuids: list[str | None]) -> SimpleNamespace:
    """Build a minimal user-like stub with the panel UUIDs we care about."""
    return SimpleNamespace(
        id=1,
        remnawave_uuid=uuid,
        subscriptions=[SimpleNamespace(remnawave_uuid=u) for u in sub_uuids],
    )


# ---------------------------------------------------------------------------
# _collect_panel_uuids
# ---------------------------------------------------------------------------


def test_collect_panel_uuids_deduplicates_and_preserves_order() -> None:
    """user.remnawave_uuid first, then unique subscription UUIDs in declared order."""
    user = _user('top-uuid', ['top-uuid', 'sub-a', None, 'sub-b', 'sub-a'])

    result = _collect_panel_uuids(user)

    assert result == ['top-uuid', 'sub-a', 'sub-b']


def test_collect_panel_uuids_handles_classic_mode_user_only() -> None:
    """Classic mode: only user.remnawave_uuid, no subscriptions array."""
    user = SimpleNamespace(id=1, remnawave_uuid='solo', subscriptions=[])

    result = _collect_panel_uuids(user)

    assert result == ['solo']


def test_collect_panel_uuids_handles_multi_tariff_no_top_uuid() -> None:
    """Multi-tariff: top-level user.remnawave_uuid often None, sub UUIDs only."""
    user = _user(None, ['sub-a', 'sub-b'])

    result = _collect_panel_uuids(user)

    assert result == ['sub-a', 'sub-b']


def test_collect_panel_uuids_returns_empty_when_no_panel_attached() -> None:
    user = _user(None, [None, None])

    assert _collect_panel_uuids(user) == []


# ---------------------------------------------------------------------------
# verify_hwid_belongs_to_user
# ---------------------------------------------------------------------------


def _patched_remnawave(devices_by_uuid: dict[str, list[dict]]) -> MagicMock:
    """Stub the RemnaWaveService API client so we can simulate panel responses."""
    api_mock = MagicMock()
    api_mock.get_user_devices_all = AsyncMock(side_effect=lambda uuid: {'devices': devices_by_uuid.get(uuid, [])})

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=api_mock)
    cm.__aexit__ = AsyncMock(return_value=None)

    service_mock = MagicMock()
    service_mock.get_api_client = MagicMock(return_value=cm)
    return service_mock


@pytest.mark.asyncio
async def test_verify_finds_hwid_on_first_panel() -> None:
    user = _user('panel-a', [])
    devices = {'panel-a': [{'hwid': 'TARGET'}, {'hwid': 'OTHER'}]}

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=_patched_remnawave(devices)):
        assert await verify_hwid_belongs_to_user(user, 'TARGET') is True


@pytest.mark.asyncio
async def test_verify_finds_hwid_on_non_primary_subscription_panel() -> None:
    """REGRESSION: multi-tariff user with device on sub-B's panel UUID must pass.

    Previously the helper queried only the first uuid (`panel-a`) and returned
    False even though sub-B's panel had the hwid.
    """
    user = _user('panel-a', ['panel-b'])
    devices = {
        'panel-a': [{'hwid': 'WRONG-DEVICE'}],
        'panel-b': [{'hwid': 'TARGET'}],
    }

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=_patched_remnawave(devices)):
        assert await verify_hwid_belongs_to_user(user, 'TARGET') is True


@pytest.mark.asyncio
async def test_verify_returns_false_when_hwid_on_no_panel() -> None:
    user = _user('panel-a', ['panel-b'])
    devices = {
        'panel-a': [{'hwid': 'OTHER-1'}],
        'panel-b': [{'hwid': 'OTHER-2'}],
    }

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=_patched_remnawave(devices)):
        assert await verify_hwid_belongs_to_user(user, 'PHANTOM') is False


@pytest.mark.asyncio
async def test_verify_short_circuits_after_first_hit() -> None:
    """We stop iterating panels as soon as we find the device — fewer remote calls."""
    user = _user('panel-a', ['panel-b', 'panel-c'])
    devices = {
        'panel-a': [{'hwid': 'TARGET'}],
        'panel-b': [{'hwid': 'OTHER'}],
        'panel-c': [{'hwid': 'OTHER-2'}],
    }
    service_mock = _patched_remnawave(devices)

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=service_mock):
        assert await verify_hwid_belongs_to_user(user, 'TARGET') is True

    api_mock = await service_mock.get_api_client().__aenter__()
    assert api_mock.get_user_devices_all.await_count == 1


@pytest.mark.asyncio
async def test_verify_degrades_open_on_remnawave_failure() -> None:
    """Degrade-open contract: panel unreachable → True so renames don't break."""
    user = _user('panel-a', [])

    service_mock = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=RuntimeError('Remnawave is down'))
    cm.__aexit__ = AsyncMock(return_value=None)
    service_mock.get_api_client = MagicMock(return_value=cm)

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=service_mock):
        assert await verify_hwid_belongs_to_user(user, 'whatever') is True


@pytest.mark.asyncio
async def test_verify_returns_false_when_user_has_no_panel_uuid() -> None:
    """No panel UUID on user or any subscription → False (nothing to validate against)."""
    user = _user(None, [None])

    assert await verify_hwid_belongs_to_user(user, 'whatever') is False
