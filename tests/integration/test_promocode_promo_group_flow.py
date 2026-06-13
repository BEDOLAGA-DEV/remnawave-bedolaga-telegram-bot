"""
Integration tests for promo code with promo group full workflow

These tests validate the complete flow from creating a promo group,
creating a promocode, to activating it and verifying the user receives
the promo group assignment.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.database.crud.promocode as promocode_crud
from app.database.models import PromoCodeType
from app.services.promocode_service import PromoCodeService


# Import fixtures


def _patch_recent_activations(monkeypatch, count: int = 0) -> AsyncMock:
    """Patch the anti-stacking counter.

    ``count_user_recent_activations`` is imported locally (``from
    app.database.crud.promocode import count_user_recent_activations``) inside
    ``activate_promocode``, so it must be patched on the REAL module. Without
    this, the real CRUD runs against the AsyncMock session and ``recent_count``
    becomes a coroutine, breaking the ``recent_count >= 5`` comparison.
    """
    mock = AsyncMock(return_value=count)
    monkeypatch.setattr(promocode_crud, 'count_user_recent_activations', mock, raising=False)
    return mock


def _assert_counter_incremented(mock_db_session, promocode_id: int) -> None:
    """The counter is now incremented atomically via ``db.execute(UPDATE ...)``.

    Production no longer mutates ``promocode.current_uses`` in Python; it runs
    ``UPDATE promocodes SET current_uses = current_uses + 1 WHERE id = ...``.
    Verify exactly one such UPDATE was executed for this promocode.
    """
    update_calls = []
    for call in mock_db_session.execute.await_args_list:
        stmt = call.args[0] if call.args else None
        if stmt is not None and stmt.__class__.__name__ == 'Update':
            update_calls.append(stmt)
    assert len(update_calls) == 1, (
        f'expected exactly one UPDATE to increment the counter, got {len(update_calls)}'
    )


async def test_promo_group_promocode_full_workflow(
    monkeypatch,
    sample_user,
    sample_promo_group,
    mock_db_session,
):
    """
    Integration test: Full workflow of promo group promocode

    Flow:
    1. Promo group exists (VIP Group, priority 50)
    2. Admin creates PROMO_GROUP type promocode
    3. User activates promocode
    4. User is added to promo group
    5. Usage is recorded
    6. Counter is incremented

    This test validates the entire integration between:
    - Promocode CRUD
    - Promo group CRUD
    - User promo group CRUD
    - Promocode service
    """
    # Setup: Create a PROMO_GROUP promocode
    promocode = SimpleNamespace(
        id=1,
        code='INTEGRATIONTEST',
        type=PromoCodeType.PROMO_GROUP.value,
        balance_bonus_kopeks=0,
        subscription_days=0,
        max_uses=100,
        current_uses=0,
        is_active=True,
        is_valid=True,
        promo_group_id=sample_promo_group.id,
        promo_group=sample_promo_group,
        valid_until=None,
    )

    # Mock all CRUD operations
    _patch_recent_activations(monkeypatch)

    get_user_mock = AsyncMock(return_value=sample_user)
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', get_user_mock)

    get_promocode_mock = AsyncMock(return_value=promocode)
    monkeypatch.setattr('app.services.promocode_service.get_promocode_by_code', get_promocode_mock)

    check_usage_mock = AsyncMock(return_value=False)
    monkeypatch.setattr('app.services.promocode_service.check_user_promocode_usage', check_usage_mock)

    get_promo_group_mock = AsyncMock(return_value=sample_promo_group)
    monkeypatch.setattr('app.services.promocode_service.get_promo_group_by_id', get_promo_group_mock)

    has_promo_group_mock = AsyncMock(return_value=False)
    monkeypatch.setattr('app.services.promocode_service.has_user_promo_group', has_promo_group_mock)

    add_promo_group_mock = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.add_user_to_promo_group', add_promo_group_mock)

    create_usage_mock = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.create_promocode_use', create_usage_mock)

    # Execute: User activates promocode
    service = PromoCodeService()
    result = await service.activate_promocode(mock_db_session, sample_user.id, 'INTEGRATIONTEST')

    # Verify: Activation successful
    assert result['success'] is True
    assert 'Test VIP Group' in result['description']

    # Verify: All steps were executed in correct order
    get_user_mock.assert_awaited_once_with(mock_db_session, sample_user.id)
    get_promocode_mock.assert_awaited_once_with(mock_db_session, 'INTEGRATIONTEST')
    check_usage_mock.assert_awaited_once_with(mock_db_session, sample_user.id, promocode.id)

    # Verify: Promo group assignment flow
    get_promo_group_mock.assert_awaited_once_with(mock_db_session, sample_promo_group.id)
    has_promo_group_mock.assert_awaited_once_with(mock_db_session, sample_user.id, sample_promo_group.id)
    # add_user_to_promo_group is now invoked with commit=False so the increment
    # and group assignment land in a single transaction committed by the service.
    add_promo_group_mock.assert_awaited_once_with(
        mock_db_session, sample_user.id, sample_promo_group.id, assigned_by='promocode', commit=False
    )

    # Verify: Usage recorded
    create_usage_mock.assert_awaited_once_with(mock_db_session, promocode.id, sample_user.id)

    # Verify: Counter incremented atomically via UPDATE (no in-Python mutation).
    _assert_counter_incremented(mock_db_session, promocode.id)
    assert result['promocode']['current_uses'] == 1

    # Verify: Database committed
    mock_db_session.commit.assert_awaited()


async def test_duplicate_promo_group_assignment_edge_case(
    monkeypatch,
    sample_user,
    sample_promo_group,
    mock_db_session,
):
    """
    Edge case: User already has promo group from previous promocode

    Scenario:
    1. User previously activated a promo group promocode
    2. User already has the VIP Group
    3. User activates another promocode for same group
    4. System should not duplicate the assignment
    5. Activation should still succeed
    """
    promocode = SimpleNamespace(
        id=2,
        code='DUPLICATE',
        type=PromoCodeType.PROMO_GROUP.value,
        balance_bonus_kopeks=0,
        subscription_days=0,
        max_uses=100,
        current_uses=5,
        is_active=True,
        is_valid=True,
        promo_group_id=sample_promo_group.id,
        promo_group=sample_promo_group,
        valid_until=None,
    )

    # Mock CRUD operations
    _patch_recent_activations(monkeypatch)

    get_user_mock = AsyncMock(return_value=sample_user)
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', get_user_mock)

    get_promocode_mock = AsyncMock(return_value=promocode)
    monkeypatch.setattr('app.services.promocode_service.get_promocode_by_code', get_promocode_mock)

    check_usage_mock = AsyncMock(return_value=False)
    monkeypatch.setattr('app.services.promocode_service.check_user_promocode_usage', check_usage_mock)

    # User ALREADY HAS this promo group
    has_promo_group_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.promocode_service.has_user_promo_group', has_promo_group_mock)

    add_promo_group_mock = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.add_user_to_promo_group', add_promo_group_mock)

    create_usage_mock = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.create_promocode_use', create_usage_mock)

    # Execute
    service = PromoCodeService()
    result = await service.activate_promocode(mock_db_session, sample_user.id, 'DUPLICATE')

    # Verify: Activation still successful
    assert result['success'] is True

    # Verify: add_user_to_promo_group was NOT called (no duplicate)
    add_promo_group_mock.assert_not_awaited()

    # Verify: Usage was still recorded
    create_usage_mock.assert_awaited_once()

    # Verify: Counter still incremented (atomic UPDATE; reflected in result data).
    _assert_counter_incremented(mock_db_session, promocode.id)
    assert result['promocode']['current_uses'] == 6


async def test_missing_promo_group_graceful_failure(
    monkeypatch,
    sample_user,
    mock_db_session,
):
    """
    Edge case: Promocode references deleted/non-existent promo group

    Scenario:
    1. Promocode was created with promo_group_id=999
    2. Promo group was later deleted
    3. User activates promocode
    4. System should handle gracefully (log warning, continue)
    5. Promocode effects should still apply
    6. No promo group is assigned (can't assign non-existent group)
    """
    # Promocode with non-existent promo_group_id
    promocode = SimpleNamespace(
        id=3,
        code='ORPHANED',
        type=PromoCodeType.PROMO_GROUP.value,
        balance_bonus_kopeks=0,
        subscription_days=0,
        max_uses=10,
        current_uses=0,
        is_active=True,
        is_valid=True,
        promo_group_id=999,  # Non-existent
        promo_group=None,
        valid_until=None,
    )

    # Mock CRUD operations
    _patch_recent_activations(monkeypatch)

    get_user_mock = AsyncMock(return_value=sample_user)
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', get_user_mock)

    get_promocode_mock = AsyncMock(return_value=promocode)
    monkeypatch.setattr('app.services.promocode_service.get_promocode_by_code', get_promocode_mock)

    check_usage_mock = AsyncMock(return_value=False)
    monkeypatch.setattr('app.services.promocode_service.check_user_promocode_usage', check_usage_mock)

    has_promo_group_mock = AsyncMock(return_value=False)
    monkeypatch.setattr('app.services.promocode_service.has_user_promo_group', has_promo_group_mock)

    # Promo group NOT FOUND
    get_promo_group_mock = AsyncMock(return_value=None)
    monkeypatch.setattr('app.services.promocode_service.get_promo_group_by_id', get_promo_group_mock)

    add_promo_group_mock = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.add_user_to_promo_group', add_promo_group_mock)

    create_usage_mock = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.create_promocode_use', create_usage_mock)

    # Execute
    service = PromoCodeService()
    result = await service.activate_promocode(mock_db_session, sample_user.id, 'ORPHANED')

    # Verify: Activation STILL successful (graceful degradation)
    assert result['success'] is True

    # Verify: Attempted to fetch promo group
    get_promo_group_mock.assert_awaited_once_with(mock_db_session, 999)

    # Verify: add_user_to_promo_group was NOT called (group doesn't exist)
    add_promo_group_mock.assert_not_awaited()

    # Verify: Usage was still recorded (promocode still works)
    create_usage_mock.assert_awaited_once()

    # Verify: Counter still incremented (atomic UPDATE; reflected in result data).
    _assert_counter_incremented(mock_db_session, promocode.id)
    assert result['promocode']['current_uses'] == 1
