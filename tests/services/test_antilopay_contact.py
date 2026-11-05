from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers.balance import antilopay
from app.states import BalanceStates


async def test_bot_requests_contact_before_creating_payment(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(antilopay, '_create_antilopay_payment_and_respond', create)
    user = SimpleNamespace(id=1, language='ru', email=None, restriction_topup=False)
    state = AsyncMock()
    state.get_data.return_value = {'payment_method': 'antilopay_sbp'}
    message = AsyncMock()
    await antilopay.process_antilopay_payment_amount(message, user, AsyncMock(), 10000, state)
    create.assert_not_awaited()
    state.set_state.assert_awaited_once_with(BalanceStates.waiting_for_antilopay_email)


async def test_bot_invalid_contact_stays_in_prompt(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(antilopay, '_create_antilopay_payment_and_respond', create)
    user = SimpleNamespace(id=1, language='ru', email=None, restriction_topup=False)
    state = AsyncMock()
    message = AsyncMock()
    message.text = 'not-an-email'
    await antilopay.process_antilopay_email(message, user, AsyncMock(), state)
    create.assert_not_awaited()
    state.clear.assert_not_awaited()


async def test_bot_contact_resumes_original_amount_and_method(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(antilopay, '_create_antilopay_payment_and_respond', create)
    user = SimpleNamespace(id=1, language='ru', email=None, restriction_topup=False)
    state = AsyncMock()
    state.get_data.return_value = {'payment_method': 'antilopay_sbp', 'amount_kopeks': 10001}
    message = AsyncMock()
    message.text = ' buyer@example.com '
    await antilopay.process_antilopay_email(message, user, AsyncMock(), state)
    assert create.await_args.args[3] == 10001
    assert create.await_args.kwargs == {'payment_method_type': 'sbp', 'customer_email': 'buyer@example.com'}
    assert user.email is None


async def test_guest_contact_and_metadata_are_bound_before_create(monkeypatch):
    from app.config import settings
    from app.services.payment_service import PaymentService

    monkeypatch.setattr(type(settings), 'is_antilopay_enabled', lambda _: True)
    purchase = SimpleNamespace(contact_type='email', contact_value='buyer@example.com', buyer_user_id=None)
    monkeypatch.setattr('app.database.crud.landing.get_purchase_by_token', AsyncMock(return_value=purchase))
    service = PaymentService()
    service.create_antilopay_payment = AsyncMock(
        return_value={'local_payment_id': 1, 'order_id': 'masked', 'payment_url': 'https://pay.example/invoice'}
    )
    result = await service.create_guest_payment(
        AsyncMock(),
        amount_kopeks=10001,
        payment_method='antilopay_sbp',
        description='Access',
        purchase_token='masked-token',
        return_url='https://cabinet.example/result',
    )
    assert result
    args = service.create_antilopay_payment.await_args.kwargs
    assert args['email'] == 'buyer@example.com'
    assert args['metadata']['purchase_token'] == 'masked-token'
    assert args['metadata']['purpose'] == 'guest_purchase'
