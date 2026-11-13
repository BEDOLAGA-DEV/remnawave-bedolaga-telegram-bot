from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PaymentMethod


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'partially_captured', 'requires_action', 'authorized'])
def test_payhot_late_success_remains_checkable_everywhere(status):
    from app.cabinet.routes import admin_payments, balance
    from app.handlers.admin import payments as telegram_admin
    from app.services.payment_verification_service import _is_payhot_pending

    payment = SimpleNamespace(is_paid=False, status=status)
    record = SimpleNamespace(method=PaymentMethod.PAYHOT, is_paid=False, status=status, is_recent=lambda: True)
    assert _is_payhot_pending(payment)
    assert balance._is_checkable(record)
    assert admin_payments._is_checkable(record)
    assert telegram_admin._is_checkable(record)


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'fa', 'zh'])
async def test_payhot_checkout_copy_uses_neutral_button(monkeypatch, language):
    from app.handlers.balance import payhot
    from app.localization.texts import get_texts

    service = SimpleNamespace(
        create_payhot_payment=AsyncMock(
            return_value={
                'payment_url': 'https://checkout.example/payment#token=example',
            }
        )
    )
    monkeypatch.setattr(payhot, 'PaymentService', lambda: service)
    message = AsyncMock()
    user = SimpleNamespace(id=1, telegram_id=None, language=language, email=None)
    await payhot._create_payhot_payment_and_respond(message, user, AsyncMock(), 10000)
    texts = get_texts(language)
    response = message.answer.await_args.args[0]
    expected = texts.t('PAYHOT_PAYMENT_CREATED')
    assert response == expected.format(name=payhot.settings.get_payhot_display_name_html(), amount='100.00')
    button = message.answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.text == texts.t('PAYHOT_CHECKOUT_BUTTON')
    assert '100' not in button.text


async def test_default_sberpay_option_collects_phone_before_any_invoice(monkeypatch):
    from app.handlers.balance import payhot
    from app.states import BalanceStates

    monkeypatch.setattr(
        payhot,
        'get_payhot_checkout_policy',
        AsyncMock(
            return_value={
                'min_amount_kopeks': 10000,
                'max_amount_kopeks': 20000,
                'options': [{'id': 'sberpay'}],
            }
        ),
    )
    create = AsyncMock()
    monkeypatch.setattr(payhot, '_create_payhot_payment_and_respond', create)
    state = AsyncMock()
    state.get_data.return_value = {'payment_method': 'payhot'}
    user = SimpleNamespace(id=1, language='ru', restriction_topup=False)
    await payhot.process_payhot_payment_amount(AsyncMock(), user, AsyncMock(), 10000, state)
    state.set_state.assert_awaited_once_with(BalanceStates.waiting_for_payhot_phone)
    create.assert_not_awaited()


def test_payhot_terminal_paid_and_refunded_not_repolled():
    from app.services.payment_verification_service import _is_payhot_pending

    assert not _is_payhot_pending(SimpleNamespace(is_paid=True, status='failed'))
    assert not _is_payhot_pending(SimpleNamespace(is_paid=False, status='refunded'))


async def test_background_recovers_old_paid_guest_without_changing_pending_ui(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock

    from app.services import payment_verification_service as verification

    payment = SimpleNamespace(
        id=7,
        user=None,
        is_paid=True,
        fulfillment_state={'state': 'pending'},
        order_id='example-order',
        amount_kopeks=10000,
        status='succeeded',
        created_at=datetime.now(UTC) - timedelta(days=3),
        expires_at=None,
    )
    db = AsyncMock()
    db.in_transaction = MagicMock(return_value=False)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [payment]
    db.execute.return_value = result
    records = await verification._fetch_payhot_fulfillment_recovery(db)
    assert len(records) == 1
    assert records[0].is_paid
    assert records[0].user is None
    assert not records[0].is_recent()
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = db
    monkeypatch.setattr(verification, 'AsyncSessionLocal', factory)
    monkeypatch.setattr(verification, 'list_recent_pending_payments', AsyncMock(return_value=[]))
    monkeypatch.setattr(verification, '_fetch_payhot_fulfillment_recovery', AsyncMock(return_value=records))
    check = AsyncMock(return_value=records[0])
    monkeypatch.setattr(verification, 'run_manual_check', check)
    service = verification.AutoPaymentVerificationService()
    service._payment_service = object()
    await service._run_checks([PaymentMethod.PAYHOT])
    check.assert_awaited_once_with(db, PaymentMethod.PAYHOT, 7, service._payment_service)


@pytest.mark.parametrize(
    'extra',
    [
        {'origin': 'recurring'},
        {'recurring_charge_id': '00000000-0000-4000-8000-000000000002'},
        {'merchant_recurring_charge_reference': 'renewal-example'},
    ],
)
def test_ordinary_invoice_rejects_recurring_charge_even_when_order_matches(extra):
    from app.services.payment.payhot import _validate_remote

    remote_id = '00000000-0000-4000-8000-000000000001'
    payment = SimpleNamespace(
        payhot_payment_id=remote_id, order_id='order-example', amount_kopeks=10000, payment_method='card'
    )
    data = {
        'id': remote_id,
        'merchant_order_reference': payment.order_id,
        'amount_minor': '10000',
        'currency': 'RUB',
        'payment_method': 'card',
        'status': 'succeeded',
        **extra,
    }
    with pytest.raises(ValueError, match='recurring'):
        _validate_remote(payment, data)
