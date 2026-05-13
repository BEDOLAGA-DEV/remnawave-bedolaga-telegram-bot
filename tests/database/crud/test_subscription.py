from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.database.crud.subscription import create_trial_subscription


async def test_create_trial_subscription_uses_all_available_squads_by_default(monkeypatch):
    db = Mock()
    db.add = Mock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=None))
    monkeypatch.setattr('app.database.crud.subscription.generate_unique_short_id', AsyncMock(return_value='abc123'))
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_available_server_squads',
        AsyncMock(
            return_value=[
                SimpleNamespace(squad_uuid='fi-uuid'),
                SimpleNamespace(squad_uuid='ru-uuid'),
            ]
        ),
    )
    get_server_ids_mock = AsyncMock(return_value=[11, 12])
    add_user_to_servers_mock = AsyncMock()
    monkeypatch.setattr('app.database.crud.server_squad.get_server_ids_by_uuids', get_server_ids_mock)
    monkeypatch.setattr('app.database.crud.server_squad.add_user_to_servers', add_user_to_servers_mock)

    subscription = await create_trial_subscription(
        db,
        user_id=1,
        duration_days=14,
        traffic_limit_gb=100,
        device_limit=5,
    )

    assert subscription.connected_squads == ['fi-uuid', 'ru-uuid']
    db.add.assert_called_once_with(subscription)
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(subscription)
    get_server_ids_mock.assert_awaited_once_with(db, ['fi-uuid', 'ru-uuid'])
    add_user_to_servers_mock.assert_awaited_once_with(db, [11, 12])
