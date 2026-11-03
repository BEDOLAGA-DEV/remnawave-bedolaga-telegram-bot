"""Contest announcements use recipient locales without changing audience or prizes."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.localization.texts import get_texts
from app.services import contest_rotation_service as module
from app.services.contest_rotation_service import ContestRotationService


@pytest.mark.asyncio
@pytest.mark.parametrize('prize_type,prize_value', [('balance', '1234'), ('custom', '<prize>&'), ('days', '17')])
async def test_channel_default_and_concurrent_recipient_languages(monkeypatch, prize_type, prize_value):
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'ua')
    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'example_bot')
    from app.services.channel_subscription_service import channel_subscription_service

    monkeypatch.setattr(channel_subscription_service, 'get_first_channel_id', AsyncMock(return_value=-123))

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr(module, 'AsyncSessionLocal', session)
    service = ContestRotationService()
    service.bot = SimpleNamespace(send_message=AsyncMock())
    users = [
        SimpleNamespace(telegram_id=i + 100, language=lang) for i, lang in enumerate(['ru', 'en', 'ua', 'zh', 'fa'])
    ]
    users += [
        SimpleNamespace(telegram_id=200),
        SimpleNamespace(telegram_id=None, language='en'),
        SimpleNamespace(telegram_id=300, language='en', notification_settings={'promo_offers_enabled': False}),
    ]
    monkeypatch.setattr(service, '_load_users_batch', AsyncMock(side_effect=[users, []]))
    template = SimpleNamespace(
        name='<operator>&', prize_type=prize_type, prize_value=prize_value, max_winners=3, attempts_per_user=7
    )
    await service._announce_round_start(template, datetime.now(UTC), datetime.now(UTC))
    calls = {c.kwargs['chat_id']: c.kwargs for c in service.bot.send_message.await_args_list}
    assert set(calls) == {-123, 100, 101, 102, 103, 104, 200}
    for chat, language in [
        (-123, 'ua'),
        (200, 'ua'),
        *[(i + 100, lang) for i, lang in enumerate(['ru', 'en', 'ua', 'zh', 'fa'])],
    ]:
        texts = get_texts(language)
        sent = calls[chat]
        assert texts.t('CONTEST_START_ANNOUNCEMENT') in sent['text']
        assert '&lt;operator&gt;&amp;' in sent['text']
        assert '<operator>' not in sent['text']
        if prize_type == 'balance':
            assert texts.t('CONTEST_PRIZE_KOPEKS').format(value='1234') in sent['text']
        elif prize_type == 'custom':
            assert '&lt;prize&gt;&amp;' in sent['text']
        else:
            assert '17 ' + texts.t('DAYS') in sent['text']
        button = sent['reply_markup'].inline_keyboard[0][0]
        assert button.text == texts.t('CONTEST_PLAY_BUTTON')
        if chat == -123:
            assert button.url == 'https://t.me/example_bot?start=contests'
        else:
            assert button.callback_data == 'contests_menu'


@pytest.mark.asyncio
async def test_disabled_broadcast_does_not_load_users(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: False)
    service = ContestRotationService()
    service.bot = SimpleNamespace(send_message=AsyncMock())
    service._load_users_batch = AsyncMock()
    await service._broadcast_to_users('operator text')
    service._load_users_batch.assert_not_awaited()
    service.bot.send_message.assert_not_awaited()
