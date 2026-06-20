from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.handlers.subscription.autopay import handle_subscription_cancel
from app.handlers.subscription.purchase import clear_saved_cart, return_to_saved_cart, save_cart_and_redirect_to_topup


@pytest.fixture
def mock_callback_query():
    callback = AsyncMock(spec=CallbackQuery)
    callback.message = AsyncMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    callback.data = 'subscription_confirm'
    return callback


@pytest.fixture
def mock_user():
    user = AsyncMock(spec=User)
    user.id = 12345
    user.telegram_id = 12345
    user.language = 'ru'
    user.balance_kopeks = 10000
    user.subscription = None
    user.has_had_paid_subscription = False
    user.promo_group_id = None
    user.get_primary_promo_group = MagicMock(return_value=None)
    user.get_promo_discount = MagicMock(return_value=0)
    user.promo_offer_discount_percent = 0
    user.promo_offer_discount_expires_at = None
    return user


@pytest.fixture
def mock_db():
    db = AsyncMock(spec=AsyncSession)
    return db


@pytest.fixture
def mock_state():
    state = AsyncMock(spec=FSMContext)
    state.get_data = AsyncMock(
        return_value={'period_days': 30, 'countries': ['ru'], 'devices': 2, 'traffic_gb': 10, 'total_price': 50000}
    )
    state.set_data = AsyncMock()
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()
    state.clear = AsyncMock()
    return state


async def test_save_cart_and_redirect_to_topup(mock_callback_query, mock_state, mock_user, mock_db):
    """Тест сохранения корзины и перенаправления к пополнению"""
    # Мокаем все зависимости
    with (
        patch('app.handlers.subscription.purchase.user_cart_service') as mock_cart_service,
        patch('app.handlers.subscription.purchase.get_payment_methods_keyboard_with_cart') as mock_keyboard_func,
        patch('app.localization.texts.get_texts') as mock_get_texts,
    ):
        # Подготовим моки
        mock_cart_service.save_user_cart = AsyncMock(return_value=True)
        mock_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='✅', callback_data='confirm')]]
        )
        mock_keyboard_func.return_value = mock_keyboard

        # Подготовим тексты
        mock_texts = AsyncMock()
        mock_texts.format_price = lambda x: f'{x / 100:.0f} ₽'
        mock_get_texts.return_value = mock_texts

        missing_amount = 40000  # 50000 - 10000 = 40000

        # Вызываем функцию
        await save_cart_and_redirect_to_topup(mock_callback_query, mock_state, mock_user, missing_amount)

        # Проверяем, что данные были сохранены в корзину
        mock_cart_service.save_user_cart.assert_called_once()
        args, kwargs = mock_cart_service.save_user_cart.call_args
        saved_user_id, saved_cart_data = args

        assert saved_user_id == mock_user.id
        assert saved_cart_data['period_days'] == 30
        assert saved_cart_data['countries'] == ['ru']
        assert saved_cart_data['devices'] == 2
        assert saved_cart_data['traffic_gb'] == 10
        assert saved_cart_data['total_price'] == 50000
        assert saved_cart_data['saved_cart'] is True
        assert saved_cart_data['missing_amount'] == missing_amount
        assert saved_cart_data['return_to_cart'] is True
        assert saved_cart_data['user_id'] == mock_user.id

        # Проверяем, что сообщение было отредактировано
        mock_callback_query.message.edit_text.assert_called_once()

        # В этой функции нет вызова callback.answer()
        # mock_callback_query.answer не должен быть вызван
        mock_callback_query.answer.assert_not_called()


async def test_return_to_saved_cart_success(mock_callback_query, mock_state, mock_user, mock_db):
    """Тест возврата к сохраненной корзине с достаточным балансом"""
    # Подготовим данные корзины
    cart_data = {
        'period_days': 30,
        'countries': ['ru', 'us'],
        'devices': 3,
        'traffic_gb': 20,
        'total_price': 30000,  # Меньше, чем баланс пользователя (50000)
        'saved_cart': True,
        'user_id': mock_user.id,
    }

    # Мокаем все зависимости
    with (
        patch('app.handlers.subscription.purchase.user_cart_service') as mock_cart_service,
        patch('app.handlers.subscription.purchase._get_available_countries') as mock_get_countries,
        patch('app.handlers.subscription.purchase.format_period_description') as mock_format_period,
        patch('app.localization.texts.get_texts') as mock_get_texts,
        patch('app.handlers.subscription.purchase.get_subscription_confirm_keyboard_with_cart') as mock_keyboard_func,
        patch('app.handlers.subscription.purchase._prepare_subscription_summary') as mock_prepare_summary,
    ):
        # Подготовим моки
        mock_cart_service.get_user_cart = AsyncMock(return_value=cart_data)
        mock_cart_service.save_user_cart = AsyncMock(return_value=True)
        mock_prepare_summary.return_value = ('summary', {})
        mock_get_countries.return_value = [{'uuid': 'ru', 'name': 'Russia'}, {'uuid': 'us', 'name': 'USA'}]
        mock_format_period.return_value = '30 дней'
        mock_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='✅', callback_data='confirm')]]
        )
        mock_keyboard_func.return_value = mock_keyboard

        # Подготовим тексты
        mock_texts = AsyncMock()
        mock_texts.format_price = lambda x: f'{x / 100:.0f} ₽'
        mock_get_texts.return_value = mock_texts

        # Увеличиваем баланс пользователя, чтобы его хватило
        mock_user.balance_kopeks = 50000

        # Вызываем функцию
        await return_to_saved_cart(mock_callback_query, mock_state, mock_user, mock_db)

        # Проверяем, что корзина была загружена
        mock_cart_service.get_user_cart.assert_called_once_with(mock_user.id)

        # Проверяем, что сообщение было отредактировано
        mock_callback_query.message.edit_text.assert_called_once()

        # В успешном сценарии вызывается callback.answer()
        mock_callback_query.answer.assert_called_once()


async def test_return_to_saved_cart_skips_edit_when_message_matches(
    mock_callback_query,
    mock_state,
    mock_user,
    mock_db,
):
    cart_data = {
        'period_days': 60,
        'countries': ['ru', 'us'],
        'devices': 3,
        'traffic_gb': 40,
        'total_price': 44000,
        'saved_cart': True,
        'user_id': mock_user.id,
    }

    confirm_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='Подтвердить', callback_data='confirm')]]
    )
    existing_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='Подтвердить', callback_data='confirm')]]
    )

    with (
        patch('app.handlers.subscription.purchase.user_cart_service') as mock_cart_service,
        patch('app.handlers.subscription.purchase._get_available_countries') as mock_get_countries,
        patch('app.handlers.subscription.purchase.format_period_description') as mock_format_period,
        patch('app.localization.texts.get_texts') as mock_get_texts,
        patch('app.handlers.subscription.purchase.get_subscription_confirm_keyboard_with_cart') as mock_keyboard_func,
        patch('app.handlers.subscription.purchase.settings') as mock_settings,
    ):
        mock_cart_service.get_user_cart = AsyncMock(return_value=cart_data)
        mock_cart_service.save_user_cart = AsyncMock()
        mock_get_countries.return_value = [
            {'uuid': 'ru', 'name': 'Russia'},
            {'uuid': 'us', 'name': 'USA'},
        ]
        mock_format_period.return_value = '60 дней'
        mock_keyboard_func.return_value = confirm_keyboard

        mock_texts = AsyncMock()
        mock_texts.format_price = lambda x: f'{x / 100:.0f} ₽'
        mock_get_texts.return_value = mock_texts

        mock_settings.is_devices_selection_enabled.return_value = True
        mock_settings.is_traffic_fixed.return_value = False

        mock_user.balance_kopeks = 50000

        summary_text = (
            '🛒 Восстановленная корзина\n\n'
            '📅 Период: 60 дней\n'
            '📊 Трафик: 40 ГБ\n'
            '🌍 Страны: Russia, USA\n'
            '📱 Устройства: 3\n\n'
            '💎 Общая стоимость: 440 ₽\n\n'
            'Подтверждаете покупку?'
        )

        mock_callback_query.message.text = summary_text
        mock_callback_query.message.reply_markup = existing_keyboard

        await return_to_saved_cart(mock_callback_query, mock_state, mock_user, mock_db)

        mock_callback_query.message.edit_text.assert_not_called()
        mock_callback_query.answer.assert_called_once_with('✅ Корзина восстановлена!')
        mock_state.set_data.assert_called_once_with(cart_data)
        mock_state.set_state.assert_called_once()
        mock_cart_service.save_user_cart.assert_not_called()


async def test_return_to_saved_cart_normalizes_devices_when_disabled(
    mock_callback_query,
    mock_state,
    mock_user,
    mock_db,
):
    cart_data = {
        'period_days': 30,
        'countries': ['ru', 'us'],
        'devices': 5,
        'traffic_gb': 20,
        'total_price': 45000,
        'total_devices_price': 15000,
        'saved_cart': True,
        'user_id': mock_user.id,
    }

    sanitized_summary_data = {
        'period_days': 30,
        'countries': ['ru', 'us'],
        'devices': 3,
        'traffic_gb': 20,
        'total_price': 30000,
        'total_devices_price': 0,
    }

    with (
        patch('app.handlers.subscription.purchase.user_cart_service') as mock_cart_service,
        patch('app.handlers.subscription.purchase._get_available_countries') as mock_get_countries,
        patch('app.handlers.subscription.purchase.format_period_description') as mock_format_period,
        patch('app.localization.texts.get_texts') as mock_get_texts,
        patch('app.handlers.subscription.purchase.get_subscription_confirm_keyboard_with_cart') as mock_keyboard_func,
        patch('app.handlers.subscription.purchase.settings') as mock_settings,
        patch(
            'app.handlers.subscription.pricing._prepare_subscription_summary',
            new=AsyncMock(return_value=('ignored', sanitized_summary_data)),
        ),
    ):
        mock_cart_service.get_user_cart = AsyncMock(return_value=cart_data)
        mock_cart_service.save_user_cart = AsyncMock()
        mock_get_countries.return_value = [{'uuid': 'ru', 'name': 'Russia'}, {'uuid': 'us', 'name': 'USA'}]
        mock_format_period.return_value = '30 дней'
        mock_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='✅', callback_data='confirm')]]
        )
        mock_keyboard_func.return_value = mock_keyboard

        mock_texts = AsyncMock()
        mock_texts.format_price = lambda x: f'{x / 100:.0f} ₽'
        mock_texts.t = lambda key, default=None: default or ''
        mock_get_texts.return_value = mock_texts

        mock_settings.is_devices_selection_enabled.return_value = False
        mock_settings.DEFAULT_DEVICE_LIMIT = 3
        mock_settings.is_traffic_fixed.return_value = False
        mock_settings.get_fixed_traffic_limit.return_value = 0

        mock_user.balance_kopeks = 60000

        await return_to_saved_cart(mock_callback_query, mock_state, mock_user, mock_db)

        mock_cart_service.save_user_cart.assert_called_once()
        _, saved_payload = mock_cart_service.save_user_cart.call_args[0]
        assert saved_payload['devices'] == 3
        assert saved_payload['total_price'] == 30000
        assert saved_payload['saved_cart'] is True

        mock_state.set_data.assert_called_once()
        normalized_data = mock_state.set_data.call_args[0][0]
        assert normalized_data['devices'] == 3
        assert normalized_data['total_price'] == 30000
        assert normalized_data['saved_cart'] is True

        edited_text = mock_callback_query.message.edit_text.call_args[0][0]
        assert '📱' not in edited_text

        mock_callback_query.answer.assert_called_once()


async def test_return_to_saved_cart_insufficient_funds(mock_callback_query, mock_state, mock_user, mock_db):
    """Тест возврата к сохраненной корзине с недостаточным балансом"""
    # Подготовим данные корзины
    cart_data = {
        'period_days': 30,
        'countries': ['ru', 'us'],
        'devices': 3,
        'traffic_gb': 20,
        'total_price': 50000,  # Больше, чем баланс пользователя (10000)
        'saved_cart': True,
        'user_id': mock_user.id,
    }

    # Мокаем все зависимости
    with (
        patch('app.handlers.subscription.purchase.user_cart_service') as mock_cart_service,
        patch('app.localization.texts.get_texts') as mock_get_texts,
        patch('app.handlers.subscription.purchase.get_insufficient_balance_keyboard_with_cart') as mock_keyboard_func,
    ):
        # Подготовим моки
        mock_cart_service.get_user_cart = AsyncMock(return_value=cart_data)
        mock_cart_service.save_user_cart = AsyncMock(return_value=True)
        mock_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='Пополнить', callback_data='topup')]]
        )
        mock_keyboard_func.return_value = mock_keyboard

        # Подготовим тексты
        mock_texts = AsyncMock()
        mock_texts.format_price = lambda x: f'{x / 100:.0f} ₽'
        mock_texts.t = lambda key, default: default
        mock_get_texts.return_value = mock_texts

        # Баланс пользователя меньше стоимости подписки
        mock_user.balance_kopeks = 10000

        # Вызываем функцию
        await return_to_saved_cart(mock_callback_query, mock_state, mock_user, mock_db)

        # Проверяем, что FSM не был изменен (данные не установлены)
        mock_state.set_data.assert_not_called()

        # Проверяем, что сообщение было отредактировано с сообщением о недостатке средств
        mock_callback_query.message.edit_text.assert_called_once()

        # В этой функции в сценарии недостатка средств вызова callback.answer() не происходит
        # (ответ отправляется через return до вызова callback.answer())
        mock_callback_query.answer.assert_not_called()


async def test_clear_saved_cart(mock_callback_query, mock_state, mock_user, mock_db):
    """Тест очистки сохраненной корзины"""
    # Мокаем все зависимости
    with (
        patch('app.handlers.subscription.purchase.user_cart_service') as mock_cart_service,
        patch('app.handlers.menu.show_main_menu') as mock_show_main_menu,
    ):
        mock_cart_service.delete_user_cart = AsyncMock(return_value=True)
        mock_show_main_menu.return_value = AsyncMock()

        # Вызываем функцию
        await clear_saved_cart(mock_callback_query, mock_state, mock_user, mock_db)

        # Проверяем, что корзина удалена из сервиса
        mock_cart_service.delete_user_cart.assert_called_once_with(mock_user.id)

        # Проверяем, что FSM очищен
        mock_state.clear.assert_called_once()

        # Проверяем, что вызван answer
        mock_callback_query.answer.assert_called_once()


async def test_handle_subscription_cancel_clears_saved_cart(mock_callback_query, mock_state, mock_user, mock_db):
    """Отмена покупки должна очищать сохраненную корзину"""
    mock_clear_draft = AsyncMock()
    mock_show_main_menu = AsyncMock()

    with (
        patch('app.handlers.subscription.autopay.user_cart_service') as mock_cart_service,
        patch('app.handlers.subscription.autopay.clear_subscription_checkout_draft', new=mock_clear_draft),
        patch('app.localization.texts.get_texts', return_value=MagicMock()) as _,
        patch('app.handlers.menu.show_main_menu', new=mock_show_main_menu),
    ):
        mock_cart_service.get_user_cart = AsyncMock(return_value=None)
        mock_cart_service.delete_user_cart = AsyncMock(return_value=True)

        await handle_subscription_cancel(mock_callback_query, mock_state, mock_user, mock_db)

        mock_state.clear.assert_called_once()
        mock_clear_draft.assert_awaited_once_with(mock_user.id)
        mock_cart_service.delete_user_cart.assert_awaited_once_with(mock_user.id)
        mock_show_main_menu.assert_awaited_once_with(mock_callback_query, mock_user, mock_db)
        mock_callback_query.answer.assert_called_once_with('❌ Покупка отменена')


async def test_handle_subscription_cancel_clears_only_current_subscription_cart(
    mock_callback_query, mock_state, mock_user, mock_db
):
    """Отмена покупки в мультитарифном сценарии чистит только корзину текущей подписки"""
    mock_clear_draft = AsyncMock()
    mock_show_main_menu = AsyncMock()

    with (
        patch('app.handlers.subscription.autopay.user_cart_service') as mock_cart_service,
        patch('app.handlers.subscription.autopay.clear_subscription_checkout_draft', new=mock_clear_draft),
        patch('app.localization.texts.get_texts', return_value=MagicMock()) as _,
        patch('app.handlers.menu.show_main_menu', new=mock_show_main_menu),
    ):
        # First read returns the per-subscription cart; second (global) read still
        # references the same subscription, so the global key is cleaned up too.
        mock_cart_service.get_user_cart = AsyncMock(side_effect=[{'subscription_id': 777}, {'subscription_id': 777}])
        mock_cart_service.delete_subscription_cart = AsyncMock(return_value=True)
        mock_cart_service.delete_global_cart_only = AsyncMock(return_value=True)
        mock_cart_service.delete_user_cart = AsyncMock(return_value=True)

        await handle_subscription_cancel(mock_callback_query, mock_state, mock_user, mock_db)

        mock_state.clear.assert_called_once()
        mock_clear_draft.assert_awaited_once_with(mock_user.id)
        # Money-safe: only the current subscription's cart is removed; the broad
        # delete_user_cart that could nuke other subscriptions' carts is NOT called.
        mock_cart_service.delete_subscription_cart.assert_awaited_once_with(mock_user.id, 777)
        mock_cart_service.delete_global_cart_only.assert_awaited_once_with(mock_user.id)
        mock_cart_service.delete_user_cart.assert_not_called()
        mock_show_main_menu.assert_awaited_once_with(mock_callback_query, mock_user, mock_db)
        mock_callback_query.answer.assert_called_once_with('❌ Покупка отменена')
