"""Ownership boundaries for subscription-scoped admin user-detail routes.

The regression these tests guard against is treating a subscription id as an
authorization token.  Every multi-tariff operation must resolve the row with
both the requested subscription id and the route's user id before doing any
read or mutation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import UpdateSubscriptionRequest
from app.config import Settings


OWNER_ID = 10
OWNED_ID = 101
FOREIGN_ID = 202
MISSING_ID = 303

# Remnawave 3.0.0: панельная запись идентифицируется числовым id, а не UUID.
OWNED_PANEL_ID = 5001
FOREIGN_PANEL_ID = 5002
LEGACY_USER_PANEL_ID = 9999


def _subscription(subscription_id: int, user_id: int, *, panel_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=subscription_id,
        user_id=user_id,
        remnawave_id=panel_id,
        is_active=True,
        status='active',
        end_date=datetime.now(UTC) + timedelta(days=10),
        traffic_limit_gb=100,
        traffic_used_gb=1.5,
        device_limit=2,
    )


def _user(*subscriptions: SimpleNamespace, remnawave_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=OWNER_ID,
        telegram_id=1000,
        email=None,
        remnawave_id=remnawave_id,
        subscriptions=list(subscriptions),
    )


@pytest.fixture
def owned_subscription() -> SimpleNamespace:
    return _subscription(OWNED_ID, OWNER_ID, panel_id=OWNED_PANEL_ID)


@pytest.fixture
def foreign_subscription() -> SimpleNamespace:
    return _subscription(FOREIGN_ID, 20, panel_id=FOREIGN_PANEL_ID)


@pytest.fixture
def ownership_boundary(monkeypatch, owned_subscription, foreign_subscription):
    """Make the authoritative lookup return only the subscription owned by OWNER_ID."""
    # Легаси-идентичность пользователя заполнена намеренно: подстановка её вместо
    # id выбранной подписки должна ловиться как «дёрнули не ту личность», а не
    # маскироваться под «панель вообще не дёрнули».
    user = _user(owned_subscription, foreign_subscription, remnawave_id=LEGACY_USER_PANEL_ID)
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))

    async def get_owned_subscription(_db, subscription_id, user_id):
        if (subscription_id, user_id) == (OWNED_ID, OWNER_ID):
            return owned_subscription
        raise HTTPException(status_code=404, detail='Subscription not found for this user')

    monkeypatch.setattr(
        admin_users,
        '_get_owned_subscription_or_404',
        get_owned_subscription,
    )
    monkeypatch.setattr('app.database.crud.user.get_user_by_id', AsyncMock(return_value=user))
    return user


class _Api:
    """Двойник клиента 3.0.0: user-методы адресуют панельного юзера числовым id.

    Каждый вызов кладёт полученный идентификатор в ``calls['panel_ids']`` —
    так тест видит не только «панель дёрнули», но и «дёрнули за ту личность».
    """

    def __init__(self, calls: dict):
        self.calls = calls

    def _record(self, name: str, user_id) -> None:
        self.calls[name] += 1
        self.calls['panel_ids'].append(user_id)

    async def get_user_by_id(self, user_id):
        self._record('get_user_by_id', user_id)
        return SimpleNamespace(
            id=user_id,
            trojan_password='owned-panel-marker',  # pragma: allowlist secret
            vless_uuid='owned-vless',
            ss_password='owned-ss',  # pragma: allowlist secret
            subscription_url='https://owned.example/sub',
            happ_link=None,
            used_traffic_bytes=1,
            lifetime_used_traffic_bytes=2,
            traffic_limit_bytes=3,
            first_connected_at=None,
            online_at=None,
            user_traffic=None,
        )

    async def get_user_devices_all(self, user_id):
        self._record('get_user_devices_all', user_id)
        return {'devices': [{'hwid': 'owned-hwid', 'platform': 'ios'}], 'total': 1}

    async def get_subscription_request_history(self, user_id):
        # offset/limit убраны из клиента: панель их игнорировала.
        self._record('get_subscription_request_history', user_id)
        return {'total': 1, 'records': [{'ip': '192.0.2.1'}]}

    async def remove_device(self, user_id, _hwid):
        self._record('remove_device', user_id)
        return True


@pytest.fixture
def panel_service(monkeypatch):
    calls = {
        'constructed': 0,
        'get_api_client': 0,
        'client_entered': 0,
        'get_user_by_id': 0,
        'get_user_devices_all': 0,
        'get_subscription_request_history': 0,
        'remove_device': 0,
        'panel_ids': [],
    }

    class Service:
        is_configured = True

        def __init__(self):
            calls['constructed'] += 1

        def get_api_client(self):
            calls['get_api_client'] += 1
            context = MagicMock()

            async def enter():
                calls['client_entered'] += 1
                return _Api(calls)

            context.__aenter__ = AsyncMock(side_effect=enter)
            context.__aexit__ = AsyncMock(return_value=None)
            return context

    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', Service)
    monkeypatch.setattr(admin_users, 'get_aliases_for_user', AsyncMock(return_value={}))
    return calls


async def test_authoritative_subscription_lookup_constrains_id_and_user_id(owned_subscription):
    """Would fail if a route reverted to an id-only subscription lookup."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = owned_subscription
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    assert await admin_users._get_owned_subscription_or_404(db, OWNED_ID, OWNER_ID) is owned_subscription

    query = str(db.execute.await_args.args[0])
    assert 'subscriptions.id' in query
    assert 'subscriptions.user_id' in query


@pytest.mark.parametrize(
    ('route_name', 'kwargs', 'foreign_marker'),
    [
        ('get_user_panel_info', {}, 'owned-panel-marker'),
        ('get_user_devices', {}, 'owned-hwid'),
        ('get_subscription_request_history', {}, '192.0.2.1'),
        ('delete_user_device', {'hwid': 'owned-hwid'}, 'Device deleted'),
    ],
    ids=['panel-info', 'devices', 'request-history', 'delete-device'],
)
@pytest.mark.parametrize('multi_tariff', [True, False], ids=['multi-tariff', 'single-tariff'])
async def test_subscription_reads_and_device_delete_accept_owned_subscription(
    monkeypatch, ownership_boundary, panel_service, route_name, kwargs, foreign_marker, multi_tariff
):
    """Every BP-S route uses the selected owned subscription in either mode."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: multi_tariff)
    route = getattr(admin_users, route_name)
    db = AsyncMock()
    admin = SimpleNamespace(id=1)

    owned_result = await route(OWNER_ID, admin=admin, db=db, subscription_id=OWNED_ID, **kwargs)
    assert foreign_marker in str(owned_result)
    # Панель адресуется id выбранной подписки, а не пользовательским/легаси.
    assert panel_service['panel_ids'] == [OWNED_PANEL_ID]


@pytest.mark.parametrize(
    ('route_name', 'kwargs'),
    [
        ('get_user_panel_info', {}),
        ('get_user_devices', {}),
        ('get_subscription_request_history', {}),
        ('delete_user_device', {'hwid': 'owned-hwid'}),
    ],
    ids=['panel-info', 'devices', 'request-history', 'delete-device'],
)
@pytest.mark.parametrize('multi_tariff', [True, False], ids=['multi-tariff', 'single-tariff'])
async def test_subscription_reads_and_device_delete_reject_foreign_and_absent_without_panel_access(
    monkeypatch, ownership_boundary, panel_service, route_name, kwargs, multi_tariff
):
    """No rejected BP-S request may construct or call the panel client in either mode."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: multi_tariff)
    route = getattr(admin_users, route_name)
    db = AsyncMock()
    admin = SimpleNamespace(id=1)

    for subscription_id in (FOREIGN_ID, MISSING_ID):
        with pytest.raises(HTTPException) as rejected:
            await route(OWNER_ID, admin=admin, db=db, subscription_id=subscription_id, **kwargs)
        assert rejected.value.status_code == 404
        assert panel_service == {
            'constructed': 0,
            'get_api_client': 0,
            'client_entered': 0,
            'get_user_by_id': 0,
            'get_user_devices_all': 0,
            'get_subscription_request_history': 0,
            'remove_device': 0,
            'panel_ids': [],
        }


@pytest.mark.parametrize('multi_tariff', [True, False], ids=['multi-tariff', 'single-tariff'])
@pytest.mark.parametrize('subscription_id', [FOREIGN_ID, MISSING_ID], ids=['foreign', 'absent'])
async def test_panel_info_validates_supplied_subscription_before_unconfigured_service_access(
    monkeypatch, ownership_boundary, multi_tariff, subscription_id
):
    """Would fail if panel-info checks service configuration before ownership."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: multi_tariff)
    calls = {'constructed': 0, 'get_api_client': 0}

    class UnconfiguredService:
        is_configured = False

        def __init__(self):
            calls['constructed'] += 1

        def get_api_client(self):
            calls['get_api_client'] += 1
            raise AssertionError('ownership rejection must not request a panel client')

    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', UnconfiguredService)

    with pytest.raises(HTTPException) as rejected:
        await admin_users.get_user_panel_info(
            OWNER_ID,
            admin=SimpleNamespace(id=1),
            db=AsyncMock(),
            subscription_id=subscription_id,
        )

    assert rejected.value.status_code == 404
    assert calls == {'constructed': 0, 'get_api_client': 0}


async def test_panel_info_does_not_fall_back_to_user_panel_id_for_selected_unlinked_subscription(
    monkeypatch, panel_service
):
    """Would fail if a selected null-link subscription leaked legacy panel information."""
    selected = _subscription(OWNED_ID, OWNER_ID, panel_id=None)
    user = _user(selected, remnawave_id=LEGACY_USER_PANEL_ID)
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))

    async def get_selected(_db, subscription_id, user_id):
        assert (subscription_id, user_id) == (OWNED_ID, OWNER_ID)
        return selected

    monkeypatch.setattr(admin_users, '_get_owned_subscription_or_404', get_selected)

    response = await admin_users.get_user_panel_info(
        OWNER_ID,
        admin=SimpleNamespace(id=1),
        db=AsyncMock(),
        subscription_id=OWNED_ID,
    )

    assert response.found is False
    assert panel_service == {
        'constructed': 0,
        'get_api_client': 0,
        'client_entered': 0,
        'get_user_by_id': 0,
        'get_user_devices_all': 0,
        'get_subscription_request_history': 0,
        'remove_device': 0,
        'panel_ids': [],
    }


@pytest.mark.parametrize(
    ('action', 'request_kwargs', 'state_field'),
    [
        ('set_end_date', {'end_date': datetime.now(UTC) + timedelta(days=30)}, 'end_date'),
        ('set_traffic', {'traffic_limit_gb': 999}, 'traffic_limit_gb'),
        ('set_device_limit', {'device_limit': 99}, 'device_limit'),
        ('reset', {}, 'status'),
    ],
    ids=['set-expiry', 'set-traffic', 'set-device-limit', 'reset-one-subscription'],
)
async def test_subscription_actions_accept_an_owned_subscription(
    monkeypatch, ownership_boundary, owned_subscription, action, request_kwargs, state_field
):
    """An ownership guard must not reject the requested user's own subscription."""
    sync = AsyncMock()
    panel_disable = AsyncMock(return_value=True)
    reset_subscription = AsyncMock()
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', sync)
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.disable_remnawave_user', panel_disable)
    monkeypatch.setattr('app.database.crud.subscription.reset_subscription', reset_subscription)
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
    db = AsyncMock()
    request = UpdateSubscriptionRequest(action=action, subscription_id=OWNED_ID, **request_kwargs)

    result = await admin_users.update_user_subscription(OWNER_ID, request, admin=SimpleNamespace(id=1), db=db)

    assert result.success is True
    if action == 'reset':
        panel_disable.assert_awaited_once_with(owned_subscription.remnawave_id)
        reset_subscription.assert_awaited_once_with(db, owned_subscription)
    else:
        sync.assert_awaited_once_with(db, ownership_boundary, owned_subscription, pinned_subscription_identity=True)
        assert db.commit.await_count == 1


@pytest.mark.parametrize(
    ('action', 'request_kwargs'),
    [
        ('set_end_date', {'end_date': datetime.now(UTC) + timedelta(days=30)}),
        ('set_traffic', {'traffic_limit_gb': 999}),
        ('set_device_limit', {'device_limit': 99}),
    ],
    ids=['set-expiry', 'set-traffic', 'set-device-limit'],
)
async def test_selected_actions_pin_sync_to_the_selected_identity_when_legacy_mode_is_enabled(
    monkeypatch, ownership_boundary, owned_subscription, action, request_kwargs
):
    """A supplied subscription id must opt out of all single-tariff fallbacks."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    ownership_boundary.remnawave_id = LEGACY_USER_PANEL_ID
    owned_subscription.remnawave_id = OWNED_PANEL_ID
    sync = AsyncMock()
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', sync)
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))

    result = await admin_users.update_user_subscription(
        OWNER_ID,
        UpdateSubscriptionRequest(action=action, subscription_id=OWNED_ID, **request_kwargs),
        admin=SimpleNamespace(id=1),
        db=AsyncMock(),
    )

    assert result.success is True
    sync.assert_awaited_once_with(
        ANY,
        ownership_boundary,
        owned_subscription,
        pinned_subscription_identity=True,
    )


async def test_selected_sync_uses_only_selected_panel_id_when_legacy_mode_is_enabled(monkeypatch):
    """The helper itself must not substitute the user's legacy panel user id."""
    looked_up: list[int] = []
    updated: list[int] = []

    class Api:
        async def get_user_by_id(self, panel_user_id):
            looked_up.append(panel_user_id)
            return SimpleNamespace(id=panel_user_id)

    class Service:
        is_configured = True

        def get_api_client(self):
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=Api())
            context.__aexit__ = AsyncMock(return_value=None)
            return context

    async def update_panel_user(_api, _subscription_id, **kwargs):
        # В 3.0.0 клиент адресует пользователя kwargs['user_id'], а не uuid.
        updated.append(kwargs['user_id'])
        return SimpleNamespace(
            subscription_url='https://selected.example/sub',
            happ_crypto_link='crypto',
            short_uuid='short',
        )

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', Service)
    monkeypatch.setattr('app.services.grace_access_runtime.update_panel_user_grace_safe', update_panel_user)
    monkeypatch.setattr('app.services.subscription_service.get_traffic_reset_strategy', lambda _tariff: 'NO_RESET')
    monkeypatch.setattr('app.utils.subscription_utils.resolve_hwid_device_limit_for_payload', lambda _sub: None)
    user = SimpleNamespace(
        id=OWNER_ID,
        full_name='Owner',
        username=None,
        telegram_id=1000,
        email=None,
        remnawave_id=LEGACY_USER_PANEL_ID,
        last_remnawave_sync=None,
    )
    subscription = SimpleNamespace(
        id=OWNED_ID,
        status='active',
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_id=OWNED_PANEL_ID,
        remnawave_short_id=None,
        traffic_limit_gb=10,
        tariff=None,
        connected_squads=[],
        device_limit=1,
        subscription_url=None,
        subscription_crypto_link=None,
        remnawave_short_uuid=None,
    )
    db = AsyncMock()

    changes = await admin_users._sync_subscription_to_panel(db, user, subscription, pinned_subscription_identity=True)

    assert changes.get('action') == 'updated'
    assert looked_up == [OWNED_PANEL_ID]
    assert updated == [OWNED_PANEL_ID]
    assert user.remnawave_id == LEGACY_USER_PANEL_ID


async def test_selected_sync_with_no_panel_link_does_not_substitute_legacy_identity(monkeypatch):
    """A null selected link must cause no panel access, even in legacy mode."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    user = SimpleNamespace(id=OWNER_ID, remnawave_id=LEGACY_USER_PANEL_ID)
    subscription = SimpleNamespace(id=OWNED_ID, remnawave_id=None)
    db = AsyncMock()

    result = await admin_users._sync_subscription_to_panel(db, user, subscription, pinned_subscription_identity=True)

    assert result == {'skipped': True, 'reason': 'Selected subscription has no panel user id'}
    db.commit.assert_not_awaited()


async def test_selected_devices_return_selected_subscription_device_limit(monkeypatch, panel_service):
    primary = _subscription(11, OWNER_ID, panel_id=4001)
    primary.device_limit = 2
    selected = _subscription(OWNED_ID, OWNER_ID, panel_id=OWNED_PANEL_ID)
    selected.device_limit = 9
    user = _user(primary, selected, remnawave_id=LEGACY_USER_PANEL_ID)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(admin_users, '_get_owned_subscription_or_404', AsyncMock(return_value=selected))

    response = await admin_users.get_user_devices(
        OWNER_ID, admin=SimpleNamespace(id=1), db=AsyncMock(), subscription_id=OWNED_ID
    )

    assert response.device_limit == 9
    assert panel_service['panel_ids'] == [OWNED_PANEL_ID]


@pytest.mark.parametrize('raises', [False, True], ids=['false-result', 'exception'])
async def test_selected_reset_returns_unsuccessful_when_panel_deactivation_fails(
    monkeypatch, ownership_boundary, owned_subscription, raises
):
    async def panel_disable(_self, _panel_user_id):
        if raises:
            raise RuntimeError('panel down')
        return False

    reset_subscription = AsyncMock()
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.disable_remnawave_user', panel_disable)
    monkeypatch.setattr('app.database.crud.subscription.reset_subscription', reset_subscription)
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))

    result = await admin_users.update_user_subscription(
        OWNER_ID,
        UpdateSubscriptionRequest(action='reset', subscription_id=OWNED_ID),
        admin=SimpleNamespace(id=1),
        db=AsyncMock(),
    )

    assert result.success is False
    reset_subscription.assert_not_awaited()


async def test_selected_reset_without_link_does_not_substitute_legacy_identity(
    monkeypatch, ownership_boundary, owned_subscription
):
    ownership_boundary.remnawave_id = LEGACY_USER_PANEL_ID
    owned_subscription.remnawave_id = None
    panel_disable = AsyncMock(return_value=True)
    reset_subscription = AsyncMock()
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.disable_remnawave_user', panel_disable)
    monkeypatch.setattr('app.database.crud.subscription.reset_subscription', reset_subscription)
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))

    result = await admin_users.update_user_subscription(
        OWNER_ID,
        UpdateSubscriptionRequest(action='reset', subscription_id=OWNED_ID),
        admin=SimpleNamespace(id=1),
        db=AsyncMock(),
    )

    assert result.success is True
    panel_disable.assert_not_awaited()
    reset_subscription.assert_awaited_once()


async def test_selected_reset_cancels_both_recurring_bindings(monkeypatch, ownership_boundary, owned_subscription):
    """Живая привязка автопродления воскресила бы только что сброшенную подписку.

    Ветка сброса выбранной подписки появилась раньше рекуррента Lava, поэтому
    отменяла только Platega — как и остальные пути сброса, обязана снимать обе.
    """
    platega_cancel = AsyncMock()
    lava_cancel = AsyncMock()
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService.disable_remnawave_user',
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr('app.database.crud.subscription.reset_subscription', AsyncMock())
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', platega_cancel)
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', lava_cancel)
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))

    result = await admin_users.update_user_subscription(
        OWNER_ID,
        UpdateSubscriptionRequest(action='reset', subscription_id=OWNED_ID),
        admin=SimpleNamespace(id=1),
        db=AsyncMock(),
    )

    assert result.success is True
    platega_cancel.assert_awaited_once()
    lava_cancel.assert_awaited_once()


@pytest.mark.parametrize(
    ('action', 'request_kwargs', 'state_field'),
    [
        ('set_end_date', {'end_date': datetime.now(UTC) + timedelta(days=30)}, 'end_date'),
        ('set_traffic', {'traffic_limit_gb': 999}, 'traffic_limit_gb'),
        ('set_device_limit', {'device_limit': 99}, 'device_limit'),
        ('reset', {}, 'status'),
    ],
    ids=['set-expiry', 'set-traffic', 'set-device-limit', 'reset-one-subscription'],
)
async def test_subscription_actions_reject_foreign_or_absent_ids_before_mutation(
    monkeypatch, ownership_boundary, foreign_subscription, action, request_kwargs, state_field
):
    """Would fail if action selection trusted an eagerly loaded subscription list."""
    sync = AsyncMock()
    reset = AsyncMock(return_value={'panel_disabled': True})
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', sync)
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))
    monkeypatch.setattr('app.services.subscription_service.reset_subscription_with_panel', reset)
    db = AsyncMock()
    before = getattr(foreign_subscription, state_field)
    request = UpdateSubscriptionRequest(action=action, subscription_id=FOREIGN_ID, **request_kwargs)

    with pytest.raises(HTTPException) as foreign:
        await admin_users.update_user_subscription(OWNER_ID, request, admin=SimpleNamespace(id=1), db=db)
    assert foreign.value.status_code == 404
    assert getattr(foreign_subscription, state_field) == before
    sync.assert_not_awaited()
    reset.assert_not_awaited()
    db.commit.assert_not_awaited()

    missing_request = UpdateSubscriptionRequest(action=action, subscription_id=MISSING_ID, **request_kwargs)
    with pytest.raises(HTTPException) as missing:
        await admin_users.update_user_subscription(OWNER_ID, missing_request, admin=SimpleNamespace(id=1), db=db)
    assert missing.value.status_code == 404
    assert getattr(foreign_subscription, state_field) == before
    sync.assert_not_awaited()
    reset.assert_not_awaited()
    db.commit.assert_not_awaited()
