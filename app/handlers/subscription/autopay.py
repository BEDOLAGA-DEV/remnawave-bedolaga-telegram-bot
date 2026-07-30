from typing import Any

import structlog
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
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
    get_autopay_period_keyboard,
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
from app.utils.formatters import format_datetime

from .countries import (
    _build_countries_selection_text,
    _get_available_countries,
    _get_preselected_free_countries,
    _should_show_countries_management,
)
from .pricing import _build_subscription_period_prompt


logger = structlog.get_logger(__name__)


async def _resolve_subscription(callback, db_user, db, state=None):
    """Resolve subscription — delegates to shared resolve_subscription_from_context."""
    from .common import resolve_subscription_from_context

    return await resolve_subscription_from_context(callback, db_user, db, state)


async def handle_autopay_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None):
    texts = get_texts(db_user.language)
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
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
        # Баланс-автоплатёж для суточных тарифов недоступен, но СБП-автопродление
        # Platega суточный интервал поддерживает (`day`) — вход в него должен
        # оставаться достижимым, поэтому кнопку добавляем в тот же экран.
        daily_keyboard_rows: list[list[types.InlineKeyboardButton]] = []
        if settings.is_platega_recurrent_enabled():
            daily_keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('SBP_RECURRING_MENU_BUTTON', '⚡ Автопродление через СБП'),
                        callback_data='sbp_recurring_menu',
                    )
                ]
            )
        back_cb = f'sm:{sub_id}' if sub_id and settings.is_multi_tariff_enabled() else 'menu_subscription'
        daily_keyboard_rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)])

        await callback.message.edit_text(
            texts.t(
                'AUTOPAY_NOT_AVAILABLE_FOR_DAILY',
                'Автоплатеж недоступен для суточных тарифов. Списание происходит автоматически раз в сутки.',
            ),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=daily_keyboard_rows),
        )
        await callback.answer()
        return

    status = (
        texts.t('AUTOPAY_STATUS_ENABLED', 'включен')
        if subscription.autopay_enabled
        else texts.t('AUTOPAY_STATUS_DISABLED', 'выключен')
    )
    days = subscription.autopay_days_before

    period_value = getattr(subscription, 'autopay_period_days', None)
    if period_value:
        period_text = texts.t('AUTOPAY_PERIOD_VALUE', '{days} дн.').format(days=period_value)
    else:
        period_text = texts.t('AUTOPAY_PERIOD_DEFAULT_VALUE', 'по умолчанию (самый дешёвый период тарифа)')

    text = texts.t(
        'AUTOPAY_MENU_TEXT',
        (
            '💳 <b>Автоплатеж</b>\n\n'
            '📊 <b>Статус:</b> {status}\n'
            '⏰ <b>Списание за:</b> {days} дн. до окончания\n'
            '📅 <b>Период продления:</b> {period}\n\n'
            'Выберите действие:'
        ),
    ).format(status=status, days=days, period=period_text)

    keyboard = get_autopay_keyboard(db_user.language, sub_id=sub_id)
    if settings.is_platega_recurrent_enabled():
        # Вставляем перед последней строкой (Back), чтобы кнопка СБП была
        # среди основных действий, а не под ней.
        keyboard.inline_keyboard.insert(
            -1,
            [
                types.InlineKeyboardButton(
                    text=texts.t('SBP_RECURRING_MENU_BUTTON', '⚡ Автопродление через СБП'),
                    callback_data='sbp_recurring_menu',
                )
            ],
        )

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


async def toggle_autopay(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None):
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return
    enable = callback.data.startswith('autopay_enable')

    if enable:
        # Trial subscriptions cannot use autopay
        if subscription.is_trial or subscription.is_trial is None:
            texts = get_texts(db_user.language)
            await callback.answer(
                texts.t(
                    'AUTOPAY_NOT_AVAILABLE_TRIAL',
                    'Автоплатеж недоступен для пробных подписок.',
                ),
                show_alert=True,
            )
            return

        # Classic subscriptions cannot use autopay when tariff mode is enabled
        if settings.is_tariffs_mode() and not subscription.tariff_id:
            texts = get_texts(db_user.language)
            await callback.answer(
                texts.t(
                    'AUTOPAY_NOT_AVAILABLE_CLASSIC',
                    'Автоплатеж недоступен. Для продления необходимо выбрать тариф.',
                ),
                show_alert=True,
            )
            return

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

    if enable:
        # Обратное взаимоисключение: включение баланс-автоплатежа должно
        # отменить активную СБП-автоподписку Platega у той же подписки —
        # иначе оба движка продления начнут списывать параллельно (двойное
        # списание). Прямое взаимоисключение (СБП -> выключение
        # balance-autopay) уже реализовано в create_platega_sbp_subscription.
        from app.services.payment.lava import cancel_lava_recurring_for_subscription_safe
        from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

        await cancel_platega_recurring_for_subscription_safe(db, subscription.id)

        await cancel_lava_recurring_for_subscription_safe(db, subscription.id)
    texts = get_texts(db_user.language)
    status = texts.t('AUTOPAY_STATUS_ENABLED', 'включен') if enable else texts.t('AUTOPAY_STATUS_DISABLED', 'выключен')
    await callback.answer(texts.t('AUTOPAY_TOGGLE_SUCCESS', '✅ Автоплатеж {status}!').format(status=status))

    try:
        await handle_autopay_menu(callback, db_user, db, state)
    except TelegramBadRequest as e:
        if 'message is not modified' in str(e):
            pass
        else:
            raise


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


async def set_autopay_days(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None):
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return
    base_data = callback.data.split(':')[0]
    days = int(base_data.split('_')[2])

    await update_subscription_autopay(db, subscription, subscription.autopay_enabled, days)

    texts = get_texts(db_user.language)
    await callback.answer(texts.t('AUTOPAY_DAYS_SET', '✅ Установлено {days} дней!').format(days=days))

    await handle_autopay_menu(callback, db_user, db, state)


def _get_subscription_renewal_periods(subscription) -> list[int]:
    """Available renewal periods for a subscription: tariff periods (preferred) or global env list."""
    tariff = getattr(subscription, 'tariff', None)
    if tariff:
        periods = tariff.get_available_periods() or []
        if periods:
            return sorted(periods)
    return sorted(settings.get_available_renewal_periods())


async def show_autopay_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None):
    """Period picker UI for autopay."""
    texts = get_texts(db_user.language)
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    try:
        await db.refresh(subscription, ['tariff'])
    except Exception:
        pass

    periods = _get_subscription_renewal_periods(subscription)
    current = getattr(subscription, 'autopay_period_days', None)

    await callback.message.edit_text(
        texts.t(
            'AUTOPAY_SELECT_PERIOD_PROMPT',
            '📅 Выберите период, на который автоплатёж будет продлевать подписку:',
        ),
        reply_markup=get_autopay_period_keyboard(periods, current, db_user.language),
    )
    await callback.answer()


async def set_autopay_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None):
    """Handle period selection (autopay_period_<N> or autopay_period_default)."""
    from app.database.crud.subscription import update_subscription_autopay as _update_autopay

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    raw = callback.data.split(':')[0]
    suffix = raw[len('autopay_period_') :] if raw.startswith('autopay_period_') else ''
    texts = get_texts(db_user.language)

    if suffix == 'default':
        await _update_autopay(db, subscription, subscription.autopay_enabled, period_days=None)
        await callback.answer(texts.t('AUTOPAY_PERIOD_SET_DEFAULT', '✅ Используется период по умолчанию.'))
    else:
        try:
            days = int(suffix)
        except ValueError:
            await callback.answer(texts.t('INVALID_REQUEST', 'Invalid request'), show_alert=True)
            return

        try:
            await db.refresh(subscription, ['tariff'])
        except Exception:
            pass

        available = _get_subscription_renewal_periods(subscription)
        if days not in available:
            await callback.answer(
                texts.t(
                    'AUTOPAY_PERIOD_NOT_AVAILABLE',
                    '❌ Этот период недоступен для данной подписки.',
                ),
                show_alert=True,
            )
            return

        await _update_autopay(db, subscription, subscription.autopay_enabled, period_days=days)
        await callback.answer(texts.t('AUTOPAY_PERIOD_SET', '✅ Период автоплатежа: {days} дн.').format(days=days))

    await handle_autopay_menu(callback, db_user, db, state)


def _platega_sbp_status_text(record: Any, texts) -> str:
    """Pure builder: человекочитаемая строка статуса СБП-автопродления Platega.

    ``record`` — ``PlategaSubscription``-подобный объект (реальная модель или
    ``SimpleNamespace`` в тестах) с атрибутами ``status``/``next_charge_at``,
    либо ``None`` (автопродление не подключено). Не трогает БД/сеть — чистая
    функция для unit-тестов и переиспользования в других UI (бот/кабинет).
    """
    if record is None:
        return texts.t('SBP_RECURRING_STATUS_NONE', 'не подключено')

    status = getattr(record, 'status', None)

    if status == 'PENDING':
        return texts.t('SBP_RECURRING_STATUS_PENDING', 'ожидает подтверждения в банке')

    if status == 'ACTIVE':
        next_charge_at = getattr(record, 'next_charge_at', None)
        when = (
            format_datetime(next_charge_at)
            if next_charge_at
            else texts.t('SBP_RECURRING_NEXT_CHARGE_UNKNOWN', 'уточняется')
        )
        return texts.t(
            'SBP_RECURRING_STATUS_ACTIVE',
            'активно (следующее списание {next_charge_at})',
        ).format(next_charge_at=when)

    if status == 'PAST_DUE':
        return texts.t('SBP_RECURRING_STATUS_PAST_DUE', 'просрочено')

    if status == 'CANCELLED':
        return texts.t('SBP_RECURRING_STATUS_CANCELLED', 'отменено')

    if status == 'FAILED':
        return texts.t('SBP_RECURRING_STATUS_FAILED', 'не удалось подключить')

    return texts.t('SBP_RECURRING_STATUS_UNKNOWN', 'статус неизвестен ({status})').format(status=status)


async def handle_sbp_recurring_menu(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    """СБП-автопродление Platega: статус текущей подписки + Enable/Cancel."""
    texts = get_texts(db_user.language)

    if not settings.is_platega_recurrent_enabled():
        await callback.answer(
            texts.t('SBP_RECURRING_UNAVAILABLE', '⚠️ Автопродление через СБП сейчас недоступно'),
            show_alert=True,
        )
        return

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if not subscription:
        await callback.answer(
            texts.t('SUBSCRIPTION_ACTIVE_REQUIRED', '⚠️ У вас нет активной подписки!'),
            show_alert=True,
        )
        return

    from app.database.crud.platega_subscription import get_active_platega_subscription_by_subscription

    record = await get_active_platega_subscription_by_subscription(db, subscription.id)
    status_text = _platega_sbp_status_text(record, texts)

    text = texts.t(
        'SBP_RECURRING_MENU_TEXT',
        '⚡ <b>Автопродление через СБП</b>\n\n📊 <b>Статус:</b> {status}',
    ).format(status=status_text)

    if record is not None:
        # get_active_platega_subscription_by_subscription уже фильтрует по
        # PENDING/ACTIVE/PAST_DUE — любая непустая запись «активна» для целей UI.
        action_button = types.InlineKeyboardButton(
            text=texts.t('SBP_RECURRING_CANCEL_BUTTON', '❌ Отменить автооплату'),
            callback_data='sbp_recurring_cancel',
        )
    else:
        action_button = types.InlineKeyboardButton(
            text=texts.t('SBP_RECURRING_ENABLE_BUTTON', '✅ Подключить'),
            callback_data='sbp_recurring_enable',
        )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [action_button],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_autopay')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def handle_sbp_recurring_enable(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    """Подключить СБП-автопродление: создать рекуррентную Platega-подписку и
    показать кнопку подтверждения в банке."""
    texts = get_texts(db_user.language)

    if not settings.is_platega_recurrent_enabled():
        await callback.answer(
            texts.t('SBP_RECURRING_UNAVAILABLE', '⚠️ Автопродление через СБП сейчас недоступно'),
            show_alert=True,
        )
        return

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if not subscription:
        await callback.answer(
            texts.t('SUBSCRIPTION_ACTIVE_REQUIRED', '⚠️ У вас нет активной подписки!'),
            show_alert=True,
        )
        return

    # Trial subscriptions cannot authorize a real recurring bank payment
    # (same guard as toggle_autopay's balance-autopay enable path).
    if subscription.is_trial or subscription.is_trial is None:
        await callback.answer(
            texts.t(
                'AUTOPAY_NOT_AVAILABLE_TRIAL',
                'Автоплатеж недоступен для пробных подписок.',
            ),
            show_alert=True,
        )
        return

    # tariff обязателен: create_platega_sbp_subscription вызывает
    # tariff.get_purchasable_price_for_period(...) без None-guard'а (контракт
    # Task 5 — тариф считается обязательным для СБП-автопродления).
    try:
        await db.refresh(subscription, ['tariff'])
    except Exception:
        # refresh может упасть на detached/уже загруженном объекте — тогда
        # просто читаем то, что есть: None-guard ниже отработает.
        pass

    tariff = getattr(subscription, 'tariff', None)
    if tariff is None:
        await callback.answer(
            texts.t(
                'SBP_RECURRING_NO_TARIFF',
                '⚠️ Автопродление через СБП доступно только для подписок с тарифом.',
            ),
            show_alert=True,
        )
        return

    from app.services.payment.platega import enable_platega_sbp_recurring

    try:
        result = await enable_platega_sbp_recurring(db, user_id=db_user.id, subscription=subscription, tariff=tariff)
    except (ValueError, RuntimeError) as error:
        logger.warning(
            'Не удалось подключить СБП-автопродление',
            error=str(error),
            subscription_id=subscription.id,
        )
        await callback.answer(
            texts.t(
                'SBP_RECURRING_ENABLE_ERROR',
                '❌ Не удалось подключить автопродление через СБП. Попробуйте позже.',
            ),
            show_alert=True,
        )
        return

    redirect_url = result.get('redirect_url')
    if not redirect_url:
        # Идемпотентный возврат уже существующей подписки без сохранённой
        # ссылки — показываем статус вместо кнопки с пустым url (Telegram
        # такую кнопку не примет).
        await callback.answer(texts.t('SBP_RECURRING_ALREADY_ACTIVE', 'СБП-автопродление уже подключено.'))
        await handle_sbp_recurring_menu(callback, db_user, db, state)
        return

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('SBP_RECURRING_CONFIRM_BUTTON', '🏦 Подтвердить в банке'),
                    url=redirect_url,
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='sbp_recurring_menu')],
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'SBP_RECURRING_ENABLE_SUCCESS',
            '⚡ <b>Автопродление через СБП</b>\n\n'
            'Подтвердите подключение в банковском приложении по кнопке ниже.\n'
            'После подтверждения автопродление станет активным.',
        ),
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


async def handle_sbp_recurring_cancel(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    """Отменить активное СБП-автопродление и обновить статус-вью.

    НЕ гейтится PLATEGA_RECURRENT_ENABLED намеренно (паритет с кабинетным
    cancel-эндпоинтом): отмена — операция безопасности. Выключение фичи при
    живых привязках не останавливает списания Platega — юзер с существующей
    кнопкой отмены обязан иметь путь отписаться с любой поверхности.
    """
    texts = get_texts(db_user.language)

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if not subscription:
        await callback.answer(
            texts.t('SUBSCRIPTION_ACTIVE_REQUIRED', '⚠️ У вас нет активной подписки!'),
            show_alert=True,
        )
        return

    # Это кнопка отмены именно СБП-автопродления Platega — привязку Lava она
    # трогать не должна (у неё своя поверхность отмены).
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    await cancel_platega_recurring_for_subscription_safe(db, subscription.id)
    await callback.answer(texts.t('SBP_RECURRING_CANCELLED', '✅ Автопродление через СБП отменено'))
    # Меню гейтится флагом — при выключенной фиче не дёргаем его, чтобы после
    # успешной отмены юзер не получил второй алерт «недоступно».
    if settings.is_platega_recurrent_enabled():
        await handle_sbp_recurring_menu(callback, db_user, db, state)


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
    try:
        card_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('INVALID_REQUEST', 'Invalid request'), show_alert=True)
        return

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
    try:
        card_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('INVALID_REQUEST', 'Invalid request'), show_alert=True)
        return

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

    # Multi-tariff safe: delete only the cart for the current subscription
    # to avoid nuking carts belonging to other subscriptions.
    cart_data = await user_cart_service.get_user_cart(db_user.id)
    cart_sub_id = None
    if cart_data:
        try:
            raw = cart_data.get('subscription_id')
            if raw is not None:
                cart_sub_id = int(raw)
        except (TypeError, ValueError):
            pass

    if cart_sub_id is not None:
        await user_cart_service.delete_subscription_cart(db_user.id, cart_sub_id)
        # Clean up global key only if it still references this subscription
        global_cart = await user_cart_service.get_user_cart(db_user.id)
        if global_cart and global_cart.get('subscription_id') is not None:
            try:
                if int(global_cart['subscription_id']) == cart_sub_id:
                    await user_cart_service.delete_global_cart_only(db_user.id)
            except (TypeError, ValueError):
                pass
    else:
        # No subscription_id in cart -- safe to delete the global cart
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
