from datetime import UTC, datetime

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.crud.subscription import add_subscription_wl_traffic
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import TransactionType, User
from app.keyboards.inline import (
    get_add_wl_traffic_keyboard,
    get_add_wl_traffic_keyboard_from_tariff,
    get_back_keyboard,
    get_devices_keyboard,
    get_insufficient_balance_keyboard,
    get_reset_wl_traffic_confirm_keyboard,
)
from app.localization.texts import get_texts
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service
from app.states import SubscriptionStates
from app.utils.pricing_utils import (
    apply_percentage_discount,
    calculate_prorated_price,
    get_remaining_months,
)

from .common import (
    _apply_addon_discount,
    _get_addon_discount_percent_for_user,
    _get_period_hint_from_subscription,
    get_confirm_switch_wl_traffic_keyboard,
    get_wl_traffic_switch_keyboard,
    logger,
)
from .countries import (
    _get_available_countries,
)
from .summary import present_subscription_summary


async def handle_add_wl_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.config import settings
    from app.database.crud.tariff import get_tariff_by_id

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t('PAID_FEATURE_ONLY', '⚠ Эта функция доступна только для платных подписок'),
            show_alert=True,
        )
        return

    if subscription.wl_traffic_limit_gb == 0:
        await callback.answer(
            texts.t('TRAFFIC_ALREADY_UNLIMITED', '⚠ У вас уже безлимитный трафик'),
            show_alert=True,
        )
        return

    if not settings.WL_TRAFFIC_TOPUP_ENABLED:
        await callback.answer(
            texts.t('TRAFFIC_TOPUP_DISABLED', '⚠️ Функция докупки БС-трафика отключена'),
            show_alert=True,
        )
        return

    # Загружаем тариф (если подписка на тарифе)
    tariff = None
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Определяем пакеты: per-tariff → глобальные
    tariff_wl_packages = None
    if tariff and getattr(tariff, 'wl_traffic_topup_packages', None):
        raw = tariff.wl_traffic_topup_packages  # {str(gb): price_kopeks}
        if raw and isinstance(raw, dict):
            # Нормализуем в список пакетов как в глобальных настройках
            tariff_wl_packages = {int(k): int(v) for k, v in raw.items() if v and int(v) > 0}

    current_traffic = subscription.wl_traffic_limit_gb
    period_hint_days = _get_period_hint_from_subscription(subscription)
    traffic_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        'traffic',
        period_hint_days,
    )

    prompt_text = texts.t(
        'ADD_WL_TRAFFIC_PROMPT',
        ('🌍 <b>Добавить БС-трафик к подписке</b>\n\nТекущий лимит: {current_traffic}\nВыберите дополнительный трафик:'),
    ).format(current_traffic=texts.format_traffic(current_traffic))

    if tariff_wl_packages:
        # Используем per-tariff пакеты (клавиатура по стилю tariff)
        markup = get_add_wl_traffic_keyboard_from_tariff(
            db_user.language,
            tariff_wl_packages,
            subscription.end_date,
            traffic_discount_percent,
        )
    else:
        # Глобальные пакеты из settings
        markup = get_add_wl_traffic_keyboard(
            db_user.language,
            subscription.end_date,
            traffic_discount_percent,
        )

    await callback.message.edit_text(prompt_text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


def _calculate_traffic_reset_price(subscription) -> int:
    """Рассчитывает цену сброса трафика в зависимости от настроек."""
    mode = settings.get_traffic_reset_price_mode()
    base_price = settings.get_traffic_reset_base_price()

    # Если базовая цена не задана, используем цену периода 30 дней
    if base_price == 0:
        base_price = PERIOD_PRICES.get(30, 0)

    if mode == 'period':
        # Старое поведение: фиксированная цена = стоимость периода
        return base_price

    if mode == 'traffic':
        # Цена = стоимость текущего пакета трафика
        traffic_price = settings.get_wl_traffic_price(subscription.wl_traffic_limit_gb)
        return max(traffic_price, base_price)

    if mode == 'traffic_with_purchased':
        # Цена = стоимость базового трафика + докупленного
        # Базовый трафик = текущий лимит - докупленный
        purchased_gb = getattr(subscription, 'wl_purchased_traffic_gb', 0) or 0
        base_traffic_gb = subscription.wl_traffic_limit_gb - purchased_gb

        # Получаем цену базового трафика
        base_traffic_price = settings.get_wl_traffic_price(base_traffic_gb) if base_traffic_gb > 0 else 0

        # Получаем цену докупленного трафика
        purchased_traffic_price = settings.get_wl_traffic_price(purchased_gb) if purchased_gb > 0 else 0

        total_price = base_traffic_price + purchased_traffic_price
        return max(total_price, base_price)

    # Fallback на базовую цену
    return base_price


async def handle_reset_wl_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        await callback.answer('⚠️ В текущем режиме трафик фиксированный и не может быть сброшен', show_alert=True)
        return

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription or subscription.is_trial:
        await callback.answer('⌛ Эта функция доступна только для платных подписок', show_alert=True)
        return

    if subscription.wl_traffic_limit_gb == 0:
        await callback.answer('⌛ У вас безлимитный трафик', show_alert=True)
        return

    reset_price = _calculate_traffic_reset_price(subscription)

    # Формируем информацию о расчете цены
    purchased_gb = getattr(subscription, 'wl_purchased_traffic_gb', 0) or 0
    price_info = ''
    if purchased_gb > 0 and settings.get_traffic_reset_price_mode() == 'traffic_with_purchased':
        base_traffic_gb = subscription.wl_traffic_limit_gb - purchased_gb
        price_info = (
            f'\n\n💡 <i>Расчет цены:</i>\n'
            f'• Базовый трафик: {texts.format_traffic(base_traffic_gb)}\n'
            f'• Докупленный: {texts.format_traffic(purchased_gb)}'
        )

    # Проверяем достаточно ли средств
    has_enough_balance = db_user.balance_kopeks >= reset_price
    missing_kopeks = max(0, reset_price - db_user.balance_kopeks)

    # Формируем текст о балансе
    balance_info = f'\n\n💰 На балансе: {texts.format_price(db_user.balance_kopeks)}'
    if not has_enough_balance:
        balance_info += f'\n⚠️ Не хватает: {texts.format_price(missing_kopeks)}'

    await callback.message.edit_text(
        f'🔄 <b>Сброс трафика</b>\n\n'
        f'Использовано: {texts.format_traffic(subscription.wl_traffic_used_gb, is_limit=False)}\n'
        f'Лимит: {texts.format_traffic(subscription.wl_traffic_limit_gb)}\n\n'
        f'Стоимость сброса: {texts.format_price(reset_price)}{price_info}{balance_info}\n\n'
        'После сброса счетчик использованного трафика станет равным 0.',
        reply_markup=get_reset_wl_traffic_confirm_keyboard(
            reset_price,
            db_user.language,
            has_enough_balance=has_enough_balance,
            missing_kopeks=missing_kopeks,
        ),
    )

    await callback.answer()


async def confirm_reset_wl_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        await callback.answer('⚠️ В текущем режиме трафик фиксированный', show_alert=True)
        return

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    reset_price = _calculate_traffic_reset_price(subscription)

    if db_user.balance_kopeks < reset_price:
        missing_kopeks = reset_price - db_user.balance_kopeks
        message_text = texts.t(
            'ADDON_INSUFFICIENT_FUNDS_MESSAGE',
            (
                '⚠️ <b>Недостаточно средств</b>\n\n'
                'Стоимость услуги: {required}\n'
                'На балансе: {balance}\n'
                'Не хватает: {missing}\n\n'
                'Выберите способ пополнения. Сумма подставится автоматически.'
            ),
        ).format(
            required=texts.format_price(reset_price),
            balance=texts.format_price(db_user.balance_kopeks),
            missing=texts.format_price(missing_kopeks),
        )

        await callback.message.edit_text(
            message_text,
            reply_markup=get_insufficient_balance_keyboard(
                db_user.language,
                amount_kopeks=missing_kopeks,
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    try:
        success = await subtract_user_balance(db, db_user, reset_price, 'Сброс трафика')

        if not success:
            await callback.answer('⌛ Ошибка списания средств', show_alert=True)
            return

        subscription.wl_traffic_used_gb = 0.0
        subscription.updated_at = datetime.now(UTC)
        await db.commit()

        remnawave_service = RemnaWaveService()

        user = db_user
        if user.remnawave_uuid:
            async with remnawave_service.get_api_client() as api:
                # Получаем UUID _wl пользователя из панели для сброса его трафика
                try:
                    from app.config import settings as cfg
                    username = cfg.format_remnawave_username(
                        full_name=user.full_name,
                        username=user.username,
                        telegram_id=user.telegram_id,
                        email=user.email,
                        user_id=user.id,
                    )
                    username_wl = f"{username[:33]}_wl"
                    wl_user = await api.get_user_by_username(username_wl)
                    if wl_user and wl_user.uuid:
                        await api.reset_user_traffic(wl_user.uuid)
                        logger.info('🔄 Сброшен трафик _wl пользователя', username_wl=username_wl)
                except Exception as wl_err:
                    logger.warning('⚠️ Не удалось сбросить трафик _wl пользователя', error=wl_err)

        await create_transaction(
            db=db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=reset_price,
            description='Сброс трафика',
        )

        await db.refresh(db_user)
        await db.refresh(subscription)

        await callback.message.edit_text(
            f'✅ БС Трафик успешно сброшен!\n\n'
            f'🔄 Использованный трафик обнулен\n'
            f'📊 Лимит: {texts.format_traffic(subscription.wl_traffic_limit_gb)}',
            reply_markup=get_back_keyboard(db_user.language),
        )

        logger.info('✅ Пользователь сбросил трафик', telegram_id=db_user.telegram_id)

    except Exception as e:
        logger.error('Ошибка сброса трафика', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()


async def refresh_traffic_config():
    try:
        from app.config import refresh_traffic_prices

        refresh_traffic_prices()

        packages = settings.get_wl_traffic_packages()
        enabled_count = sum(1 for pkg in packages if pkg['enabled'])

        logger.info('🔄 Конфигурация трафика обновлена: активных пакетов', enabled_count=enabled_count)
        for pkg in packages:
            if pkg['enabled']:
                gb_text = '♾️ Безлимит' if pkg['gb'] == 0 else f'{pkg["gb"]} ГБ'
                logger.info('📦 ₽', gb_text=gb_text, pkg=pkg['price'] / 100)

        return True

    except Exception as e:
        logger.error('⚠️ Ошибка обновления конфигурации трафика', error=e)
        return False


async def get_traffic_packages_info() -> str:
    try:
        packages = settings.get_wl_traffic_packages()

        info_lines = ['📦 Настроенные пакеты трафика:']

        enabled_packages = [pkg for pkg in packages if pkg['enabled']]
        disabled_packages = [pkg for pkg in packages if not pkg['enabled']]

        if enabled_packages:
            info_lines.append('\n✅ Активные:')
            for pkg in enabled_packages:
                gb_text = '♾️ Безлимит' if pkg['gb'] == 0 else f'{pkg["gb"]} ГБ'
                info_lines.append(f'   • {gb_text}: {pkg["price"] // 100}₽')

        if disabled_packages:
            info_lines.append('\n❌ Отключенные:')
            for pkg in disabled_packages:
                gb_text = '♾️ Безлимит' if pkg['gb'] == 0 else f'{pkg["gb"]} ГБ'
                info_lines.append(f'   • {gb_text}: {pkg["price"] // 100}₽')

        info_lines.append(f'\n📊 Всего пакетов: {len(packages)}')
        info_lines.append(f'🟢 Активных: {len(enabled_packages)}')
        info_lines.append(f'🔴 Отключенных: {len(disabled_packages)}')

        return '\n'.join(info_lines)

    except Exception as e:
        return f'⚠️ Ошибка получения информации: {e}'


async def select_wl_traffic(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    traffic_gb = int(callback.data.split('_')[3])
    texts = get_texts(db_user.language)

    data = await state.get_data()
    data['traffic_gb'] = traffic_gb

    traffic_price = settings.get_wl_traffic_price(traffic_gb)
    data['total_price'] += traffic_price

    await state.set_data(data)

    countries = await _get_available_countries(db_user.promo_group_id)
    available_countries = [c for c in countries if c.get('is_available', True)]
    data['countries'] = [available_countries[0]['uuid']] if available_countries else []
    await state.set_data(data)

    if settings.is_devices_selection_enabled():
        selected_devices = data.get('devices', settings.DEFAULT_DEVICE_LIMIT)

        await callback.message.edit_text(
            texts.SELECT_DEVICES, reply_markup=get_devices_keyboard(selected_devices, db_user.language)
        )
        await state.set_state(SubscriptionStates.selecting_devices)
        await callback.answer()
        return

    if await present_subscription_summary(callback, state, db_user, texts):
        await callback.answer()


async def add_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.database.crud.tariff import get_tariff_by_id

    traffic_gb = int(callback.data.split('_')[4])
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    # Получаем цену: из тарифа (WL-пакеты) или из глобальных настроек
    base_price = 0
    tariff = None

    if settings.is_tariffs_mode() and subscription and subscription.tariff_id:
        # Режим тарифов - берем цену из WL-пакетов тарифа
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff and tariff.can_topup_wl_traffic():
            base_price = tariff.get_wl_traffic_topup_price(traffic_gb) or 0
            if base_price == 0 and traffic_gb != 0:
                # Пакет не задан в тарифе — откат к глобальным ценам
                base_price = settings.get_wl_traffic_topup_price(traffic_gb)
        elif not settings.is_traffic_topup_blocked():
            # Нет WL-пакетов в тарифе — используем глобальные цены
            base_price = settings.get_wl_traffic_topup_price(traffic_gb)
        else:
            await callback.answer('⚠️ На вашем тарифе докупка БС-трафика недоступна', show_alert=True)
            return
    else:
        # Стандартный режим
        if settings.is_traffic_topup_blocked():
            await callback.answer('⚠️ В текущем режиме трафик фиксированный', show_alert=True)
            return
        base_price = settings.get_wl_traffic_topup_price(traffic_gb)

    if base_price == 0 and traffic_gb != 0:
        await callback.answer('⚠️ Цена для этого пакета не настроена', show_alert=True)
        return

    period_hint_days = _get_period_hint_from_subscription(subscription)
    discount_result = _apply_addon_discount(
        db_user,
        'traffic',
        base_price,
        period_hint_days,
    )

    discounted_per_month = discount_result['discounted']
    discount_per_month = discount_result['discount']
    charged_months = 1

    # На тарифах пакеты трафика покупаются на 1 месяц (30 дней),
    # цена в тарифе уже месячная — не умножаем на оставшиеся месяцы подписки.
    # Пропорциональный расчёт применяем только в классическом режиме.
    is_tariff_mode = settings.is_tariffs_mode() and subscription and subscription.tariff_id

    if is_tariff_mode:
        price = discounted_per_month
    elif subscription:
        price, charged_months = calculate_prorated_price(
            discounted_per_month,
            subscription.end_date,
        )
    else:
        price = discounted_per_month

    total_discount_value = discount_per_month * charged_months

    if db_user.balance_kopeks < price:
        missing_kopeks = price - db_user.balance_kopeks

        # Save cart for auto-purchase after balance top-up
        cart_data = {
            'cart_mode': 'add_wl_traffic',
            'subscription_id': subscription.id,
            'traffic_gb': traffic_gb,
            'price_kopeks': price,
            'base_price_kopeks': discounted_per_month,
            'discount_percent': discount_result['percent'],
            'source': 'bot',
            'description': f'Докупка {traffic_gb} ГБ трафика',
        }
        try:
            await user_cart_service.save_user_cart(db_user.id, cart_data)
            logger.info(
                'Cart saved for traffic purchase (bot) user +', telegram_id=db_user.telegram_id, traffic_gb=traffic_gb
            )
        except Exception as e:
            logger.error('Error saving cart for traffic purchase (bot)', error=e)

        message_text = texts.t(
            'ADDON_INSUFFICIENT_FUNDS_MESSAGE',
            (
                '⚠️ <b>Недостаточно средств</b>\n\n'
                'Стоимость услуги: {required}\n'
                'На балансе: {balance}\n'
                'Не хватает: {missing}\n\n'
                'Выберите способ пополнения. Сумма подставится автоматически.'
            ),
        ).format(
            required=texts.format_price(price),
            balance=texts.format_price(db_user.balance_kopeks),
            missing=texts.format_price(missing_kopeks),
        )

        await callback.message.edit_text(
            message_text,
            reply_markup=get_insufficient_balance_keyboard(
                db_user.language,
                amount_kopeks=missing_kopeks,
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Сохраняем старое значение трафика для уведомления
    old_traffic_limit = subscription.wl_traffic_limit_gb

    try:
        success = await subtract_user_balance(
            db,
            db_user,
            price,
            f'Добавление {traffic_gb} ГБ трафика',
        )

        if not success:
            await callback.answer('⚠️ Ошибка списания средств', show_alert=True)
            return

        if traffic_gb == 0:
            subscription.wl_traffic_limit_gb = 0
            # При переходе на безлимит сбрасываем все докупки
            from sqlalchemy import delete

            from app.database.models import WlTrafficPurchase

            await db.execute(delete(WlTrafficPurchase).where(WlTrafficPurchase.subscription_id == subscription.id))
            subscription.wl_purchased_traffic_gb = 0
            subscription.wl_traffic_reset_at = None
        else:
            # add_subscription_wl_traffic уже создаёт WlTrafficPurchase и обновляет все необходимые поля
            await add_subscription_wl_traffic(db, subscription, traffic_gb)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        await create_transaction(
            db=db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price,
            description=f'Добавление {traffic_gb} ГБ трафика',
        )

        await db.refresh(db_user)
        await db.refresh(subscription)

        # Удаляем корзину — покупка выполнена вручную, авто-покупка не нужна
        try:
            await user_cart_service.delete_user_cart(db_user.id)
        except Exception as e:
            logger.warning('Не удалось удалить корзину после покупки WL-трафика', error=e)

        # Отправляем уведомление админам о докупке трафика
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_update_notification(
                db, db_user, subscription, 'traffic', old_traffic_limit, subscription.wl_traffic_limit_gb, price
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления о докупке трафика', error=e)

        success_text = '✅ Трафик успешно добавлен!\n\n'
        if traffic_gb == 0:
            success_text += '🎉 Теперь у вас безлимитный трафик!'
        else:
            success_text += f'📈 Добавлено: {traffic_gb} ГБ\n'
            success_text += f'Новый лимит: {texts.format_traffic(subscription.wl_traffic_limit_gb)}'

        if price > 0:
            success_text += f'\n💰 Списано: {texts.format_price(price)}'
            if total_discount_value > 0:
                success_text += f' (скидка {discount_result["percent"]}%: -{texts.format_price(total_discount_value)})'

        await callback.message.edit_text(success_text, reply_markup=get_back_keyboard(db_user.language))

        logger.info('✅ Пользователь добавил ГБ трафика', telegram_id=db_user.telegram_id, traffic_gb=traffic_gb)

    except Exception as e:
        logger.error('Ошибка добавления трафика', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()


async def handle_no_wl_traffic_packages(callback: types.CallbackQuery, db_user: User):
    await callback.answer(
        '⚠️ В данный момент нет доступных пакетов трафика. Обратитесь в техподдержку для получения информации.',
        show_alert=True,
    )


async def handle_switch_wl_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        await callback.answer('⚠️ В текущем режиме трафик фиксированный', show_alert=True)
        return

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription or subscription.is_trial:
        await callback.answer('⚠️ Эта функция доступна только для платных подписок', show_alert=True)
        return

    current_traffic = subscription.wl_traffic_limit_gb
    # Вычисляем базовый трафик (без докупленного) для корректного расчёта цен
    purchased_traffic = getattr(subscription, 'wl_purchased_traffic_gb', 0) or 0
    base_traffic = current_traffic - purchased_traffic

    period_hint_days = _get_period_hint_from_subscription(subscription)
    traffic_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        'traffic',
        period_hint_days,
    )

    # Показываем информацию о докупленном трафике, если он есть
    purchased_info = ''
    if purchased_traffic > 0:
        purchased_info = f'\n📦 Базовый пакет: {texts.format_traffic(base_traffic)}\n➕ Докуплено: {texts.format_traffic(purchased_traffic)}'

    await callback.message.edit_text(
        f'🔄 <b>Переключение лимита трафика</b>\n\n'
        f'Текущий лимит: {texts.format_traffic(current_traffic)}{purchased_info}\n'
        f'Выберите новый лимит трафика:\n\n'
        f'💡 <b>Важно:</b>\n'
        f'• При увеличении - доплата за разницу\n'
        f'• При уменьшении - возврат средств не производится\n'
        f'• Докупленный трафик будет сброшен',
        reply_markup=get_wl_traffic_switch_keyboard(
            current_traffic,
            db_user.language,
            subscription.end_date,
            traffic_discount_percent,
            base_traffic_gb=base_traffic,
        ),
        parse_mode='HTML',
    )

    await callback.answer()


async def confirm_switch_wl_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    new_traffic_gb = int(callback.data.split('_')[4])
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    current_traffic = subscription.wl_traffic_limit_gb

    # Вычисляем базовый трафик (без докупленного) для корректного расчёта цены
    purchased_traffic = getattr(subscription, 'wl_purchased_traffic_gb', 0) or 0
    base_traffic = current_traffic - purchased_traffic

    if new_traffic_gb == current_traffic:
        await callback.answer('ℹ️ Лимит трафика не изменился', show_alert=True)
        return

    # Используем базовый трафик для определения текущей цены пакета
    old_price_per_month = settings.get_wl_traffic_price(base_traffic)
    new_price_per_month = settings.get_wl_traffic_price(new_traffic_gb)

    months_remaining = get_remaining_months(subscription.end_date)
    period_hint_days = months_remaining * 30 if months_remaining > 0 else None
    traffic_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        'traffic',
        period_hint_days,
    )

    discounted_old_per_month, _ = apply_percentage_discount(
        old_price_per_month,
        traffic_discount_percent,
    )
    discounted_new_per_month, _ = apply_percentage_discount(
        new_price_per_month,
        traffic_discount_percent,
    )
    price_difference_per_month = discounted_new_per_month - discounted_old_per_month
    discount_savings_per_month = (new_price_per_month - old_price_per_month) - price_difference_per_month

    if price_difference_per_month > 0:
        total_price_difference = price_difference_per_month * months_remaining

        if db_user.balance_kopeks < total_price_difference:
            missing_kopeks = total_price_difference - db_user.balance_kopeks
            message_text = texts.t(
                'ADDON_INSUFFICIENT_FUNDS_MESSAGE',
                (
                    '⚠️ <b>Недостаточно средств</b>\n\n'
                    'Стоимость услуги: {required}\n'
                    'На балансе: {balance}\n'
                    'Не хватает: {missing}\n\n'
                    'Выберите способ пополнения. Сумма подставится автоматически.'
                ),
            ).format(
                required=f'{texts.format_price(total_price_difference)} (за {months_remaining} мес)',
                balance=texts.format_price(db_user.balance_kopeks),
                missing=texts.format_price(missing_kopeks),
            )

            await callback.message.edit_text(
                message_text,
                reply_markup=get_insufficient_balance_keyboard(
                    db_user.language,
                    amount_kopeks=missing_kopeks,
                ),
                parse_mode='HTML',
            )
            await callback.answer()
            return

        action_text = f'увеличить до {texts.format_traffic(new_traffic_gb)}'
        cost_text = f'Доплата: {texts.format_price(total_price_difference)} (за {months_remaining} мес)'
        if discount_savings_per_month > 0:
            total_discount_savings = discount_savings_per_month * months_remaining
            cost_text += f' (скидка {traffic_discount_percent}%: -{texts.format_price(total_discount_savings)})'
    else:
        total_price_difference = 0
        action_text = f'уменьшить до {texts.format_traffic(new_traffic_gb)}'
        cost_text = 'Возврат средств не производится'

    confirm_text = '🔄 <b>Подтверждение переключения трафика</b>\n\n'
    confirm_text += f'Текущий лимит: {texts.format_traffic(current_traffic)}\n'
    confirm_text += f'Новый лимит: {texts.format_traffic(new_traffic_gb)}\n\n'
    confirm_text += f'Действие: {action_text}\n'
    confirm_text += f'💰 {cost_text}\n\n'
    confirm_text += 'Подтвердить переключение?'

    await callback.message.edit_text(
        confirm_text,
        reply_markup=get_confirm_switch_wl_traffic_keyboard(new_traffic_gb, total_price_difference, db_user.language),
        parse_mode='HTML',
    )

    await callback.answer()


async def execute_switch_wl_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    callback_parts = callback.data.split('_')
    new_traffic_gb = int(callback_parts[5])
    price_difference = int(callback_parts[6])

    texts = get_texts(db_user.language)
    subscription = db_user.subscription
    current_traffic = subscription.wl_traffic_limit_gb

    try:
        if price_difference > 0:
            success = await subtract_user_balance(
                db, db_user, price_difference, f'Переключение трафика с {current_traffic}GB на {new_traffic_gb}GB'
            )

            if not success:
                await callback.answer('⚠️ Ошибка списания средств', show_alert=True)
                return

            months_remaining = get_remaining_months(subscription.end_date)
            await create_transaction(
                db=db,
                user_id=db_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=price_difference,
                description=f'Переключение трафика с {current_traffic}GB на {new_traffic_gb}GB на {months_remaining} мес',
            )

        subscription.wl_traffic_limit_gb = new_traffic_gb
        # Сбрасываем все докупки трафика при переключении пакета
        from sqlalchemy import delete

        from app.database.models import WlTrafficPurchase

        await db.execute(delete(WlTrafficPurchase).where(WlTrafficPurchase.subscription_id == subscription.id))
        subscription.wl_purchased_traffic_gb = 0
        subscription.wl_traffic_reset_at = None  # Сбрасываем дату сброса трафика
        subscription.updated_at = datetime.now(UTC)

        await db.commit()

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        await db.refresh(db_user)
        await db.refresh(subscription)

        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_update_notification(
                db, db_user, subscription, 'traffic', current_traffic, new_traffic_gb, price_difference
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления об изменении трафика', error=e)

        if new_traffic_gb > current_traffic:
            success_text = '✅ Лимит трафика увеличен!\n\n'
            success_text += f'📊 Было: {texts.format_traffic(current_traffic)} → '
            success_text += f'Стало: {texts.format_traffic(new_traffic_gb)}\n'
            if price_difference > 0:
                success_text += f'💰 Списано: {texts.format_price(price_difference)}'
        elif new_traffic_gb < current_traffic:
            success_text = '✅ Лимит трафика уменьшен!\n\n'
            success_text += f'📊 Было: {texts.format_traffic(current_traffic)} → '
            success_text += f'Стало: {texts.format_traffic(new_traffic_gb)}\n'
            success_text += 'ℹ️ Возврат средств не производится'

        await callback.message.edit_text(success_text, reply_markup=get_back_keyboard(db_user.language))

        logger.info(
            '✅ Пользователь переключил трафик с на доплата: ₽',
            telegram_id=db_user.telegram_id,
            current_traffic=current_traffic,
            new_traffic_gb=new_traffic_gb,
            price_difference=price_difference / 100,
        )

    except Exception as e:
        logger.error('Ошибка переключения трафика', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()
