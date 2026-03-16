from datetime import UTC, datetime

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.crud.subscription import add_subscription_traffic, reactivate_subscription
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import TransactionType, User
from app.keyboards.inline import (
    get_add_traffic_keyboard,
    get_add_traffic_keyboard_from_tariff,
    get_back_keyboard,
    get_countries_keyboard,
    get_devices_keyboard,
    get_insufficient_balance_keyboard,
    get_reset_traffic_confirm_keyboard,
)
from app.localization.texts import get_texts
from app.services.pricing_engine import PricingEngine
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service
from app.states import SubscriptionStates
from app.utils.pricing_utils import (
    calculate_prorated_price,
)

from .common import (
    _get_period_hint_from_subscription,
    get_confirm_switch_traffic_keyboard,
    get_traffic_switch_keyboard,
    logger,
)
from .countries import (
    _build_countries_selection_text,
    _get_available_countries,
    _get_preselected_free_countries,
    _should_show_countries_management,
)
from .summary import present_subscription_summary


async def handle_add_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
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

    if subscription.traffic_limit_gb == 0:
        await callback.answer(
            texts.t('TRAFFIC_ALREADY_UNLIMITED', '⚠ У вас уже безлимитный трафик'),
            show_alert=True,
        )
        return

    # Режим тарифов - проверяем настройки тарифа
    if settings.is_tariffs_mode() and subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if not tariff or not tariff.can_topup_traffic():
            await callback.answer(
                texts.t(
                    'TARIFF_TRAFFIC_TOPUP_DISABLED',
                    '⚠️ На вашем тарифе докупка трафика недоступна',
                ),
                show_alert=True,
            )
            return

        # Показываем пакеты из тарифа
        current_traffic = subscription.traffic_limit_gb
        packages = tariff.get_traffic_topup_packages()

        period_hint_days = _get_period_hint_from_subscription(subscription)
        traffic_discount_percent = PricingEngine.get_addon_discount_percent(
            db_user,
            'traffic',
            period_hint_days,
        )

        prompt_text = texts.t(
            'ADD_TRAFFIC_PROMPT',
            (
                '📈 <b>Добавить трафик к подписке</b>\n\n'
                'Текущий лимит: {current_traffic}\n'
                'Выберите дополнительный трафик:'
            ),
        ).format(current_traffic=texts.format_traffic(current_traffic))

        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_add_traffic_keyboard_from_tariff(
                db_user.language,
                packages,
                subscription.end_date,
                traffic_discount_percent,
            ),
            parse_mode='HTML',
        )

        await callback.answer()
        return

    # Стандартный режим - проверяем глобальные настройки
    if not settings.is_traffic_topup_enabled():
        await callback.answer(
            texts.t(
                'TRAFFIC_TOPUP_DISABLED',
                '⚠️ Функция докупки трафика отключена',
            ),
            show_alert=True,
        )
        return

    if settings.is_traffic_topup_blocked():
        await callback.answer(
            texts.t(
                'TRAFFIC_FIXED_MODE',
                '⚠️ В текущем режиме трафик фиксированный и не может быть изменен',
            ),
            show_alert=True,
        )
        return

    current_traffic = subscription.traffic_limit_gb
    period_hint_days = _get_period_hint_from_subscription(subscription)
    traffic_discount_percent = PricingEngine.get_addon_discount_percent(
        db_user,
        'traffic',
        period_hint_days,
    )

    prompt_text = texts.t(
        'ADD_TRAFFIC_PROMPT',
        ('📈 <b>Добавить трафик к подписке</b>\n\nТекущий лимит: {current_traffic}\nВыберите дополнительный трафик:'),
    ).format(current_traffic=texts.format_traffic(current_traffic))

    await callback.message.edit_text(
        prompt_text,
        reply_markup=get_add_traffic_keyboard(
            db_user.language,
            subscription.end_date,
            traffic_discount_percent,
        ),
        parse_mode='HTML',
    )

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
        traffic_price = settings.get_traffic_price(subscription.traffic_limit_gb)
        return max(traffic_price, base_price)

    if mode == 'traffic_with_purchased':
        # Цена = стоимость базового трафика + докупленного
        # Базовый трафик = текущий лимит - докупленный
        purchased_gb = getattr(subscription, 'purchased_traffic_gb', 0) or 0
        base_traffic_gb = subscription.traffic_limit_gb - purchased_gb

        # Получаем цену базового трафика
        base_traffic_price = settings.get_traffic_price(base_traffic_gb) if base_traffic_gb > 0 else 0

        # Получаем цену докупленного трафика
        purchased_traffic_price = settings.get_traffic_price(purchased_gb) if purchased_gb > 0 else 0

        total_price = base_traffic_price + purchased_traffic_price
        return max(total_price, base_price)

    # Fallback на базовую цену
    return base_price


async def handle_reset_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        await callback.answer('⚠️ В текущем режиме трафик фиксированный и не может быть сброшен', show_alert=True)
        return

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription or subscription.is_trial:
        await callback.answer('⌛ Эта функция доступна только для платных подписок', show_alert=True)
        return

    if subscription.traffic_limit_gb == 0:
        await callback.answer('⌛ У вас безлимитный трафик', show_alert=True)
        return

    reset_price = _calculate_traffic_reset_price(subscription)

    # Формируем информацию о расчете цены
    purchased_gb = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    price_info = ''
    if purchased_gb > 0 and settings.get_traffic_reset_price_mode() == 'traffic_with_purchased':
        base_traffic_gb = subscription.traffic_limit_gb - purchased_gb
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
        f'Использовано: {texts.format_traffic(subscription.traffic_used_gb, is_limit=False)}\n'
        f'Лимит: {texts.format_traffic(subscription.traffic_limit_gb)}\n\n'
        f'Стоимость сброса: {texts.format_price(reset_price)}{price_info}{balance_info}\n\n'
        'После сброса счетчик использованного трафика станет равным 0.',
        reply_markup=get_reset_traffic_confirm_keyboard(
            reset_price,
            db_user.language,
            has_enough_balance=has_enough_balance,
            missing_kopeks=missing_kopeks,
        ),
    )

    await callback.answer()


async def confirm_reset_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        await callback.answer('⚠️ В текущем режиме трафик фиксированный', show_alert=True)
        return

    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

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

        subscription.traffic_used_gb = 0.0
        subscription.updated_at = datetime.now(UTC)
        await db.commit()

        SubscriptionService()
        remnawave_service = RemnaWaveService()

        user = db_user
        if user.remnawave_uuid:
            async with remnawave_service.get_api_client() as api:
                await api.reset_user_traffic(user.remnawave_uuid)

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
            f'✅ Трафик успешно сброшен!\n\n'
            f'🔄 Использованный трафик обнулен\n'
            f'📊 Лимит: {texts.format_traffic(subscription.traffic_limit_gb)}',
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

        packages = settings.get_traffic_packages()
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
        packages = settings.get_traffic_packages()

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


async def select_traffic(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    traffic_gb = int(callback.data.split('_')[1])
    texts = get_texts(db_user.language)

    data = await state.get_data()
    data['traffic_gb'] = traffic_gb

    traffic_price = settings.get_traffic_price(traffic_gb)
    data['total_price'] += traffic_price

    await state.set_data(data)

    if await _should_show_countries_management(db_user):
        countries = await _get_available_countries(db_user.promo_group_id)
        # Автоматически предвыбираем бесплатные серверы
        preselected = _get_preselected_free_countries(countries)
        data['countries'] = preselected
        await state.set_data(data)
        # Формируем текст с описаниями сквадов
        selection_text = _build_countries_selection_text(countries, texts.SELECT_COUNTRIES)
        await callback.message.edit_text(
            selection_text,
            reply_markup=get_countries_keyboard(countries, preselected, db_user.language),
            parse_mode='HTML',
        )
        await state.set_state(SubscriptionStates.selecting_countries)
        await callback.answer()
        return

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

    traffic_gb = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    # Получаем цену: из тарифа или из глобальных настроек
    base_price = 0
    tariff = None

    if settings.is_tariffs_mode() and subscription and subscription.tariff_id:
        # Режим тарифов - берем цену из тарифа
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff and tariff.can_topup_traffic():
            base_price = tariff.get_traffic_topup_price(traffic_gb) or 0
        else:
            await callback.answer('⚠️ На вашем тарифе докупка трафика недоступна', show_alert=True)
            return
    else:
        # Стандартный режим
        if settings.is_traffic_topup_blocked():
            await callback.answer('⚠️ В текущем режиме трафик фиксированный', show_alert=True)
            return
        base_price = settings.get_traffic_topup_price(traffic_gb)

    if base_price == 0 and traffic_gb != 0:
        await callback.answer('⚠️ Цена для этого пакета не настроена', show_alert=True)
        return

    # Lock user BEFORE price computation to prevent TOCTOU on group discount
    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)
    subscription = db_user.subscription

    period_hint_days = _get_period_hint_from_subscription(subscription)
    discounted_per_month, discount_per_month, traffic_discount_pct = PricingEngine.calculate_traffic_discount(
        base_price,
        db_user,
        period_hint_days,
    )
    charged_days = 30

    # На тарифах пакеты трафика покупаются на 1 месяц (30 дней),
    # цена в тарифе уже месячная — не умножаем на оставшиеся месяцы подписки.
    # Пропорциональный расчёт применяем только в классическом режиме.
    is_tariff_mode = settings.is_tariffs_mode() and subscription and subscription.tariff_id

    if is_tariff_mode:
        price = discounted_per_month
    elif subscription:
        price, charged_days = calculate_prorated_price(
            discounted_per_month,
            subscription.end_date,
        )
    else:
        price = discounted_per_month

    total_discount_value = int(discount_per_month * charged_days / 30)

    if db_user.balance_kopeks < price:
        missing_kopeks = price - db_user.balance_kopeks

        # Save cart for auto-purchase after balance top-up
        cart_data = {
            'cart_mode': 'add_traffic',
            'subscription_id': subscription.id,
            'traffic_gb': traffic_gb,
            'price_kopeks': price,
            'base_price_kopeks': discounted_per_month,
            'discount_percent': traffic_discount_pct,
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
    old_traffic_limit = subscription.traffic_limit_gb

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
            subscription.traffic_limit_gb = 0
            # При переходе на безлимит сбрасываем все докупки
            from sqlalchemy import delete

            from app.database.models import TrafficPurchase

            await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
            subscription.purchased_traffic_gb = 0
            subscription.traffic_reset_at = None
        else:
            # add_subscription_traffic уже создаёт TrafficPurchase и обновляет все необходимые поля
            await add_subscription_traffic(db, subscription, traffic_gb)

        # Реактивируем подписку если она была DISABLED/EXPIRED (например, после LIMITED/EXPIRED в RemnaWave)
        await reactivate_subscription(db, subscription)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        # Явно включаем пользователя на панели (PATCH может не снять LIMITED-статус)
        if db_user.remnawave_uuid and subscription.status == 'active':
            await subscription_service.enable_remnawave_user(db_user.remnawave_uuid)

        await create_transaction(
            db=db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price,
            description=f'Добавление {traffic_gb} ГБ трафика',
        )

        await db.refresh(db_user)
        await db.refresh(subscription)

        # Отправляем уведомление админам о докупке трафика
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_update_notification(
                db, db_user, subscription, 'traffic', old_traffic_limit, subscription.traffic_limit_gb, price
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления о докупке трафика', error=e)

        success_text = '✅ Трафик успешно добавлен!\n\n'
        if traffic_gb == 0:
            success_text += '🎉 Теперь у вас безлимитный трафик!'
        else:
            success_text += f'📈 Добавлено: {traffic_gb} ГБ\n'
            success_text += f'Новый лимит: {texts.format_traffic(subscription.traffic_limit_gb)}'

        if price > 0:
            success_text += f'\n💰 Списано: {texts.format_price(price)}'
            if total_discount_value > 0:
                success_text += f' (скидка {traffic_discount_pct}%: -{texts.format_price(total_discount_value)})'

        await callback.message.edit_text(success_text, reply_markup=get_back_keyboard(db_user.language))

        logger.info('✅ Пользователь добавил ГБ трафика', telegram_id=db_user.telegram_id, traffic_gb=traffic_gb)

    except Exception as e:
        logger.error('Ошибка добавления трафика', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()


async def handle_no_traffic_packages(callback: types.CallbackQuery, db_user: User):
    await callback.answer(
        '⚠️ В данный момент нет доступных пакетов трафика. Обратитесь в техподдержку для получения информации.',
        show_alert=True,
    )


async def handle_switch_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        await callback.answer('⚠️ В текущем режиме трафик фиксированный', show_alert=True)
        return

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription or subscription.is_trial:
        await callback.answer('⚠️ Эта функция доступна только для платных подписок', show_alert=True)
        return

    # Проверяем настройку тарифа
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff and not tariff.allow_traffic_topup:
            await callback.answer('⚠️ Для вашего тарифа переключение трафика недоступно', show_alert=True)
            return

    current_traffic = subscription.traffic_limit_gb
    # Вычисляем базовый трафик (без докупленного) для корректного расчёта цен
    purchased_traffic = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    base_traffic = current_traffic - purchased_traffic

    period_hint_days = _get_period_hint_from_subscription(subscription)
    traffic_discount_percent = PricingEngine.get_addon_discount_percent(
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
        reply_markup=get_traffic_switch_keyboard(
            current_traffic,
            db_user.language,
            subscription.end_date,
            traffic_discount_percent,
            base_traffic_gb=base_traffic,
        ),
        parse_mode='HTML',
    )

    await callback.answer()


async def confirm_switch_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    new_traffic_gb = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    current_traffic = subscription.traffic_limit_gb

    # Вычисляем базовый трафик (без докупленного) для корректного расчёта цены
    purchased_traffic = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    base_traffic = current_traffic - purchased_traffic

    if new_traffic_gb == current_traffic:
        await callback.answer('ℹ️ Лимит трафика не изменился', show_alert=True)
        return

    # Используем базовый трафик для определения текущей цены пакета
    old_price_per_month = settings.get_traffic_price(base_traffic)
    new_price_per_month = settings.get_traffic_price(new_traffic_gb)

    now = datetime.now(UTC)
    days_remaining = max(1, (subscription.end_date - now).days)
    period_hint_days = days_remaining if days_remaining > 0 else None
    traffic_discount_percent = PricingEngine.get_addon_discount_percent(
        db_user,
        'traffic',
        period_hint_days,
    )

    discounted_old_per_month = PricingEngine.apply_discount(
        old_price_per_month,
        traffic_discount_percent,
    )
    discounted_new_per_month = PricingEngine.apply_discount(
        new_price_per_month,
        traffic_discount_percent,
    )
    price_difference_per_month = discounted_new_per_month - discounted_old_per_month
    discount_savings_per_month = (new_price_per_month - old_price_per_month) - price_difference_per_month

    if price_difference_per_month > 0:
        total_price_difference = int(price_difference_per_month * days_remaining / 30)
        total_price_difference = max(100, total_price_difference)

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
                required=f'{texts.format_price(total_price_difference)} (за {days_remaining} дн.)',
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
        cost_text = f'Доплата: {texts.format_price(total_price_difference)} (за {days_remaining} дн.)'
        if discount_savings_per_month > 0:
            total_discount_savings = int(discount_savings_per_month * days_remaining / 30)
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
        reply_markup=get_confirm_switch_traffic_keyboard(new_traffic_gb, total_price_difference, db_user.language),
        parse_mode='HTML',
    )

    await callback.answer()


async def execute_switch_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    callback_parts = callback.data.split('_')
    new_traffic_gb = int(callback_parts[3])

    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    texts = get_texts(db_user.language)
    subscription = db_user.subscription
    current_traffic = subscription.traffic_limit_gb

    # Recompute price under lock (callback-baked value may be stale)
    purchased_traffic = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    base_traffic = current_traffic - purchased_traffic
    old_price_per_month = settings.get_traffic_price(base_traffic)
    new_price_per_month = settings.get_traffic_price(new_traffic_gb)
    days_remaining = max(1, (subscription.end_date - datetime.now(UTC)).days)
    traffic_discount_percent = PricingEngine.get_addon_discount_percent(
        db_user,
        'traffic',
        days_remaining,
    )
    discounted_old = PricingEngine.apply_discount(old_price_per_month, traffic_discount_percent)
    discounted_new = PricingEngine.apply_discount(new_price_per_month, traffic_discount_percent)
    price_diff_per_month = discounted_new - discounted_old
    if price_diff_per_month > 0:
        price_difference = int(price_diff_per_month * days_remaining / 30)
        price_difference = max(100, price_difference)
    else:
        price_difference = 0

    try:
        if price_difference > 0:
            success = await subtract_user_balance(
                db, db_user, price_difference, f'Переключение трафика с {current_traffic}GB на {new_traffic_gb}GB'
            )

            if not success:
                await callback.answer('⚠️ Ошибка списания средств', show_alert=True)
                return

            days_remaining = max(1, (subscription.end_date - datetime.now(UTC)).days)
            await create_transaction(
                db=db,
                user_id=db_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=price_difference,
                description=f'Переключение трафика с {current_traffic}GB на {new_traffic_gb}GB за {days_remaining} дн.',
            )

        subscription.traffic_limit_gb = new_traffic_gb
        # Сбрасываем все докупки трафика при переключении пакета
        from sqlalchemy import delete

        from app.database.models import TrafficPurchase

        await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
        subscription.purchased_traffic_gb = 0
        subscription.traffic_reset_at = None  # Сбрасываем дату сброса трафика
        subscription.updated_at = datetime.now(UTC)

        await db.commit()

        # Реактивируем подписку если она была DISABLED/EXPIRED (например, после LIMITED/EXPIRED в RemnaWave)
        await reactivate_subscription(db, subscription)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        # Явно включаем пользователя на панели (PATCH может не снять LIMITED-статус)
        if db_user.remnawave_uuid and subscription.status == 'active':
            await subscription_service.enable_remnawave_user(db_user.remnawave_uuid)

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
