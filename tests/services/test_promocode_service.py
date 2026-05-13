"""
Tests for PromoCodeService - focus on promo group integration
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database.models import PromoCodeType
from app.services.promocode_service import PromoCodeService


# Import fixtures


async def test_activate_promo_group_promocode_success(
    monkeypatch,
    sample_user,
    sample_promo_group,
    sample_promocode_promo_group,
    mock_db_session,
):
    """
    Test successful activation of PROMO_GROUP type promocode

    Scenario:
    - User activates valid promo group promocode
    - User doesn't have this promo group yet
    - User is successfully added to promo group
    - Result includes promo group name
    """
    # Make promocode valid
    sample_promocode_promo_group.is_valid = True

    # Mock CRUD functions
    get_user_mock = AsyncMock(return_value=sample_user)
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', get_user_mock)

    get_promocode_mock = AsyncMock(return_value=sample_promocode_promo_group)
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

    # Execute
    service = PromoCodeService()
    result = await service.activate_promocode(mock_db_session, sample_user.id, 'VIPGROUP')

    # Assertions
    assert result['success'] is True
    assert 'Test VIP Group' in result['description']
    assert result['promocode']['promo_group_id'] == sample_promo_group.id

    # Verify promo group was fetched
    get_promo_group_mock.assert_awaited_once_with(mock_db_session, sample_promo_group.id)

    # Verify user promo group check
    has_promo_group_mock.assert_awaited_once_with(mock_db_session, sample_user.id, sample_promo_group.id)

    # Verify promo group assignment
    add_promo_group_mock.assert_awaited_once_with(
        mock_db_session, sample_user.id, sample_promo_group.id, assigned_by='promocode'
    )

    # Verify usage recorded
    create_usage_mock.assert_awaited_once_with(mock_db_session, sample_promocode_promo_group.id, sample_user.id)

    # Verify counter incremented
    assert sample_promocode_promo_group.current_uses == 21
    mock_db_session.commit.assert_awaited()


async def test_activate_promo_group_user_already_has_group(
    monkeypatch,
    sample_user,
    sample_promo_group,
    sample_promocode_promo_group,
    mock_db_session,
):
    """
    Test activation when user already has the promo group

    Scenario:
    - User activates promo group promocode
    - User already has this promo group
    - add_user_to_promo_group should NOT be called
    - Activation still succeeds
    """
    sample_promocode_promo_group.is_valid = True

    # Mock CRUD functions
    get_user_mock = AsyncMock(return_value=sample_user)
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', get_user_mock)

    get_promocode_mock = AsyncMock(return_value=sample_promocode_promo_group)
    monkeypatch.setattr('app.services.promocode_service.get_promocode_by_code', get_promocode_mock)

    check_usage_mock = AsyncMock(return_value=False)
    monkeypatch.setattr('app.services.promocode_service.check_user_promocode_usage', check_usage_mock)

    # User ALREADY HAS the promo group
    has_promo_group_mock = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.promocode_service.has_user_promo_group', has_promo_group_mock)

    add_promo_group_mock = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.add_user_to_promo_group', add_promo_group_mock)

    create_usage_mock = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.create_promocode_use', create_usage_mock)

    # Execute
    service = PromoCodeService()
    result = await service.activate_promocode(mock_db_session, sample_user.id, 'VIPGROUP')

    # Assertions
    assert result['success'] is True

    # Verify promo group assignment was NOT called
    add_promo_group_mock.assert_not_awaited()

    # But usage was still recorded
    create_usage_mock.assert_awaited_once()


async def test_activate_promo_group_group_not_found(
    monkeypatch,
    sample_user,
    sample_promocode_promo_group,
    mock_db_session,
):
    """
    Test activation when promo group doesn't exist (deleted/invalid)

    Scenario:
    - Promocode references non-existent promo_group_id
    - get_promo_group_by_id returns None
    - Warning is logged but activation doesn't fail
    - Promocode effects still apply (graceful degradation)
    """
    sample_promocode_promo_group.is_valid = True

    # Mock CRUD functions
    get_user_mock = AsyncMock(return_value=sample_user)
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', get_user_mock)

    get_promocode_mock = AsyncMock(return_value=sample_promocode_promo_group)
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
    result = await service.activate_promocode(mock_db_session, sample_user.id, 'VIPGROUP')

    # Assertions
    assert result['success'] is True  # Still succeeds!

    # Verify promo group was attempted to fetch
    get_promo_group_mock.assert_awaited_once()

    # Verify promo group assignment was NOT called (because group not found)
    add_promo_group_mock.assert_not_awaited()

    # But usage was still recorded
    create_usage_mock.assert_awaited_once()


async def test_activate_promo_group_assignment_error(
    monkeypatch,
    sample_user,
    sample_promo_group,
    sample_promocode_promo_group,
    mock_db_session,
):
    """
    Test activation when promo group assignment fails

    Scenario:
    - add_user_to_promo_group raises exception
    - Error is logged but activation doesn't fail
    - Promocode usage is still recorded (graceful degradation)
    """
    sample_promocode_promo_group.is_valid = True

    # Mock CRUD functions
    get_user_mock = AsyncMock(return_value=sample_user)
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', get_user_mock)

    get_promocode_mock = AsyncMock(return_value=sample_promocode_promo_group)
    monkeypatch.setattr('app.services.promocode_service.get_promocode_by_code', get_promocode_mock)

    check_usage_mock = AsyncMock(return_value=False)
    monkeypatch.setattr('app.services.promocode_service.check_user_promocode_usage', check_usage_mock)

    get_promo_group_mock = AsyncMock(return_value=sample_promo_group)
    monkeypatch.setattr('app.services.promocode_service.get_promo_group_by_id', get_promo_group_mock)

    has_promo_group_mock = AsyncMock(return_value=False)
    monkeypatch.setattr('app.services.promocode_service.has_user_promo_group', has_promo_group_mock)

    # add_user_to_promo_group RAISES EXCEPTION
    add_promo_group_mock = AsyncMock(side_effect=Exception('Database error'))
    monkeypatch.setattr('app.services.promocode_service.add_user_to_promo_group', add_promo_group_mock)

    create_usage_mock = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.create_promocode_use', create_usage_mock)

    # Execute
    service = PromoCodeService()
    result = await service.activate_promocode(mock_db_session, sample_user.id, 'VIPGROUP')

    # Assertions
    assert result['success'] is True  # Still succeeds!

    # Verify promo group assignment was attempted
    add_promo_group_mock.assert_awaited_once()

    # But usage was still recorded
    create_usage_mock.assert_awaited_once()


async def test_activate_promo_group_assigned_by_value(
    monkeypatch,
    sample_user,
    sample_promo_group,
    sample_promocode_promo_group,
    mock_db_session,
):
    """
    Test that assigned_by parameter is correctly set to 'promocode'

    Scenario:
    - Verify add_user_to_promo_group is called with assigned_by="promocode"
    """
    sample_promocode_promo_group.is_valid = True

    # Mock CRUD functions
    get_user_mock = AsyncMock(return_value=sample_user)
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', get_user_mock)

    get_promocode_mock = AsyncMock(return_value=sample_promocode_promo_group)
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

    # Execute
    service = PromoCodeService()
    await service.activate_promocode(mock_db_session, sample_user.id, 'VIPGROUP')

    # Verify assigned_by="promocode"
    add_promo_group_mock.assert_awaited_once_with(
        mock_db_session,
        sample_user.id,
        sample_promo_group.id,
        assigned_by='promocode',  # Critical assertion
    )


async def test_activate_promo_group_description_includes_group_name(
    monkeypatch,
    sample_user,
    sample_promo_group,
    sample_promocode_promo_group,
    mock_db_session,
):
    """
    Test that result description includes promo group name

    Scenario:
    - When promo group is assigned, description should include group name
    """
    sample_promocode_promo_group.is_valid = True

    # Mock CRUD functions
    get_user_mock = AsyncMock(return_value=sample_user)
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', get_user_mock)

    get_promocode_mock = AsyncMock(return_value=sample_promocode_promo_group)
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

    # Execute
    service = PromoCodeService()
    result = await service.activate_promocode(mock_db_session, sample_user.id, 'VIPGROUP')

    # Verify description includes promo group name
    assert 'Назначена промогруппа: Test VIP Group' in result['description']


async def test_promocode_data_includes_promo_group_id(
    monkeypatch,
    sample_user,
    sample_promo_group,
    sample_promocode_promo_group,
    mock_db_session,
):
    """
    Test that returned promocode data includes promo_group_id

    Scenario:
    - Verify result["promocode"]["promo_group_id"] is present
    """
    sample_promocode_promo_group.is_valid = True

    # Mock CRUD functions
    get_user_mock = AsyncMock(return_value=sample_user)
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', get_user_mock)

    get_promocode_mock = AsyncMock(return_value=sample_promocode_promo_group)
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

    # Execute
    service = PromoCodeService()
    result = await service.activate_promocode(mock_db_session, sample_user.id, 'VIPGROUP')

    # Verify promocode data structure
    assert 'promocode' in result
    assert 'promo_group_id' in result['promocode']
    assert result['promocode']['promo_group_id'] == sample_promo_group.id


async def test_activate_trial_promocode_uses_all_available_squads_when_tariff_has_no_restrictions(
    monkeypatch,
):
    sample_user = SimpleNamespace(
        id=1,
        telegram_id=123456789,
        username='testuser',
        full_name='Test User',
        balance_kopeks=0,
        language='ru',
        has_had_paid_subscription=False,
        total_spent_kopeks=0,
    )
    mock_db_session = AsyncMock()
    mock_db_session.commit = AsyncMock()
    mock_db_session.rollback = AsyncMock()
    mock_db_session.refresh = AsyncMock()
    mock_db_session.delete = AsyncMock()

    promocode = SimpleNamespace(
        id=10,
        code='KRTN14',
        type=PromoCodeType.TRIAL_SUBSCRIPTION.value,
        balance_bonus_kopeks=0,
        subscription_days=14,
        tariff_id=7,
        promo_group_id=None,
        promo_group=None,
        first_purchase_only=False,
        max_uses=20,
        current_uses=0,
        is_active=True,
        is_valid=True,
        valid_until=None,
    )
    trial_tariff = SimpleNamespace(
        id=7,
        name='Trial',
        traffic_limit_gb=100,
        device_limit=5,
        allowed_squads=[],
        trial_duration_days=14,
    )
    created_subscription = SimpleNamespace(id=99)

    monkeypatch.setattr('app.services.promocode_service.RemnaWaveService', lambda: SimpleNamespace())
    create_remnawave_user_mock = AsyncMock()
    monkeypatch.setattr(
        'app.services.promocode_service.SubscriptionService',
        lambda: SimpleNamespace(create_remnawave_user=create_remnawave_user_mock),
    )
    monkeypatch.setattr('app.services.promocode_service.get_user_by_id', AsyncMock(return_value=sample_user))
    monkeypatch.setattr('app.services.promocode_service.get_promocode_by_code', AsyncMock(return_value=promocode))
    monkeypatch.setattr('app.services.promocode_service.check_user_promocode_usage', AsyncMock(return_value=False))
    monkeypatch.setattr('app.database.crud.promocode.count_user_recent_activations', AsyncMock(return_value=0))
    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=None))
    monkeypatch.setattr('app.services.promocode_service.create_promocode_use', AsyncMock(return_value=object()))
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=trial_tariff))
    monkeypatch.setattr('app.database.crud.tariff.get_trial_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_available_server_squads',
        AsyncMock(
            return_value=[
                SimpleNamespace(squad_uuid='fi-uuid'),
                SimpleNamespace(squad_uuid='ru-uuid'),
            ]
        ),
    )
    create_trial_subscription_mock = AsyncMock(return_value=created_subscription)
    monkeypatch.setattr('app.database.crud.subscription.create_trial_subscription', create_trial_subscription_mock)

    service = PromoCodeService()
    result = await service.activate_promocode(mock_db_session, sample_user.id, promocode.code)

    assert result['success'] is True
    create_trial_subscription_mock.assert_awaited_once_with(
        mock_db_session,
        sample_user.id,
        duration_days=14,
        traffic_limit_gb=100,
        device_limit=5,
        connected_squads=['fi-uuid', 'ru-uuid'],
        tariff_id=7,
    )
    create_remnawave_user_mock.assert_awaited_once_with(mock_db_session, created_subscription)
