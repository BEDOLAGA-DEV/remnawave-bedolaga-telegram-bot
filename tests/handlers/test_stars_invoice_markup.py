from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.balance.stars import process_stars_payment_amount


@pytest.mark.asyncio
async def test_stars_invoice_markup_starts_with_pay_button(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)

    invoice_message = SimpleNamespace(message_id=3, chat=SimpleNamespace(id=1))
    send_invoice = AsyncMock(return_value=invoice_message)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_id=2,
        bot=SimpleNamespace(send_invoice=send_invoice, delete_message=AsyncMock()),
        delete=AsyncMock(),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    state.get_data.return_value = {}
    user = SimpleNamespace(id=42, language='ru', restriction_topup=False)

    await process_stars_payment_amount(message, user, amount_kopeks=10000, state=state)

    markup = send_invoice.await_args.kwargs['reply_markup']
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert markup.inline_keyboard[0][0].pay is True
    assert sum(button.pay is True for button in buttons) == 1
    assert markup.inline_keyboard[1][0].callback_data == 'balance_topup'
