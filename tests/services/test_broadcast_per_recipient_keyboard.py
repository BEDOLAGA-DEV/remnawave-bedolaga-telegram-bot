"""BroadcastConfig.recipient_ids и keyboard_factory: рассылка с персональной кнопкой.

Промопредложения шлют каждому свою кнопку (в callback_data зашит id его оффера),
поэтому сервису рассылок нужен готовый список получателей и фабрика клавиатур.
"""

import asyncio

from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.broadcast_service import BroadcastConfig, BroadcastService


def _promo_keyboard(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='🎁', callback_data=f'claim_discount_{offer_id}')]]
    )


def _config(**kwargs) -> BroadcastConfig:
    defaults = {
        'target': 'promo_offer_direct',
        'message_text': 'Промо',
        'selected_buttons': [],
        'category': 'promo',
    }
    defaults.update(kwargs)
    return BroadcastConfig(**defaults)


async def test_keyboard_factory_builds_personal_keyboard_per_recipient(monkeypatch):
    """Каждому получателю уходит своя клавиатура, а не одна общая."""
    service = BroadcastService()
    delivered: list[tuple[int, InlineKeyboardMarkup | None]] = []

    async def fake_deliver(telegram_id, config, keyboard):
        delivered.append((telegram_id, keyboard))

    async def noop_progress(*args, **kwargs):
        return None

    monkeypatch.setattr(service, '_deliver_message', fake_deliver)
    monkeypatch.setattr(service, '_update_progress', noop_progress)
    monkeypatch.setattr('app.services.broadcast_service._TG_BATCH_DELAY', 0)

    offers = {101: 5001, 102: 5002}
    config = _config(
        recipient_ids=list(offers),
        keyboard_factory=lambda telegram_id: _promo_keyboard(offers[telegram_id]),
    )

    sent, failed, blocked, cancelled = await service._send_batched(
        1,
        list(offers),
        config,
        None,
        asyncio.Event(),
    )

    assert (sent, failed, blocked, cancelled) == (2, 0, 0, False)
    assert [telegram_id for telegram_id, _ in delivered] == [101, 102]
    callbacks = [keyboard.inline_keyboard[0][0].callback_data for _, keyboard in delivered]
    assert callbacks == ['claim_discount_5001', 'claim_discount_5002']


async def test_blocked_recipients_counted_separately(monkeypatch):
    """Заблокировавшие бота идут в blocked, а не в общую кучу ошибок."""
    service = BroadcastService()

    async def fake_deliver(telegram_id, config, keyboard):
        if telegram_id == 102:
            raise TelegramForbiddenError(method=None, message='bot was blocked by the user')

    async def noop_progress(*args, **kwargs):
        return None

    monkeypatch.setattr(service, '_deliver_message', fake_deliver)
    monkeypatch.setattr(service, '_update_progress', noop_progress)
    monkeypatch.setattr('app.services.broadcast_service._TG_BATCH_DELAY', 0)

    config = _config(recipient_ids=[101, 102], keyboard_factory=lambda telegram_id: _promo_keyboard(1))

    sent, failed, blocked, cancelled = await service._send_batched(
        1,
        [101, 102],
        config,
        None,
        asyncio.Event(),
    )

    assert (sent, failed, blocked, cancelled) == (1, 0, 1, False)


async def test_without_factory_shared_keyboard_is_used(monkeypatch):
    """Обычные рассылки не затронуты: без фабрики уходит общая клавиатура."""
    service = BroadcastService()
    delivered: list[InlineKeyboardMarkup | None] = []

    async def fake_deliver(telegram_id, config, keyboard):
        delivered.append(keyboard)

    async def noop_progress(*args, **kwargs):
        return None

    monkeypatch.setattr(service, '_deliver_message', fake_deliver)
    monkeypatch.setattr(service, '_update_progress', noop_progress)
    monkeypatch.setattr('app.services.broadcast_service._TG_BATCH_DELAY', 0)

    shared = _promo_keyboard(1)
    sent, _failed, _blocked, _cancelled = await service._send_batched(
        1,
        [101, 102],
        _config(),
        shared,
        asyncio.Event(),
    )

    assert sent == 2
    assert delivered == [shared, shared]
