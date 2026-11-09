"""Tribute «цифровой товар» в сервисе — маршрутизация вебхука и возврат."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.services import tribute_service as module
from app.services.tribute_service import TributeService


def _event(name='new_digital_product', **overrides):
    payload = {
        'product_id': 456,
        'product_name': 'VPN 1 month',
        'amount': 30000,
        'currency': 'rub',
        'trb_user_id': 'T-31326',
        'telegram_user_id': 12321321,
        'purchase_id': 78901,
        'transaction_id': 234567,
    }
    payload.update(overrides)
    return json.dumps({'name': name, 'created_at': '2026-11-09T10:00:00Z', 'payload': payload})


@pytest.fixture
def service(monkeypatch):
    service = TributeService(bot=MagicMock())
    service._handle_successful_payment = AsyncMock()
    service._handle_refund = AsyncMock()
    monkeypatch.setattr(module, 'alert_logger', MagicMock())
    return service


@pytest.mark.asyncio
async def test_purchase_goes_to_successful_payment(service):
    result = await service.process_webhook(_event())

    assert result == {'status': 'ok', 'event': 'payment'}
    service._handle_successful_payment.assert_awaited_once()
    data = service._handle_successful_payment.await_args.args[0]
    assert (data['payment_id'], data['user_id'], data['amount_kopeks']) == ('digital_78901', 12321321, 30000)
    service._handle_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_goes_to_refund_handler(service):
    result = await service.process_webhook(_event(name='digital_product_refunded', refund_reason='telegram_refund'))

    assert result == {'status': 'ok', 'event': 'refund'}
    service._handle_refund.assert_awaited_once()
    service._handle_successful_payment.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_currency_is_alerted_and_not_processed(service):
    result = await service.process_webhook(_event(currency='usd'))

    assert result == {'status': 'ignored', 'reason': 'unsupported_currency'}
    module.alert_logger.error.assert_called_once()
    service._handle_successful_payment.assert_not_awaited()
    service._handle_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_currency_refund_is_not_processed(service):
    result = await service.process_webhook(_event(name='digital_product_refunded', currency='eur'))

    assert result['reason'] == 'unsupported_currency'
    service._handle_refund.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['new_digital_product', 'digital_product_refunded'])
async def test_email_buyer_without_telegram_id_raises_alert(service, name):
    result = await service.process_webhook(_event(name=name, telegram_user_id=None, trb_user_id='W-1'))

    assert result == {'status': 'ignored', 'reason': 'invalid_data'}
    module.alert_logger.error.assert_called_once()
    service._handle_successful_payment.assert_not_awaited()
    service._handle_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_money_event_without_user_does_not_alert(service):
    payload = json.dumps({'name': 'cancelled_subscription', 'payload': {}})

    result = await service.process_webhook(payload)

    assert result == {'status': 'ignored', 'reason': 'invalid_data'}
    module.alert_logger.error.assert_not_called()


# ---- _handle_refund ----------------------------------------------------------------------------------------


def _refund_data(amount=30000):
    return {'user_id': 12321321, 'amount_kopeks': amount, 'payment_id': 'digital_78901', 'trb_user_id': 'T-31326'}


@pytest.fixture
def refund_env(monkeypatch):
    """Подменяем БД: session, поиск транзакций и пользователя, создание транзакции."""
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    async def fake_get_db():
        yield session

    user = SimpleNamespace(id=7, balance_kopeks=50000, language='ru', updated_at=None)
    existing = {'donation_digital_78901': SimpleNamespace(id=1)}  # исходное зачисление есть

    async def fake_get_tx(_session, external_id, _method):
        return existing.get(external_id)

    env = SimpleNamespace(
        session=session,
        user=user,
        existing=existing,
        create_transaction=AsyncMock(),
        notify=AsyncMock(),
        alert=MagicMock(),
    )
    monkeypatch.setattr(module, 'get_db', fake_get_db)
    monkeypatch.setattr(module, 'get_transaction_by_external_id', fake_get_tx)
    monkeypatch.setattr(module, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(module, 'create_transaction', env.create_transaction)
    monkeypatch.setattr(module, 'alert_logger', env.alert)
    monkeypatch.setattr('app.database.crud.user.lock_user_for_update', AsyncMock(side_effect=lambda _s, u: u))
    return env


@pytest.fixture
def refund_service(refund_env):
    service = TributeService(bot=MagicMock())
    service._send_refund_notification = refund_env.notify
    return service


@pytest.mark.asyncio
async def test_refund_deducts_balance_and_writes_refund_on_internal_user_id(refund_service, refund_env):
    await refund_service._handle_refund(_refund_data())

    assert refund_env.user.balance_kopeks == 20000
    kwargs = refund_env.create_transaction.await_args.kwargs
    assert kwargs['user_id'] == 7  # id пользователя в БД, не Telegram ID
    assert kwargs['amount_kopeks'] == -30000
    assert kwargs['external_id'] == 'refund_digital_78901'
    assert kwargs['commit'] is False
    refund_env.session.commit.assert_awaited_once()
    refund_env.notify.assert_awaited_once()
    refund_env.alert.error.assert_not_called()


@pytest.mark.asyncio
async def test_refund_replay_hits_unique_constraint_and_changes_nothing(refund_service, refund_env):
    # refund_<payment_id> уже есть в БД: flush даёт IntegrityError — это повтор доставки или гонка вебхуков
    refund_env.create_transaction.side_effect = IntegrityError('insert', {}, Exception('duplicate key'))

    await refund_service._handle_refund(_refund_data())  # не поднимает, Tribute получит 200

    assert refund_env.user.balance_kopeks == 50000
    refund_env.session.rollback.assert_awaited_once()
    refund_env.session.commit.assert_not_awaited()
    refund_env.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_without_original_credit_changes_nothing_and_alerts(refund_service, refund_env):
    refund_env.existing.clear()

    await refund_service._handle_refund(_refund_data())

    assert refund_env.user.balance_kopeks == 50000
    refund_env.create_transaction.assert_not_awaited()
    refund_env.alert.error.assert_called_once()


@pytest.mark.asyncio
async def test_refund_larger_than_balance_zeroes_balance_and_alerts(refund_service, refund_env):
    refund_env.user.balance_kopeks = 10000

    await refund_service._handle_refund(_refund_data(amount=30000))

    assert refund_env.user.balance_kopeks == 0  # не уходим в минус
    assert refund_env.create_transaction.await_args.kwargs['amount_kopeks'] == -30000
    refund_env.alert.error.assert_called_once()


@pytest.mark.asyncio
async def test_refund_for_unknown_user_changes_nothing_and_alerts(refund_service, refund_env, monkeypatch):
    monkeypatch.setattr(module, 'get_user_by_telegram_id', AsyncMock(return_value=None))

    await refund_service._handle_refund(_refund_data())

    refund_env.create_transaction.assert_not_awaited()
    refund_env.alert.error.assert_called_once()


@pytest.mark.asyncio
async def test_refund_error_before_commit_is_raised_so_tribute_retries(refund_service, refund_env):
    refund_env.create_transaction.side_effect = RuntimeError('db down')

    with pytest.raises(RuntimeError):
        await refund_service._handle_refund(_refund_data())

    refund_env.session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_error_after_commit_is_swallowed(refund_service, refund_env):
    refund_env.notify.side_effect = RuntimeError('telegram down')

    await refund_service._handle_refund(_refund_data())  # не поднимает: деньги уже списаны

    assert refund_env.user.balance_kopeks == 20000
    refund_env.alert.error.assert_called_once()
