"""Tribute «цифровой товар» в боте — выбор суммы и ссылка на оплату."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers.balance import tribute as handler


LINK_100 = 'https://t.me/tribute/app?startapp=pAAA'
LINK_300 = 'https://t.me/tribute/app?startapp=pBBB'


class _Texts:
    BACK = '⬅️ Назад'

    @staticmethod
    def t(_key, default):
        return default


def _callback(data='topup_tribute'):
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        bot=MagicMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), chat=SimpleNamespace(id=1), message_id=2),
    )


def _user(**overrides):
    values = {'language': 'ru', 'telegram_id': 42, 'restriction_topup': False}
    values.update(overrides)
    return SimpleNamespace(**values)


def _buttons(callback):
    markup = callback.message.edit_text.await_args.kwargs['reply_markup']
    return [button for row in markup.inline_keyboard for button in row]


@pytest.fixture(autouse=True)
def tribute_settings(monkeypatch):
    monkeypatch.setattr(handler, 'get_texts', lambda _language: _Texts())
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIBUTE_DONATE_LINK', 'https://t.me/tribute/app?startapp=dDON')
    monkeypatch.setattr(settings, 'TRIBUTE_PRODUCT_LINKS', f'100={LINK_100},300={LINK_300}')
    # метод, а не поле pydantic-модели: подменяем на классе
    monkeypatch.setattr(type(settings), 'get_support_contact_url', lambda _self: None)


@pytest.mark.asyncio
async def test_menu_shows_amount_buttons_in_product_mode():
    callback = _callback()

    await handler.start_tribute_payment(callback, _user())

    buttons = _buttons(callback)
    assert [(b.text, b.callback_data) for b in buttons if b.callback_data.startswith('tribute_pay_')] == [
        ('100 ₽', 'tribute_pay_10000'),
        ('300 ₽', 'tribute_pay_30000'),
    ]
    assert all(b.url is None for b in buttons)  # ссылки на оплату пока нет
    assert buttons[-1].callback_data == 'balance_topup'  # «Назад»
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_in_donation_mode_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, 'TRIBUTE_PRODUCT_LINKS', None)
    callback = _callback()

    await handler.start_tribute_payment(callback, _user())

    urls = [b.url for b in _buttons(callback) if b.url]
    assert urls == ['https://t.me/tribute/app?startapp=dDON&user_id=42']
    assert 'любую сумму' in callback.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_chosen_amount_opens_product_link():
    callback = _callback('tribute_pay_30000')

    await handler.start_tribute_product_payment(callback, _user())

    urls = [b.url for b in _buttons(callback) if b.url]
    assert urls == [LINK_300]
    assert '300 ₽' in callback.message.edit_text.await_args.args[0]
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize('data', ['tribute_pay_20000', 'tribute_pay_0', 'tribute_pay_abc', 'tribute_pay_'])
async def test_unknown_amount_shows_alert_and_menu_again(data):
    callback = _callback(data)

    await handler.start_tribute_product_payment(callback, _user())

    assert callback.answer.await_args.kwargs['show_alert'] is True
    # заново показан выбор сумм, ссылки на оплату нет
    assert all(b.url is None for b in _buttons(callback))
    assert {b.callback_data for b in _buttons(callback)} >= {'tribute_pay_10000', 'tribute_pay_30000'}


@pytest.mark.asyncio
async def test_stale_button_after_switching_back_to_donation_does_not_pay(monkeypatch):
    monkeypatch.setattr(settings, 'TRIBUTE_PRODUCT_LINKS', None)
    callback = _callback('tribute_pay_30000')

    await handler.start_tribute_product_payment(callback, _user())

    assert callback.answer.await_args.kwargs['show_alert'] is True
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['menu', 'product'])
async def test_disabled_tribute_gives_alert_and_no_menu(monkeypatch, entry):
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', False)
    callback = _callback('tribute_pay_10000')

    if entry == 'menu':
        await handler.start_tribute_payment(callback, _user())
    else:
        await handler.start_tribute_product_payment(callback, _user())

    assert callback.answer.await_args.kwargs['show_alert'] is True
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['menu', 'product'])
async def test_restricted_user_gets_no_payment_link(entry):
    callback = _callback('tribute_pay_10000')
    user = _user(restriction_topup=True, restriction_reason='нарушение')

    if entry == 'menu':
        await handler.start_tribute_payment(callback, user)
    else:
        await handler.start_tribute_product_payment(callback, user)

    assert all(b.url is None for b in _buttons(callback))
    assert 'нарушение' in callback.message.edit_text.await_args.args[0]
