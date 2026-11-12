"""Обработчики для простой покупки подписки."""

import asyncio
import html
from typing import Any

import structlog
from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_happ_download_button_row
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.services.subscription_purchase_service import SubscriptionPurchaseService
from app.states import SubscriptionStates
from app.utils.decorators import error_handler
from app.utils.pricing_utils import compute_simple_subscription_price
from app.utils.subscription_time import local_days_until
from app.utils.subscription_utils import (
    get_display_subscription_link,
    resolve_simple_subscription_device_limit,
)
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)


async def _reject_classic_checkout_in_tariff_mode(callback: types.CallbackQuery, language: str | None = None) -> bool:
    """Old simple-purchase buttons must not bypass the tariff renewal entry."""
    texts = get_texts(language)
    if settings.is_tariffs_mode() or settings.is_multi_tariff_enabled():
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_UI_OPEN_PURCHASE_OR_RENEW', 'Откройте покупку или продление подписки и выберите тариф'
            ),
            show_alert=True,
        )
        return True
    return False


@error_handler
async def start_simple_subscription_purchase(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Начинает процесс простой покупки подписки."""
    if await _reject_classic_checkout_in_tariff_mode(callback, db_user.language):
        return
    texts = get_texts(db_user.language)

    if not settings.SIMPLE_SUBSCRIPTION_ENABLED:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_SIMPLE_UNAVAILABLE', '❌ Простая покупка подписки временно недоступна'),
            show_alert=True,
        )
        return

    if settings.is_multi_tariff_enabled():
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_USE_TARIFF_SELECTOR', 'Используйте выбор тарифа для управления подписками'),
            show_alert=True,
        )
        return

    # Проверка ограничения на покупку/продление подписки
    if getattr(db_user, 'restriction_subscription', False):
        reason = html.escape(
            getattr(db_user, 'restriction_reason', None)
            or texts.t('SUBSCRIPTION_UI_ADMIN_RESTRICTION_REASON', 'Действие ограничено администратором')
        )
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_UI_APPEAL', '🆘 Обжаловать'),
                        url=support_url,
                    )
                ]
            )
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription')])

        await callback.message.edit_text(
            texts.t(
                'SUBSCRIPTION_UI_RESTRICTION_REASON',
                '🚫 <b>Покупка подписки ограничена</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.',
            ).format(reason=reason),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    # Проверяем, есть ли у пользователя подписка
    from app.database.crud.subscription import get_subscription_by_user_id

    current_subscription = await get_subscription_by_user_id(db, db_user.id)

    device_limit = resolve_simple_subscription_device_limit()

    # При продлении учитываем количество устройств из текущей подписки
    # (независимо от того, включён ли выбор устройств)
    if current_subscription:
        current_device_limit = current_subscription.device_limit or device_limit
        # Используем максимум из текущего и дефолтного
        device_limit = max(device_limit, current_device_limit)

    # Подготовим параметры простой подписки
    subscription_params = {
        'period_days': settings.SIMPLE_SUBSCRIPTION_PERIOD_DAYS,
        'device_limit': device_limit,
        'traffic_limit_gb': settings.SIMPLE_SUBSCRIPTION_TRAFFIC_GB,
        'squad_uuid': settings.SIMPLE_SUBSCRIPTION_SQUAD_UUID,
    }

    # Сохраняем параметры в состояние
    await state.update_data(subscription_params=subscription_params)

    data = await state.get_data()
    resolved_squad_uuid = await _ensure_simple_subscription_squad_uuid(
        db,
        state,
        subscription_params,
        user_id=db_user.id,
        state_data=data,
    )

    price_kopeks, price_breakdown = await _calculate_simple_subscription_price(
        db,
        subscription_params,
        user=db_user,
        resolved_squad_uuid=resolved_squad_uuid,
    )

    period_days = subscription_params['period_days']
    user_balance_kopeks = getattr(db_user, 'balance_kopeks', 0)

    logger.warning(
        'SIMPLE_SUBSCRIPTION_DEBUG_START | user=%s | period=%s | base=%s | traffic=%s | devices=%s | servers=%s | discount=%s | total=%s | squads=%s',
        db_user.id,
        period_days,
        price_breakdown.get('base_price', 0),
        price_breakdown.get('traffic_price', 0),
        price_breakdown.get('devices_price', 0),
        price_breakdown.get('servers_price', 0),
        price_breakdown.get('total_discount', 0),
        price_kopeks,
        ','.join(price_breakdown.get('resolved_squad_uuids', []))
        if price_breakdown.get('resolved_squad_uuids')
        else 'none',
    )

    can_pay_from_balance = user_balance_kopeks >= price_kopeks
    logger.warning(
        'SIMPLE_SUBSCRIPTION_DEBUG_START_BALANCE',
        db_user_id=db_user.id,
        user_balance_kopeks=user_balance_kopeks,
        price_kopeks=price_kopeks,
        can_pay_from_balance=can_pay_from_balance,
    )

    # Проверяем, является ли у пользователя текущая подписка активной платной подпиской
    has_active_paid_subscription = False
    trial_notice = ''
    if current_subscription:
        if not getattr(current_subscription, 'is_trial', False) and current_subscription.is_active:
            # Это платная активная подписка - требуем подтверждение
            has_active_paid_subscription = True
        elif getattr(current_subscription, 'is_trial', False):
            # Это тестовая подписка
            try:
                days_left = local_days_until(current_subscription.end_date)
            except Exception:
                days_left = 0
            key = (
                'SIMPLE_SUBSCRIPTION_TRIAL_NOTICE_ACTIVE'
                if current_subscription.is_active
                else 'SIMPLE_SUBSCRIPTION_TRIAL_NOTICE_TRIAL'
            )
            trial_notice = texts.t(
                key,
                'ℹ️ У вас уже есть триальная подписка. Она истекает через {days} дн.',
            ).format(days=days_left)

    server_label = _get_simple_subscription_server_label(
        texts,
        subscription_params,
        resolved_squad_uuid,
    )
    show_devices = settings.is_devices_selection_enabled()

    message_lines = [
        texts.t('SUBSCRIPTION_UI_SIMPLE_TITLE', '⚡ <b>Простая покупка подписки</b>'),
        '',
        texts.t('SUBSCRIPTION_UI_PERIOD_DAYS', '📅 Период: {days} дней').format(
            days=subscription_params['period_days']
        ),
    ]

    if show_devices:
        message_lines.append(
            texts.t('SUBSCRIPTION_UI_DEVICES_VALUE', '📱 Устройства: {devices}').format(
                devices=subscription_params['device_limit']
            )
        )

    traffic_limit_gb = subscription_params['traffic_limit_gb']
    traffic_label = (
        texts.t('SUBSCRIPTION_UI_UNLIMITED', 'Безлимит')
        if traffic_limit_gb == 0
        else texts.t('SUBSCRIPTION_UI_GB_VALUE', '{amount} ГБ').format(amount=traffic_limit_gb)
    )

    message_lines.extend(
        [
            texts.t('SUBSCRIPTION_UI_TRAFFIC_VALUE', '📊 Трафик: {traffic}').format(traffic=traffic_label),
            texts.t('SUBSCRIPTION_UI_SERVER_VALUE', '🌍 Сервер: {server}').format(server=server_label),
            '',
            texts.t('SUBSCRIPTION_UI_COST_VALUE', '💰 Стоимость: {amount}').format(
                amount=settings.format_price(price_kopeks)
            ),
            texts.t('SUBSCRIPTION_UI_BALANCE_VALUE', '💳 Ваш баланс: {amount}').format(
                amount=settings.format_price(user_balance_kopeks)
            ),
            '',
        ]
    )

    # Если у пользователя уже есть активная платная подписка, требуем подтверждение
    if has_active_paid_subscription:
        # У пользователя уже есть активная платная подписка
        message_lines.append(
            texts.t(
                'SUBSCRIPTION_UI_ACTIVE_CHANGE_WARNING',
                '⚠️ У вас уже есть активная платная подписка. Покупка простой подписки изменит параметры вашей текущей подписки. Требуется подтверждение.',
            )
        )
        message_text = '\n'.join(message_lines)

        # Клавиатура с подтверждением
        keyboard_rows = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_CONFIRM_PURCHASE', '✅ Подтвердить покупку'),
                    callback_data='simple_subscription_confirm_purchase',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
        ]
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    else:
        # У пользователя нет активной платной подписки (или есть только пробная)
        # Показываем стандартный выбор метода оплаты
        if can_pay_from_balance:
            message_lines.append(
                texts.t(
                    'SUBSCRIPTION_UI_BALANCE_OR_METHOD',
                    'Вы можете оплатить подписку с баланса или выбрать другой способ оплаты.',
                )
            )
        else:
            message_lines.append(
                texts.t(
                    'SUBSCRIPTION_UI_INSUFFICIENT_SELECT_METHOD',
                    'Баланс пока недостаточный для мгновенной оплаты. Выберите подходящий способ оплаты:',
                )
            )

        message_text = '\n'.join(message_lines)

        if trial_notice:
            message_text = f'{trial_notice}\n\n{message_text}'

        methods_keyboard = _get_simple_subscription_payment_keyboard(db_user.language)
        keyboard_rows = []

        if can_pay_from_balance:
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_UI_PAY_BALANCE', '✅ Оплатить с баланса'),
                        callback_data='simple_subscription_pay_with_balance',
                    )
                ]
            )

        keyboard_rows.extend(methods_keyboard.inline_keyboard)

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

    # Устанавливаем соответствующее состояние
    if has_active_paid_subscription:
        await state.set_state(SubscriptionStates.waiting_for_simple_subscription_confirmation)
    else:
        await state.set_state(SubscriptionStates.waiting_for_simple_subscription_payment_method)
    await callback.answer()


async def _calculate_simple_subscription_price(
    db: AsyncSession,
    params: dict,
    *,
    user: User | None = None,
    resolved_squad_uuid: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Рассчитывает цену простой подписки."""

    resolved_uuids = [resolved_squad_uuid] if resolved_squad_uuid else None
    return await compute_simple_subscription_price(
        db,
        params,
        user=user,
        resolved_squad_uuids=resolved_uuids,
    )


def _get_simple_subscription_payment_keyboard(language: str) -> types.InlineKeyboardMarkup:
    """Создает клавиатуру с методами оплаты для простой подписки."""
    texts = get_texts(language)
    keyboard = []

    # Добавляем доступные методы оплаты
    if settings.TELEGRAM_STARS_ENABLED:
        keyboard.append(
            [types.InlineKeyboardButton(text='⭐ Telegram Stars', callback_data='simple_subscription_stars')]
        )

    if settings.is_yookassa_enabled():
        yookassa_methods = []
        if settings.YOOKASSA_SBP_ENABLED:
            yookassa_methods.append(
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_YOOKASSA_SBP', '🏦 YooKassa (СБП)'),
                    callback_data='simple_subscription_yookassa_sbp',
                )
            )
        yookassa_methods.append(
            types.InlineKeyboardButton(
                text=texts.t('SUBSCRIPTION_UI_YOOKASSA_CARD', '💳 YooKassa (Карта)'),
                callback_data='simple_subscription_yookassa',
            )
        )
        if yookassa_methods:
            keyboard.append(yookassa_methods)

    if settings.is_cryptobot_enabled():
        keyboard.append(
            [types.InlineKeyboardButton(text='🪙 CryptoBot', callback_data='simple_subscription_cryptobot')]
        )

    if settings.is_heleket_enabled():
        keyboard.append([types.InlineKeyboardButton(text='🪙 Heleket', callback_data='simple_subscription_heleket')])

    if settings.is_mulenpay_enabled():
        mulenpay_name = settings.get_mulenpay_display_name()
        keyboard.append(
            [types.InlineKeyboardButton(text=f'💳 {mulenpay_name}', callback_data='simple_subscription_mulenpay')]
        )

    if settings.is_pal24_enabled():
        keyboard.append([types.InlineKeyboardButton(text='💳 PayPalych', callback_data='simple_subscription_pal24')])

    if settings.is_anore_enabled():
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_TOPUP_PROVIDER', '💳 Пополнить баланс: {provider}').format(
                        provider=settings.get_anore_display_name()
                    ),
                    callback_data='topup_anore',
                )
            ]
        )

    if settings.is_wata_enabled():
        keyboard.append([types.InlineKeyboardButton(text='💳 WATA', callback_data='simple_subscription_wata')])

    # Кнопка назад
    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')])

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _get_simple_subscription_server_label(
    texts,
    subscription_params: dict[str, Any],
    resolved_squad_uuid: str | None = None,
) -> str:
    """Возвращает локализованное описание выбранного сервера."""

    if subscription_params.get('squad_uuid'):
        return texts.t('SIMPLE_SUBSCRIPTION_SERVER_SELECTED', 'Выбранный')

    if resolved_squad_uuid:
        return texts.t(
            'SIMPLE_SUBSCRIPTION_SERVER_ASSIGNED',
            'Назначен автоматически',
        )

    return texts.t('SIMPLE_SUBSCRIPTION_SERVER_ANY', 'Любой доступный')


async def _ensure_simple_subscription_squad_uuid(
    db: AsyncSession,
    state: FSMContext,
    subscription_params: dict[str, Any],
    *,
    user_id: int | None = None,
    state_data: dict[str, Any] | None = None,
) -> str | None:
    """Определяет UUID сквада для простой подписки."""

    explicit_uuid = subscription_params.get('squad_uuid')
    if explicit_uuid:
        return explicit_uuid

    if state_data is None:
        state_data = await state.get_data()

    resolved_uuid = state_data.get('resolved_squad_uuid')
    if resolved_uuid:
        return resolved_uuid

    try:
        from app.database.crud.server_squad import get_random_active_squad_uuid

        resolved_uuid = await get_random_active_squad_uuid(db)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error('SIMPLE_SUBSCRIPTION_RANDOM_SQUAD_ERROR', user_id=user_id, error=error)
        return None

    if resolved_uuid:
        await state.update_data(resolved_squad_uuid=resolved_uuid)
        logger.info('SIMPLE_SUBSCRIPTION_RANDOM_SQUAD_ASSIGNED', user_id=user_id, resolved_uuid=resolved_uuid)

    return resolved_uuid


@error_handler
async def handle_simple_subscription_pay_with_balance(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Обрабатывает оплату простой подписки с баланса."""
    if await _reject_classic_checkout_in_tariff_mode(callback, db_user.language):
        return
    texts = get_texts(db_user.language)

    data = await state.get_data()
    subscription_params = data.get('subscription_params', {})

    if not subscription_params:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_STALE_DATA', '❌ Данные подписки устарели. Пожалуйста, начните сначала.'),
            show_alert=True,
        )
        return

    # Проверяем, имеет ли пользователь активную платную подписку
    from app.database.crud.subscription import get_subscription_by_user_id

    current_subscription = await get_subscription_by_user_id(db, db_user.id)

    if current_subscription and not getattr(current_subscription, 'is_trial', False) and current_subscription.is_active:
        # У пользователя есть активная платная подписка - требуем подтверждение
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_UI_CONFIRM_ACTIVE',
                '⚠️ У вас уже есть активная платная подписка. Пожалуйста, подтвердите покупку.',
            ),
            show_alert=True,
        )
        return

    resolved_squad_uuid = await _ensure_simple_subscription_squad_uuid(
        db,
        state,
        subscription_params,
        user_id=db_user.id,
        state_data=data,
    )

    # Lock user BEFORE pricing to prevent TOCTOU
    from app.database.crud.user import lock_user_for_pricing, subtract_user_balance

    db_user = await lock_user_for_pricing(db, db_user.id)

    # Рассчитываем цену подписки (group discounts per-category)
    price_kopeks, price_breakdown = await _calculate_simple_subscription_price(
        db,
        subscription_params,
        user=db_user,
        resolved_squad_uuid=resolved_squad_uuid,
    )

    # PricingEngine already applies promo-offer discount inside calculate_classic_new_subscription_price.
    # Only determine whether to consume the offer (zero it out after use).
    from app.utils.promo_offer import get_user_active_promo_discount_percent

    consume_promo = get_user_active_promo_discount_percent(db_user) > 0

    total_required = price_kopeks
    logger.warning(
        'SIMPLE_SUBSCRIPTION_DEBUG_PAY_BALANCE',
        db_user_id=db_user.id,
        subscription_params=subscription_params['period_days'],
        price_breakdown=price_breakdown.get('base_price', 0),
        price_breakdown_2=price_breakdown.get('traffic_price', 0),
        price_breakdown_3=price_breakdown.get('devices_price', 0),
        price_breakdown_4=price_breakdown.get('servers_price', 0),
        price_breakdown_5=price_breakdown.get('total_discount', 0),
        total_required=total_required,
        getattr=getattr(db_user, 'balance_kopeks', 0),
    )

    # Проверяем баланс пользователя
    user_balance_kopeks = getattr(db_user, 'balance_kopeks', 0)

    if total_required > 0 and user_balance_kopeks < total_required:
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_UI_INSUFFICIENT_SUBSCRIPTION', '❌ Недостаточно средств на балансе для оплаты подписки'
            ),
            show_alert=True,
        )
        return

    from app.database.crud.subscription import get_subscription_by_user_id

    existing_subscription = await get_subscription_by_user_id(db, db_user.id)
    atomic_purchase = bool(existing_subscription and existing_subscription.is_trial) or getattr(
        existing_subscription, 'traffic_accounting_mode', None
    ) in ('managed', 'migrating')
    purchase_user_id = db_user.id
    try:
        # Списываем средства с баланса пользователя
        purchase_description = f'Оплата подписки на {subscription_params["period_days"]} дней'
        success = await subtract_user_balance(
            db,
            db_user,
            price_kopeks,
            purchase_description,
            consume_promo_offer=consume_promo,
            mark_as_paid_subscription=True,
            commit=not atomic_purchase,
        )

        if not success:
            await callback.answer(
                texts.t('SUBSCRIPTION_UI_BALANCE_DEBIT_FAILED', '❌ Ошибка списания средств с баланса'),
                show_alert=True,
            )
            return

        # Создаём транзакцию для учёта списания
        from app.database.crud.transaction import create_transaction
        from app.database.models import PaymentMethod, TransactionType

        transaction = await create_transaction(
            db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price_kopeks,
            description=purchase_description,
            payment_method=PaymentMethod.BALANCE,
            commit=not atomic_purchase,
        )

        # Проверяем, есть ли у пользователя уже подписка
        from app.database.crud.subscription import extend_subscription

        # Reuse the pre-debit snapshot: the status-updating getter may commit
        # when a trial expires, exposing a debit before its conversion.

        reset_traffic = settings.RESET_TRAFFIC_ON_PAYMENT or bool(
            existing_subscription and existing_subscription.is_trial
        )
        if existing_subscription:
            # Если подписка уже существует (платная или тестовая), продлеваем её
            # Сохраняем информацию о текущей подписке, особенно является ли она пробной
            was_trial = getattr(existing_subscription, 'is_trial', False)

            subscription = await extend_subscription(
                db=db,
                subscription=existing_subscription,
                days=subscription_params['period_days'],
                paid_trial_conversion=was_trial,
                traffic_limit_gb=subscription_params['traffic_limit_gb'],
                device_limit=subscription_params['device_limit'],
                connected_squads=[resolved_squad_uuid] if resolved_squad_uuid else None,
                paid_traffic_reset=False,
                reset_used_traffic=reset_traffic,
                commit=not atomic_purchase,
            )

            # Если текущая подписка была пробной, и мы обновляем её
            # нужно изменить статус подписки
            if was_trial:
                from app.database.models import SubscriptionStatus

                # Переводим подписку из пробной в активную платную
                subscription.status = SubscriptionStatus.ACTIVE.value
                subscription.is_trial = False

            await db.commit()
            if atomic_purchase:
                from app.database.crud.transaction import emit_transaction_side_effects

                await emit_transaction_side_effects(
                    db,
                    transaction,
                    amount_kopeks=price_kopeks,
                    user_id=db_user.id,
                    type=TransactionType.SUBSCRIPTION_PAYMENT,
                    payment_method=PaymentMethod.BALANCE,
                    description=purchase_description,
                )
            await db.refresh(subscription)
        else:
            # Если подписки нет, создаём новую
            from app.database.crud.subscription import create_paid_subscription

            subscription = await create_paid_subscription(
                db=db,
                user_id=db_user.id,
                duration_days=subscription_params['period_days'],
                traffic_limit_gb=subscription_params['traffic_limit_gb'],
                device_limit=subscription_params['device_limit'],
                connected_squads=[resolved_squad_uuid] if resolved_squad_uuid else [],
                update_server_counters=True,
            )

        if not subscription:
            # Возвращаем средства на баланс в случае ошибки
            from app.services.payment_service import add_user_balance

            await add_user_balance(
                db,
                db_user.id,
                price_kopeks,
                f'Возврат средств за неудавшуюся подписку на {subscription_params["period_days"]} дней',
            )
            await callback.answer(
                texts.t(
                    'SUBSCRIPTION_UI_CREATION_REFUNDED', '❌ Ошибка создания подписки. Средства возвращены на баланс.'
                ),
                show_alert=True,
            )
            return

        # Обновляем баланс пользователя
        await db.refresh(db_user)

        # Обновляем или создаём ссылку подписки в RemnaWave
        try:
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            remnawave_user = await subscription_service.create_remnawave_user(
                db, subscription, reset_traffic=reset_traffic
            )
            if remnawave_user:
                await db.refresh(subscription)
        except Exception as sync_error:
            logger.error(
                'Ошибка синхронизации подписки с RemnaWave для пользователя',
                db_user_id=db_user.id,
                sync_error=sync_error,
                exc_info=True,
            )
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            if hasattr(subscription, 'id') and hasattr(subscription, 'user_id'):
                remnawave_retry_queue.enqueue(
                    subscription_id=subscription.id,
                    user_id=subscription.user_id,
                    action='create',
                )

        # Отправляем уведомление об успешной покупке
        server_label = _get_simple_subscription_server_label(
            texts,
            subscription_params,
            resolved_squad_uuid,
        )
        show_devices = settings.is_devices_selection_enabled()

        success_lines = [
            texts.t('SUBSCRIPTION_UI_ACTIVATED_TITLE', '✅ <b>Подписка успешно активирована!</b>'),
            '',
            texts.t('SUBSCRIPTION_UI_PERIOD_DAYS', '📅 Период: {days} дней').format(
                days=subscription_params['period_days']
            ),
        ]

        if show_devices:
            success_lines.append(
                texts.t('SUBSCRIPTION_UI_DEVICES_VALUE', '📱 Устройства: {devices}').format(
                    devices=subscription_params['device_limit']
                )
            )

        success_traffic_gb = subscription_params['traffic_limit_gb']
        success_traffic_label = (
            texts.t('SUBSCRIPTION_UI_UNLIMITED', 'Безлимит')
            if success_traffic_gb == 0
            else texts.t('SUBSCRIPTION_UI_GB_VALUE', '{amount} ГБ').format(amount=success_traffic_gb)
        )

        success_lines.extend(
            [
                texts.t('SUBSCRIPTION_UI_TRAFFIC_VALUE', '📊 Трафик: {traffic}').format(traffic=success_traffic_label),
                texts.t('SUBSCRIPTION_UI_SERVER_VALUE', '🌍 Сервер: {server}').format(server=server_label),
                '',
                texts.t('SUBSCRIPTION_UI_DEBIT_VALUE', '💰 Списано с баланса: {amount}').format(
                    amount=settings.format_price(price_kopeks)
                ),
                texts.t('SUBSCRIPTION_UI_BALANCE_VALUE', '💳 Ваш баланс: {amount}').format(
                    amount=settings.format_price(db_user.balance_kopeks)
                ),
                '',
                texts.t('SUBSCRIPTION_UI_CONNECT_SECTION', "🔗 Для подключения перейдите в раздел 'Подключиться'"),
            ]
        )

        success_message = '\n'.join(success_lines)

        connect_mode = settings.CONNECT_BUTTON_MODE
        subscription_link = get_display_subscription_link(subscription)
        connect_button_text = texts.t('CONNECT_BUTTON', '🔗 Подключиться')

        def _fallback_connect_button() -> types.InlineKeyboardButton:
            return types.InlineKeyboardButton(
                text=connect_button_text,
                callback_data='subscription_connect',
            )

        if connect_mode == 'miniapp_subscription':
            if subscription_link:
                connect_row = [
                    types.InlineKeyboardButton(
                        text=connect_button_text,
                        web_app=types.WebAppInfo(url=subscription_link),
                    )
                ]
            else:
                connect_row = [_fallback_connect_button()]
        elif connect_mode == 'miniapp_custom':
            custom_url = settings.MINIAPP_CUSTOM_URL
            if custom_url:
                connect_row = [
                    types.InlineKeyboardButton(
                        text=connect_button_text,
                        web_app=types.WebAppInfo(url=custom_url),
                    )
                ]
            else:
                connect_row = [_fallback_connect_button()]
        elif connect_mode == 'link':
            if subscription_link:
                connect_row = [
                    types.InlineKeyboardButton(
                        text=connect_button_text,
                        url=subscription_link,
                    )
                ]
            else:
                connect_row = [_fallback_connect_button()]
        elif connect_mode == 'happ_cryptolink':
            if subscription_link:
                connect_row = [
                    types.InlineKeyboardButton(
                        text=connect_button_text,
                        callback_data='open_subscription_link',
                    )
                ]
            else:
                connect_row = [_fallback_connect_button()]
        else:
            connect_row = [_fallback_connect_button()]

        keyboard_rows = [connect_row]

        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            keyboard_rows.append(happ_row)

        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_MAIN_MENU', '🏠 Главное меню'),
                    callback_data='back_to_menu',
                )
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.message.edit_text(success_message, reply_markup=keyboard, parse_mode='HTML')

        # Отправляем уведомление админам
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_purchase_notification(
                db,
                db_user,
                subscription,
                transaction,
                subscription_params['period_days'],
                False,  # was_trial_conversion
                amount_kopeks=price_kopeks,
                purchase_type='renewal' if existing_subscription else 'first_purchase',
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления админам о покупке', error=e)

        await state.clear()
        await callback.answer()

        logger.info(
            'Пользователь успешно купил подписку с баланса',
            telegram_id=db_user.telegram_id,
            price_kopeks=price_kopeks / 100,
        )

    except asyncio.CancelledError:
        if atomic_purchase:
            await db.rollback()
        raise
    except Exception as error:
        if atomic_purchase:
            await db.rollback()
        logger.error(
            'Ошибка оплаты простой подписки с баланса для пользователя',
            db_user_id=purchase_user_id,
            error=error,
            exc_info=True,
        )
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_UI_PAYMENT_FAILED_SUPPORT',
                '❌ Ошибка оплаты подписки. Попробуйте позже или обратитесь в поддержку.',
            ),
            show_alert=True,
        )
        await state.clear()


@error_handler
async def handle_simple_subscription_pay_with_balance_disabled(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Показывает уведомление, если баланса недостаточно для прямой оплаты."""
    await callback.answer(
        get_texts(getattr(db_user, 'language', None)).t(
            'SUBSCRIPTION_UI_INSUFFICIENT_TOPUP',
            '❌ Недостаточно средств на балансе. Пополните баланс или выберите другой способ оплаты.',
        ),
        show_alert=True,
    )


@error_handler
async def handle_simple_subscription_other_payment_methods(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Обрабатывает выбор других способов оплаты."""
    texts = get_texts(db_user.language)

    data = await state.get_data()
    subscription_params = data.get('subscription_params', {})

    if not subscription_params:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_STALE_DATA', '❌ Данные подписки устарели. Пожалуйста, начните сначала.'),
            show_alert=True,
        )
        return

    resolved_squad_uuid = await _ensure_simple_subscription_squad_uuid(
        db,
        state,
        subscription_params,
        user_id=db_user.id,
        state_data=data,
    )

    # Рассчитываем цену подписки
    price_kopeks, price_breakdown = await _calculate_simple_subscription_price(
        db,
        subscription_params,
        user=db_user,
        resolved_squad_uuid=resolved_squad_uuid,
    )

    user_balance_kopeks = getattr(db_user, 'balance_kopeks', 0)
    can_pay_from_balance = user_balance_kopeks >= price_kopeks
    logger.warning(
        'SIMPLE_SUBSCRIPTION_DEBUG_METHODS',
        db_user_id=db_user.id,
        user_balance_kopeks=user_balance_kopeks,
        price_breakdown=price_breakdown.get('base_price', 0),
        price_breakdown_2=price_breakdown.get('traffic_price', 0),
        price_breakdown_3=price_breakdown.get('devices_price', 0),
        price_breakdown_4=price_breakdown.get('servers_price', 0),
        price_breakdown_5=price_breakdown.get('total_discount', 0),
        price_kopeks=price_kopeks,
        can_pay_from_balance=can_pay_from_balance,
    )

    # Отображаем доступные методы оплаты
    server_label = _get_simple_subscription_server_label(
        texts,
        subscription_params,
        resolved_squad_uuid,
    )
    show_devices = settings.is_devices_selection_enabled()

    message_lines = [
        texts.t('SUBSCRIPTION_UI_PAYMENT_TITLE', '💳 <b>Оплата подписки</b>'),
        '',
        texts.t('SUBSCRIPTION_UI_PERIOD_DAYS', '📅 Период: {days} дней').format(
            days=subscription_params['period_days']
        ),
    ]

    if show_devices:
        message_lines.append(
            texts.t('SUBSCRIPTION_UI_DEVICES_VALUE', '📱 Устройства: {devices}').format(
                devices=subscription_params['device_limit']
            )
        )

    payment_traffic_gb = subscription_params['traffic_limit_gb']
    payment_traffic_label = (
        texts.t('SUBSCRIPTION_UI_UNLIMITED', 'Безлимит')
        if payment_traffic_gb == 0
        else texts.t('SUBSCRIPTION_UI_GB_VALUE', '{amount} ГБ').format(amount=payment_traffic_gb)
    )

    message_lines.extend(
        [
            texts.t('SUBSCRIPTION_UI_TRAFFIC_VALUE', '📊 Трафик: {traffic}').format(traffic=payment_traffic_label),
            texts.t('SUBSCRIPTION_UI_SERVER_VALUE', '🌍 Сервер: {server}').format(server=server_label),
            '',
            texts.t('SUBSCRIPTION_UI_COST_VALUE', '💰 Стоимость: {amount}').format(
                amount=settings.format_price(price_kopeks)
            ),
            '',
            (
                texts.t(
                    'SUBSCRIPTION_UI_BALANCE_OR_METHOD_COLON',
                    'Вы можете оплатить подписку с баланса или выбрать другой способ оплаты:',
                )
                if can_pay_from_balance
                else texts.t('SUBSCRIPTION_UI_SELECT_METHOD', 'Выберите подходящий способ оплаты:')
            ),
        ]
    )

    message_text = '\n'.join(message_lines)

    base_keyboard = _get_simple_subscription_payment_keyboard(db_user.language)
    keyboard_rows = []

    if can_pay_from_balance:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_PAY_BALANCE', '✅ Оплатить с баланса'),
                    callback_data='simple_subscription_pay_with_balance',
                )
            ]
        )

    keyboard_rows.extend(base_keyboard.inline_keyboard)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

    await callback.answer()


@error_handler
async def handle_simple_subscription_payment_method(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Обрабатывает выбор метода оплаты для простой подписки."""
    if await _reject_classic_checkout_in_tariff_mode(callback, db_user.language):
        return
    texts = get_texts(db_user.language)

    data = await state.get_data()
    subscription_params = data.get('subscription_params', {})

    if not subscription_params:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_STALE_DATA', '❌ Данные подписки устарели. Пожалуйста, начните сначала.'),
            show_alert=True,
        )
        return

    # Проверяем, имеет ли пользователь активную платную подписку
    from app.database.crud.subscription import get_subscription_by_user_id

    current_subscription = await get_subscription_by_user_id(db, db_user.id)

    if current_subscription and not getattr(current_subscription, 'is_trial', False) and current_subscription.is_active:
        # У пользователя есть активная платная подписка - показываем сообщение
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_UI_CONFIRM_ACTIVE_MENU',
                '⚠️ У вас уже есть активная платная подписка. Пожалуйста, подтвердите покупку через главное меню.',
            ),
            show_alert=True,
        )
        return

    payment_method = callback.data.replace('simple_subscription_', '')

    try:
        payment_service = PaymentService(callback.bot)
        purchase_service = SubscriptionPurchaseService()

        resolved_squad_uuid = await _ensure_simple_subscription_squad_uuid(
            db,
            state,
            subscription_params,
            user_id=db_user.id,
            state_data=data,
        )

        # Рассчитываем цену подписки (group discounts per-category)
        price_kopeks, _ = await _calculate_simple_subscription_price(
            db,
            subscription_params,
            user=db_user,
            resolved_squad_uuid=resolved_squad_uuid,
        )

        # Apply promo-offer discount on top of group discounts (consistent with balance-pay path)
        from app.services.pricing_engine import PricingEngine
        from app.utils.promo_offer import get_user_active_promo_discount_percent

        offer_pct = get_user_active_promo_discount_percent(db_user)
        if offer_pct > 0:
            price_kopeks = PricingEngine.apply_discount(price_kopeks, offer_pct)

        if payment_method == 'stars':
            # Оплата через Telegram Stars
            order = await purchase_service.create_subscription_order(
                db=db,
                user_id=db_user.id,
                period_days=subscription_params['period_days'],
                device_limit=subscription_params['device_limit'],
                traffic_limit_gb=subscription_params['traffic_limit_gb'],
                squad_uuid=resolved_squad_uuid,
                payment_method='telegram_stars',
                total_price_kopeks=price_kopeks,
            )

            if not order:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_ORDER_PREPARATION_FAILED', '❌ Не удалось подготовить заказ. Попробуйте позже.'
                    ),
                    show_alert=True,
                )
                return

            stars_count = settings.rubles_to_stars(settings.kopeks_to_rubles(price_kopeks))

            stars_traffic_gb = subscription_params['traffic_limit_gb']
            stars_traffic_label = (
                texts.t('SUBSCRIPTION_UI_UNLIMITED', 'Безлимит')
                if stars_traffic_gb == 0
                else texts.t('SUBSCRIPTION_UI_GB_VALUE', '{amount} ГБ').format(amount=stars_traffic_gb)
            )

            await callback.bot.send_invoice(
                chat_id=callback.from_user.id,
                title=texts.t('SUBSCRIPTION_UI_INVOICE_TITLE', 'Подписка на {days} дней').format(
                    days=subscription_params['period_days']
                ),
                description=(
                    texts.t(
                        'SUBSCRIPTION_UI_INVOICE_DESCRIPTION',
                        'Простая покупка подписки\nПериод: {days} дней\nУстройства: {devices}\nТрафик: {traffic}',
                    ).format(
                        days=subscription_params['period_days'],
                        devices=subscription_params['device_limit'],
                        traffic=stars_traffic_label,
                    )
                ),
                payload=(f'simple_sub_{db_user.id}_{order.id}_{subscription_params["period_days"]}'),
                provider_token='',  # Пустой токен для Telegram Stars
                currency='XTR',  # Telegram Stars
                prices=[types.LabeledPrice(label=texts.t('SUBSCRIPTION_UI_NAME', 'Подписка'), amount=stars_count)],
            )

            await state.clear()
            await callback.answer()

        elif payment_method in ['yookassa', 'yookassa_sbp']:
            # Оплата через YooKassa
            if not settings.is_yookassa_enabled():
                await callback.answer(
                    texts.t('SUBSCRIPTION_UI_YOOKASSA_UNAVAILABLE', '❌ Оплата через YooKassa временно недоступна'),
                    show_alert=True,
                )
                return

            if payment_method == 'yookassa_sbp' and not settings.YOOKASSA_SBP_ENABLED:
                await callback.answer(
                    texts.t('SUBSCRIPTION_UI_SBP_UNAVAILABLE', '❌ Оплата через СБП временно недоступна'),
                    show_alert=True,
                )
                return

            # Создаем заказ на подписку
            order = await purchase_service.create_subscription_order(
                db=db,
                user_id=db_user.id,
                period_days=subscription_params['period_days'],
                device_limit=subscription_params['device_limit'],
                traffic_limit_gb=subscription_params['traffic_limit_gb'],
                squad_uuid=resolved_squad_uuid,
                payment_method='yookassa_sbp' if payment_method == 'yookassa_sbp' else 'yookassa',
                total_price_kopeks=price_kopeks,
            )

            if not order:
                await callback.answer(
                    texts.t('SUBSCRIPTION_UI_ORDER_CREATION_FAILED', '❌ Ошибка создания заказа'),
                    show_alert=True,
                )
                return

            # Создаем платеж через YooKassa
            if payment_method == 'yookassa_sbp':
                payment_result = await payment_service.create_yookassa_sbp_payment(
                    db=db,
                    user_id=db_user.id,
                    amount_kopeks=price_kopeks,
                    description=f'Оплата подписки на {subscription_params["period_days"]} дней',
                    receipt_email=db_user.email if hasattr(db_user, 'email') and db_user.email else None,
                    receipt_phone=db_user.phone if hasattr(db_user, 'phone') and db_user.phone else None,
                    metadata={
                        'user_telegram_id': str(db_user.telegram_id),
                        'user_username': db_user.username or '',
                        'order_id': str(order.id),
                        'subscription_id': str(order.id),
                        'subscription_period': str(subscription_params['period_days']),
                        'payment_purpose': 'simple_subscription_purchase',
                    },
                )
            else:
                payment_result = await payment_service.create_yookassa_payment(
                    db=db,
                    user_id=db_user.id,
                    amount_kopeks=price_kopeks,
                    description=f'Оплата подписки на {subscription_params["period_days"]} дней',
                    receipt_email=db_user.email if hasattr(db_user, 'email') and db_user.email else None,
                    receipt_phone=db_user.phone if hasattr(db_user, 'phone') and db_user.phone else None,
                    metadata={
                        'user_telegram_id': str(db_user.telegram_id),
                        'user_username': db_user.username or '',
                        'order_id': str(order.id),
                        'subscription_id': str(order.id),
                        'subscription_period': str(subscription_params['period_days']),
                        'payment_purpose': 'simple_subscription_purchase',
                    },
                )

            if not payment_result:
                await callback.answer(
                    texts.t('SUBSCRIPTION_UI_PAYMENT_CREATION_FAILED', '❌ Ошибка создания платежа'),
                    show_alert=True,
                )
                return

            # Отправляем QR-код и/или ссылку для оплаты
            confirmation_url = payment_result.get('confirmation_url')
            qr_confirmation_data = payment_result.get('qr_confirmation_data')

            if not confirmation_url and not qr_confirmation_data:
                await callback.answer(
                    texts.t('SUBSCRIPTION_UI_PAYMENT_DATA_FAILED', '❌ Ошибка получения данных для оплаты'),
                    show_alert=True,
                )
                return

            # Подготовим QR-код для вставки в основное сообщение
            qr_photo = None
            if qr_confirmation_data or confirmation_url:
                try:
                    # Импортируем необходимые модули для генерации QR-кода
                    from io import BytesIO

                    import qrcode
                    from aiogram.types import BufferedInputFile

                    # Используем qr_confirmation_data если доступно, иначе confirmation_url
                    qr_data = qr_confirmation_data or confirmation_url

                    # Создаем QR-код из полученных данных
                    qr = qrcode.QRCode(version=1, box_size=10, border=5)
                    qr.add_data(qr_data)
                    qr.make(fit=True)

                    img = qr.make_image(fill_color='black', back_color='white')

                    # Сохраняем изображение в байты
                    img_bytes = BytesIO()
                    img.save(img_bytes, format='PNG')
                    img_bytes.seek(0)

                    qr_photo = BufferedInputFile(img_bytes.getvalue(), filename='qrcode.png')
                except ImportError:
                    logger.warning('qrcode библиотека не установлена, QR-код не будет сгенерирован')
                except Exception as e:
                    logger.error('Ошибка генерации QR-кода', error=e)

            # Создаем клавиатуру с кнопками для оплаты по ссылке и проверки статуса
            keyboard_buttons = []

            # Добавляем кнопку оплаты, если доступна ссылка
            if confirmation_url:
                keyboard_buttons.append(
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('SUBSCRIPTION_UI_OPEN_PAYMENT', '🔗 Перейти к оплате'),
                            url=confirmation_url,
                        )
                    ]
                )
            else:
                # Если ссылка недоступна, предлагаем оплатить через ID платежа в приложении банка
                keyboard_buttons.append(
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('SUBSCRIPTION_UI_BANK_APP_PAYMENT', '📱 Оплатить в приложении банка'),
                            callback_data='temp_disabled',
                        )
                    ]
                )

            # Добавляем общие кнопки
            keyboard_buttons.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_UI_CHECK_STATUS', '📊 Проверить статус'),
                        callback_data=f'check_yookassa_{payment_result["local_payment_id"]}',
                    )
                ]
            )
            keyboard_buttons.append(
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')]
            )

            keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            # Подготавливаем текст сообщения
            show_devices = settings.is_devices_selection_enabled()

            message_lines = [
                texts.t('SUBSCRIPTION_UI_YOOKASSA_TITLE', '💳 <b>Оплата подписки через YooKassa</b>'),
                '',
                texts.t('SUBSCRIPTION_UI_PERIOD_DAYS', '📅 Период: {days} дней').format(
                    days=subscription_params['period_days']
                ),
            ]

            if show_devices:
                message_lines.append(
                    texts.t('SUBSCRIPTION_UI_DEVICES_VALUE', '📱 Устройства: {devices}').format(
                        devices=subscription_params['device_limit']
                    )
                )

            yookassa_traffic_gb = subscription_params['traffic_limit_gb']
            yookassa_traffic_label = (
                texts.t('SUBSCRIPTION_UI_UNLIMITED', 'Безлимит')
                if yookassa_traffic_gb == 0
                else texts.t('SUBSCRIPTION_UI_GB_VALUE', '{amount} ГБ').format(amount=yookassa_traffic_gb)
            )

            message_lines.extend(
                [
                    texts.t('SUBSCRIPTION_UI_TRAFFIC_VALUE', '📊 Трафик: {traffic}').format(
                        traffic=yookassa_traffic_label
                    ),
                    texts.t('SUBSCRIPTION_UI_AMOUNT_VALUE', '💰 Сумма: {amount}').format(
                        amount=settings.format_price(price_kopeks)
                    ),
                    texts.t('SUBSCRIPTION_UI_PAYMENT_ID_SHORT', '🆔 ID платежа: {payment_id}...').format(
                        payment_id=payment_result['yookassa_payment_id'][:8]
                    ),
                    '',
                ]
            )

            message_text = '\n'.join(message_lines)

            # Добавляем инструкции в зависимости от доступных способов оплаты
            if not confirmation_url:
                message_text += texts.t(
                    'SUBSCRIPTION_UI_BANK_INSTRUCTIONS',
                    '📱 <b>Инструкция по оплате:</b>\n1. Откройте приложение вашего банка\n2. Найдите функцию оплаты по реквизитам или перевод по СБП\n3. Введите ID платежа: <code>{payment_id}</code>\n4. Подтвердите платеж в приложении банка\n5. Деньги поступят на баланс автоматически\n\n',
                ).format(payment_id=payment_result['yookassa_payment_id'])

            message_text += texts.t(
                'SUBSCRIPTION_UI_YOOKASSA_FOOTER',
                '🔒 Оплата происходит через защищенную систему YooKassa\n✅ Принимаем карты: Visa, MasterCard, МИР\n\n❓ Если возникнут проблемы, обратитесь в {support}',
            ).format(support=settings.get_support_contact_display_html())

            # Отправляем сообщение с инструкциями и клавиатурой
            # Если есть QR-код, отправляем его как медиа-сообщение
            if qr_photo:
                # Используем метод отправки фото с описанием
                await callback.message.edit_media(
                    media=types.InputMediaPhoto(media=qr_photo, caption=message_text, parse_mode='HTML'),
                    reply_markup=keyboard,
                )
            else:
                # Если QR-код недоступен, отправляем обычное текстовое сообщение
                await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

            await state.clear()
            await callback.answer()

        elif payment_method == 'cryptobot':
            # Оплата через CryptoBot
            if not settings.is_cryptobot_enabled():
                await callback.answer(
                    texts.t('SUBSCRIPTION_UI_CRYPTOBOT_UNAVAILABLE', '❌ Оплата через CryptoBot временно недоступна'),
                    show_alert=True,
                )
                return

            amount_rubles = price_kopeks / 100
            if amount_rubles < 100 or amount_rubles > 100000:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_CRYPTOBOT_RUB_RANGE',
                        '❌ Сумма должна быть от 100 до 100 000 ₽ для оплаты через CryptoBot',
                    ),
                    show_alert=True,
                )
                return

            try:
                from app.utils.currency_converter import currency_converter

                usd_rate = await currency_converter.get_usd_to_rub_rate()
            except Exception as rate_error:
                logger.warning('Не удалось получить курс USD', rate_error=rate_error)
                usd_rate = 95.0

            amount_usd = round(amount_rubles / usd_rate, 2)
            if amount_usd < 1:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_CRYPTOBOT_MIN',
                        '❌ Минимальная сумма для оплаты через CryptoBot — примерно 1 USD',
                    ),
                    show_alert=True,
                )
                return
            if amount_usd > 1000:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_CRYPTOBOT_MAX', '❌ Максимальная сумма для оплаты через CryptoBot — 1000 USD'
                    ),
                    show_alert=True,
                )
                return

            payment_service = PaymentService(callback.bot)
            crypto_result = await payment_service.create_cryptobot_payment(
                db=db,
                user_id=db_user.id,
                amount_usd=amount_usd,
                asset=settings.CRYPTOBOT_DEFAULT_ASSET,
                description=settings.get_subscription_payment_description(
                    subscription_params['period_days'],
                    price_kopeks,
                ),
                payload=f'simple_subscription_{db_user.id}_{price_kopeks}',
            )

            if not crypto_result:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_CRYPTOBOT_CREATION_FAILED',
                        '❌ Ошибка создания платежа через CryptoBot. Попробуйте позже или обратитесь в поддержку.',
                    ),
                    show_alert=True,
                )
                return

            payment_url = (
                crypto_result.get('mini_app_invoice_url')
                or crypto_result.get('bot_invoice_url')
                or crypto_result.get('web_app_invoice_url')
            )

            if not payment_url:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_PAYMENT_LINK_FAILED',
                        '❌ Не удалось получить ссылку для оплаты. Обратитесь в поддержку.',
                    ),
                    show_alert=True,
                )
                return

            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('SUBSCRIPTION_UI_PAY_CRYPTOBOT', '🪙 Оплатить через CryptoBot'),
                            url=payment_url,
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                            callback_data=f'check_simple_cryptobot_{crypto_result["local_payment_id"]}',
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
                ]
            )

            message_text = texts.t(
                'SUBSCRIPTION_UI_CRYPTOBOT_DETAILS',
                "🪙 <b>Оплата через CryptoBot</b>\n\n💰 Сумма к оплате: {rubles:.0f} ₽\n💵 В долларах: {dollars:.2f} USD\n🪙 Актив: {asset}\n💱 Курс: 1 USD ≈ {rate:.2f} ₽\n🆔 ID платежа: {invoice_id}...\n\n📱 <b>Инструкция:</b>\n1. Нажмите кнопку 'Оплатить через CryptoBot'\n2. Выберите актив и следуйте подсказкам\n3. Подтвердите перевод\n4. Средства зачислятся автоматически\n\n❓ Если возникнут проблемы, обратитесь в {support}",
            ).format(
                rubles=amount_rubles,
                dollars=amount_usd,
                asset=crypto_result['asset'],
                rate=usd_rate,
                invoice_id=crypto_result['invoice_id'][:8],
                support=settings.get_support_contact_display_html(),
            )

            await callback.message.edit_text(
                message_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )

            await state.clear()
            await callback.answer()
            return

        elif payment_method == 'heleket':
            if not settings.is_heleket_enabled():
                await callback.answer(
                    texts.t('SUBSCRIPTION_UI_HELEKET_UNAVAILABLE', '❌ Оплата через Heleket временно недоступна'),
                    show_alert=True,
                )
                return

            amount_rubles = price_kopeks / 100
            if amount_rubles < 100 or amount_rubles > 100000:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_HELEKET_RUB_RANGE',
                        '❌ Сумма должна быть от 100 до 100 000 ₽ для оплаты через Heleket',
                    ),
                    show_alert=True,
                )
                return

            heleket_result = await payment_service.create_heleket_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=price_kopeks,
                description=settings.get_subscription_payment_description(
                    subscription_params['period_days'],
                    price_kopeks,
                ),
                language=db_user.language,
            )

            if not heleket_result:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_HELEKET_CREATION_FAILED',
                        '❌ Ошибка создания платежа Heleket. Попробуйте позже или обратитесь в поддержку.',
                    ),
                    show_alert=True,
                )
                return

            payment_url = heleket_result.get('payment_url')
            if not payment_url:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_HELEKET_LINK_FAILED',
                        '❌ Не удалось получить ссылку для оплаты Heleket. Обратитесь в поддержку.',
                    ),
                    show_alert=True,
                )
                return

            local_payment_id = heleket_result.get('local_payment_id')
            payer_amount = heleket_result.get('payer_amount')
            payer_currency = heleket_result.get('payer_currency')
            discount_percent = heleket_result.get('discount_percent')

            markup_percent = None
            if discount_percent is not None:
                try:
                    markup_percent = -int(discount_percent)
                except (TypeError, ValueError):
                    markup_percent = None

            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('SUBSCRIPTION_UI_PAY_HELEKET', '🪙 Оплатить через Heleket'),
                            url=payment_url,
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                            callback_data=f'check_simple_heleket_{local_payment_id}',
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
                ]
            )

            message_lines = [
                texts.t('SUBSCRIPTION_UI_HELEKET_TITLE', '🪙 <b>Оплата через Heleket</b>'),
                '',
                texts.t('SUBSCRIPTION_UI_AMOUNT_VALUE', '💰 Сумма: {amount}').format(
                    amount=settings.format_price(price_kopeks)
                ),
            ]

            if payer_amount and payer_currency:
                message_lines.append(
                    texts.t('SUBSCRIPTION_UI_CRYPTO_DUE', '🪙 К оплате: {amount} {currency}').format(
                        amount=payer_amount, currency=payer_currency
                    )
                )
                try:
                    payer_amount_float = float(payer_amount)
                    if payer_amount_float > 0:
                        rub_per_currency = amount_rubles / payer_amount_float
                        message_lines.append(
                            texts.t('SUBSCRIPTION_UI_EXCHANGE_RATE', '💱 Курс: 1 {currency} ≈ {rate:.2f} ₽').format(
                                currency=payer_currency, rate=rub_per_currency
                            )
                        )
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

            if markup_percent:
                sign = '+' if markup_percent > 0 else ''
                message_lines.append(
                    texts.t('SUBSCRIPTION_UI_MARKUP', '📈 Наценка: {sign}{percent}%').format(
                        sign=sign, percent=markup_percent
                    )
                )

            message_lines.extend(
                [
                    '',
                    texts.t('SUBSCRIPTION_UI_INSTRUCTIONS_TITLE', '📱 <b>Инструкция:</b>'),
                    texts.t('SUBSCRIPTION_UI_HELEKET_STEP1', "1. Нажмите кнопку 'Оплатить через Heleket'"),
                    texts.t('SUBSCRIPTION_UI_HELEKET_STEP2', '2. Следуйте подсказкам на странице оплаты'),
                    texts.t('SUBSCRIPTION_UI_HELEKET_STEP3', '3. Подтвердите перевод'),
                    texts.t('SUBSCRIPTION_UI_HELEKET_STEP4', '4. Средства зачислятся автоматически'),
                    '',
                    texts.t(
                        'SUBSCRIPTION_UI_CONTACT_SUPPORT', '❓ Если возникнут проблемы, обратитесь в {support}'
                    ).format(support=settings.get_support_contact_display_html()),
                ]
            )

            await callback.message.edit_text(
                '\n'.join(message_lines),
                reply_markup=keyboard,
                parse_mode='HTML',
            )

            await state.clear()
            await callback.answer()
            return

        elif payment_method == 'mulenpay':
            # Оплата через MulenPay
            mulenpay_name = settings.get_mulenpay_display_name()
            if not settings.is_mulenpay_enabled():
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_PROVIDER_UNAVAILABLE', '❌ Оплата через {provider} временно недоступна'
                    ).format(provider=mulenpay_name),
                    show_alert=True,
                )
                return

            if price_kopeks < settings.MULENPAY_MIN_AMOUNT_KOPEKS or price_kopeks > settings.MULENPAY_MAX_AMOUNT_KOPEKS:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_MULENPAY_RANGE',
                        '❌ Сумма для Mulen Pay должна быть в пределах от {minimum} до {maximum}',
                    ).format(
                        minimum=settings.format_price(settings.MULENPAY_MIN_AMOUNT_KOPEKS),
                        maximum=settings.format_price(settings.MULENPAY_MAX_AMOUNT_KOPEKS),
                    ),
                    show_alert=True,
                )
                return

            payment_service = PaymentService(callback.bot)
            mulen_result = await payment_service.create_mulenpay_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=price_kopeks,
                description=settings.get_subscription_payment_description(
                    subscription_params['period_days'],
                    price_kopeks,
                ),
                language=db_user.language,
            )

            if not mulen_result or not mulen_result.get('payment_url'):
                await callback.answer(
                    texts.t(
                        'MULENPAY_PAYMENT_ERROR',
                        '❌ Ошибка создания платежа Mulen Pay. Попробуйте позже или обратитесь в поддержку.',
                    ),
                    show_alert=True,
                )
                return

            payment_url = mulen_result['payment_url']
            local_payment_id = mulen_result.get('local_payment_id')
            payment_id_display = mulen_result.get('mulen_payment_id') or local_payment_id

            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('MULENPAY_PAY_BUTTON', '💳 Оплатить через Mulen Pay'),
                            url=payment_url,
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                            callback_data=f'check_simple_mulenpay_{local_payment_id}',
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
                ]
            )

            message_template = texts.t(
                'MULENPAY_PAYMENT_INSTRUCTIONS',
                (
                    '💳 <b>Оплата через {mulenpay_name_html}</b>\n\n'
                    '💰 Сумма: {amount}\n'
                    '🆔 ID платежа: {payment_id}\n\n'
                    '📱 <b>Инструкция:</b>\n'
                    "1. Нажмите кнопку 'Оплатить через {mulenpay_name}'\n"
                    '2. Следуйте подсказкам платежной системы\n'
                    '3. Подтвердите перевод\n'
                    '4. Средства зачислятся автоматически\n\n'
                    '❓ Если возникнут проблемы, обратитесь в {support}'
                ),
            )

            await callback.message.edit_text(
                message_template.format(
                    mulenpay_name=mulenpay_name,
                    mulenpay_name_html=settings.get_mulenpay_display_name_html(),
                    amount=settings.format_price(price_kopeks),
                    payment_id=payment_id_display,
                    support=settings.get_support_contact_display_html(),
                ),
                reply_markup=keyboard,
                parse_mode='HTML',
            )

            await state.clear()
            await callback.answer()
            return

        elif payment_method == 'pal24':
            # Оплата через PayPalych
            if not settings.is_pal24_enabled():
                await callback.answer(
                    texts.t('SUBSCRIPTION_UI_PAL24_UNAVAILABLE', '❌ Оплата через PayPalych временно недоступна'),
                    show_alert=True,
                )
                return

            payment_service = PaymentService(callback.bot)
            pal24_result = await payment_service.create_pal24_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=price_kopeks,
                description=settings.get_subscription_payment_description(
                    subscription_params['period_days'],
                    price_kopeks,
                ),
                language=db_user.language,
            )

            if not pal24_result:
                await callback.answer(
                    texts.t(
                        'PAL24_PAYMENT_ERROR',
                        '❌ Ошибка создания платежа PayPalych. Попробуйте позже или обратитесь в поддержку.',
                    ),
                    show_alert=True,
                )
                return

            sbp_url = pal24_result.get('sbp_url') or pal24_result.get('transfer_url')
            card_url = pal24_result.get('card_url')
            fallback_url = pal24_result.get('link_page_url') or pal24_result.get('link_url')

            if not (sbp_url or card_url or fallback_url):
                await callback.answer(
                    texts.t(
                        'PAL24_PAYMENT_ERROR',
                        '❌ Ошибка создания платежа PayPalych. Попробуйте позже или обратитесь в поддержку.',
                    ),
                    show_alert=True,
                )
                return

            if not sbp_url:
                sbp_url = fallback_url

            bill_id = pal24_result.get('bill_id')
            local_payment_id = pal24_result.get('local_payment_id')

            pay_buttons: list[list[types.InlineKeyboardButton]] = []
            steps: list[str] = []
            step_counter = 1

            default_sbp_text = texts.t(
                'PAL24_SBP_PAY_BUTTON',
                '🏦 Оплатить через PayPalych (СБП)',
            )
            sbp_button_text = settings.get_pal24_sbp_button_text(default_sbp_text)

            if sbp_url and settings.is_pal24_sbp_button_visible():
                pay_buttons.append(
                    [
                        types.InlineKeyboardButton(
                            text=sbp_button_text,
                            url=sbp_url,
                        )
                    ]
                )
                steps.append(
                    texts.t(
                        'PAL24_INSTRUCTION_BUTTON',
                        '{step}. Нажмите кнопку «{button}»',
                    ).format(step=step_counter, button=html.escape(sbp_button_text))
                )
                step_counter += 1

            default_card_text = texts.t(
                'PAL24_CARD_PAY_BUTTON',
                '💳 Оплатить банковской картой (PayPalych)',
            )
            card_button_text = settings.get_pal24_card_button_text(default_card_text)

            if card_url and card_url != sbp_url and settings.is_pal24_card_button_visible():
                pay_buttons.append(
                    [
                        types.InlineKeyboardButton(
                            text=card_button_text,
                            url=card_url,
                        )
                    ]
                )
                steps.append(
                    texts.t(
                        'PAL24_INSTRUCTION_BUTTON',
                        '{step}. Нажмите кнопку «{button}»',
                    ).format(step=step_counter, button=html.escape(card_button_text))
                )
                step_counter += 1

            if not pay_buttons and fallback_url and settings.is_pal24_sbp_button_visible():
                pay_buttons.append(
                    [
                        types.InlineKeyboardButton(
                            text=sbp_button_text,
                            url=fallback_url,
                        )
                    ]
                )
                steps.append(
                    texts.t(
                        'PAL24_INSTRUCTION_BUTTON',
                        '{step}. Нажмите кнопку «{button}»',
                    ).format(step=step_counter, button=html.escape(sbp_button_text))
                )
                step_counter += 1

            follow_template = texts.t(
                'PAL24_INSTRUCTION_FOLLOW',
                '{step}. Следуйте подсказкам платёжной системы',
            )
            steps.append(follow_template.format(step=step_counter))
            step_counter += 1

            confirm_template = texts.t(
                'PAL24_INSTRUCTION_CONFIRM',
                '{step}. Подтвердите перевод',
            )
            steps.append(confirm_template.format(step=step_counter))
            step_counter += 1

            success_template = texts.t(
                'PAL24_INSTRUCTION_COMPLETE',
                '{step}. Средства зачислятся автоматически',
            )
            steps.append(success_template.format(step=step_counter))

            message_template = texts.t(
                'PAL24_PAYMENT_INSTRUCTIONS',
                (
                    '🏦 <b>Оплата через PayPalych</b>\n\n'
                    '💰 Сумма: {amount}\n'
                    '🆔 ID счета: {bill_id}\n\n'
                    '📱 <b>Инструкция:</b>\n{steps}\n\n'
                    '❓ Если возникнут проблемы, обратитесь в {support}'
                ),
            )

            keyboard_rows = pay_buttons + [
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                        callback_data=f'check_simple_pal24_{local_payment_id}',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
            ]

            keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

            message_text = message_template.format(
                amount=settings.format_price(price_kopeks),
                bill_id=bill_id,
                steps='\n'.join(steps),
                support=settings.get_support_contact_display_html(),
            )

            await callback.message.edit_text(
                message_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )

            await state.clear()
            await callback.answer()
            return

        elif payment_method == 'wata':
            # Оплата через WATA
            if not settings.is_wata_enabled():
                await callback.answer(
                    texts.t('SUBSCRIPTION_UI_WATA_UNAVAILABLE', '❌ Оплата через WATA временно недоступна'),
                    show_alert=True,
                )
                return
            if price_kopeks < settings.WATA_MIN_AMOUNT_KOPEKS or price_kopeks > settings.WATA_MAX_AMOUNT_KOPEKS:
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_WATA_RANGE', '❌ Сумма для WATA должна быть между {minimum} и {maximum}.'
                    ).format(
                        minimum=settings.format_price(settings.WATA_MIN_AMOUNT_KOPEKS),
                        maximum=settings.format_price(settings.WATA_MAX_AMOUNT_KOPEKS),
                    ),
                    show_alert=True,
                )
                return

            payment_service = PaymentService(callback.bot)
            try:
                wata_result = await payment_service.create_wata_payment(
                    db=db,
                    user_id=db_user.id,
                    amount_kopeks=price_kopeks,
                    description=settings.get_subscription_payment_description(
                        subscription_params['period_days'],
                        price_kopeks,
                    ),
                    language=db_user.language,
                )
            except Exception as error:
                logger.error('Ошибка создания WATA платежа', error=error)
                wata_result = None

            if not wata_result or not wata_result.get('payment_url'):
                await callback.answer(
                    texts.t(
                        'WATA_PAYMENT_ERROR',
                        '❌ Ошибка создания платежа WATA. Попробуйте позже или обратитесь в поддержку.',
                    ),
                    show_alert=True,
                )
                return

            payment_url = wata_result['payment_url']
            payment_link_id = wata_result.get('payment_link_id')
            local_payment_id = wata_result.get('local_payment_id')

            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('WATA_PAY_BUTTON', '💳 Оплатить через WATA'),
                            url=payment_url,
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                            callback_data=f'check_simple_wata_{local_payment_id}',
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
                ]
            )

            message_template = texts.t(
                'WATA_PAYMENT_INSTRUCTIONS',
                (
                    '💳 <b>Оплата через WATA</b>\n\n'
                    '💰 Сумма: {amount}\n'
                    '🆔 ID платежа: {payment_id}\n\n'
                    '📱 <b>Инструкция:</b>\n'
                    "1. Нажмите кнопку 'Оплатить через WATA'\n"
                    '2. Следуйте подсказкам платежной системы\n'
                    '3. Подтвердите перевод\n'
                    '4. Средства зачислятся автоматически\n\n'
                    '❓ Если возникнут проблемы, обратитесь в {support}'
                ),
            )

            await callback.message.edit_text(
                message_template.format(
                    amount=settings.format_price(price_kopeks),
                    payment_id=payment_link_id,
                    support=settings.get_support_contact_display_html(),
                ),
                reply_markup=keyboard,
                parse_mode='HTML',
            )

            await state.clear()
            await callback.answer()
            return

        else:
            await callback.answer(
                texts.t('SUBSCRIPTION_UI_UNKNOWN_PAYMENT_METHOD', '❌ Неизвестный способ оплаты'),
                show_alert=True,
            )

    except Exception as e:
        logger.error('Ошибка обработки метода оплаты простой подписки', error=e)
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_UI_REQUEST_FAILED_SUPPORT',
                '❌ Ошибка обработки запроса. Попробуйте позже или обратитесь в поддержку.',
            ),
            show_alert=True,
        )
        await state.clear()


@error_handler
async def check_simple_pal24_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
    db_user: User | None = None,
):
    texts = get_texts(getattr(db_user, 'language', None))
    if db_user is None:
        await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
        return
    try:
        local_payment_id = int(callback.data.rsplit('_', 1)[-1])
        from app.database.crud.pal24 import get_pal24_payment_by_id

        payment = await get_pal24_payment_by_id(db, local_payment_id)
        if not payment or payment.user_id != db_user.id:
            await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
            return

        payment_service = PaymentService(callback.bot)
        status_info = await payment_service.get_pal24_payment_status(db, local_payment_id)

        if not status_info:
            await callback.answer(
                texts.t('SUBSCRIPTION_UI_PAYMENT_NOT_FOUND', '❌ Платеж не найден'),
                show_alert=True,
            )
            return

        payment = status_info['payment']

        status_labels = {
            'NEW': (
                '⏳',
                texts.t('SUBSCRIPTION_UI_STATUS_PENDING', 'Ожидает оплаты'),
            ),
            'PROCESS': (
                '⌛',
                texts.t('SUBSCRIPTION_UI_STATUS_PROCESSING', 'Обрабатывается'),
            ),
            'SUCCESS': (
                '✅',
                texts.t('SUBSCRIPTION_UI_STATUS_PAID', 'Оплачен'),
            ),
            'FAIL': (
                '❌',
                texts.t('SUBSCRIPTION_UI_STATUS_CANCELLED', 'Отменен'),
            ),
            'UNDERPAID': (
                '⚠️',
                texts.t('SUBSCRIPTION_UI_STATUS_UNDERPAID', 'Недоплата'),
            ),
            'OVERPAID': (
                '⚠️',
                texts.t('SUBSCRIPTION_UI_STATUS_OVERPAID', 'Переплата'),
            ),
        }

        emoji, status_text = status_labels.get(
            payment.status,
            ('❓', texts.t('SUBSCRIPTION_UI_STATUS_UNKNOWN', 'Неизвестно')),
        )

        metadata = payment.metadata_json or {}
        links_meta = metadata.get('links') if isinstance(metadata, dict) else {}
        if not isinstance(links_meta, dict):
            links_meta = {}

        links_info = status_info.get('links') or {}
        sbp_link = links_info.get('sbp') or links_meta.get('sbp') or status_info.get('sbp_url') or payment.link_url
        card_link = links_info.get('card') or links_meta.get('card') or status_info.get('card_url')
        if not card_link and payment.link_page_url and payment.link_page_url != sbp_link:
            card_link = payment.link_page_url

        message_lines = [
            texts.t('SUBSCRIPTION_UI_STATUS_PAL24_TITLE', '🏦 Статус платежа PayPalych:'),
            '',
            texts.t('SUBSCRIPTION_UI_BILL_ID', '🆔 ID счета: {bill_id}').format(bill_id=payment.bill_id),
            texts.t('SUBSCRIPTION_UI_AMOUNT_VALUE', '💰 Сумма: {amount}').format(
                amount=settings.format_price(payment.amount_kopeks)
            ),
            texts.t('SUBSCRIPTION_UI_STATUS_VALUE', '📊 Статус: {emoji} {status}').format(
                emoji=emoji, status=status_text
            ),
            texts.t('SUBSCRIPTION_UI_CREATED_VALUE', '📅 Создан: {date}').format(
                date=format_local_datetime(payment.created_at, '%d.%m.%Y %H:%M')
            ),
        ]

        if payment.is_paid:
            message_lines += [
                '',
                texts.t('SUBSCRIPTION_UI_PAYMENT_COMPLETE', '✅ Платеж успешно завершен! Средства уже зачислены.'),
            ]
        elif payment.status in {'NEW', 'PROCESS'}:
            message_lines += [
                '',
                texts.t(
                    'SUBSCRIPTION_UI_PAYMENT_WAIT', '⏳ Платеж еще не завершен. Оплатите счет и проверьте статус позже.'
                ),
            ]
            if sbp_link:
                message_lines += ['', texts.t('SUBSCRIPTION_UI_SBP_LINK', '🏦 СБП: {link}').format(link=sbp_link)]
            if card_link and card_link != sbp_link:
                message_lines.append(texts.t('SUBSCRIPTION_UI_CARD_LINK', '💳 Карта: {link}').format(link=card_link))
        elif payment.status in {'FAIL', 'UNDERPAID', 'OVERPAID'}:
            message_lines += [
                '',
                texts.t(
                    'SUBSCRIPTION_UI_PAYMENT_INCOMPLETE_SUPPORT',
                    '❌ Платеж не завершен корректно. Обратитесь в {support}',
                ).format(support=settings.get_support_contact_display()),
            ]

        pay_rows: list[list[types.InlineKeyboardButton]] = []

        if not payment.is_paid and payment.status in {'NEW', 'PROCESS'}:
            default_sbp_text = texts.t(
                'PAL24_SBP_PAY_BUTTON',
                '🏦 Оплатить через PayPalych (СБП)',
            )
            sbp_button_text = settings.get_pal24_sbp_button_text(default_sbp_text)

            if sbp_link and settings.is_pal24_sbp_button_visible():
                pay_rows.append(
                    [
                        types.InlineKeyboardButton(
                            text=sbp_button_text,
                            url=sbp_link,
                        )
                    ]
                )

            default_card_text = texts.t(
                'PAL24_CARD_PAY_BUTTON',
                '💳 Оплатить банковской картой (PayPalych)',
            )
            card_button_text = settings.get_pal24_card_button_text(default_card_text)

            if card_link and settings.is_pal24_card_button_visible():
                if not pay_rows or pay_rows[-1][0].url != card_link:
                    pay_rows.append(
                        [
                            types.InlineKeyboardButton(
                                text=card_button_text,
                                url=card_link,
                            )
                        ]
                    )

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=pay_rows
            + [
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                        callback_data=f'check_simple_pal24_{local_payment_id}',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
            ]
        )

        await callback.answer()
        try:
            await callback.message.edit_text(
                '\n'.join(message_lines),
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as error:
            if 'message is not modified' in str(error).lower():
                await callback.answer(texts.t('CHECK_STATUS_NO_CHANGES', 'Статус не изменился'))
            else:
                raise

    except Exception as error:
        logger.error('Ошибка проверки статуса PayPalych для простой подписки', error=error)
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_STATUS_CHECK_FAILED', '❌ Ошибка проверки статуса'),
            show_alert=True,
        )


@error_handler
async def check_simple_mulenpay_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
    db_user: User | None = None,
):
    texts = get_texts(getattr(db_user, 'language', None))
    _ui_texts = texts
    if db_user is None:
        await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
        return
    try:
        local_payment_id = int(callback.data.rsplit('_', 1)[-1])
    except (ValueError, IndexError):
        await callback.answer(
            _ui_texts.t('SUBSCRIPTION_UI_INVALID_PAYMENT_ID', '❌ Некорректный идентификатор платежа'),
            show_alert=True,
        )
        return

    from app.database.crud.mulenpay import get_mulenpay_payment_by_local_id

    payment = await get_mulenpay_payment_by_local_id(db, local_payment_id)
    if not payment or payment.user_id != db_user.id:
        await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
        return

    payment_service = PaymentService(callback.bot)
    status_info = await payment_service.get_mulenpay_payment_status(db, local_payment_id)

    if not status_info:
        await callback.answer(
            _ui_texts.t('SUBSCRIPTION_UI_PAYMENT_NOT_FOUND', '❌ Платеж не найден'),
            show_alert=True,
        )
        return

    payment = status_info['payment']

    user_language = settings.DEFAULT_LANGUAGE
    try:
        from app.services.payment_service import get_user_by_id as fetch_user_by_id

        user = await fetch_user_by_id(db, payment.user_id)
        if user and getattr(user, 'language', None):
            user_language = user.language
    except Exception as error:
        logger.debug('Не удалось получить пользователя для MulenPay статуса', error=error)

    texts = get_texts(getattr(db_user, 'language', None) or user_language)
    status_labels = {
        'created': (
            '⏳',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_PENDING', 'Ожидает оплаты'),
        ),
        'processing': (
            '⌛',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_PROCESSING', 'Обрабатывается'),
        ),
        'success': ('✅', _ui_texts.t('SUBSCRIPTION_UI_STATUS_PAID', 'Оплачен')),
        'canceled': (
            '❌',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_CANCELLED', 'Отменен'),
        ),
        'error': ('⚠️', _ui_texts.t('SUBSCRIPTION_UI_STATUS_ERROR', 'Ошибка')),
        'hold': ('🔒', _ui_texts.t('SUBSCRIPTION_UI_STATUS_HOLD', 'Холд')),
        'unknown': (
            '❓',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_UNKNOWN', 'Неизвестно'),
        ),
    }

    emoji, status_text = status_labels.get(
        payment.status,
        ('❓', _ui_texts.t('SUBSCRIPTION_UI_STATUS_UNKNOWN', 'Неизвестно')),
    )

    message_lines = [
        _ui_texts.t('SUBSCRIPTION_UI_STATUS_MULENPAY_TITLE', '💳 Статус платежа Mulen Pay:'),
        '',
        f'🆔 ID: {payment.mulen_payment_id or payment.id}',
        texts.t('SUBSCRIPTION_UI_AMOUNT_VALUE', '💰 Сумма: {amount}').format(
            amount=settings.format_price(payment.amount_kopeks)
        ),
        texts.t('SUBSCRIPTION_UI_STATUS_VALUE', '📊 Статус: {emoji} {status}').format(emoji=emoji, status=status_text),
        texts.t('SUBSCRIPTION_UI_CREATED_VALUE', '📅 Создан: {date}').format(
            date=format_local_datetime(payment.created_at, '%d.%m.%Y %H:%M') if payment.created_at else '—'
        ),
    ]

    if payment.is_paid:
        message_lines.append(
            _ui_texts.t(
                'SUBSCRIPTION_UI_PAYMENT_COMPLETE_LINE', '\n✅ Платеж успешно завершен! Средства уже зачислены.'
            )
        )
    elif payment.status in {'created', 'processing'}:
        message_lines.append(
            _ui_texts.t(
                'SUBSCRIPTION_UI_PAYMENT_WAIT_LINE',
                '\n⏳ Платеж еще не завершен. Завершите оплату и проверьте статус позже.',
            )
        )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_simple_mulenpay_{local_payment_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
        ]
    )

    await callback.answer()
    await callback.message.edit_text(
        '\n'.join(message_lines),
        reply_markup=keyboard,
        parse_mode='HTML',
    )


@error_handler
async def check_simple_cryptobot_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
    db_user: User | None = None,
):
    texts = get_texts(getattr(db_user, 'language', None))
    _ui_texts = texts
    if db_user is None:
        await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
        return
    try:
        local_payment_id = int(callback.data.rsplit('_', 1)[-1])
    except (ValueError, IndexError):
        await callback.answer(
            _ui_texts.t('SUBSCRIPTION_UI_INVALID_PAYMENT_ID', '❌ Некорректный идентификатор платежа'),
            show_alert=True,
        )
        return

    from app.database.crud.cryptobot import get_cryptobot_payment_by_id

    payment = await get_cryptobot_payment_by_id(db, local_payment_id)
    if not payment or payment.user_id != db_user.id:
        await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
        return

    status_labels = {
        'active': (
            '⏳',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_PENDING', 'Ожидает оплаты'),
        ),
        'paid': ('✅', _ui_texts.t('SUBSCRIPTION_UI_STATUS_PAID', 'Оплачен')),
        'expired': ('❌', _ui_texts.t('SUBSCRIPTION_UI_STATUS_EXPIRED', 'Истек')),
    }
    emoji, status_text = status_labels.get(
        payment.status,
        ('❓', _ui_texts.t('SUBSCRIPTION_UI_STATUS_UNKNOWN', 'Неизвестно')),
    )

    language = settings.DEFAULT_LANGUAGE
    try:
        from app.services.payment_service import get_user_by_id as fetch_user_by_id

        user = await fetch_user_by_id(db, payment.user_id)
        if user and getattr(user, 'language', None):
            language = user.language
    except Exception as error:
        logger.debug('Не удалось получить пользователя для CryptoBot статуса', error=error)

    texts = get_texts(getattr(db_user, 'language', None) or language)
    message_lines = [
        _ui_texts.t('SUBSCRIPTION_UI_STATUS_CRYPTOBOT_TITLE', '🪙 <b>Статус платежа CryptoBot</b>'),
        '',
        f'🆔 ID: {payment.invoice_id}',
        texts.t('SUBSCRIPTION_UI_CURRENCY_AMOUNT', '💰 Сумма: {amount} {currency}').format(
            amount=payment.amount, currency=payment.asset
        ),
        texts.t('SUBSCRIPTION_UI_STATUS_VALUE', '📊 Статус: {emoji} {status}').format(emoji=emoji, status=status_text),
        texts.t('SUBSCRIPTION_UI_CREATED_VALUE', '📅 Создан: {date}').format(
            date=format_local_datetime(payment.created_at, '%d.%m.%Y %H:%M') if payment.created_at else '—'
        ),
    ]

    if payment.status == 'paid':
        message_lines.append(
            _ui_texts.t('SUBSCRIPTION_UI_PAYMENT_CONFIRMED_LINE', '\n✅ Платеж подтвержден. Средства уже зачислены.')
        )
    elif payment.status == 'active':
        message_lines.append(
            _ui_texts.t(
                'SUBSCRIPTION_UI_PAYMENT_CONFIRM_WAIT_LINE',
                '\n⏳ Платеж еще ожидает подтверждения. Оплатите счет и проверьте статус позже.',
            )
        )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_simple_cryptobot_{local_payment_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
        ]
    )

    await callback.answer()
    await callback.message.edit_text(
        '\n'.join(message_lines),
        reply_markup=keyboard,
        parse_mode='HTML',
    )


@error_handler
async def check_simple_heleket_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
    db_user: User | None = None,
):
    texts = get_texts(getattr(db_user, 'language', None))
    _ui_texts = texts
    if db_user is None:
        await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
        return
    try:
        local_payment_id = int(callback.data.rsplit('_', 1)[-1])
    except (ValueError, IndexError):
        await callback.answer(
            _ui_texts.t('SUBSCRIPTION_UI_INVALID_PAYMENT_ID', '❌ Некорректный идентификатор платежа'),
            show_alert=True,
        )
        return

    from app.database.crud.heleket import get_heleket_payment_by_id

    payment = await get_heleket_payment_by_id(db, local_payment_id)
    if not payment or payment.user_id != db_user.id:
        await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
        return

    status_labels = {
        'check': (
            '⏳',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_PENDING', 'Ожидает оплаты'),
        ),
        'paid': ('✅', _ui_texts.t('SUBSCRIPTION_UI_STATUS_PAID', 'Оплачен')),
        'paid_over': (
            '✅',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_OVERPAID_PAID', 'Оплачен (переплата)'),
        ),
        'wrong_amount': (
            '⚠️',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_WRONG_AMOUNT', 'Неверная сумма'),
        ),
        'cancel': (
            '❌',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_CANCELLED', 'Отменен'),
        ),
        'fail': ('❌', _ui_texts.t('SUBSCRIPTION_UI_STATUS_ERROR', 'Ошибка')),
        'process': (
            '⌛',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_PROCESSING', 'Обрабатывается'),
        ),
        'confirm_check': (
            '⌛',
            _ui_texts.t('SUBSCRIPTION_UI_STATUS_CONFIRMING', 'Ожидает подтверждения'),
        ),
    }

    emoji, status_text = status_labels.get(
        payment.status,
        ('❓', _ui_texts.t('SUBSCRIPTION_UI_STATUS_UNKNOWN', 'Неизвестно')),
    )

    language = settings.DEFAULT_LANGUAGE
    try:
        from app.services.payment_service import get_user_by_id as fetch_user_by_id

        user = await fetch_user_by_id(db, payment.user_id)
        if user and getattr(user, 'language', None):
            language = user.language
    except Exception as error:
        logger.debug('Не удалось получить пользователя для Heleket статуса', error=error)

    texts = get_texts(getattr(db_user, 'language', None) or language)

    message_lines = [
        _ui_texts.t('SUBSCRIPTION_UI_STATUS_HELEKET_TITLE', '🪙 Статус платежа Heleket:'),
        '',
        f'🆔 UUID: {payment.uuid[:8]}...',
        texts.t('SUBSCRIPTION_UI_AMOUNT_VALUE', '💰 Сумма: {amount}').format(
            amount=settings.format_price(payment.amount_kopeks)
        ),
        texts.t('SUBSCRIPTION_UI_STATUS_VALUE', '📊 Статус: {emoji} {status}').format(emoji=emoji, status=status_text),
        texts.t('SUBSCRIPTION_UI_CREATED_VALUE', '📅 Создан: {date}').format(
            date=format_local_datetime(payment.created_at, '%d.%m.%Y %H:%M') if payment.created_at else '—'
        ),
    ]

    if payment.payer_amount and payment.payer_currency:
        message_lines.append(
            texts.t('SUBSCRIPTION_UI_CRYPTO_PAYMENT', '🪙 Оплата: {amount} {currency}').format(
                amount=payment.payer_amount, currency=payment.payer_currency
            )
        )

    if payment.is_paid:
        message_lines.append(
            _ui_texts.t(
                'SUBSCRIPTION_UI_PAYMENT_COMPLETE_LINE', '\n✅ Платеж успешно завершен! Средства уже зачислены.'
            )
        )
    elif payment.status in {'check', 'process', 'confirm_check'}:
        message_lines.append(
            _ui_texts.t(
                'SUBSCRIPTION_UI_PAYMENT_PROCESSING_LINE',
                '\n⏳ Платеж еще обрабатывается. Завершите оплату и проверьте статус позже.',
            )
        )
        if payment.payment_url:
            message_lines.append(
                texts.t('SUBSCRIPTION_UI_PAYMENT_LINK', '\n🔗 Ссылка на оплату: {link}').format(
                    link=payment.payment_url
                )
            )
    elif payment.status in {'fail', 'cancel', 'wrong_amount'}:
        message_lines.append(
            texts.t(
                'SUBSCRIPTION_UI_PAYMENT_INCOMPLETE_SUPPORT_LINE',
                '\n❌ Платеж не завершен корректно. Обратитесь в {support}',
            ).format(support=settings.get_support_contact_display())
        )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_simple_heleket_{local_payment_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
        ]
    )

    await callback.answer()
    await callback.message.edit_text(
        '\n'.join(message_lines),
        reply_markup=keyboard,
        parse_mode='HTML',
    )


@error_handler
async def check_simple_wata_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
    db_user: User | None = None,
):
    texts = get_texts(getattr(db_user, 'language', None))
    if db_user is None:
        await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
        return
    try:
        local_payment_id = int(callback.data.rsplit('_', 1)[-1])
    except (ValueError, IndexError):
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_INVALID_PAYMENT_ID', '❌ Некорректный идентификатор платежа'),
            show_alert=True,
        )
        return

    from app.database.crud.wata import get_wata_payment_by_id

    payment = await get_wata_payment_by_id(db, local_payment_id)
    if not payment or payment.user_id != db_user.id:
        await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
        return

    payment_service = PaymentService(callback.bot)
    status_info = await payment_service.get_wata_payment_status(db, local_payment_id)

    if not status_info:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_PAYMENT_NOT_FOUND', '❌ Платеж не найден'),
            show_alert=True,
        )
        return

    payment = status_info['payment']

    status_labels = {
        'Opened': ('⏳', texts.t('WATA_STATUS_OPENED', 'Ожидает оплаты')),
        'Closed': ('⌛', texts.t('WATA_STATUS_CLOSED', 'Обрабатывается')),
        'Paid': ('✅', texts.t('WATA_STATUS_PAID', 'Оплачен')),
        'Declined': ('❌', texts.t('WATA_STATUS_DECLINED', 'Отклонен')),
    }
    emoji, status_text = status_labels.get(payment.status, ('❓', texts.t('WATA_STATUS_UNKNOWN', 'Неизвестно')))

    message_lines = [
        texts.t('WATA_STATUS_TITLE', '💳 <b>Статус платежа WATA</b>'),
        '',
        f'🆔 ID: {payment.payment_link_id}',
        texts.t('SUBSCRIPTION_UI_AMOUNT_VALUE', '💰 Сумма: {amount}').format(
            amount=settings.format_price(payment.amount_kopeks)
        ),
        texts.t('SUBSCRIPTION_UI_STATUS_VALUE', '📊 Статус: {emoji} {status}').format(emoji=emoji, status=status_text),
        texts.t('SUBSCRIPTION_UI_CREATED_VALUE', '📅 Создан: {date}').format(
            date=format_local_datetime(payment.created_at, '%d.%m.%Y %H:%M') if payment.created_at else '—'
        ),
    ]

    if payment.is_paid:
        message_lines.append(
            texts.t('SUBSCRIPTION_UI_PAYMENT_COMPLETE_LINE', '\n✅ Платеж успешно завершен! Средства уже зачислены.')
        )
    elif payment.status in {'Opened', 'Closed'}:
        message_lines.append(
            texts.t(
                'SUBSCRIPTION_UI_PAYMENT_WAIT_LINE',
                '\n⏳ Платеж еще не завершен. Завершите оплату и проверьте статус позже.',
            )
        )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_simple_wata_{local_payment_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
        ]
    )

    await callback.answer()
    await callback.message.edit_text(
        '\n'.join(message_lines),
        reply_markup=keyboard,
        parse_mode='HTML',
    )


@error_handler
async def confirm_simple_subscription_purchase(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Обрабатывает подтверждение простой покупки подписки при наличии активной платной подписки."""
    if await _reject_classic_checkout_in_tariff_mode(callback, db_user.language):
        return
    texts = get_texts(db_user.language)

    data = await state.get_data()
    subscription_params = data.get('subscription_params', {})

    if not subscription_params:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_STALE_DATA', '❌ Данные подписки устарели. Пожалуйста, начните сначала.'),
            show_alert=True,
        )
        return

    resolved_squad_uuid = await _ensure_simple_subscription_squad_uuid(
        db,
        state,
        subscription_params,
        user_id=db_user.id,
        state_data=data,
    )

    # Lock user BEFORE pricing to prevent TOCTOU
    from app.database.crud.user import lock_user_for_pricing, subtract_user_balance

    db_user = await lock_user_for_pricing(db, db_user.id)

    # Рассчитываем цену подписки (group discounts per-category)
    price_kopeks, price_breakdown = await _calculate_simple_subscription_price(
        db,
        subscription_params,
        user=db_user,
        resolved_squad_uuid=resolved_squad_uuid,
    )

    # PricingEngine already applies promo-offer discount inside calculate_classic_new_subscription_price.
    # Only determine whether to consume the offer (zero it out after use).
    from app.utils.promo_offer import get_user_active_promo_discount_percent

    consume_promo = get_user_active_promo_discount_percent(db_user) > 0

    total_required = price_kopeks
    logger.warning(
        'SIMPLE_SUBSCRIPTION_DEBUG_CONFIRM',
        db_user_id=db_user.id,
        subscription_params=subscription_params['period_days'],
        price_breakdown=price_breakdown.get('base_price', 0),
        price_breakdown_2=price_breakdown.get('traffic_price', 0),
        price_breakdown_3=price_breakdown.get('devices_price', 0),
        price_breakdown_4=price_breakdown.get('servers_price', 0),
        price_breakdown_5=price_breakdown.get('total_discount', 0),
        total_required=total_required,
        getattr=getattr(db_user, 'balance_kopeks', 0),
    )

    # Проверяем баланс пользователя
    user_balance_kopeks = getattr(db_user, 'balance_kopeks', 0)

    if total_required > 0 and user_balance_kopeks < total_required:
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_UI_INSUFFICIENT_SUBSCRIPTION', '❌ Недостаточно средств на балансе для оплаты подписки'
            ),
            show_alert=True,
        )
        return

    from app.database.crud.subscription import get_subscription_by_user_id

    existing_subscription = await get_subscription_by_user_id(db, db_user.id)
    atomic_purchase = bool(existing_subscription and existing_subscription.is_trial) or getattr(
        existing_subscription, 'traffic_accounting_mode', None
    ) in ('managed', 'migrating')
    purchase_user_id = db_user.id
    try:
        # Списываем средства с баланса пользователя
        purchase_description = f'Оплата подписки на {subscription_params["period_days"]} дней'
        success = await subtract_user_balance(
            db,
            db_user,
            price_kopeks,
            purchase_description,
            consume_promo_offer=consume_promo,
            mark_as_paid_subscription=True,
            commit=not atomic_purchase,
        )

        if not success:
            await callback.answer(
                texts.t('SUBSCRIPTION_UI_BALANCE_DEBIT_FAILED', '❌ Ошибка списания средств с баланса'),
                show_alert=True,
            )
            return

        # Создаём транзакцию для учёта списания
        from app.database.crud.transaction import create_transaction
        from app.database.models import PaymentMethod, TransactionType

        transaction = await create_transaction(
            db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price_kopeks,
            description=purchase_description,
            payment_method=PaymentMethod.BALANCE,
            commit=not atomic_purchase,
        )

        # Проверяем, есть ли у пользователя уже подписка
        from app.database.crud.subscription import extend_subscription

        # Reuse the pre-debit snapshot: the status-updating getter may commit
        # when a trial expires, exposing a debit before its conversion.

        reset_traffic = settings.RESET_TRAFFIC_ON_PAYMENT or bool(
            existing_subscription and existing_subscription.is_trial
        )
        if existing_subscription:
            # Если подписка уже существует, продлеваем её
            # Сохраняем информацию о текущей подписке, особенно является ли она пробной
            was_trial = getattr(existing_subscription, 'is_trial', False)

            subscription = await extend_subscription(
                db=db,
                subscription=existing_subscription,
                days=subscription_params['period_days'],
                paid_trial_conversion=was_trial,
                traffic_limit_gb=subscription_params['traffic_limit_gb'],
                device_limit=subscription_params['device_limit'],
                connected_squads=[resolved_squad_uuid] if resolved_squad_uuid else None,
                paid_traffic_reset=False,
                reset_used_traffic=reset_traffic,
                commit=not atomic_purchase,
            )

            # Если текущая подписка была пробной, и мы обновляем её
            # нужно изменить статус подписки
            if was_trial:
                from app.database.models import SubscriptionStatus

                # Переводим подписку из пробной в активную платную
                subscription.status = SubscriptionStatus.ACTIVE.value
                subscription.is_trial = False

            await db.commit()
            if atomic_purchase:
                from app.database.crud.transaction import emit_transaction_side_effects

                await emit_transaction_side_effects(
                    db,
                    transaction,
                    amount_kopeks=price_kopeks,
                    user_id=db_user.id,
                    type=TransactionType.SUBSCRIPTION_PAYMENT,
                    payment_method=PaymentMethod.BALANCE,
                    description=purchase_description,
                )
            await db.refresh(subscription)
        else:
            # Если подписки нет, создаём новую
            from app.database.crud.subscription import create_paid_subscription

            subscription = await create_paid_subscription(
                db=db,
                user_id=db_user.id,
                duration_days=subscription_params['period_days'],
                traffic_limit_gb=subscription_params['traffic_limit_gb'],
                device_limit=subscription_params['device_limit'],
                connected_squads=[resolved_squad_uuid] if resolved_squad_uuid else [],
                update_server_counters=True,
            )

        if not subscription:
            # Возвращаем средства на баланс в случае ошибки
            from app.services.payment_service import add_user_balance

            await add_user_balance(
                db,
                db_user.id,
                price_kopeks,
                f'Возврат средств за неудавшуюся подписку на {subscription_params["period_days"]} дней',
            )
            await callback.answer(
                texts.t(
                    'SUBSCRIPTION_UI_CREATION_REFUNDED', '❌ Ошибка создания подписки. Средства возвращены на баланс.'
                ),
                show_alert=True,
            )
            return

        # Обновляем баланс пользователя
        await db.refresh(db_user)

        # Обновляем или создаём ссылку подписки в RemnaWave
        try:
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            remnawave_user = await subscription_service.create_remnawave_user(
                db, subscription, reset_traffic=reset_traffic
            )
            if remnawave_user:
                await db.refresh(subscription)
        except Exception as sync_error:
            logger.error(
                'Ошибка синхронизации подписки с RemnaWave для пользователя',
                db_user_id=db_user.id,
                sync_error=sync_error,
                exc_info=True,
            )
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            if hasattr(subscription, 'id') and hasattr(subscription, 'user_id'):
                remnawave_retry_queue.enqueue(
                    subscription_id=subscription.id,
                    user_id=subscription.user_id,
                    action='create',
                )

        # Отправляем уведомление об успешной покупке
        server_label = _get_simple_subscription_server_label(
            texts,
            subscription_params,
            resolved_squad_uuid,
        )
        show_devices = settings.is_devices_selection_enabled()

        success_lines = [
            texts.t('SUBSCRIPTION_UI_ACTIVATED_TITLE', '✅ <b>Подписка успешно активирована!</b>'),
            '',
            texts.t('SUBSCRIPTION_UI_PERIOD_DAYS', '📅 Период: {days} дней').format(
                days=subscription_params['period_days']
            ),
        ]

        if show_devices:
            success_lines.append(
                texts.t('SUBSCRIPTION_UI_DEVICES_VALUE', '📱 Устройства: {devices}').format(
                    devices=subscription_params['device_limit']
                )
            )

        success_traffic_gb = subscription_params['traffic_limit_gb']
        success_traffic_label = (
            texts.t('SUBSCRIPTION_UI_UNLIMITED', 'Безлимит')
            if success_traffic_gb == 0
            else texts.t('SUBSCRIPTION_UI_GB_VALUE', '{amount} ГБ').format(amount=success_traffic_gb)
        )

        success_lines.extend(
            [
                texts.t('SUBSCRIPTION_UI_TRAFFIC_VALUE', '📊 Трафик: {traffic}').format(traffic=success_traffic_label),
                texts.t('SUBSCRIPTION_UI_SERVER_VALUE', '🌍 Сервер: {server}').format(server=server_label),
                '',
                texts.t('SUBSCRIPTION_UI_DEBIT_VALUE', '💰 Списано с баланса: {amount}').format(
                    amount=settings.format_price(price_kopeks)
                ),
                texts.t('SUBSCRIPTION_UI_BALANCE_VALUE', '💳 Ваш баланс: {amount}').format(
                    amount=settings.format_price(db_user.balance_kopeks)
                ),
                '',
                texts.t('SUBSCRIPTION_UI_CONNECT_SECTION', "🔗 Для подключения перейдите в раздел 'Подключиться'"),
            ]
        )

        success_message = '\n'.join(success_lines)

        connect_mode = settings.CONNECT_BUTTON_MODE
        subscription_link = get_display_subscription_link(subscription)
        connect_button_text = texts.t('CONNECT_BUTTON', '🔗 Подключиться')

        def _fallback_connect_button() -> types.InlineKeyboardButton:
            return types.InlineKeyboardButton(
                text=connect_button_text,
                callback_data='subscription_connect',
            )

        if connect_mode == 'miniapp_subscription':
            if subscription_link:
                connect_row = [
                    types.InlineKeyboardButton(
                        text=connect_button_text,
                        web_app=types.WebAppInfo(url=subscription_link),
                    )
                ]
            else:
                connect_row = [_fallback_connect_button()]
        elif connect_mode == 'miniapp_custom':
            custom_url = settings.MINIAPP_CUSTOM_URL
            if custom_url:
                connect_row = [
                    types.InlineKeyboardButton(
                        text=connect_button_text,
                        web_app=types.WebAppInfo(url=custom_url),
                    )
                ]
            else:
                connect_row = [_fallback_connect_button()]
        elif connect_mode == 'link':
            if subscription_link:
                connect_row = [
                    types.InlineKeyboardButton(
                        text=connect_button_text,
                        url=subscription_link,
                    )
                ]
            else:
                connect_row = [_fallback_connect_button()]
        elif connect_mode == 'happ_cryptolink':
            if subscription_link:
                connect_row = [
                    types.InlineKeyboardButton(
                        text=connect_button_text,
                        callback_data='open_subscription_link',
                    )
                ]
            else:
                connect_row = [_fallback_connect_button()]
        else:
            connect_row = [_fallback_connect_button()]

        keyboard_rows = [connect_row]

        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            keyboard_rows.append(happ_row)

        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_MAIN_MENU', '🏠 Главное меню'),
                    callback_data='back_to_menu',
                )
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.message.edit_text(success_message, reply_markup=keyboard, parse_mode='HTML')

        # Отправляем уведомление админам
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_purchase_notification(
                db,
                db_user,
                subscription,
                transaction,
                subscription_params['period_days'],
                False,  # was_trial_conversion
                amount_kopeks=price_kopeks,
                purchase_type='renewal' if existing_subscription else 'first_purchase',
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления админам о покупке', error=e)

        await state.clear()
        await callback.answer()

        logger.info(
            'Пользователь успешно купил подписку с баланса',
            telegram_id=db_user.telegram_id,
            price_kopeks=price_kopeks / 100,
        )

    except asyncio.CancelledError:
        if atomic_purchase:
            await db.rollback()
        raise
    except Exception as error:
        if atomic_purchase:
            await db.rollback()
        logger.error(
            'Ошибка подтверждения простой подписки с баланса для пользователя',
            db_user_id=purchase_user_id,
            error=error,
            exc_info=True,
        )
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_UI_PAYMENT_FAILED_SUPPORT',
                '❌ Ошибка оплаты подписки. Попробуйте позже или обратитесь в поддержку.',
            ),
            show_alert=True,
        )
        await state.clear()


def register_simple_subscription_handlers(dp):
    """Регистрирует обработчики простой покупки подписки."""

    dp.callback_query.register(start_simple_subscription_purchase, F.data == 'simple_subscription_purchase')

    dp.callback_query.register(confirm_simple_subscription_purchase, F.data == 'simple_subscription_confirm_purchase')

    dp.callback_query.register(
        handle_simple_subscription_pay_with_balance, F.data == 'simple_subscription_pay_with_balance'
    )

    dp.callback_query.register(
        handle_simple_subscription_pay_with_balance_disabled, F.data == 'simple_subscription_pay_with_balance_disabled'
    )

    dp.callback_query.register(
        handle_simple_subscription_other_payment_methods, F.data == 'simple_subscription_other_payment_methods'
    )

    dp.callback_query.register(handle_simple_subscription_payment_method, F.data.startswith('simple_subscription_'))

    dp.callback_query.register(check_simple_pal24_payment_status, F.data.startswith('check_simple_pal24_'))

    dp.callback_query.register(check_simple_mulenpay_payment_status, F.data.startswith('check_simple_mulenpay_'))

    dp.callback_query.register(check_simple_cryptobot_payment_status, F.data.startswith('check_simple_cryptobot_'))

    dp.callback_query.register(check_simple_heleket_payment_status, F.data.startswith('check_simple_heleket_'))

    dp.callback_query.register(check_simple_wata_payment_status, F.data.startswith('check_simple_wata_'))

    dp.callback_query.register(check_simple_pal24_payment_status, F.data.startswith('check_simple_pal24_'))
