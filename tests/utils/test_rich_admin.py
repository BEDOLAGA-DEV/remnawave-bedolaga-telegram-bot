"""Тесты rich-сообщений админ-чата: конвертер, латч недоступности, фоллбеки."""

from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings, settings
from app.utils import rich_admin


@pytest.fixture(autouse=True)
def _rich_admin_env(monkeypatch):
    rich_admin._reset_rich_admin_availability()
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_RICH_ENABLED', True, raising=False)
    yield
    rich_admin._reset_rich_admin_availability()


def test_rich_flag_default_is_enabled():
    assert Settings.model_fields['ADMIN_NOTIFICATIONS_RICH_ENABLED'].default is True


def test_classic_html_to_rich_conversion():
    classic = '<b>💎 ПОКУПКА ПОДПИСКИ</b>\n\n👤 <b>Юзер:</b> Егор\n<blockquote expandable>детали\nстрока</blockquote>'

    rich = rich_admin.classic_admin_html_to_rich(classic)

    # Первая жирная строка вынесена в заголовок
    assert rich.startswith('<h6><b>💎 ПОКУПКА ПОДПИСКИ</b></h6><hr/>')
    # Неподдерживаемый rich-HTML атрибут expandable убран, переносы стали <br>
    assert '<blockquote expandable>' not in rich
    assert '<blockquote>детали<br>строка</blockquote>' in rich
    # Тело — абзацами (в rich-HTML голый \n схлопывается)
    assert '<p>👤 <b>Юзер:</b> Егор</p>' in rich
    # Футер с tg-time
    assert '<footer>' in rich
    assert '<tg-time' in rich


def test_classic_html_emoji_before_bold_becomes_header():
    """Заголовки вида «🔧 <b>ВКЛЮЧЕНИЕ ТЕХРАБОТ</b>» (эмодзи до тега) тоже выносятся в h6."""
    classic = '🔧 <b>ВКЛЮЧЕНИЕ ТЕХРАБОТ</b>\n\n📋 <b>Причина:</b> настройки\n🤖 <b>Автоматически:</b> Нет'

    rich = rich_admin.classic_admin_html_to_rich(classic)

    assert rich.startswith('<h6>🔧 <b>ВКЛЮЧЕНИЕ ТЕХРАБОТ</b></h6><hr/>')
    # Строки не склеиваются в кашу — каждая на своём месте
    assert '<p>📋 <b>Причина:</b> настройки<br>🤖 <b>Автоматически:</b> Нет</p>' in rich


def test_classic_html_without_bold_header_kept_as_is():
    rich = rich_admin.classic_admin_html_to_rich('просто текст уведомления')
    assert '<h6>' not in rich
    assert rich.startswith('<p>просто текст уведомления</p>')
    assert '<footer>' in rich


def test_kv_table_escapes_keys_and_keeps_value_html():
    table = rich_admin.rich_kv_table([('Версия <x>', '<code>1.0</code>')])
    assert '<table bordered striped>' in table
    assert 'Версия &lt;x&gt;' in table
    assert '<code>1.0</code>' in table


def test_traceback_details_escapes_content():
    block = rich_admin.rich_traceback_details('📋 ValueError', 'Trace <b>raw</b>', open_by_default=True)
    assert block.startswith('<details open><summary>📋 ValueError</summary>')
    assert '<pre><code class="language-python">Trace &lt;b&gt;raw&lt;/b&gt;</code></pre>' in block


async def test_try_send_passes_thread_and_markup():
    bot = AsyncMock()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='x', url='https://e.com')]])

    sent = await rich_admin.try_send_rich_admin_message(bot, -100123, '<p>hi</p>', thread_id=7, reply_markup=keyboard)

    assert sent is True
    kwargs = bot.send_rich_message.await_args.kwargs
    assert kwargs['chat_id'] == -100123
    assert kwargs['message_thread_id'] == 7
    assert kwargs['reply_markup'] is keyboard
    assert kwargs['rich_message'].html == '<p>hi</p>'
    assert kwargs['rich_message'].skip_entity_detection is True


async def test_try_send_disabled_by_setting(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_RICH_ENABLED', False, raising=False)
    bot = AsyncMock()

    assert await rich_admin.try_send_rich_admin_message(bot, 1, '<p>hi</p>') is False
    bot.send_rich_message.assert_not_awaited()


async def test_try_send_unsupported_marks_latch():
    bot = AsyncMock()
    bot.send_rich_message.side_effect = TelegramNotFound(method=None, message='Not Found')

    assert await rich_admin.try_send_rich_admin_message(bot, 1, '<p>hi</p>') is False
    assert rich_admin.is_rich_admin_enabled() is False

    bot.send_rich_message.reset_mock()
    assert await rich_admin.try_send_rich_admin_message(bot, 1, '<p>hi</p>') is False
    bot.send_rich_message.assert_not_awaited()


async def test_try_send_render_error_does_not_latch():
    bot = AsyncMock()
    bot.send_rich_message.side_effect = TelegramBadRequest(method=None, message="can't parse rich message")

    assert await rich_admin.try_send_rich_admin_message(bot, 1, '<p>hi</p>') is False
    assert rich_admin.is_rich_admin_enabled() is True


async def test_try_send_oversized_falls_back():
    bot = AsyncMock()
    assert await rich_admin.try_send_rich_admin_message(bot, 1, 'x' * (rich_admin.RICH_TEXT_LIMIT + 1)) is False
    bot.send_rich_message.assert_not_awaited()


def test_pre_blocks_survive_conversion():
    """<pre>-блоки (описание релиза из markdown) сохраняют форматирование —
    переносы внутри не конвертируются в <br>."""
    classic = (
        '<b>🆕 Доступна новая версия</b>\n\n'
        'Изменения:\n'
        '<blockquote>Список:\n<pre><code class="language-python">line1\nline2</code></pre>\nконец</blockquote>\n\n'
        '<pre>raw\nblock</pre>'
    )

    rich = rich_admin.classic_admin_html_to_rich(classic)

    assert '<pre><code class="language-python">line1\nline2</code></pre>' in rich
    assert '<pre>raw\nblock</pre>' in rich
    # pre внутри цитаты не разорван и примыкает к тексту как блок
    assert '<blockquote>Список:<pre><code' in rich
    assert 'line1<br>line2' not in rich
    # Обычные строки при этом абзацируются
    assert '<p>Изменения:</p>' in rich
