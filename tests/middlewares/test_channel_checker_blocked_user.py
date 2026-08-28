"""Заблокировавший бота пользователь не превращается в отчёт об ошибке админам.

Гейт обязательной подписки честно пытается ответить на апдейт (пользователь мог
заблокировать бота уже после отправки сообщения) и получает 403. Раньше это
ловил ``except Exception`` с ``logger.error``, а ``TelegramNotifierProcessor``
на error-уровне разворачивает ``sys.exc_info()`` — то есть каждый такой апдейт
улетал админам полным traceback'ом.

Настоящие ошибки отправки на error-уровне остаться обязаны.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message

import app.middlewares.channel_checker as mw
from app.config import settings


CHANNELS = [
    {
        'channel_link': '@vpn_news',
        'title': 'VPN News',
        'is_subscribed': False,
        'disable_trial_on_leave': False,
        'disable_paid_on_leave': False,
    }
]

BLOCKED = TelegramForbiddenError(method=None, message='Forbidden: bot was blocked by the user')
DEACTIVATED = TelegramBadRequest(method=None, message='Bad Request: user is deactivated')
UNKNOWN = TelegramBadRequest(method=None, message="Bad Request: can't parse entities")


@pytest.fixture
def fake_logger(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(mw, 'logger', logger)
    return logger


def _message(answer_error) -> MagicMock:
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=1182451092, language_code='ru')
    message.answer = AsyncMock(side_effect=answer_error)
    return message


# -- _deny_message ---------------------------------------------------------


@pytest.mark.parametrize('error', [BLOCKED, DEACTIVATED], ids=['blocked', 'deactivated'])
async def test_unreachable_user_does_not_reach_admins(fake_logger, error):
    await mw.ChannelCheckerMiddleware._deny_message(_message(error), MagicMock(), CHANNELS)

    fake_logger.error.assert_not_called()
    fake_logger.debug.assert_called_once()
    # Исключение передано строкой: даже сменив уровень обратно, traceback не соберут
    assert isinstance(fake_logger.debug.call_args.kwargs['error'], str)
    assert fake_logger.debug.call_args.kwargs['telegram_id'] == 1182451092


async def test_real_send_failure_still_reported(fake_logger):
    await mw.ChannelCheckerMiddleware._deny_message(_message(UNKNOWN), MagicMock(), CHANNELS)

    fake_logger.debug.assert_not_called()
    fake_logger.error.assert_called_once()
    # Traceback админам нужен — исключение уходит объектом, не строкой
    assert isinstance(fake_logger.error.call_args.kwargs['error'], BaseException)


async def test_prompt_is_delivered_when_user_is_reachable(fake_logger):
    message = _message(None)
    message.answer.return_value = 'sent'

    result = await mw.ChannelCheckerMiddleware._deny_message(message, MagicMock(), CHANNELS)

    assert result == 'sent'
    fake_logger.error.assert_not_called()
    fake_logger.debug.assert_not_called()
    assert message.answer.await_args.kwargs['reply_markup'] is not None


# -- кнопка «Я подписался» -------------------------------------------------


def _sub_check_callback(edit_error) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = 'sub_channel_check'
    callback.from_user = SimpleNamespace(id=1182451092, language_code='ru')
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.text = None  # _capture_start_payload выходит сразу
    callback.message.edit_text = AsyncMock(side_effect=edit_error)
    return callback


async def _run_sub_check(monkeypatch, edit_error):
    monkeypatch.setattr(settings, 'CHANNEL_IS_REQUIRED_SUB', True)
    monkeypatch.setattr(type(settings), 'is_admin', lambda self, uid: False)
    monkeypatch.setattr(
        'app.services.support_settings_service.SupportSettingsService.is_moderator', staticmethod(lambda uid: False)
    )
    monkeypatch.setattr(mw.channel_subscription_service, 'bot', MagicMock())
    monkeypatch.setattr(mw.channel_subscription_service, 'get_channels_with_status', AsyncMock(return_value=CHANNELS))
    monkeypatch.setattr(mw.channel_subscription_service, 'invalidate_user_cache', AsyncMock())
    monkeypatch.setattr(mw.cache, 'exists', AsyncMock(return_value=False))
    monkeypatch.setattr(mw.cache, 'set', AsyncMock(return_value=True))

    callback = _sub_check_callback(edit_error)
    handler = AsyncMock()
    result = await mw.ChannelCheckerMiddleware()(handler, callback, {'bot': MagicMock(), 'state': None})

    handler.assert_not_awaited()  # неподписанного дальше не пускаем
    assert result is None


async def test_blocked_user_pressing_stale_button_is_swallowed(monkeypatch, fake_logger):
    """403 на edit_text не должен долетать до GlobalErrorMiddleware.

    Тот логирует неизвестное исключение с ``exc_info=True`` и пробрасывает
    дальше — вместо тихого «обновлять клавиатуру некому» получаем отчёт админам
    и упавший апдейт.
    """
    await _run_sub_check(monkeypatch, BLOCKED)

    fake_logger.error.assert_not_called()
    assert any(
        call.kwargs.get('telegram_id') == 1182451092 and isinstance(call.kwargs.get('error'), str)
        for call in fake_logger.debug.call_args_list
    )


async def test_unexpected_edit_failure_still_propagates(monkeypatch, fake_logger):
    with pytest.raises(TelegramBadRequest):
        await _run_sub_check(monkeypatch, UNKNOWN)


# -- уведомление об отключении подписки ------------------------------------


class _FakeSession:
    """AsyncSessionLocal() как асинхронный контекстный менеджер."""

    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


async def _run_deactivation(monkeypatch, send_error):
    subscription = SimpleNamespace(status=mw.SubscriptionStatus.ACTIVE.value, is_trial=False, remnawave_id=None, id=7)
    user = SimpleNamespace(language='ru', remnawave_id='uuid-1', subscriptions=[subscription])

    monkeypatch.setattr(mw, 'AsyncSessionLocal', _FakeSession)
    monkeypatch.setattr(mw, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(mw, 'deactivate_subscription', AsyncMock())
    monkeypatch.setattr(mw, 'SubscriptionService', lambda: SimpleNamespace(disable_remnawave_user=AsyncMock()))
    monkeypatch.setattr(mw.channel_subscription_service, 'should_disable_subscription', lambda ch, is_trial: True)
    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=send_error)
    await mw.ChannelCheckerMiddleware()._deactivate_subscription_on_unsubscribe(1182451092, bot, CHANNELS)

    bot.send_message.assert_awaited_once()


async def test_deactivation_notice_to_blocked_user_is_quiet(monkeypatch, fake_logger):
    """Отписавшийся от канала и заблокировавший бота давал сразу два отчёта админам."""
    await _run_deactivation(monkeypatch, BLOCKED)

    fake_logger.error.assert_not_called()
    assert any(
        call.kwargs.get('telegram_id') == 1182451092 and isinstance(call.kwargs.get('error'), str)
        for call in fake_logger.debug.call_args_list
    )


async def test_real_notification_failure_still_reported(monkeypatch, fake_logger):
    await _run_deactivation(monkeypatch, UNKNOWN)

    fake_logger.error.assert_called_once()
    assert isinstance(fake_logger.error.call_args.kwargs['notify_error'], BaseException)
