"""Tests for `app.cabinet.utils.device_ownership.verify_hwid_belongs_to_user`.

Regression cover for the multi-tariff false-negative reported in code
review: previously, the helper picked the FIRST non-null panel user and
queried only that one — devices on a non-primary subscription's panel
returned 404 even though the user legitimately owned them.

Also covers the degrade-open contract: RemnaWave outage must not block
rename writes (the alias is per-user-id, no auth concern from accepting
a write during a partial outage) — и его границу: непригодный локальный
идентификатор (`RemnaWaveInvalidUserIdError`) degrade-open НЕ включает,
иначе проверка владения отключилась бы целиком.

API 3.0.0: панельный пользователь адресуется числовым `remnawave_id`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cabinet.utils.device_ownership import _collect_panel_user_ids, verify_hwid_belongs_to_user
from app.external.remnawave_api import RemnaWaveInvalidUserIdError


def _user(panel_id: int | None, sub_ids: list[int | None]) -> SimpleNamespace:
    """Build a minimal user-like stub with the panel user ids we care about.

    Легаси-колонка `remnawave_uuid` намеренно заполнена: helper обязан читать
    только числовой `remnawave_id`, иначе в панель уйдёт мусорный идентификатор.
    """
    return SimpleNamespace(
        id=1,
        remnawave_id=panel_id,
        remnawave_uuid='legacy-user-uuid',
        subscriptions=[SimpleNamespace(remnawave_id=i, remnawave_uuid='legacy-sub-uuid') for i in sub_ids],
    )


# ---------------------------------------------------------------------------
# _collect_panel_user_ids
# ---------------------------------------------------------------------------


def test_collect_panel_user_ids_deduplicates_and_preserves_order() -> None:
    """user.remnawave_id first, then unique subscription ids in declared order."""
    user = _user(10, [10, 20, None, 30, 20])

    result = _collect_panel_user_ids(user)

    assert result == [10, 20, 30]


def test_collect_panel_user_ids_handles_classic_mode_user_only() -> None:
    """Classic mode: only user.remnawave_id, no subscriptions array."""
    user = SimpleNamespace(id=1, remnawave_id=10, remnawave_uuid='legacy-user-uuid', subscriptions=[])

    result = _collect_panel_user_ids(user)

    assert result == [10]


def test_collect_panel_user_ids_handles_multi_tariff_no_top_id() -> None:
    """Multi-tariff: top-level user.remnawave_id often None, sub ids only."""
    user = _user(None, [20, 30])

    result = _collect_panel_user_ids(user)

    assert result == [20, 30]


def test_collect_panel_user_ids_returns_empty_when_no_panel_attached() -> None:
    user = _user(None, [None, None])

    assert _collect_panel_user_ids(user) == []


def test_collect_panel_user_ids_ignores_legacy_uuid_column() -> None:
    """Только remnawave_id: юзер с одними легаси-UUID панели не привязан."""
    user = SimpleNamespace(
        id=1,
        remnawave_id=None,
        remnawave_uuid='panel-a',
        subscriptions=[SimpleNamespace(remnawave_id=None, remnawave_uuid='panel-b')],
    )

    assert _collect_panel_user_ids(user) == []


# ---------------------------------------------------------------------------
# verify_hwid_belongs_to_user
# ---------------------------------------------------------------------------


def _patched_remnawave(devices_by_panel_id: dict[int, list[dict]]) -> MagicMock:
    """Stub the RemnaWaveService API client so we can simulate panel responses."""
    api_mock = MagicMock()
    api_mock.get_user_devices_all = AsyncMock(
        side_effect=lambda panel_user_id: {'devices': devices_by_panel_id.get(panel_user_id, [])}
    )

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=api_mock)
    cm.__aexit__ = AsyncMock(return_value=None)

    service_mock = MagicMock()
    service_mock.get_api_client = MagicMock(return_value=cm)
    return service_mock


@pytest.mark.asyncio
async def test_verify_finds_hwid_on_first_panel() -> None:
    user = _user(10, [])
    devices = {10: [{'hwid': 'TARGET'}, {'hwid': 'OTHER'}]}
    service_mock = _patched_remnawave(devices)

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=service_mock):
        assert await verify_hwid_belongs_to_user(user, 'TARGET') is True

    # Панель адресуется числовым id, а не легаси-UUID.
    api_mock = await service_mock.get_api_client().__aenter__()
    assert api_mock.get_user_devices_all.await_args.args == (10,)


@pytest.mark.asyncio
async def test_verify_finds_hwid_on_non_primary_subscription_panel() -> None:
    """REGRESSION: multi-tariff user with device on sub-B's panel user must pass.

    Previously the helper queried only the first id (`10`) and returned
    False even though sub-B's panel had the hwid.
    """
    user = _user(10, [20])
    devices = {
        10: [{'hwid': 'WRONG-DEVICE'}],
        20: [{'hwid': 'TARGET'}],
    }

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=_patched_remnawave(devices)):
        assert await verify_hwid_belongs_to_user(user, 'TARGET') is True


@pytest.mark.asyncio
async def test_verify_returns_false_when_hwid_on_no_panel() -> None:
    user = _user(10, [20])
    devices = {
        10: [{'hwid': 'OTHER-1'}],
        20: [{'hwid': 'OTHER-2'}],
    }

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=_patched_remnawave(devices)):
        assert await verify_hwid_belongs_to_user(user, 'PHANTOM') is False


@pytest.mark.asyncio
async def test_verify_short_circuits_after_first_hit() -> None:
    """We stop iterating panels as soon as we find the device — fewer remote calls."""
    user = _user(10, [20, 30])
    devices = {
        10: [{'hwid': 'TARGET'}],
        20: [{'hwid': 'OTHER'}],
        30: [{'hwid': 'OTHER-2'}],
    }
    service_mock = _patched_remnawave(devices)

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=service_mock):
        assert await verify_hwid_belongs_to_user(user, 'TARGET') is True

    api_mock = await service_mock.get_api_client().__aenter__()
    assert api_mock.get_user_devices_all.await_count == 1


@pytest.mark.asyncio
async def test_verify_degrades_open_on_remnawave_failure() -> None:
    """Degrade-open contract: panel unreachable → True so renames don't break."""
    user = _user(10, [])

    service_mock = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=RuntimeError('Remnawave is down'))
    cm.__aexit__ = AsyncMock(return_value=None)
    service_mock.get_api_client = MagicMock(return_value=cm)

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=service_mock):
        assert await verify_hwid_belongs_to_user(user, 'whatever') is True


@pytest.mark.asyncio
async def test_verify_does_not_degrade_open_on_unusable_panel_id() -> None:
    """Битая ссылка в НАШЕЙ БД — не сбой панели: такой id пропускается, проверка закрыта.

    Расширение degrade-open на RemnaWaveInvalidUserIdError означало бы «принимаем
    любой hwid», то есть отключение проверки владения целиком.
    """
    user = _user(10, [])
    service_mock = _patched_remnawave({})
    api_mock = await service_mock.get_api_client().__aenter__()
    api_mock.get_user_devices_all = AsyncMock(side_effect=RemnaWaveInvalidUserIdError('Invalid panel user id'))

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=service_mock):
        assert await verify_hwid_belongs_to_user(user, 'whatever') is False


@pytest.mark.asyncio
async def test_verify_skips_unusable_id_but_still_checks_remaining_panels() -> None:
    """Один непригодный id не должен обрывать обход остальных панелей юзера."""
    user = _user(10, [20])
    good_devices = {20: [{'hwid': 'TARGET'}]}
    service_mock = _patched_remnawave(good_devices)
    api_mock = await service_mock.get_api_client().__aenter__()
    healthy_side_effect = api_mock.get_user_devices_all.side_effect

    def _side_effect(panel_user_id):
        if panel_user_id == 10:
            raise RemnaWaveInvalidUserIdError('Invalid panel user id')
        return healthy_side_effect(panel_user_id)

    api_mock.get_user_devices_all = AsyncMock(side_effect=_side_effect)

    with patch('app.services.remnawave_service.RemnaWaveService', return_value=service_mock):
        assert await verify_hwid_belongs_to_user(user, 'TARGET') is True

    assert api_mock.get_user_devices_all.await_count == 2


@pytest.mark.asyncio
async def test_verify_returns_false_when_user_has_no_panel_id() -> None:
    """No panel id on user or any subscription → False (nothing to validate against)."""
    user = _user(None, [None])

    assert await verify_hwid_belongs_to_user(user, 'whatever') is False
