from unittest.mock import AsyncMock, MagicMock

from app.database.crud import saved_payment_method as saved_methods, yookassa as yk
from app.database.models import SavedPaymentMethod, YooKassaPayment


def _result(value):
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)
    res.scalars.return_value.all.return_value = value
    return res


def _statement_text(statement) -> str:
    return str(statement.compile(compile_kwargs={'literal_binds': True}))


def test_yookassa_payment_model_stores_scope():
    payment = YooKassaPayment(
        user_id=42,
        yookassa_payment_id='payment-1',
        amount_kopeks=1000,
        currency='RUB',
        description='test',
        status='pending',
        yookassa_scope='cabinet',
    )

    assert payment.yookassa_scope == 'cabinet'


async def test_create_yookassa_payment_saves_scope():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(None))
    db.add = MagicMock()

    payment = await yk.create_yookassa_payment(
        db=db,
        user_id=42,
        yookassa_payment_id='payment-1',
        amount_kopeks=1000,
        currency='RUB',
        description='test',
        status='pending',
        yookassa_scope='bot',
    )

    db.add.assert_called_once()
    assert payment is not None
    assert payment.yookassa_scope == 'bot'


async def test_get_yookassa_payment_by_id_filters_by_scope_when_passed():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(MagicMock()))

    await yk.get_yookassa_payment_by_id(db, 'payment-1', yookassa_scope='cabinet')

    statement = db.execute.await_args.args[0]
    statement_text = _statement_text(statement)
    assert "yookassa_payments.yookassa_payment_id = 'payment-1'" in statement_text
    assert "yookassa_payments.yookassa_scope = 'cabinet'" in statement_text


async def test_get_yookassa_payment_by_id_without_scope_keeps_legacy_lookup():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(MagicMock()))

    await yk.get_yookassa_payment_by_id(db, 'payment-1')

    statement_text = _statement_text(db.execute.await_args.args[0])
    assert "yookassa_payments.yookassa_payment_id = 'payment-1'" in statement_text
    assert 'yookassa_payments.yookassa_scope =' not in statement_text


def test_saved_payment_method_model_stores_scope():
    method = SavedPaymentMethod(
        user_id=42,
        yookassa_payment_method_id='pm-1',
        method_type='bank_card',
        yookassa_scope='cabinet',
    )

    assert method.yookassa_scope == 'cabinet'


async def test_create_saved_payment_method_saves_scope():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(None))
    db.add = MagicMock()

    method = await saved_methods.create_saved_payment_method(
        db=db,
        user_id=42,
        yookassa_payment_method_id='pm-1',
        yookassa_scope='bot',
    )

    db.add.assert_called_once()
    assert method is not None
    assert method.yookassa_scope == 'bot'


async def test_active_saved_payment_methods_filters_by_scope_when_passed():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result([]))

    await saved_methods.get_active_payment_methods_by_user(db, user_id=42, yookassa_scope='cabinet')

    statement_text = _statement_text(db.execute.await_args.args[0])
    assert 'saved_payment_methods.user_id = 42' in statement_text
    assert "saved_payment_methods.yookassa_scope = 'cabinet'" in statement_text
    assert 'saved_payment_methods.is_active = true' in statement_text


async def test_active_saved_payment_methods_without_scope_keeps_legacy_lookup():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result([]))

    await saved_methods.get_active_payment_methods_by_user(db, user_id=42)

    statement_text = _statement_text(db.execute.await_args.args[0])
    assert 'saved_payment_methods.user_id = 42' in statement_text
    assert 'saved_payment_methods.yookassa_scope =' not in statement_text
    assert 'saved_payment_methods.is_active = true' in statement_text


async def test_user_ids_with_active_payment_methods_filters_by_scope_when_passed():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result([]))

    await saved_methods.get_user_ids_with_active_payment_methods(db, user_ids=[42, 43], yookassa_scope='bot')

    statement_text = _statement_text(db.execute.await_args.args[0])
    assert "saved_payment_methods.yookassa_scope = 'bot'" in statement_text
    assert 'saved_payment_methods.is_active = true' in statement_text
