"""Пользовательские уведомления rich-сообщением.

Главная опасность перевода — переносы строк. Классический Telegram-HTML рвёт
текст по '\\n', а rich-разметка блочная: спецификация про свой набор тегов прямо
отмечает «all the text above was on the same line». Отданный как есть текст
уведомления слипся бы в сплошное полотно, и это было бы видно каждому получателю.
"""

from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import ClientDecodeError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.services.notification_delivery_service import NotificationType
from app.utils import rich_notify
from app.utils.rich_notify import build_notification_rich_html, try_send_rich_notification


@pytest.fixture(autouse=True)
def _enable_rich(monkeypatch):
    monkeypatch.setattr(settings, 'USER_NOTIFICATIONS_RICH_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_INLINE_BUTTONS', False, raising=False)
    monkeypatch.setattr(rich_notify, 'is_rich_menu_enabled', lambda: True)


class TestBuildHtml:
    def test_first_line_becomes_heading_like_in_the_menu(self):
        """Стиль меню: заголовок в <h4>, под ним разделитель, дальше содержимое."""
        html = build_notification_rich_html('⚠️ Подписка истекает\n\nОсталось 3 дня')

        assert html == '<h4>⚠️ Подписка истекает</h4><hr/><p>Осталось 3 дня</p>'

    def test_single_newline_inside_body_becomes_br(self):
        html = build_notification_rich_html('Заголовок\nпервая\nвторая')

        assert html == '<h4>Заголовок</h4><hr/><p>первая<br>вторая</p>'

    def test_lone_line_stays_paragraph_without_heading(self):
        """Заголовок без содержимого под ним — это уже не заголовок."""
        assert build_notification_rich_html('Баланс пополнен') == '<p>Баланс пополнен</p>'

    def test_long_first_line_is_not_turned_into_heading(self):
        long_line = 'Э' * 120

        html = build_notification_rich_html(f'{long_line}\nвторая')

        assert '<h4>' not in html
        assert html.startswith('<p>')

    def test_heading_length_counts_text_not_markup(self):
        """Разметка не должна съедать лимит: <b> — это ноль видимых символов."""
        html = build_notification_rich_html('<b>Короткий заголовок</b>\nтело')

        assert html.startswith('<h4><b>Короткий заголовок</b></h4>')

    def test_several_blank_lines_do_not_make_empty_paragraphs(self):
        html = build_notification_rich_html('Заголовок\n\nа\n\n\n\nб')

        assert html == '<h4>Заголовок</h4><hr/><p>а</p><p>б</p>'
        assert '<p></p>' not in html

    def test_logo_leads_the_message_like_in_the_menu(self):
        html = build_notification_rich_html('Заголовок\nтело', logo_url='https://cdn.example/logo.png')

        assert html.startswith('<img src="https://cdn.example/logo.png"/><h4>Заголовок</h4><hr/>')

    def test_inline_markup_survives(self):
        html = build_notification_rich_html('<b>Подписка</b> истекает <a href="https://t.me">тут</a>')

        assert '<b>Подписка</b>' in html
        assert '<a href="https://t.me">тут</a>' in html

    def test_classic_spoiler_span_becomes_rich_tag(self):
        html = build_notification_rich_html('<span class="tg-spoiler">секрет</span>')

        assert '<tg-spoiler>секрет</tg-spoiler>' in html
        assert 'span' not in html

    @pytest.mark.parametrize(
        'text',
        [
            '<pre>код</pre>',
            '<blockquote>цитата</blockquote>',
            '<ul><li>пункт</li></ul>',
        ],
    )
    def test_block_markup_refuses_conversion(self, text):
        """Блочную разметку rich воспроизводит иначе — честнее отдать классику."""
        assert build_notification_rich_html(text) is None

    @pytest.mark.parametrize('text', ['', '   ', '\n\n'])
    def test_empty_text_refuses_conversion(self, text):
        assert build_notification_rich_html(text) is None


class TestSend:
    async def test_sends_rich_and_reports_success(self):
        bot = AsyncMock()

        sent = await try_send_rich_notification(bot, 42, 'Подписка истекает\n\nПродлите её')

        assert sent is True
        html = bot.send_rich_message.await_args.kwargs['rich_message'].html
        assert html == '<h4>Подписка истекает</h4><hr/><p>Продлите её</p>'

    async def test_disabled_setting_falls_back(self, monkeypatch):
        monkeypatch.setattr(settings, 'USER_NOTIFICATIONS_RICH_ENABLED', False, raising=False)
        bot = AsyncMock()

        assert await try_send_rich_notification(bot, 42, 'текст') is False
        bot.send_rich_message.assert_not_awaited()

    async def test_rich_menu_off_falls_back(self, monkeypatch):
        """Вне rich-режима сервер может не знать про rich вовсе."""
        monkeypatch.setattr(rich_notify, 'is_rich_menu_enabled', lambda: False)
        bot = AsyncMock()

        assert await try_send_rich_notification(bot, 42, 'текст') is False
        bot.send_rich_message.assert_not_awaited()

    async def test_keyboard_stays_outside_by_default(self):
        bot = AsyncMock()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='a', callback_data='a')]])

        await try_send_rich_notification(bot, 42, 'текст', keyboard=keyboard)

        kwargs = bot.send_rich_message.await_args.kwargs
        assert kwargs['reply_markup'] is keyboard
        assert '<tg-button' not in kwargs['rich_message'].html

    async def test_buttons_move_inside_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, 'MAIN_MENU_RICH_INLINE_BUTTONS', True, raising=False)
        bot = AsyncMock()
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='Продлить', callback_data='subscription_extend')]]
        )

        await try_send_rich_notification(bot, 42, 'текст', keyboard=keyboard)

        kwargs = bot.send_rich_message.await_args.kwargs
        assert '<tg-button type="callback_data" data="subscription_extend">' in kwargs['rich_message'].html
        assert 'reply_markup' not in kwargs

    async def test_unmovable_button_keeps_keyboard_outside(self, monkeypatch):
        monkeypatch.setattr(settings, 'MAIN_MENU_RICH_INLINE_BUTTONS', True, raising=False)
        bot = AsyncMock()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Оплатить', pay=True)]])

        await try_send_rich_notification(bot, 42, 'текст', keyboard=keyboard)

        kwargs = bot.send_rich_message.await_args.kwargs
        assert kwargs['reply_markup'] is keyboard
        assert '<tg-button' not in kwargs['rich_message'].html

    @pytest.mark.parametrize(
        'error',
        [
            TelegramBadRequest(method=None, message='RICH_MESSAGE_INVALID'),
            ClientDecodeError('Failed to decode object', ValueError('unknown block'), '{}'),
        ],
    )
    async def test_send_failure_falls_back_to_classic(self, error):
        """Любой отказ обязан отдать False, чтобы отработал классический путь."""
        bot = AsyncMock()
        bot.send_rich_message.side_effect = error

        assert await try_send_rich_notification(bot, 42, 'текст') is False

    async def test_block_markup_falls_back_without_calling_api(self):
        bot = AsyncMock()

        assert await try_send_rich_notification(bot, 42, '<pre>код</pre>') is False
        bot.send_rich_message.assert_not_awaited()


class TestDeliveryIntegration:
    """Единый сток доставки обязан пробовать rich и честно откатываться на классику."""

    @staticmethod
    def _service_and_user():
        from types import SimpleNamespace

        from app.services.notification_delivery_service import NotificationDeliveryService

        service = NotificationDeliveryService.__new__(NotificationDeliveryService)
        return service, SimpleNamespace(telegram_id=42, id=1, language='ru')

    async def test_rich_success_skips_classic_send(self, monkeypatch):
        service, user = self._service_and_user()
        monkeypatch.setattr('app.utils.rich_notify.try_send_rich_notification', AsyncMock(return_value=True))
        bot = AsyncMock()

        ok = await service._send_telegram_notification(
            user, NotificationType.SUBSCRIPTION_EXPIRING, {}, bot, 'текст', None
        )

        assert ok is True
        bot.send_message.assert_not_awaited()

    async def test_rich_refusal_falls_through_to_classic_send(self, monkeypatch):
        service, user = self._service_and_user()
        monkeypatch.setattr('app.utils.rich_notify.try_send_rich_notification', AsyncMock(return_value=False))
        bot = AsyncMock()

        ok = await service._send_telegram_notification(
            user, NotificationType.SUBSCRIPTION_EXPIRING, {}, bot, 'текст', None
        )

        assert ok is True
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs['parse_mode'] == 'HTML'


class TestLogoAndTimeout:
    """Логотип и бюджет времени — две вещи, которые легко потерять на этом пути."""

    async def test_logo_is_embedded_as_image_header(self, monkeypatch):
        monkeypatch.setattr(rich_notify, '_resolve_rich_logo_url', lambda: 'https://cdn.example/logo.png')
        bot = AsyncMock()

        await try_send_rich_notification(bot, 42, 'текст', with_logo=True)

        html = bot.send_rich_message.await_args.kwargs['rich_message'].html
        assert html.startswith('<img src="https://cdn.example/logo.png"/>')

    async def test_logo_absent_when_not_requested(self, monkeypatch):
        monkeypatch.setattr(rich_notify, '_resolve_rich_logo_url', lambda: 'https://cdn.example/logo.png')
        bot = AsyncMock()

        await try_send_rich_notification(bot, 42, 'текст', with_logo=False)

        assert '<img' not in bot.send_rich_message.await_args.kwargs['rich_message'].html

    async def test_undownloadable_logo_retries_once_without_it(self, monkeypatch):
        """Ровно как в rich-меню: картинку не скачали — шлём то же самое без шапки."""
        monkeypatch.setattr(rich_notify, '_resolve_rich_logo_url', lambda: 'https://cdn.example/logo.png')
        monkeypatch.setattr(rich_notify, '_is_media_fetch_error', lambda error: True)
        monkeypatch.setattr(rich_notify, '_mark_logo_unavailable_once', lambda error: True)

        bot = AsyncMock()
        bot.send_rich_message.side_effect = [
            TelegramBadRequest(method=None, message='failed to get HTTP URL content'),
            None,
        ]

        sent = await try_send_rich_notification(bot, 42, 'текст', with_logo=True)

        assert sent is True
        assert bot.send_rich_message.await_count == 2
        assert '<img' not in bot.send_rich_message.await_args.kwargs['rich_message'].html

    async def test_timeout_propagates_instead_of_retrying(self, monkeypatch):
        """Повтор с тем же таймаутом удвоил бы бюджет цикла на получателя."""
        import asyncio

        async def never_returns(**_kwargs):
            await asyncio.sleep(10)

        bot = AsyncMock()
        bot.send_rich_message = never_returns

        with pytest.raises(TimeoutError):
            await try_send_rich_notification(bot, 42, 'текст', timeout=0.01)


class TestMonitoringIntegration:
    @staticmethod
    def _service():
        from app.services.monitoring_service import MonitoringService

        service = MonitoringService.__new__(MonitoringService)
        service.bot = AsyncMock()
        return service

    async def test_rich_success_skips_photo_and_text(self, monkeypatch):
        service = self._service()
        monkeypatch.setattr('app.services.monitoring_service.try_send_rich_notification', AsyncMock(return_value=True))

        await service._send_message_with_logo(42, 'текст')

        service.bot.send_photo.assert_not_awaited()
        service.bot.send_message.assert_not_awaited()

    async def test_rich_refusal_falls_through_to_classic(self, monkeypatch):
        service = self._service()
        monkeypatch.setattr('app.services.monitoring_service.try_send_rich_notification', AsyncMock(return_value=False))
        monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False, raising=False)

        await service._send_message_with_logo(42, 'текст')

        service.bot.send_message.assert_awaited_once()

    async def test_rich_timeout_skips_recipient_without_second_attempt(self, monkeypatch):
        """Бюджет на получателя должен остаться одинарным."""
        service = self._service()
        monkeypatch.setattr(
            'app.services.monitoring_service.try_send_rich_notification', AsyncMock(side_effect=TimeoutError)
        )

        result = await service._send_message_with_logo(42, 'текст')

        assert result is None
        service.bot.send_photo.assert_not_awaited()
        service.bot.send_message.assert_not_awaited()


class TestBroadcastIntegration:
    """Рассылка — тоже клиентское уведомление, но с медиа rich не работает."""

    @staticmethod
    def _service():
        from app.services.broadcast_service import BroadcastService

        service = BroadcastService()
        service.set_bot(AsyncMock())
        return service

    @staticmethod
    def _config(media=None):
        from app.services.broadcast_service import BroadcastConfig

        return BroadcastConfig(target='all', message_text='Новость\nподробности', selected_buttons=[], media=media)

    async def test_text_broadcast_goes_rich(self, monkeypatch):
        service = self._service()
        rich = AsyncMock(return_value=True)
        monkeypatch.setattr('app.utils.rich_notify.try_send_rich_notification', rich)

        await service._deliver_message(42, self._config(), None)

        rich.assert_awaited_once()
        service._bot.send_message.assert_not_awaited()

    async def test_rich_refusal_falls_back_to_plain_send(self, monkeypatch):
        service = self._service()
        monkeypatch.setattr('app.utils.rich_notify.try_send_rich_notification', AsyncMock(return_value=False))

        await service._deliver_message(42, self._config(), None)

        service._bot.send_message.assert_awaited_once()

    async def test_media_broadcast_never_touches_rich(self, monkeypatch):
        """Загруженный по file_id файл rich-сообщение не несёт."""
        from app.services.broadcast_service import BroadcastMediaConfig

        service = self._service()
        rich = AsyncMock(return_value=True)
        monkeypatch.setattr('app.utils.rich_notify.try_send_rich_notification', rich)
        config = self._config(media=BroadcastMediaConfig(type='photo', file_id='abc', caption='подпись'))

        await service._deliver_message(42, config, None)

        rich.assert_not_awaited()
        service._bot.send_photo.assert_awaited_once()


class TestUnsupportedServer:
    async def test_unsupported_rich_is_disabled_globally(self, monkeypatch):
        """Иначе рассылка на тысячи получателей удвоила бы число запросов."""
        marked: list[Exception] = []
        monkeypatch.setattr(rich_notify, '_looks_like_unsupported', lambda error: True)
        monkeypatch.setattr(rich_notify, '_mark_rich_unavailable', lambda error: marked.append(error))

        bot = AsyncMock()
        bot.send_rich_message.side_effect = TelegramBadRequest(method=None, message='METHOD_NOT_FOUND')

        sent = await try_send_rich_notification(bot, 42, 'Заголовок\nтело')

        assert sent is False
        assert len(marked) == 1
