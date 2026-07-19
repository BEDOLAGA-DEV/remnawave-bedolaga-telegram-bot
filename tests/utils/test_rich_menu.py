"""Тесты rich-меню (Bot API 10.1): билдер HTML, delivery-хелперы, fallback-флаг."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings, settings
from app.utils import rich_menu


class DummyTexts:
    language = 'ru'

    def t(self, key, default=None):
        return default

    @staticmethod
    def format_traffic(gb, is_limit=True):
        if not gb and is_limit:
            return '∞'
        return f'{gb:g} ГБ'


@pytest.fixture(autouse=True)
def _rich_menu_env(monkeypatch):
    """Включает rich-меню, изолирует логотип/эффект и сбрасывает флаги недоступности."""
    rich_menu._reset_rich_menu_availability()
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', '', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', None, raising=False)
    yield
    rich_menu._reset_rich_menu_availability()


def _make_subscription(now, *, status='active', days_left=12, is_trial=False, tariff_name='Стандарт'):
    return SimpleNamespace(
        id=7,
        actual_status=status,
        is_trial=is_trial,
        end_date=now + timedelta(days=days_left),
        start_date=now - timedelta(days=18),
        tariff_id=None,
        tariff=SimpleNamespace(name=tariff_name),
        traffic_used_gb=12.5,
        traffic_limit_gb=100,
        device_limit=3,
        subscription_url='https://sub.example.com/u/abc',
        subscription_crypto_link=None,
    )


def _make_user(subscription, *, trial_used=True):
    return SimpleNamespace(
        id=1,
        full_name='Егор <script>',
        language='ru',
        auth_type='telegram',
        balance_kopeks=125_000,
        subscription=subscription,
        subscriptions=[subscription] if subscription else [],
        is_trial_already_used=lambda: trial_used,
    )


def _patch_content_sources(monkeypatch, *, promo=None, test_access=None, random_message=None):
    async def fake_promo(db, user, texts, percent=None):
        return promo

    async def fake_test_access(db, user, texts):
        return test_access

    async def fake_random(db):
        return random_message

    monkeypatch.setattr(rich_menu, 'build_promo_offer_hint', fake_promo)
    monkeypatch.setattr(rich_menu, 'build_test_access_hint', fake_test_access)
    monkeypatch.setattr(rich_menu, 'get_random_active_message', fake_random)


def test_rich_flag_default_is_enabled():
    assert Settings.model_fields['MAIN_MENU_RICH_ENABLED'].default is True


async def test_builder_single_subscription_structure(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: False)

    now = datetime.now(UTC)
    subscription = _make_subscription(now)
    user = _make_user(subscription)

    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())

    # Имя экранировано, сырых тегов пользователя нет
    assert 'Егор &lt;script&gt;' in html_out
    assert '<script>' not in html_out
    # Структура: заголовок, блок подписки, баланс, футер
    assert html_out.startswith('<h4>')
    assert '<blockquote>' in html_out
    assert '<footer>' in html_out
    # Дата окончания — через tg-time с relative-форматом и unix конца подписки
    assert f'unix="{int(subscription.end_date.timestamp())}"' in html_out
    assert 'format="r"' in html_out
    # Прогресс-бар остатка дней
    assert '<code>[' in html_out
    # Баланс из format_price
    assert '1250' in html_out


async def test_builder_multi_tariff_table(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    now = datetime.now(UTC)
    active_sub = _make_subscription(now, tariff_name='Промо & Бонус')
    expired_sub = _make_subscription(now, status='expired', days_left=-3, tariff_name='Старый')

    async def fake_get_all(db, user_id):
        return [active_sub, expired_sub]

    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', fake_get_all)

    user = _make_user(active_sub)
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())

    assert '<table bordered striped>' in html_out
    assert 'Промо &amp; Бонус' in html_out
    assert '🟢 Активна' in html_out
    assert '🔴 Истекла' in html_out
    # Даты обеих подписок — через tg-time
    assert html_out.count('<tg-time') >= 2


async def test_builder_without_subscription(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    user = _make_user(None)
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())

    assert '❌ Отсутствует' in html_out
    assert '<footer>' in html_out


async def test_builder_hints_in_details_and_random_message_sanitized(monkeypatch):
    _patch_content_sources(
        monkeypatch,
        promo='⚡ Скидка 20%\n<code>[███░░░░░░░]</code>',
        random_message='Наш <b>канал</b> <span class="x">тут</span> <img src="logo.png"/>',
    )
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    user = _make_user(None)
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())

    # Подсказки — в раскрытом details-блоке
    assert '<details open>' in html_out
    assert '⚡ Скидка 20%' in html_out
    # span/img вычищены из случайного сообщения, разрешённые inline-теги сохранены
    assert '<span' not in html_out
    assert '<img' not in html_out
    assert '<b>канал</b>' in html_out


async def test_builder_without_hints_has_no_details(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(None), DummyTexts(), AsyncMock())

    assert '<details' not in html_out


def test_input_rich_message_flags():
    rich = rich_menu._input_rich_message('<p>x</p>', 'fa')
    assert rich.is_rtl is True
    assert rich.skip_entity_detection is True

    assert rich_menu._input_rich_message('<p>x</p>', 'ru').is_rtl is None


async def test_try_send_disabled_by_setting(monkeypatch):
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_ENABLED', False, raising=False)
    bot = AsyncMock()

    sent = await rich_menu.try_send_rich_main_menu(bot, 1, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert sent is False
    bot.send_rich_message.assert_not_awaited()


async def test_try_send_unsupported_server_marks_unavailable(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    bot = AsyncMock()
    bot.send_rich_message.side_effect = TelegramNotFound(method=None, message='Not Found')

    sent = await rich_menu.try_send_rich_main_menu(bot, 1, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert sent is False
    assert rich_menu.is_rich_menu_enabled() is False

    # Повторный вызов не трогает Bot API
    bot.send_rich_message.reset_mock()
    sent_again = await rich_menu.try_send_rich_main_menu(
        bot, 1, _make_user(None), DummyTexts(), AsyncMock(), MagicMock()
    )
    assert sent_again is False
    bot.send_rich_message.assert_not_awaited()


async def test_try_send_render_error_does_not_disable_rich(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    bot = AsyncMock()
    bot.send_rich_message.side_effect = TelegramBadRequest(method=None, message="can't parse rich message")

    sent = await rich_menu.try_send_rich_main_menu(bot, 1, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert sent is False
    # Разовая ошибка рендера не выключает rich-меню целиком
    assert rich_menu.is_rich_menu_enabled() is True


def _make_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='x', callback_data='y')]])


def _make_callback(*, text='menu', photo=None):
    message = MagicMock()
    message.text = text
    message.photo = photo
    message.rich_message = None
    message.message_id = 42
    message.chat = MagicMock(id=100)
    message.delete = AsyncMock()

    callback = MagicMock()
    callback.message = message
    callback.bot = AsyncMock()
    return callback


async def test_try_edit_text_message_uses_edit_message_text(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback()
    keyboard = _make_keyboard()

    edited = await rich_menu.try_edit_rich_main_menu(callback, _make_user(None), DummyTexts(), AsyncMock(), keyboard)

    assert edited is True
    callback.bot.assert_awaited_once()
    method = callback.bot.await_args.args[0]
    assert isinstance(method, EditMessageText)
    assert method.chat_id == 100
    assert method.message_id == 42
    assert method.rich_message.html == '<p>menu</p>'
    assert method.reply_markup is keyboard
    callback.bot.send_rich_message.assert_not_awaited()


async def test_try_edit_photo_message_recreates_via_send(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback(text=None, photo=[MagicMock()])

    edited = await rich_menu.try_edit_rich_main_menu(callback, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert edited is True
    callback.message.delete.assert_awaited_once()
    callback.bot.send_rich_message.assert_awaited_once()
    assert callback.bot.send_rich_message.await_args.kwargs['chat_id'] == 100


async def test_try_edit_not_modified_is_success(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback()
    callback.bot.side_effect = TelegramBadRequest(method=None, message='message is not modified')

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is True
    assert rich_menu.is_rich_menu_enabled() is True


async def test_try_edit_unsupported_on_edit_marks_unavailable(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback()
    # Устаревший bot-api: editMessageText без text отвечает 'message text is empty'
    callback.bot.side_effect = TelegramBadRequest(method=None, message='Bad Request: message text is empty')

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is False
    assert rich_menu.is_rich_menu_enabled() is False


async def test_try_edit_build_failure_falls_back(monkeypatch):
    async def broken_build(user, texts, db):
        raise RuntimeError('boom')

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', broken_build)

    callback = _make_callback()

    edited = await rich_menu.try_edit_rich_main_menu(callback, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert edited is False
    callback.bot.assert_not_awaited()


async def test_try_send_happy_path_sends_rich_message(monkeypatch):
    """Успешная отправка: реальный билдер (застабены только источники контента)."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    bot = AsyncMock()
    keyboard = _make_keyboard()

    sent = await rich_menu.try_send_rich_main_menu(bot, 100, _make_user(None), DummyTexts(), AsyncMock(), keyboard)

    assert sent is True
    bot.send_rich_message.assert_awaited_once()
    kwargs = bot.send_rich_message.await_args.kwargs
    assert kwargs['chat_id'] == 100
    assert kwargs['reply_markup'] is keyboard
    assert '<h4>' in kwargs['rich_message'].html
    assert '<footer>' in kwargs['rich_message'].html
    assert kwargs['rich_message'].skip_entity_detection is True


async def test_try_send_forbidden_is_handled_without_fallback(monkeypatch):
    """Бот заблокирован: True, чтобы классический рендер не долбил тот же чат."""

    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    bot = AsyncMock()
    bot.send_rich_message.side_effect = TelegramForbiddenError(method=None, message='bot was blocked by the user')

    sent = await rich_menu.try_send_rich_main_menu(bot, 1, _make_user(None), DummyTexts(), AsyncMock(), MagicMock())

    assert sent is True
    assert rich_menu.is_rich_menu_enabled() is True


async def test_try_edit_forbidden_is_handled_without_fallback(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback()
    callback.bot.side_effect = TelegramForbiddenError(method=None, message='bot was blocked by the user')

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is True
    assert rich_menu.is_rich_menu_enabled() is True


async def test_try_edit_transient_edit_error_falls_back_without_disabling(monkeypatch):
    """'message to edit not found' — не признак старого сервера: rich остаётся включён,
    а рендер уходит классической цепочке фоллбеков."""

    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback()
    callback.bot.side_effect = TelegramBadRequest(method=None, message='Bad Request: message to edit not found')

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is False
    assert rich_menu.is_rich_menu_enabled() is True
    callback.bot.send_rich_message.assert_not_awaited()


async def test_try_edit_photo_delete_failure_falls_back_to_classic(monkeypatch):
    """deleteMessage запрещён для сообщений старше 48ч: rich не отправляется новым
    сообщением (иначе копились бы дубли меню) — классика отредактирует фото на месте."""

    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)

    callback = _make_callback(text=None, photo=[MagicMock()])
    callback.message.delete.side_effect = TelegramBadRequest(
        method=None, message="Bad Request: message can't be deleted"
    )

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is False
    callback.bot.send_rich_message.assert_not_awaited()
    assert rich_menu.is_rich_menu_enabled() is True


async def test_multi_tariff_table_is_fully_localized(monkeypatch):
    """Все строки таблицы идут через texts.t — маркер-стаб не должен оставить
    захардкоженной кириллицы (кроме fallback-даты в tg-time)."""

    class MarkerTexts:
        language = 'en'

        def t(self, key, default=None):
            return f'[{key}]'

        @staticmethod
        def format_traffic(gb, is_limit=True):
            return 'GB'

    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    now = datetime.now(UTC)
    subs = [
        _make_subscription(now, tariff_name='Plan-A'),
        SimpleNamespace(
            id=8,
            actual_status='active',
            is_trial=False,
            end_date=now + timedelta(days=5),
            start_date=now,
            tariff_id=None,
            tariff=None,  # тарифless-подписка использует локализованный fallback
            traffic_used_gb=0,
            traffic_limit_gb=0,
            device_limit=1,
        ),
    ]

    html_out = rich_menu._build_subscriptions_table(subs, MarkerTexts())

    assert '[MAIN_MENU_RICH_DAYS_LEFT]' in html_out
    assert '[MAIN_MENU_RICH_TARIFF_FALLBACK]' in html_out
    assert 'дн.' not in html_out
    assert 'Подписка' not in html_out


async def test_show_main_menu_prefers_rich_and_falls_back(monkeypatch):
    """Поведенческий тест ветвления show_main_menu: rich True — классика не зовётся,
    rich False — классика рисует меню."""
    import app.handlers.menu as menu_mod

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(menu_mod, 'has_subscription_checkout_draft', AsyncMock(return_value=False))
    monkeypatch.setattr(menu_mod, 'should_offer_checkout_resume', lambda *a, **k: False)
    monkeypatch.setattr(menu_mod.user_cart_service, 'has_user_cart', AsyncMock(return_value=False))
    monkeypatch.setattr(menu_mod.SupportSettingsService, 'is_moderator', lambda tid: False)
    monkeypatch.setattr(type(settings), 'is_admin', lambda self, tid: False)
    monkeypatch.setattr(type(settings), 'is_text_main_menu_mode', lambda self: True)
    keyboard = _make_keyboard()
    monkeypatch.setattr(menu_mod, 'get_main_menu_keyboard_async', AsyncMock(return_value=keyboard))
    fake_menu_text = AsyncMock(return_value='classic menu')
    monkeypatch.setattr(menu_mod, 'get_main_menu_text', fake_menu_text)
    classic_render = AsyncMock()
    monkeypatch.setattr(menu_mod, 'edit_or_answer_photo', classic_render)

    db_user = MagicMock()
    db_user.language = 'ru'
    db_user.subscriptions = []
    db_user.subscription = None
    db_user.balance_kopeks = 0
    db_user.has_had_paid_subscription = False
    db_user.telegram_id = 1

    callback = _make_callback()
    callback.answer = AsyncMock()
    db = AsyncMock()

    # rich отрисовался — классика не вызывается
    rich_render = AsyncMock(return_value=True)
    monkeypatch.setattr(menu_mod, 'try_edit_rich_main_menu', rich_render)
    await menu_mod.show_main_menu(callback, db_user, db)
    rich_render.assert_awaited_once()
    rich_args = rich_render.await_args.args
    assert rich_args[0] is callback
    assert rich_args[1] is db_user
    assert rich_args[3] is db
    assert rich_args[4] is keyboard
    classic_render.assert_not_awaited()
    fake_menu_text.assert_not_awaited()

    # rich не отрисовался — классика рисует меню
    monkeypatch.setattr(menu_mod, 'try_edit_rich_main_menu', AsyncMock(return_value=False))
    await menu_mod.show_main_menu(callback, db_user, db)
    classic_render.assert_awaited_once()
    assert classic_render.await_args.kwargs['caption'] == 'classic menu'
    assert classic_render.await_args.kwargs['keyboard'] is keyboard
    assert rich_menu.is_rich_menu_enabled() is True


async def test_expired_subscription_renew_link_in_cabinet_mode(monkeypatch):
    """Истёкшая подписка в cabinet-режиме получает ссылку «Продлить» в кабинет."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_cabinet_mode', lambda self: True)
    monkeypatch.setattr(
        rich_menu,
        'build_miniapp_startapp_url',
        lambda start_param: f'https://t.me/bot/cab?startapp={start_param}',
    )

    now = datetime.now(UTC)
    expired = _make_subscription(now, status='expired', days_left=-3)

    async def fake_get_all(db, user_id):
        return [expired]

    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', fake_get_all)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(expired), DummyTexts(), AsyncMock())

    assert '<a href="https://t.me/bot/cab?startapp=renew_7">🔄 Продлить</a>' in html_out


async def test_expired_subscription_no_renew_link_outside_cabinet_mode(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_cabinet_mode', lambda self: False)

    now = datetime.now(UTC)
    expired = _make_subscription(now, status='expired', days_left=-3)

    async def fake_get_all(db, user_id):
        return [expired]

    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', fake_get_all)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(expired), DummyTexts(), AsyncMock())

    assert 'startapp=renew_' not in html_out
    assert 'Продлить' not in html_out


async def test_single_mode_expired_renew_link(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_cabinet_mode', lambda self: True)
    monkeypatch.setattr(
        rich_menu,
        'build_miniapp_startapp_url',
        lambda start_param: f'https://t.me/bot/cab?startapp={start_param}',
    )

    now = datetime.now(UTC)
    expired = _make_subscription(now, status='expired', days_left=-3)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(expired), DummyTexts(), AsyncMock())

    assert 'startapp=renew_7' in html_out


async def test_usage_traffic_and_devices_displayed(monkeypatch):
    """Активная подписка показывает текущий трафик и лимит устройств."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: False)

    now = datetime.now(UTC)
    user = _make_user(_make_subscription(now))

    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())

    assert '📊 Трафик: 12.5 ГБ / 100 ГБ' in html_out
    assert '📱 Устройства: 3' in html_out


async def test_usage_row_in_multi_tariff_table(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    now = datetime.now(UTC)
    active = _make_subscription(now)

    async def fake_get_all(db, user_id):
        return [active]

    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', fake_get_all)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(active), DummyTexts(), AsyncMock())

    # Расход и кнопка подключения — в нижней colspan-строке ряда (узкая 4-я
    # колонка не влезала на мобильных: таблица уезжала за край экрана).
    assert '<td colspan="3">📊 12.5 ГБ / 100 ГБ · 📱 3 · ' in html_out
    assert '<td colspan="4"' not in html_out


async def test_send_passes_message_effect(monkeypatch):
    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_EFFECT_ID', '5046509860389126442', raising=False)

    bot = AsyncMock()
    sent = await rich_menu.try_send_rich_main_menu(
        bot, 1, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert sent is True
    assert bot.send_rich_message.await_args.kwargs['message_effect_id'] == '5046509860389126442'


async def test_rejected_effect_degrades_and_resends(monkeypatch):
    """Отклонённый эффект: повтор без него, эффект отключается до рестарта."""

    async def fake_build(user, texts, db):
        return '<p>menu</p>'

    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', fake_build)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_EFFECT_ID', '123', raising=False)

    bot = AsyncMock()

    def _reject_effect(**kwargs):
        if kwargs.get('message_effect_id'):
            raise TelegramBadRequest(method=None, message='Bad Request: wrong message effect identifier')
        return AsyncMock()()

    bot.send_rich_message.side_effect = _reject_effect

    sent = await rich_menu.try_send_rich_main_menu(
        bot, 1, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert sent is True
    assert bot.send_rich_message.await_count == 2
    # Второй вызов — без эффекта; последующие отправки эффект не включают
    assert 'message_effect_id' not in bot.send_rich_message.await_args.kwargs

    bot.send_rich_message.reset_mock()
    bot.send_rich_message.side_effect = None
    await rich_menu.try_send_rich_main_menu(bot, 1, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard())
    assert bot.send_rich_message.await_args.kwargs['message_effect_id'] is None


async def test_logo_included_from_explicit_url(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', 'https://example.com/logo.png', raising=False)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(None), DummyTexts(), AsyncMock())

    assert html_out.startswith('<img src="https://example.com/logo.png"/>')


async def test_logo_auto_url_from_webhook(monkeypatch, tmp_path):
    logo = tmp_path / 'logo.png'
    logo.write_bytes(b'png')
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', '', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://bot.example.com/webhook', raising=False)
    monkeypatch.setattr(settings, 'LOGO_FILE', str(logo), raising=False)

    assert rich_menu._resolve_rich_logo_url() == 'https://bot.example.com/cabinet/branding/bot-logo'

    # Файла нет — логотип не подставляется
    monkeypatch.setattr(settings, 'LOGO_FILE', str(tmp_path / 'missing.png'), raising=False)
    assert rich_menu._resolve_rich_logo_url() == ''


async def test_logo_fetch_failure_degrades_and_resends(monkeypatch):
    """Telegram не скачал логотип: единственный повтор без логотипа, флаг до рестарта."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', 'https://example.com/logo.png', raising=False)

    bot = AsyncMock()
    calls: list[str] = []

    def _reject_logo(**kwargs):
        calls.append(kwargs['rich_message'].html)
        if '<img' in kwargs['rich_message'].html:
            raise TelegramBadRequest(method=None, message='Bad Request: failed to get HTTP URL content')
        return AsyncMock()()

    bot.send_rich_message.side_effect = _reject_logo

    sent = await rich_menu.try_send_rich_main_menu(
        bot, 1, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert sent is True
    assert len(calls) == 2
    assert '<img' in calls[0]
    assert '<img' not in calls[1]
    assert rich_menu.is_rich_menu_enabled() is True


async def test_connect_link_for_active_subscription_in_table(monkeypatch):
    """Активная строка таблицы получает «кнопку» подключения — ссылку на subscription_url."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    now = datetime.now(UTC)
    active = _make_subscription(now)

    async def fake_get_all(db, user_id):
        return [active]

    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', fake_get_all)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(active), DummyTexts(), AsyncMock())

    assert '<a href="https://sub.example.com/u/abc"><b>⚡ Подключить</b></a>' in html_out


async def test_connect_link_hidden_when_subscription_link_hidden(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'should_hide_subscription_link', lambda self: True)

    now = datetime.now(UTC)
    active = _make_subscription(now)

    async def fake_get_all(db, user_id):
        return [active]

    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', fake_get_all)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(active), DummyTexts(), AsyncMock())

    assert 'Подключить' not in html_out
    assert 'sub.example.com' not in html_out


async def test_connect_link_uses_happ_redirect_in_happ_mode(monkeypatch):
    """В happ-режиме подключение идёт через https-обёртку редиректа, не через сырую happ://."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_happ_cryptolink_mode', lambda self: True)
    monkeypatch.setattr(
        rich_menu,
        'get_happ_cryptolink_redirect_link',
        lambda link: f'https://redirect.example.com/?l={link}' if link else None,
    )

    now = datetime.now(UTC)
    sub = _make_subscription(now)
    sub.subscription_crypto_link = 'happ://crypt4/xyz'

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(sub), DummyTexts(), AsyncMock())

    assert 'https://redirect.example.com/?l=happ://crypt4/xyz' in html_out
    assert 'href="happ://' not in html_out


async def test_trial_offer_free_deeplink(monkeypatch):
    """Новый юзер без триала: ссылка t.me/<bot>?start=trial (бесплатный триал)."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_trial_paid_activation_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'get_bot_username', lambda self: 'testbot')
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3, raising=False)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none', raising=False)

    user = _make_user(None, trial_used=False)
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())

    assert '<a href="https://t.me/testbot?start=trial"><b>🚀 Активировать триал</b></a>' in html_out


async def test_trial_offer_paid_opens_miniapp(monkeypatch):
    """Платный триал: ссылка ведёт на оплату в миниапп (startapp=trial), не на диплинк."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_trial_paid_activation_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3, raising=False)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none', raising=False)
    monkeypatch.setattr(
        rich_menu,
        'build_miniapp_startapp_url',
        lambda start_param: f'https://t.me/bot/cab?startapp={start_param}',
    )

    user = _make_user(None, trial_used=False)
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())

    assert 'https://t.me/bot/cab?startapp=trial' in html_out
    assert 'start=trial' not in html_out.replace('startapp=trial', '')


async def test_trial_offer_absent_when_trial_used(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3, raising=False)

    user = _make_user(None, trial_used=True)
    html_out = await rich_menu.build_main_menu_rich_html(user, DummyTexts(), AsyncMock())

    assert 'Активировать триал' not in html_out


async def test_multiple_subscriptions_collapse_into_details(monkeypatch):
    """При >1 подписки таблица сворачивается в details со счётчиком в summary."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    now = datetime.now(UTC)
    subs = [_make_subscription(now), _make_subscription(now, status='expired', days_left=-3)]

    async def fake_get_all(db, user_id):
        return subs

    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', fake_get_all)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(subs[0]), DummyTexts(), AsyncMock())

    assert '<details><summary><b>📱 Подписки (2)</b></summary>' in html_out
    assert '<table bordered striped>' in html_out
    # Заголовок не дублируется: summary заменяет h6
    assert '<h6>' not in html_out


async def test_single_multi_tariff_subscription_stays_expanded(monkeypatch):
    """Одна подписка — обычный заголовок и таблица без сворачивания."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    now = datetime.now(UTC)
    sub = _make_subscription(now)

    async def fake_get_all(db, user_id):
        return [sub]

    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', fake_get_all)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(sub), DummyTexts(), AsyncMock())

    assert '<h6>📱 Подписки</h6>' in html_out
    assert '<details><summary>' not in html_out


async def test_collapsible_disabled_keeps_plain_table(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_SUBSCRIPTIONS_COLLAPSIBLE', False, raising=False)

    now = datetime.now(UTC)
    subs = [_make_subscription(now), _make_subscription(now)]

    async def fake_get_all(db, user_id):
        return subs

    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', fake_get_all)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(subs[0]), DummyTexts(), AsyncMock())

    assert '<h6>📱 Подписки</h6>' in html_out
    assert '<details><summary>' not in html_out


def test_collapsible_flag_default_is_enabled():
    assert Settings.model_fields['MAIN_MENU_RICH_SUBSCRIPTIONS_COLLAPSIBLE'].default is True


def test_tg_time_outside_int32_range_falls_back_to_text():
    """Telegram хранит даты 32-битным unix time: tg-time с датой после 19.01.2038
    или до эпохи сервер отклоняет ошибкой RICH_MESSAGE_DATE_INVALID — вместо тега
    остаётся fallback-текст."""
    valid = rich_menu._tg_time(datetime(2030, 1, 1, tzinfo=UTC), 'd', '01.01.2030')
    assert valid.startswith('<tg-time unix="')

    boundary = rich_menu._tg_time(datetime.fromtimestamp(2**31 - 1, UTC), 'd', 'граница')
    assert boundary.startswith('<tg-time unix="2147483647"')

    too_late = rich_menu._tg_time(datetime(2038, 1, 20, tzinfo=UTC), 'd', '20.01.2038')
    assert too_late == '20.01.2038'

    too_early = rich_menu._tg_time(datetime(1969, 12, 31, tzinfo=UTC), 'd', '31.12.1969')
    assert too_early == '31.12.1969'

    # Fallback-текст экранируется так же, как внутри tg-time
    escaped = rich_menu._tg_time(datetime(2099, 1, 1, tzinfo=UTC), 'd', 'a<b>&c')
    assert escaped == 'a&lt;b&gt;&amp;c'


async def test_far_future_end_date_in_table_renders_without_tg_time(monkeypatch):
    """«Вечная» подписка (например, импорт из панели с датой 2099) не роняет
    rich-меню: дата в таблице — обычным текстом без tg-time."""
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    now = datetime.now(UTC)
    eternal = _make_subscription(now)
    eternal.end_date = datetime(2099, 12, 31, 12, 0, tzinfo=UTC)

    async def fake_get_all(db, user_id):
        return [eternal]

    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', fake_get_all)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(eternal), DummyTexts(), AsyncMock())

    assert '<tg-time' not in html_out
    assert '2099' in html_out
    assert '🟢 Активна' in html_out


async def test_far_future_end_date_in_single_block_renders_without_tg_time(monkeypatch):
    _patch_content_sources(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: False)

    now = datetime.now(UTC)
    eternal = _make_subscription(now)
    eternal.end_date = datetime(2099, 12, 31, 12, 0, tzinfo=UTC)

    html_out = await rich_menu.build_main_menu_rich_html(_make_user(eternal), DummyTexts(), AsyncMock())

    assert '<tg-time' not in html_out
    # Строка «истекает …» осталась — с текстом остатка дней вместо tg-time
    assert 'осталось' in html_out
