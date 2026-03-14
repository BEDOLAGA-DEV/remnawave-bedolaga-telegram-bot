from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.saved_payment_method import (
    deactivate_payment_method,
    get_active_payment_methods_by_user,
)
from app.database.crud.subscription import update_subscription_autopay
from app.database.models import User
from app.keyboards.inline import (
    _get_payment_method_display_name,
    get_autopay_days_keyboard,
    get_autopay_keyboard,
    get_confirm_unlink_keyboard,
    get_countries_keyboard,
    get_devices_keyboard,
    get_saved_cards_keyboard,
    get_subscription_period_keyboard,
    get_traffic_packages_keyboard,
)
from app.localization.texts import get_texts
from app.services.subscription_checkout_service import (
    clear_subscription_checkout_draft,
)
from app.services.user_cart_service import user_cart_service
from app.states import SubscriptionStates

from .countries import (
    _build_countries_selection_text,
    _get_available_countries,
    _get_preselected_free_countries,
    _should_show_countries_management,
)
from .pricing import _build_subscription_period_prompt


async def handle_autopay_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    subscription = db_user.subscription
    if not subscription:
        await callback.answer(
            texts.t('SUBSCRIPTION_ACTIVE_REQUIRED', '⚠️ У вас нет активной подписки!'),
            show_alert=True,
        )
        return

    # Суточные подписки имеют свой механизм продления, глобальный autopay не применяется
    try:
        await db.refresh(subscription, ['tariff'])
    except Exception:
        pass
    if subscription.tariff and getattr(subscription.tariff, 'is_daily', False):
        await callback.answer(
            texts.t(
                'AUTOPAY_NOT_AVAILABLE_FOR_DAILY',
                'Автоплатеж недоступен для суточных тарифов. Списание происходит автоматически раз в сутки.',
            ),
            show_alert=True,
        )
        return

    status = (
        texts.t('AUTOPAY_STATUS_ENABLED', 'включен')
        if subscription.autopay_enabled
        else texts.t('AUTOPAY_STATUS_DISABLED', 'выключен')
    )
    days = subscription.autopay_days_before

    text = texts.t(
        'AUTOPAY_MENU_TEXT',
        (
            '💳 <b>Автоплатеж</b>\n\n'
            '📊 <b>Статус:</b> {status}\n'
            '⏰ <b>Списание за:</b> {days} дн. до окончания\n\n'
            'Выберите действие:'
        ),
    ).format(status=status, days=days)

    await callback.message.edit_text(
        text,
        reply_markup=get_autopay_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


async def toggle_autopay(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    subscription = db_user.subscription
    enable = callback.data == 'autopay_enable'

    # Суточные подписки имеют свой механизм продления (DailySubscriptionService),
    # глобальный autopay для них запрещён
    if enable:
        try:
            await db.refresh(subscription, ['tariff'])
        except Exception:
            pass
        if subscription.tariff and getattr(subscription.tariff, 'is_daily', False):
            texts = get_texts(db_user.language)
            await callback.answer(
                texts.t(
                    'AUTOPAY_NOT_AVAILABLE_FOR_DAILY',
                    'Автоплатеж недоступен для суточных тарифов. Списание происходит автоматически раз в сутки.',
                ),
                show_alert=True,
            )
            return

    await update_subscription_autopay(db, subscription, enable)

    texts = get_texts(db_user.language)
    status = texts.t('AUTOPAY_STATUS_ENABLED', 'включен') if enable else texts.t('AUTOPAY_STATUS_DISABLED', 'выключен')
    await callback.answer(texts.t('AUTOPAY_TOGGLE_SUCCESS', '✅ Автоплатеж {status}!').format(status=status))

    await handle_autopay_menu(callback, db_user, db)


async def show_autopay_days(callback: types.CallbackQuery, db_user: User):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'AUTOPAY_SELECT_DAYS_PROMPT',
            '⏰ Выберите за сколько дней до окончания списывать средства:',
        ),
        reply_markup=get_autopay_days_keyboard(db_user.language),
    )
    await callback.answer()


async def set_autopay_days(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    days = int(callback.data.split('_')[2])
    subscription = db_user.subscription

    await update_subscription_autopay(db, subscription, subscription.autopay_enabled, days)

    texts = get_texts(db_user.language)
    await callback.answer(texts.t('AUTOPAY_DAYS_SET', '✅ Установлено {days} дней!').format(days=days))

    await handle_autopay_menu(callback, db_user, db)


async def handle_saved_cards_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    cards = await get_active_payment_methods_by_user(db, db_user.id)

    if not cards:
        await callback.message.edit_text(
            texts.t(
                'SAVED_CARDS_EMPTY',
                '💳 <b>Привязанные карты</b>\n\nНет привязанных карт.\n'
                'Карта привяжется автоматически при следующем пополнении баланса.',
            ),
            reply_markup=get_saved_cards_keyboard([], db_user.language),
            parse_mode='HTML',
        )
    else:
        await callback.message.edit_text(
            texts.t(
                'SAVED_CARDS_TITLE',
                '💳 <b>Привязанные карты</b>\n\nВыберите карту для отвязки:',
            ),
            reply_markup=get_saved_cards_keyboard(cards, db_user.language),
            parse_mode='HTML',
        )
    await callback.answer()


async def handle_unlink_card(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    card_id = int(callback.data.split('_')[-1])

    cards = await get_active_payment_methods_by_user(db, db_user.id)
    card = next((c for c in cards if c.id == card_id), None)

    if not card:
        await callback.answer(
            texts.t('SAVED_CARDS_UNLINK_ERROR', '❌ Не удалось отвязать карту'),
            show_alert=True,
        )
        return

    card_label = _get_payment_method_display_name(card, db_user.language)
    text = texts.t(
        'SAVED_CARDS_CONFIRM_UNLINK',
        'Вы уверены, что хотите отвязать карту <b>{card}</b>?\n\n'
        'После отвязки автоплатеж не сможет использовать эту карту.',
    ).format(card=card_label)

    if len(cards) == 1:
        text += texts.t(
            'SAVED_CARDS_LAST_CARD_WARNING',
            '\n\n⚠️ <b>Внимание:</b> это ваша последняя привязанная карта. '
            'После отвязки автоплатеж не сможет списывать средства.',
        )

    await callback.message.edit_text(
        text,
        reply_markup=get_confirm_unlink_keyboard(card_id, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


async def handle_confirm_unlink(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    card_id = int(callback.data.split('_')[-1])

    success = await deactivate_payment_method(db, card_id, db_user.id)

    if success:
        await callback.answer(
            texts.t('SAVED_CARDS_UNLINKED', '✅ Карта отвязана'),
        )
    else:
        await callback.answer(
            texts.t('SAVED_CARDS_UNLINK_ERROR', '❌ Не удалось отвязать карту'),
            show_alert=True,
        )
        return

    # Return to the updated cards list
    await handle_saved_cards_list(callback, db_user, db)


async def handle_subscription_config_back(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession
):
    current_state = await state.get_state()
    texts = get_texts(db_user.language)

    if current_state == SubscriptionStates.selecting_traffic.state:
        await callback.message.edit_text(
            await _build_subscription_period_prompt(db_user, texts, db),
            reply_markup=get_subscription_period_keyboard(db_user.language, db_user),
            parse_mode='HTML',
        )
        await state.set_state(SubscriptionStates.selecting_period)

    elif current_state == SubscriptionStates.selecting_countries.state:
        if settings.is_traffic_selectable():
            await callback.message.edit_text(
                texts.SELECT_TRAFFIC, reply_markup=get_traffic_packages_keyboard(db_user.language)
            )
            await state.set_state(SubscriptionStates.selecting_traffic)
        else:
            await callback.message.edit_text(
                await _build_subscription_period_prompt(db_user, texts, db),
                reply_markup=get_subscription_period_keyboard(db_user.language, db_user),
                parse_mode='HTML',
            )
            await state.set_state(SubscriptionStates.selecting_period)

    elif current_state == SubscriptionStates.selecting_devices.state:
        await _show_previous_configuration_step(callback, state, db_user, texts, db)

    elif current_state == SubscriptionStates.confirming_purchase.state:
        if settings.is_devices_selection_enabled():
            data = await state.get_data()
            selected_devices = data.get('devices', settings.DEFAULT_DEVICE_LIMIT)

            await callback.message.edit_text(
                texts.SELECT_DEVICES, reply_markup=get_devices_keyboard(selected_devices, db_user.language)
            )
            await state.set_state(SubscriptionStates.selecting_devices)
        else:
            await _show_previous_configuration_step(callback, state, db_user, texts, db)

    else:
        from app.handlers.menu import show_main_menu

        await show_main_menu(callback, db_user, db)
        await state.clear()

    await callback.answer()


async def handle_subscription_cancel(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    get_texts(db_user.language)

    await state.clear()
    await clear_subscription_checkout_draft(db_user.id)

    # Удаляем сохраненную корзину, чтобы не показывать кнопку возврата
    await user_cart_service.delete_user_cart(db_user.id)

    from app.handlers.menu import show_main_menu

    await show_main_menu(callback, db_user, db)

    await callback.answer('❌ Покупка отменена')


async def _show_previous_configuration_step(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    texts,
    db: AsyncSession,
):
    if await _should_show_countries_management(db_user):
        countries = await _get_available_countries(db_user.promo_group_id)
        data = await state.get_data()
        selected_countries = data.get('countries', [])

        # Если страны не выбраны — автоматически предвыбираем бесплатные
        if not selected_countries:
            selected_countries = _get_preselected_free_countries(countries)
            data['countries'] = selected_countries
            await state.set_data(data)

        # Формируем текст с описаниями сквадов
        selection_text = _build_countries_selection_text(countries, texts.SELECT_COUNTRIES)
        await callback.message.edit_text(
            selection_text,
            reply_markup=get_countries_keyboard(countries, selected_countries, db_user.language),
            parse_mode='HTML',
        )
        await state.set_state(SubscriptionStates.selecting_countries)
        return

    if settings.is_traffic_selectable():
        await callback.message.edit_text(
            texts.SELECT_TRAFFIC, reply_markup=get_traffic_packages_keyboard(db_user.language)
        )
        await state.set_state(SubscriptionStates.selecting_traffic)
        return

    await callback.message.edit_text(
        await _build_subscription_period_prompt(db_user, texts, db),
        reply_markup=get_subscription_period_keyboard(db_user.language, db_user),
        parse_mode='HTML',
    )
    await state.set_state(SubscriptionStates.selecting_period)
