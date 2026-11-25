"""Тесты подписочных методов PlategaService (SBP recurring, задача 4)."""

import contextlib
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.crud import platega_subscription as sub_crud
from app.database.models import Base, PlategaSubscription
from app.services.monitoring_service import MonitoringService
from app.services.payment.payer_identity import PayerIdentity
from app.services.platega_service import PlategaService


PAYER = PayerIdentity(user_id='555', user_name='@neo', contact='555', platega_email='555@telegram.org')


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    values = {
        'PLATEGA_ENABLED': True,
        'PLATEGA_MERCHANT_ID': 'm',
        'PLATEGA_SECRET': 's',
        'PLATEGA_BASE_URL': 'https://app.platega.io',
        'PLATEGA_API_VERSION': 'v1',
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)


# --- create_subscription ---


@pytest.mark.asyncio
async def test_create_subscription_posts_method_6(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    service = PlategaService()
    captured = {}

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        captured.update(method=method, endpoint=endpoint, json_data=json_data)
        payload = {'transactionId': 'tx-1', 'redirect': 'https://pay/x', 'status': 'PENDING'}
        return (payload, 200) if return_status else payload

    monkeypatch.setattr(service, '_request', fake_request)
    res = await service.create_subscription(payer=PAYER, amount=199.0, currency='RUB', interval=3, description='Тариф')

    assert captured['method'] == 'POST'
    assert captured['endpoint'] == '/transaction/process'
    assert captured['json_data']['paymentMethod'] == 6
    # docs.platega.io «Создать подписку»: intervalCount обязателен; каденс считаем «одно
    # списание за interval» (resolve_platega_interval) — значит, 1.
    assert captured['json_data']['paymentDetails'] == {
        'amount': 199,
        'currency': 'RUB',
        'interval': 3,
        'intervalCount': 1,
    }
    assert captured['json_data']['description'] == 'Тариф'
    assert res['transactionId'] == 'tx-1'


@pytest.mark.asyncio
async def test_create_subscription_uses_v2_endpoint_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, PLATEGA_API_VERSION='v2')
    service = PlategaService()
    captured = {}

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        captured.update(method=method, endpoint=endpoint)
        payload = {'transactionId': 'tx-2', 'status': 'PENDING'}
        return (payload, 200) if return_status else payload

    monkeypatch.setattr(service, '_request', fake_request)
    await service.create_subscription(payer=PAYER, amount=149.5, currency='RUB', interval=1)

    assert service.api_version == 'v2'
    assert captured['endpoint'] == '/v2/transaction/process'


@pytest.mark.asyncio
async def test_create_subscription_omits_description_when_not_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    service = PlategaService()
    captured = {}

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        captured.update(json_data=json_data)
        payload = {'transactionId': 'tx-3', 'status': 'PENDING'}
        return (payload, 200) if return_status else payload

    monkeypatch.setattr(service, '_request', fake_request)
    await service.create_subscription(payer=PAYER, amount=100.0, currency='RUB', interval=2)

    assert 'description' not in captured['json_data']


@pytest.mark.asyncio
async def test_create_subscription_truncates_long_cyrillic_description(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    service = PlategaService()
    captured = {}

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        captured.update(json_data=json_data)
        payload = {'transactionId': 'tx-4', 'status': 'PENDING'}
        return (payload, 200) if return_status else payload

    monkeypatch.setattr(service, '_request', fake_request)
    long_description = 'Премиум тариф на 3 месяца безлимит и ещё немного текста'
    await service.create_subscription(
        payer=PAYER, amount=199.0, currency='RUB', interval=3, description=long_description
    )

    description_in_body = captured['json_data']['description']
    # Verify truncated to 64 bytes
    assert len(description_in_body.encode('utf-8')) <= 64
    # Verify it's a valid UTF-8 string and a prefix of the original
    assert long_description.startswith(description_in_body)
    # Verify it's not empty
    assert description_in_body


# --- get_subscription ---


@pytest.mark.asyncio
async def test_get_subscription_is_unversioned(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, PLATEGA_API_VERSION='v2')
    service = PlategaService()
    captured = {}

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        captured.update(method=method, endpoint=endpoint)
        payload = {'subscriptionId': 'sub-1', 'status': 'active'}
        return (payload, 200) if return_status else payload

    monkeypatch.setattr(service, '_request', fake_request)
    res = await service.get_subscription('sub-1')

    assert captured == {'method': 'GET', 'endpoint': '/subscription/sub-1'}
    assert res['subscriptionId'] == 'sub-1'


# --- list_subscriptions ---


@pytest.mark.asyncio
async def test_list_subscriptions_builds_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    service = PlategaService()
    captured = {}

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        captured.update(method=method, endpoint=endpoint, params=params)
        payload = {'items': [], 'total': 0}
        return (payload, 200) if return_status else payload

    monkeypatch.setattr(service, '_request', fake_request)
    await service.list_subscriptions(status='active', date_from='2026-01-01', date_to='2026-02-01', page=2, size=10)

    assert captured['method'] == 'GET'
    assert captured['endpoint'] == '/subscription'
    assert captured['params'] == {
        'status': 'active',
        'from': '2026-01-01',
        'to': '2026-02-01',
        'page': 2,
        'size': 10,
    }


@pytest.mark.asyncio
async def test_list_subscriptions_omits_none_params(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    service = PlategaService()
    captured = {}

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        captured.update(endpoint=endpoint, params=params)
        payload = {'items': []}
        return (payload, 200) if return_status else payload

    monkeypatch.setattr(service, '_request', fake_request)
    await service.list_subscriptions()

    assert captured['endpoint'] == '/subscription'
    assert captured['params'] == {}


# --- cancel_subscription ---


@pytest.mark.asyncio
async def test_cancel_subscription_posts_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    service = PlategaService()
    captured = {}

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        captured.update(method=method, endpoint=endpoint, json_data=json_data)
        payload = {'subscriptionId': 'sub-1', 'status': 'cancelled'}
        return (payload, 200) if return_status else payload

    monkeypatch.setattr(service, '_request', fake_request)
    res = await service.cancel_subscription('sub-1')

    assert captured['method'] == 'POST'
    assert captured['endpoint'] == '/subscription/sub-1/cancel'
    assert res['status'] == 'cancelled'


# --- _format_amount ---


def test_format_amount_integer_and_decimal() -> None:
    assert PlategaService._format_amount(199.0) == 199
    assert PlategaService._format_amount(149.5) == 149.5
    assert isinstance(PlategaService._format_amount(199.0), int)
    assert isinstance(PlategaService._format_amount(149.5), float)


# --- config gate ---


def test_recurrent_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PLATEGA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_MERCHANT_ID', 'm', raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_SECRET', 's', raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_RECURRENT_ENABLED', False, raising=False)
    assert settings.is_platega_recurrent_enabled() is False
    monkeypatch.setattr(settings, 'PLATEGA_RECURRENT_ENABLED', True, raising=False)
    assert settings.is_platega_recurrent_enabled() is True


# --- MonitoringService._reconcile_platega_subscriptions (Task 10, reconciler loop) ---


def _ensure_real_aiosqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """See identical helper in test_platega_recurrent_cancel_hooks.py: tests/conftest.py
    stubs sys.modules['aiosqlite'] with a non-functional shim that shadows the real
    installed package and breaks create_async_engine('sqlite+aiosqlite:///:memory:').
    """
    stub = sys.modules.get('aiosqlite')
    if stub is not None and not hasattr(stub, 'connect'):
        monkeypatch.delitem(sys.modules, 'aiosqlite', raising=False)


@contextlib.asynccontextmanager
async def _memory_session(monkeypatch: pytest.MonkeyPatch):
    _ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[PlategaSubscription.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


async def test_reconcile_unconfigured_platega_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неконфигурированный Platega (нет мерчанта/секрета) — no-op до БД.

    Флаг PLATEGA_RECURRENT_ENABLED reconciler больше НЕ гейтит: выключение
    фичи при живых привязках не останавливает списания, а cancel-свип и
    доначисление пропущенных чарджей должны продолжать работать."""
    monkeypatch.setattr(settings, 'PLATEGA_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_RECURRENT_ENABLED', False, raising=False)

    service = MonitoringService()
    # db=None would blow up on first use if the is_configured guard didn't return.
    await service._reconcile_platega_subscriptions(db=None)


async def test_reconcile_cancelled_sweep_runs_with_recurrent_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelled-свип (ретрай недошедших отмен) обязан работать и при
    выключенном PLATEGA_RECURRENT_ENABLED — иначе транзиентный сбой отмены
    никогда не ретраится и Platega продолжает списывать."""
    from unittest.mock import AsyncMock

    _configure(monkeypatch, PLATEGA_RECURRENT_ENABLED=False)

    mock_get = AsyncMock(return_value={'status': 'ACTIVE'})
    mock_cancel = AsyncMock(return_value={'status': 'cancelled'})
    monkeypatch.setattr(PlategaService, 'get_subscription', mock_get)
    monkeypatch.setattr(PlategaService, 'cancel_subscription', mock_cancel)

    async with _memory_session(monkeypatch) as db:
        await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-flagoff',
            status='CANCELLED',
        )

        service = MonitoringService()
        await service._reconcile_platega_subscriptions(db)

        mock_cancel.assert_awaited_once_with('ps-flagoff')


async def test_reconcile_marks_stuck_pending_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety net: a PENDING record that never got a platega_subscription_id back
    (so there's nothing to look up on Platega's side) and is older than 30 minutes
    gets marked FAILED — the stuck-pending branch of platega_reconcile_decision."""
    _configure(monkeypatch, PLATEGA_RECURRENT_ENABLED=True)

    async with _memory_session(monkeypatch) as db:
        record = await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id=None,
            status='PENDING',
        )
        record.created_at = datetime.now(UTC) - timedelta(minutes=45)
        await db.commit()
        await db.refresh(record)

        service = MonitoringService()
        await service._reconcile_platega_subscriptions(db)

        await db.refresh(record)
        assert record.status == 'FAILED'


async def test_reconcile_recancels_remotely_active_cancelled_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Контрольный свип отменённых: локальный CANCELLED, но remote-статус
    активный (cancel-запрос когда-то не дошёл) — reconciler должен повторить
    удалённую отмену, иначе Platega продолжит списывать по «отменённой» у
    нас подписке.
    """
    from unittest.mock import AsyncMock

    _configure(monkeypatch, PLATEGA_RECURRENT_ENABLED=True)

    mock_get = AsyncMock(return_value={'status': 'ACTIVE'})
    mock_cancel = AsyncMock(return_value={'status': 'cancelled'})
    monkeypatch.setattr(PlategaService, 'get_subscription', mock_get)
    monkeypatch.setattr(PlategaService, 'cancel_subscription', mock_cancel)

    async with _memory_session(monkeypatch) as db:
        record = await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-zombie',
            status='CANCELLED',
        )

        service = MonitoringService()
        await service._reconcile_platega_subscriptions(db)

        mock_get.assert_awaited_once_with('ps-zombie')
        mock_cancel.assert_awaited_once_with('ps-zombie')
        await db.refresh(record)
        assert record.status == 'CANCELLED'  # локальный статус не трогаем


async def test_reconcile_skips_cancelled_record_confirmed_remotely(monkeypatch: pytest.MonkeyPatch) -> None:
    """CANCELLED-запись, у которой remote-статус тоже cancelled, — свип не
    должен дёргать cancel_subscription повторно (лишний трафик к провайдеру)."""
    from unittest.mock import AsyncMock

    _configure(monkeypatch, PLATEGA_RECURRENT_ENABLED=True)

    mock_get = AsyncMock(return_value={'status': 'CANCELLED'})
    mock_cancel = AsyncMock()
    monkeypatch.setattr(PlategaService, 'get_subscription', mock_get)
    monkeypatch.setattr(PlategaService, 'cancel_subscription', mock_cancel)

    async with _memory_session(monkeypatch) as db:
        await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-done',
            status='CANCELLED',
        )

        service = MonitoringService()
        await service._reconcile_platega_subscriptions(db)

        mock_cancel.assert_not_awaited()


async def test_create_subscription_raises_actionable_error_on_val0001(monkeypatch: pytest.MonkeyPatch) -> None:
    """VAL_0001 с key=paymentMethod (формат запроса совпадает с доками) =
    метод Subscription не включён у мерчанта — ошибка должна нести
    человекочитаемую причину и подсказку, а не превращаться в голый None."""
    from app.services.platega_service import PlategaApiError

    _configure(monkeypatch)
    service = PlategaService()

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        assert return_status is True
        return (
            {
                'code': 'Common:VAL_0001',
                'type': 4001,
                'message': 'Wrong input parameters',
                'data': [{'key': 'paymentMethod', 'message': 'Subscription'}],
            },
            400,
        )

    monkeypatch.setattr(service, '_request', fake_request)

    with pytest.raises(PlategaApiError) as exc_info:
        await service.create_subscription(payer=PAYER, amount=199, currency='RUB', interval=3, description='x')

    assert exc_info.value.http_status == 400
    assert 'paymentMethod: Subscription' in str(exc_info.value)
    assert 'не включён' in str(exc_info.value)


async def test_create_subscription_transport_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Транспортный сбой (status=None) — прежний контракт: None, без исключения."""
    _configure(monkeypatch)
    service = PlategaService()

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        return (None, None)

    monkeypatch.setattr(service, '_request', fake_request)

    result = await service.create_subscription(payer=PAYER, amount=199, currency='RUB', interval=3)
    assert result is None


@pytest.mark.asyncio
async def test_create_subscription_sends_payer_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """СБП-подписка — тот же POST /transaction/process: metadata обязательна и здесь."""
    _configure(monkeypatch)
    service = PlategaService()
    captured = {}

    async def fake_request(method, endpoint, *, json_data=None, params=None, return_status=False):
        captured.update(json_data=json_data)
        payload = {'transactionId': 'tx-1', 'redirect': 'https://pay/x', 'status': 'PENDING'}
        return (payload, 200) if return_status else payload

    monkeypatch.setattr(service, '_request', fake_request)
    await service.create_subscription(payer=PAYER, amount=199.0, currency='RUB', interval=3)

    assert captured['json_data']['metadata'] == {'userId': '555', 'userName': '@neo', 'user_email': '555@telegram.org'}


@pytest.mark.asyncio
async def test_reconcile_throttles_repeat_polls_and_aborts_on_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторный цикл сразу следом не опрашивает заново уже сверенные ACTIVE/CANCELLED
    даже после внутреннего db.rollback() при синке панели, а ошибки транспорта не
    утраиваются ретраями и прерывают оставшиеся HTTP-вызовы текущего тика после двух подряд."""
    from unittest.mock import AsyncMock

    import aiohttp

    from app.services.payment import platega as platega_module

    _configure(monkeypatch, PLATEGA_RECURRENT_ENABLED=True)
    status_mock = AsyncMock(return_value=({'subscriptionId': 'ps-act-1', 'status': 'active'}, 200))
    get_mock = AsyncMock(return_value={'subscriptionId': 'ps-can-1', 'status': 'cancelled'})
    monkeypatch.setattr(PlategaService, 'get_subscription_status', status_mock)
    monkeypatch.setattr(PlategaService, 'get_subscription', get_mock)

    async def replay_with_panel_rollback(db, record, remote):
        # Эмулирует db.rollback() внутри best-effort синка панели (экспайрит ORM-атрибуты record).
        await db.rollback()
        return 0

    monkeypatch.setattr(platega_module, 'replay_missed_platega_charges', replay_with_panel_rollback)

    async with _memory_session(monkeypatch) as db:
        await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-act-1',
            status='ACTIVE',
        )
        await sub_crud.create_platega_subscription(
            db,
            user_id=2,
            subscription_id=2,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-can-1',
            status='CANCELLED',
        )
        svc = MonitoringService()
        await svc._reconcile_platega_subscriptions(db)
        assert status_mock.await_count == 1
        assert get_mock.await_count == 1

        await svc._reconcile_platega_subscriptions(db)
        assert status_mock.await_count == 1
        assert get_mock.await_count == 1

    monkeypatch.undo()
    _configure(monkeypatch, PLATEGA_RECURRENT_ENABLED=True)
    http_attempts = 0

    class _TimeoutRequestCtx:
        async def __aenter__(self):
            nonlocal http_attempts
            http_attempts += 1
            raise TimeoutError('ddos-guard read timeout')

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(aiohttp.ClientSession, 'request', lambda self, *a, **kw: _TimeoutRequestCtx())
    async with _memory_session(monkeypatch) as db:
        for idx in range(5):
            await sub_crud.create_platega_subscription(
                db,
                user_id=10 + idx,
                subscription_id=10 + idx,
                tariff_id=None,
                interval=3,
                charge_days=30,
                amount_kopeks=19900,
                redirect_url=None,
                platega_subscription_id=f'ps-to-{idx}',
                status='PENDING',
            )
        await MonitoringService()._reconcile_platega_subscriptions(db)
        # _max_retries=1 + отсечка consecutive_timeouts>=2 → ровно 2 попытки вместо 5*3=15.
        assert http_attempts == 2


@pytest.mark.asyncio
async def test_cancelled_sweep_replays_paid_period_and_recovers_after_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Свип отменённых подписок доначисляет пропущенный оплаченный период и не
    теряет следующие записи после rollback на сбое первой."""
    from unittest.mock import AsyncMock

    from app.services.payment import platega as platega_module

    _configure(monkeypatch, PLATEGA_RECURRENT_ENABLED=False)
    monkeypatch.setattr(PlategaService, 'get_subscription', AsyncMock(return_value={'status': 'Cancelled'}))
    cancel_mock = AsyncMock()
    monkeypatch.setattr(PlategaService, 'cancel_subscription', cancel_mock)
    seen: list[str] = []

    async def fake_replay(db, record, remote):
        seen.append(record.platega_subscription_id)
        if len(seen) == 1:
            await db.rollback()
            raise TimeoutError('panel timeout after billing commit')
        return 0

    monkeypatch.setattr(platega_module, 'replay_missed_platega_charges', fake_replay)
    async with _memory_session(monkeypatch) as db:
        for sid in ('cancelled-paid-1', 'cancelled-paid-2'):
            await sub_crud.create_platega_subscription(
                db,
                user_id=1,
                subscription_id=1,
                tariff_id=None,
                interval=3,
                charge_days=30,
                amount_kopeks=19900,
                redirect_url=None,
                platega_subscription_id=sid,
                status='CANCELLED',
            )
        svc = MonitoringService()
        await svc._reconcile_platega_subscriptions(db)
        assert set(seen) == {'cancelled-paid-1', 'cancelled-paid-2'}
        cancel_mock.assert_not_awaited()
        # Упавшая первая запись не попала в кэш и должна перепровериться в следующем цикле.
        await svc._reconcile_platega_subscriptions(db)
        assert seen.count('cancelled-paid-1') == 2
        assert seen.count('cancelled-paid-2') == 1
