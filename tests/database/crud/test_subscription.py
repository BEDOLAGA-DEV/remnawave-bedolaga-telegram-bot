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


async def test_extend_subscription_convert_trial_false_keeps_trial(monkeypatch):
    """Bug #629889 guardrail: extend_subscription(tariff_id=..., convert_trial=False)
    must NOT clear is_trial. A free relabel keeps the sub a trial so it stays gated
    out of try_auto_extend_expired_after_topup and never self-renews to a full period.
    """
    from datetime import UTC, datetime, timedelta

    from app.database.crud.subscription import extend_subscription

    monkeypatch.setattr('app.database.crud.subscription._lock_subscription_row', AsyncMock())
    monkeypatch.setattr('app.database.crud.subscription._housekeep_expired_purchases', AsyncMock())
    monkeypatch.setattr('app.database.crud.subscription.clear_notifications', AsyncMock())
    monkeypatch.setattr(
        'app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(is_daily=False))
    )
    deactivate_mock = AsyncMock(return_value=[])
    monkeypatch.setattr('app.database.crud.subscription.deactivate_user_trial_subscriptions', deactivate_mock)

    db = AsyncMock()
    db.flush = AsyncMock()

    now = datetime.now(UTC)
    sub = SimpleNamespace(
        id=1,
        user_id=7,
        status='trial',
        is_trial=True,
        start_date=now,
        end_date=now + timedelta(days=1),
        tariff_id=1,
        traffic_limit_gb=10,
        traffic_used_gb=0.0,
        device_limit=1,
        connected_squads=[],
        purchased_traffic_gb=0,
        updated_at=now,
    )

    result = await extend_subscription(db, sub, 14, tariff_id=2, convert_trial=False, commit=False)

    assert result.is_trial is True  # NOT converted on a free relabel
    assert result.tariff_id == 2  # the relabel still applied
    deactivate_mock.assert_not_awaited()  # other trials not killed


async def test_extend_subscription_default_converts_trial_on_purchase(monkeypatch):
    """Default convert_trial=True (a real tariff purchase) still clears is_trial."""
    from datetime import UTC, datetime, timedelta

    from app.database.crud.subscription import extend_subscription

    monkeypatch.setattr('app.database.crud.subscription._lock_subscription_row', AsyncMock())
    monkeypatch.setattr('app.database.crud.subscription._housekeep_expired_purchases', AsyncMock())
    monkeypatch.setattr('app.database.crud.subscription.clear_notifications', AsyncMock())
    monkeypatch.setattr(
        'app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(is_daily=False))
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.deactivate_user_trial_subscriptions', AsyncMock(return_value=[])
    )

    db = AsyncMock()
    db.flush = AsyncMock()

    now = datetime.now(UTC)
    sub = SimpleNamespace(
        id=1,
        user_id=7,
        status='trial',
        is_trial=True,
        start_date=now,
        end_date=now + timedelta(days=1),
        tariff_id=1,
        traffic_limit_gb=10,
        traffic_used_gb=0.0,
        device_limit=1,
        connected_squads=[],
        purchased_traffic_gb=0,
        updated_at=now,
    )

    result = await extend_subscription(db, sub, 14, tariff_id=2, commit=False)

    assert result.is_trial is False  # genuine purchase converts the trial


def _trial_sub(sub_id, user_id, panel_uuid):
    from types import SimpleNamespace

    user = SimpleNamespace(id=user_id, remnawave_uuid=panel_uuid)
    return SimpleNamespace(
        id=sub_id,
        user_id=user_id,
        user=user,
        remnawave_uuid=panel_uuid,
        subscription_servers=[],
        connected_squads=[],
    )


def _patch_reset_env(monkeypatch, *, subs, is_configured, delete_side_effect=None):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import app.database.crud.subscription as crud

    # SELECT result -> subs; later delete/update calls ignore the return.
    result_mock = MagicMock()
    result_mock.scalars.return_value.unique.return_value.all.return_value = subs
    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    fake_api = MagicMock()
    fake_api.delete_user = AsyncMock(side_effect=delete_side_effect)

    @asynccontextmanager
    async def fake_get_api_client():
        yield fake_api

    fake_service = SimpleNamespace(is_configured=is_configured, get_api_client=fake_get_api_client)
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService', lambda: fake_service)

    fake_settings = MagicMock()
    fake_settings.is_multi_tariff_enabled.return_value = False  # single-tariff
    monkeypatch.setattr(crud, 'settings', fake_settings)
    monkeypatch.setattr(crud, 'decrement_subscription_server_counts', AsyncMock())

    return crud, db, fake_api


async def test_reset_trials_deletes_panel_first_and_skips_panel_failures(monkeypatch):
    """#630055-trial: панель удаляется ПЕРВОЙ; если удалить в панели не удалось —
    строку в БД не трогаем (иначе orphan + воскрешение синком)."""
    subs = [_trial_sub(1, 11, 'uuid-ok'), _trial_sub(2, 22, 'uuid-fail')]

    def delete_side_effect(uuid):
        if uuid == 'uuid-fail':
            raise RuntimeError('panel 500')
        return True

    crud, db, fake_api = _patch_reset_env(
        monkeypatch, subs=subs, is_configured=True, delete_side_effect=delete_side_effect
    )

    count = await crud.reset_trials_for_users_without_paid_subscription(db)

    # Панель дёрнули для обоих.
    called = {c.args[0] for c in fake_api.delete_user.await_args_list}
    assert called == {'uuid-ok', 'uuid-fail'}
    # Сбросили только того, у кого панель реально удалилась.
    assert count == 1
    db.commit.assert_awaited()


async def test_reset_trials_panel_not_configured_db_only(monkeypatch):
    """Панель не настроена → orphan'ить нечего, чистим только БД, без вызовов панели."""
    subs = [_trial_sub(1, 11, 'uuid-a'), _trial_sub(2, 22, 'uuid-b')]
    crud, db, fake_api = _patch_reset_env(monkeypatch, subs=subs, is_configured=False)

    count = await crud.reset_trials_for_users_without_paid_subscription(db)

    fake_api.delete_user.assert_not_awaited()
    assert count == 2
    db.commit.assert_awaited()


def test_is_trial_already_used_gate():
    """Единый гейт триала (раньше дублировался в 4 местах purchase.py)."""
    from app.database.models import Subscription, SubscriptionStatus, User

    def _user(paid, sub=None):
        u = User(has_had_paid_subscription=paid)
        u.subscriptions = [sub] if sub is not None else []
        return u

    # уже платил → заблокирован
    assert _user(True).is_trial_already_used() is True
    # не платил, нет подписки → можно
    assert _user(False).is_trial_already_used() is False
    # не платил, есть платная активная подписка → заблокирован
    paid_sub = Subscription(status=SubscriptionStatus.ACTIVE.value, is_trial=False)
    assert _user(False, paid_sub).is_trial_already_used() is True
    # не платил, активный триал → заблокирован (второй триал нельзя)
    active_trial = Subscription(status=SubscriptionStatus.TRIAL.value, is_trial=True)
    assert _user(False, active_trial).is_trial_already_used() is True
    # не платил, PENDING-триал (повторная попытка оплаты) → можно
    pending_trial = Subscription(status=SubscriptionStatus.PENDING.value, is_trial=True)
    assert _user(False, pending_trial).is_trial_already_used() is False
