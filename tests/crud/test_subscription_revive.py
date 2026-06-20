"""
Регрессия: в мульти-тарифе повторная покупка тарифа, чья подписка уже истекла,
должна РЕАНИМИРОВАТЬ ту же запись, а не плодить дубликат.

Partial unique index ``uq_subscriptions_user_tariff_active`` сторожит только
живые статусы (active/trial/limited), поэтому истёкшие дубли одного тарифа
копились пачкой у пользователей (отчёт not2clean, топик «Баги»). Фикс:
``create_paid_subscription`` в мульти-тарифе сначала ищет существующую подписку
тарифа ВКЛЮЧАЯ истёкшие и реанимирует её через ``_revive_paid_subscription``.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.database.crud import subscription as sub_crud
from app.database.models import SubscriptionStatus


def _sub(**kw) -> MagicMock:
    s = MagicMock()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


async def test_revive_expired_starts_fresh_period(monkeypatch):
    """Истёкшую реанимируем: статус active, период с «сейчас», трафик обнулён."""
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    past = datetime.now(UTC) - timedelta(days=5)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=past,
        start_date=past - timedelta(days=30),
        is_trial=False,
        traffic_used_gb=44.0,
        connected_squads=['sq'],
        device_limit=2,
        traffic_limit_gb=100,
    )

    result = await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=True,
    )

    assert result is s
    assert s.status == SubscriptionStatus.ACTIVE.value
    assert s.is_trial is False
    assert s.traffic_used_gb == 0.0
    assert s.end_date > datetime.now(UTC) + timedelta(days=29)  # свежий период ~ now+30
    db.commit.assert_awaited_once()
    db.add.assert_not_called()  # никакой новой записи


async def test_revive_alive_extends_from_end_date(monkeypatch):
    """Ещё живую продлеваем от её end_date, накопленный трафик не сбрасываем."""
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    future = datetime.now(UTC) + timedelta(days=10)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=future,
        start_date=datetime.now(UTC),
        is_trial=False,
        traffic_used_gb=20.0,
        connected_squads=['sq'],
        device_limit=2,
        traffic_limit_gb=100,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=None,
        update_server_counters=False,
        commit=True,
    )

    assert s.end_date > future + timedelta(days=29)  # продлено от end_date, не от now
    assert s.traffic_used_gb == 20.0  # живой трафик не трогаем


async def test_create_paid_subscription_revives_existing_in_multitariff(monkeypatch):
    """Мульти-тариф + есть ИСТЁКШАЯ запись тарифа → revive, без вставки дубля."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    existing = _sub(id=5, user_id=7, tariff_id=3, is_trial=False, status=SubscriptionStatus.EXPIRED.value)
    lookup = AsyncMock(return_value=existing)
    revive = AsyncMock(return_value=existing)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)
    monkeypatch.setattr(sub_crud, '_revive_paid_subscription', revive)
    db = _db()

    result = await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=3)

    assert result is existing
    lookup.assert_awaited_once()
    assert lookup.await_args.kwargs.get('include_inactive') is True
    revive.assert_awaited_once()
    db.add.assert_not_called()  # новую подписку НЕ создавали


async def test_create_paid_subscription_does_not_revive_active(monkeypatch):
    """Активную (не истёкшую) НЕ реанимируем — падаем в обычное создание/IntegrityError."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    active = _sub(id=5, user_id=7, tariff_id=3, is_trial=False, status=SubscriptionStatus.ACTIVE.value)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=active))
    revive = AsyncMock(return_value=active)
    monkeypatch.setattr(sub_crud, '_revive_paid_subscription', revive)
    # short-circuit тяжёлый путь создания сразу после guard
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(side_effect=RuntimeError('reached create')))
    db = _db()

    try:
        await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=3)
    except RuntimeError as e:
        assert str(e) == 'reached create'  # дошли до создания, не реанимировали

    revive.assert_not_awaited()  # active не реанимируется


async def test_create_paid_subscription_skips_revive_without_tariff(monkeypatch):
    """Классический режим (tariff_id=None) — lookup тарифа не дёргаем, создаём как раньше."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)
    # short-circuit тяжёлый путь создания сразу после guard
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(side_effect=RuntimeError('reached create')))
    db = _db()

    try:
        await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, tariff_id=None)
    except RuntimeError as e:
        assert str(e) == 'reached create'  # дошли до создания, не до revive

    lookup.assert_not_awaited()  # без tariff_id поиск тарифа не запускается
