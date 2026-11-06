from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.localization.texts import get_texts
from app.services import tribute_service
from app.services.tribute_service import TributeService


@pytest.fixture
def service(monkeypatch):
    bot = SimpleNamespace(send_message=AsyncMock())
    service = TributeService(bot)
    monkeypatch.setattr(
        tribute_service, 'settings', SimpleNamespace(is_notifications_enabled=lambda: True, DEFAULT_LANGUAGE='en')
    )
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_actual_notification_senders_keep_recipient_language_and_amount(service, monkeypatch, language):
    user = SimpleNamespace(id=7, language=language)
    session = SimpleNamespace()

    async def get_db():
        yield session

    monkeypatch.setattr(tribute_service, 'get_db', get_db)
    monkeypatch.setattr(tribute_service, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    keyboard = object()
    build = AsyncMock(return_value=keyboard)
    monkeypatch.setattr(
        tribute_service, 'PaymentService', lambda bot: SimpleNamespace(build_topup_success_keyboard=build)
    )
    from app.services.payment import common

    cart = AsyncMock()
    monkeypatch.setattr(common, 'send_cart_notification_after_topup', cart)
    texts = get_texts(language)
    await service._send_success_notification(123, 12345)
    args = service.bot.send_message.await_args
    assert args.args == (123, texts.t('TRIBUTE_SUCCESS_NOTICE', '').format(amount=123))
    assert args.kwargs == {'reply_markup': keyboard, 'parse_mode': 'Markdown'}
    cart.assert_awaited_once_with(user, 12345, session, service.bot)
    await service._send_failure_notification(123, language=language)
    args = service.bot.send_message.await_args
    assert args.args == (123, texts.t('TRIBUTE_FAILED_NOTICE', ''))
    assert args.kwargs['parse_mode'] == 'Markdown'
    assert [row[0].callback_data for row in args.kwargs['reply_markup'].inline_keyboard] == [
        'menu_balance',
        'menu_support',
    ]
    assert args.kwargs['reply_markup'].inline_keyboard[0][0].text == texts.t('TRIBUTE_RETRY_BUTTON', '')
    await service._send_refund_notification(123, 12345, language=language)
    args = service.bot.send_message.await_args
    assert args.args == (123, texts.t('TRIBUTE_REFUND_NOTICE', '').format(amount=123))
    assert [row[0].callback_data for row in args.kwargs['reply_markup'].inline_keyboard] == [
        'menu_support',
        'back_to_menu',
    ]


@pytest.mark.asyncio
async def test_existing_optional_sender_calls_and_disabled_notifications(service, monkeypatch):
    await service._send_failure_notification(123)
    assert service.bot.send_message.await_args.args[1] == get_texts('en').t('TRIBUTE_FAILED_NOTICE', '')
    service.bot.send_message.reset_mock()
    monkeypatch.setattr(tribute_service.settings, 'is_notifications_enabled', lambda: False)
    await service._send_failure_notification(123, language='fa')
    await service._send_refund_notification(123, 100, language='fa')
    await service._send_success_notification(123, 100)
    service.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['failure', 'refund'])
async def test_handlers_capture_recipient_language_before_commit(service, monkeypatch, kind):
    committed = False

    class User:
        balance_kopeks = 50000

        @property
        def language(self):
            assert not committed, 'expired ORM language read after commit'
            return 'fa'

    user = User()

    async def commit():
        nonlocal committed
        committed = True

    session = SimpleNamespace(commit=AsyncMock(side_effect=commit))

    async def get_db():
        yield session

    monkeypatch.setattr(tribute_service, 'get_db', get_db)
    lookup = AsyncMock(return_value=user)
    monkeypatch.setattr(tribute_service, 'get_user_by_telegram_id', lookup)
    transaction = SimpleNamespace(description='Original')
    monkeypatch.setattr(tribute_service, 'get_transaction_by_external_id', AsyncMock(return_value=transaction))
    create = AsyncMock()
    monkeypatch.setattr(tribute_service, 'create_transaction', create)
    from app.database.crud import user as user_crud

    monkeypatch.setattr(user_crud, 'lock_user_for_update', AsyncMock(return_value=user))
    data = {'user_id': 123, 'payment_id': 'masked-1', 'amount_kopeks': 12345}
    if kind == 'failure':
        service._send_failure_notification = AsyncMock()
        await service._handle_failed_payment(data)
        service._send_failure_notification.assert_awaited_once_with(123, language='fa')
        assert transaction.description == 'Original (платеж отклонен)'
    else:
        service._send_refund_notification = AsyncMock()
        await service._handle_refund(data)
        service._send_refund_notification.assert_awaited_once_with(123, 12345, language='fa')
        assert user.balance_kopeks == 37655
        assert create.await_args.kwargs['amount_kopeks'] == -12345
        assert create.await_args.kwargs['commit'] is False
    lookup.assert_awaited_once_with(session, 123)
    session.commit.assert_awaited_once()
