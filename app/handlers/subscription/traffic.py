import math
from datetime import UTC, datetime

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.crud.subscription import reactivate_subscription
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
from app.services.traffic_purchase_service import add_subscription_traffic
from app.services.user_cart_service import user_cart_service
from app.states import SubscriptionStates
from app.utils.legacy_subscription import is_legacy_subscription
from app.utils.pricing_utils import (
    calculate_prorated_price,
)
from app.utils.subscription_time import local_days_until

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


async def _resolve_subscription(callback, db_user, db, state=None):
    """Resolve subscription — delegates to shared resolve_subscription_from_context."""
    from .common import resolve_subscription_from_context

    return await resolve_subscription_from_context(callback, db_user, db, state)


async def handle_add_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None):
    from app.config import settings
    from app.database.crud.subscription import get_active_subscriptions_by_user_id
    from app.database.crud.tariff import get_tariff_by_id

    texts = get_texts(db_user.language)

    # В режиме мульти-тарифов без явного sub_id в callback — показываем выбор подписки.
    if settings.is_multi_tariff_enabled() and callback.data == 'buy_traffic':
        active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
        if len(active_subs) > 1:
            from app.database.crud.tariff import get_tariff_by_id as _get_tariff

            keyboard = []
            for sub in sorted(active_subs, key=lambda s: s.id):
                if sub.is_trial:
                    continue
                tariff_name = ''
                if sub.tariff_id:
                    _t = await _get_tariff(db, sub.tariff_id)
                    tariff_name = _t.name if _t else f'#{sub.id}'
                else:
                    tariff_name = texts.t('SUBSCRIPTION_UI_NAME_ID', 'Подписка #{id}').format(id=sub.id)
                days_left = local_days_until(sub.end_date) if sub.end_date else 0
                keyboard.append(
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('SUBSCRIPTION_UI_TRAFFIC_DAYS_BUTTON', '📊 {traffic} ({days}д.)').format(
                                traffic=tariff_name, days=days_left
                            ),
                            callback_data=f'st:{sub.id}',
                        )
                    ]
                )
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_UI_BACK', '◀️ Назад'),
                        callback_data='back_to_menu',
                    )
                ]
            )
            await callback.message.edit_text(
                texts.t('SUBSCRIPTION_UI_TRAFFIC_SELECT', '📊 <b>Докупить трафик</b>\n\nВыберите подписку:'),
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            )
            await callback.answer()
            return

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t('PAID_FEATURE_ONLY', '⚠ Эта функция доступна только для платных подписок'),
            show_alert=True,
        )
        return

    if is_legacy_subscription(subscription):
        # Старая подписка (без тарифа при включённых тарифах): докупок нет, сперва переход на тариф.
        await callback.answer(
            texts.t(
                'LEGACY_ADDONS_UNAVAILABLE',
                '⚠️ Сначала перейдите на тариф — докупки для этой подписки недоступны',
            ),
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
                sub_id=sub_id,
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
            sub_id=sub_id,
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


async def handle_reset_traffic(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        await callback.answer(
            get_texts(getattr(db_user, 'language', None)).t(
                'SUBSCRIPTION_UI_FIXED_TRAFFIC_RESET', '⚠️ В текущем режиме трафик фиксированный и не может быть сброшен'
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_PAID_ONLY_HOURGLASS', '⌛ Эта функция доступна только для платных подписок'),
            show_alert=True,
        )
        return

    if subscription.traffic_limit_gb == 0:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_UNLIMITED_TRAFFIC', '⌛ У вас безлимитный трафик'),
            show_alert=True,
        )
        return

    reset_price = _calculate_traffic_reset_price(subscription)

    # Формируем информацию о расчете цены
    purchased_gb = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    price_info = ''
    if purchased_gb > 0 and settings.get_traffic_reset_price_mode() == 'traffic_with_purchased':
        base_traffic_gb = subscription.traffic_limit_gb - purchased_gb
        price_info = texts.t(
            'SUBSCRIPTION_UI_RESET_PRICE_BREAKDOWN',
            '\n\n💡 <i>Расчет цены:</i>\n• Базовый трафик: {base}\n• Докупленный: {extra}',
        ).format(base=texts.format_traffic(base_traffic_gb), extra=texts.format_traffic(purchased_gb))

    # Проверяем достаточно ли средств
    has_enough_balance = db_user.balance_kopeks >= reset_price
    missing_kopeks = max(0, reset_price - db_user.balance_kopeks)

    # Формируем текст о балансе
    balance_info = texts.t('SUBSCRIPTION_UI_BALANCE_LINE', '\n\n💰 На балансе: {balance}').format(
        balance=texts.format_price(db_user.balance_kopeks)
    )
    if not has_enough_balance:
        balance_info += texts.t('SUBSCRIPTION_UI_MISSING_LINE', '\n⚠️ Не хватает: {amount}').format(
            amount=texts.format_price(missing_kopeks)
        )

    reset_keyboard = get_reset_traffic_confirm_keyboard(
        reset_price,
        db_user.language,
        has_enough_balance=has_enough_balance,
        missing_kopeks=missing_kopeks,
    )
    if getattr(subscription, 'traffic_accounting_mode', None) == 'managed':
        from uuid import uuid4

        token = uuid4().hex[:24]
        for row in reset_keyboard.inline_keyboard:
            for button in row:
                if button.callback_data == 'confirm_reset_traffic':
                    button.callback_data = f'confirm_reset_traffic:{subscription.id}:{token}'

    await callback.message.edit_text(
        texts.t(
            'SUBSCRIPTION_UI_RESET_DETAILS',
            '🔄 <b>Сброс трафика</b>\n\nИспользовано: {used}\nЛимит: {limit}\n\nСтоимость сброса: {price}{discount}{breakdown}\n\n',
        ).format(
            used=texts.format_traffic(subscription.traffic_used_gb, is_limit=False),
            limit=texts.format_traffic(subscription.traffic_limit_gb),
            price=texts.format_price(reset_price),
            discount=price_info,
            breakdown=balance_info,
        )
        + (
            texts.t(
                'SUBSCRIPTION_UI_RESET_KEEP_EXTRA',
                'Базовый трафик будет обновлён. Остатки докупленных пакетов сохранятся.',
            )
            if getattr(subscription, 'traffic_accounting_mode', None) == 'managed'
            else texts.t(
                'SUBSCRIPTION_UI_RESET_USAGE_ZERO', 'После сброса счетчик использованного трафика станет равным 0.'
            )
        ),
        reply_markup=reset_keyboard,
    )

    await callback.answer()


async def confirm_reset_traffic(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        await callback.answer(
            get_texts(getattr(db_user, 'language', None)).t(
                'SUBSCRIPTION_UI_FIXED_TRAFFIC', '⚠️ В текущем режиме трафик фиксированный'
            ),
            show_alert=True,
        )
        return

    if settings.is_multi_tariff_enabled():
        _state_data = await state.get_data() if state else {}
        if not _state_data.get('active_subscription_id'):
            await callback.answer(
                get_texts(getattr(db_user, 'language', None)).t(
                    'SUBSCRIPTION_UI_SELECT_MY_SUBSCRIPTIONS', 'Выберите подписку через "Мои подписки"'
                ),
                show_alert=True,
            )
            return

    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    texts = get_texts(db_user.language)
    # Re-resolve after lock since db_user was refreshed
    subscription, _ = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    mode = getattr(subscription, 'traffic_accounting_mode', None)
    if mode in ('managed', 'migrating'):
        from hashlib import sha256

        from app.database.crud.transaction import emit_transaction_side_effects
        from app.services.paid_traffic_reset_service import purchase_managed_reset
        from app.services.traffic_accounting import AccountingConflict
        from app.services.traffic_accounting_service import refresh_managed_account

        if mode == 'migrating':
            await callback.answer(
                texts.t('SUBSCRIPTION_UI_ACCOUNTING_UPDATING', 'Учёт трафика обновляется. Повторите позже.'),
                show_alert=True,
            )
            return
        subscription_id, owner_id, language = subscription.id, db_user.id, db_user.language
        # Each rendered confirmation has its own nonce; retries reuse it, while
        # returning to this screen later permits a genuinely new paid reset.
        parts = (callback.data or '').split(':')
        token = parts[2] if len(parts) == 3 else ''
        if (
            len(parts) != 3
            or parts[1] != str(subscription_id)
            or len(token) != 24
            or any(char not in '0123456789abcdef' for char in token)
        ):
            await callback.answer(
                texts.t('SUBSCRIPTION_UI_REOPEN_RESET_CONFIRM', 'Откройте подтверждение сброса ещё раз.'),
                show_alert=True,
            )
            return
        source = f'{owner_id}:{subscription_id}:{token}'
        operation_key = f'telegram-reset:{sha256(source.encode()).hexdigest()}'
        try:
            transaction, charged = await purchase_managed_reset(
                db,
                subscription_id,
                user_id=owner_id,
                operation_key=operation_key,
                price=_calculate_traffic_reset_price,
            )
            if charged:
                await emit_transaction_side_effects(
                    db,
                    transaction,
                    amount_kopeks=abs(transaction.amount_kopeks),
                    user_id=owner_id,
                    type=TransactionType.SUBSCRIPTION_PAYMENT,
                    external_id=operation_key,
                    description='Сброс трафика',
                )
            try:
                await refresh_managed_account(db, subscription)
                await db.commit()
            except Exception:
                await db.rollback()
                from app.services.remnawave_retry_queue import remnawave_retry_queue

                remnawave_retry_queue.enqueue(subscription_id=subscription_id, user_id=owner_id, action='update')
                await callback.answer(
                    texts.t(
                        'SUBSCRIPTION_UI_PAYMENT_SAVED_RETRY', 'Оплата сохранена. Обновление доступа будет повторено.'
                    ),
                    show_alert=True,
                )
                return
            await callback.message.edit_text(
                texts.t(
                    'SUBSCRIPTION_UI_BASE_RESET_SUCCESS',
                    '✅ Базовый трафик обновлён. Остатки докупленных пакетов сохранены.',
                ),
                reply_markup=get_back_keyboard(language),
            )
            await callback.answer()
        except AccountingConflict:
            await callback.answer(
                texts.t(
                    'SUBSCRIPTION_UI_TRAFFIC_BALANCE_RETRY',
                    'Не удалось обновить трафик. Проверьте баланс и повторите позже.',
                ),
                show_alert=True,
            )
        except Exception as error:
            logger.error('Ошибка оплаты обновления трафика', error=error)
            await callback.answer(
                texts.t('SUBSCRIPTION_UI_TRAFFIC_RETRY', 'Не удалось обновить трафик. Повторите позже.'),
                show_alert=True,
            )
        return

    reset_price = _calculate_traffic_reset_price(subscription)

    if reset_price > 0 and db_user.balance_kopeks < reset_price:
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
            required=texts.format_price(reset_price, round_kopeks=False),
            balance=texts.format_price(db_user.balance_kopeks, round_kopeks=False),
            missing=texts.format_price(missing_kopeks, round_kopeks=False),
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
            await callback.answer(
                texts.t('SUBSCRIPTION_UI_DEBIT_HOURGLASS', '⌛ Ошибка списания средств'),
                show_alert=True,
            )
            return

        subscription.traffic_used_gb = 0.0
        subscription.updated_at = datetime.now(UTC)
        await db.commit()

        SubscriptionService()
        remnawave_service = RemnaWaveService()

        user = db_user
        # В multi-tariff у каждой подписки СВОЙ панельный юзер: фолбэк на
        # user-уровень обнулил бы трафик соседнему тарифу, за который тут
        # никто не платил, а нужный остался бы на лимите.
        panel_user_id = getattr(subscription, 'remnawave_id', None)
        if not panel_user_id and not settings.is_multi_tariff_enabled():
            panel_user_id = user.remnawave_id
        async with remnawave_service.get_api_client() as api:
            if not panel_user_id:
                # Деньги уже списаны и закоммичены выше. Между миграцией 0104 и
                # прогоном бэкфила `remnawave_id` пуст у всех доапгрейдных строк,
                # и без подхвата по shortUuid сброс молча не доезжал бы до панели:
                # пользователь заплатил, остался LIMITED, а бот отрапортовал успех.
                _short_uuid = (getattr(subscription, 'remnawave_short_uuid', None) or '').strip()
                if _short_uuid:
                    _adopted = await api.get_user_by_short_uuid(_short_uuid)
                    if _adopted is not None:
                        panel_user_id = _adopted.id
                        subscription.remnawave_id = _adopted.id
                        if not settings.is_multi_tariff_enabled():
                            user.remnawave_id = _adopted.id
                        await db.commit()
            if panel_user_id:
                await api.reset_user_traffic(panel_user_id)
            else:
                logger.error(
                    'Сброс трафика оплачен, но панельный пользователь не опознан',
                    user_id=db_user.id,
                    subscription_id=subscription.id,
                )

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
            texts.t(
                'SUBSCRIPTION_UI_RESET_SUCCESS_DETAILS',
                '✅ Трафик успешно сброшен!\n\n🔄 Использованный трафик обнулен\n📊 Лимит: {limit}',
            ).format(limit=texts.format_traffic(subscription.traffic_limit_gb)),
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


async def get_traffic_packages_info(language: str | None = None) -> str:
    texts = get_texts(language)
    try:
        packages = settings.get_traffic_packages()

        info_lines = [texts.t('SUBSCRIPTION_UI_PACKAGES_TITLE', '📦 Настроенные пакеты трафика:')]

        enabled_packages = [pkg for pkg in packages if pkg['enabled']]
        disabled_packages = [pkg for pkg in packages if not pkg['enabled']]

        if enabled_packages:
            info_lines.append(texts.t('SUBSCRIPTION_UI_PACKAGES_ACTIVE_TITLE', '\n✅ Активные:'))
            for pkg in enabled_packages:
                gb_text = (
                    texts.t('SUBSCRIPTION_UI_UNLIMITED_INFINITY', '♾️ Безлимит')
                    if pkg['gb'] == 0
                    else texts.t('SUBSCRIPTION_UI_GB_VALUE', '{amount} ГБ').format(amount=pkg['gb'])
                )
                info_lines.append(f'   • {gb_text}: {pkg["price"] // 100}₽')

        if disabled_packages:
            info_lines.append(texts.t('SUBSCRIPTION_UI_PACKAGES_DISABLED_TITLE', '\n❌ Отключенные:'))
            for pkg in disabled_packages:
                gb_text = (
                    texts.t('SUBSCRIPTION_UI_UNLIMITED_INFINITY', '♾️ Безлимит')
                    if pkg['gb'] == 0
                    else texts.t('SUBSCRIPTION_UI_GB_VALUE', '{amount} ГБ').format(amount=pkg['gb'])
                )
                info_lines.append(f'   • {gb_text}: {pkg["price"] // 100}₽')

        info_lines.append(
            texts.t('SUBSCRIPTION_UI_PACKAGES_TOTAL', '\n📊 Всего пакетов: {count}').format(count=len(packages))
        )
        info_lines.append(
            texts.t('SUBSCRIPTION_UI_PACKAGES_ACTIVE', '🟢 Активных: {count}').format(count=len(enabled_packages))
        )
        info_lines.append(
            texts.t('SUBSCRIPTION_UI_PACKAGES_DISABLED', '🔴 Отключенных: {count}').format(count=len(disabled_packages))
        )

        return '\n'.join(info_lines)

    except Exception as e:
        return texts.t('SUBSCRIPTION_UI_PACKAGES_INFO_ERROR', '⚠️ Ошибка получения информации: {error}').format(error=e)


async def select_traffic(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    traffic_gb = int(callback.data.split('_')[1])
    texts = get_texts(db_user.language)

    data = await state.get_data()
    data['traffic_gb'] = traffic_gb

    traffic_price = settings.get_traffic_price(traffic_gb)
    data['total_price'] += traffic_price

    await state.set_data(data)

    if await _should_show_countries_management(db_user):
        countries = await _get_available_countries(db_user.promo_group_id, language=db_user.language)
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

    countries = await _get_available_countries(db_user.promo_group_id, language=db_user.language)
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


async def add_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None):
    from app.database.crud.tariff import get_tariff_by_id

    traffic_gb = int(callback.data.split('_')[2])
    user_language = db_user.language
    texts = get_texts(user_language)
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    # Получаем цену: из тарифа или из глобальных настроек
    base_price = 0
    tariff = None

    if settings.is_tariffs_mode() and subscription and subscription.tariff_id:
        # Режим тарифов - берем цену из тарифа
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff and tariff.can_topup_traffic():
            base_price = tariff.get_traffic_topup_price(traffic_gb) or 0
        else:
            await callback.answer(
                get_texts(getattr(db_user, 'language', None)).t(
                    'SUBSCRIPTION_UI_TARIFF_TRAFFIC_UNAVAILABLE', '⚠️ На вашем тарифе докупка трафика недоступна'
                ),
                show_alert=True,
            )
            return
    else:
        # Стандартный режим
        if settings.is_traffic_topup_blocked():
            await callback.answer(
                get_texts(getattr(db_user, 'language', None)).t(
                    'SUBSCRIPTION_UI_FIXED_TRAFFIC', '⚠️ В текущем режиме трафик фиксированный'
                ),
                show_alert=True,
            )
            return
        base_price = settings.get_traffic_topup_price(traffic_gb)

    if base_price == 0 and traffic_gb != 0:
        await callback.answer(
            get_texts(getattr(db_user, 'language', None)).t(
                'SUBSCRIPTION_UI_PACKAGE_PRICE_MISSING', '⚠️ Цена для этого пакета не настроена'
            ),
            show_alert=True,
        )
        return

    # Lock user BEFORE price computation to prevent TOCTOU on group discount
    from app.database.crud.user import lock_user_for_pricing
    from app.services.traffic_purchase_service import prepare_traffic_purchase

    try:
        subscription, accounting_panel = await prepare_traffic_purchase(db, subscription, requested_gb=traffic_gb)
    except Exception:
        await db.rollback()
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_ACCOUNTING_UNAVAILABLE', '⚠️ Учёт трафика временно недоступен. Попробуйте позже.'),
            show_alert=True,
        )
        return

    db_user = await lock_user_for_pricing(db, db_user.id)
    # Re-resolve after lock since db_user was refreshed
    subscription, _ = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

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

    if price > 0 and db_user.balance_kopeks < price:
        missing_kopeks = price - db_user.balance_kopeks

        # Save cart for auto-purchase after balance top-up
        cart_data = {
            'cart_mode': 'add_traffic',
            # Намерение пополнить ради этой корзины: без него тихая автопокупка после
            # пополнения пропускает корзину, а кнопка «вернуться» её не знает.
            'return_to_cart': True,
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
            required=texts.format_price(price, round_kopeks=False),
            balance=texts.format_price(db_user.balance_kopeks, round_kopeks=False),
            missing=texts.format_price(missing_kopeks, round_kopeks=False),
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
            commit=False,
        )

        if not success:
            await callback.answer(
                get_texts(getattr(db_user, 'language', None)).t(
                    'SUBSCRIPTION_UI_DEBIT_WARNING_EMOJI', '⚠️ Ошибка списания средств'
                ),
                show_alert=True,
            )
            return

        if traffic_gb == 0:
            from app.services.traffic_accounting_service import transition_to_unlimited_locked

            await transition_to_unlimited_locked(db, subscription, accounting_panel)
            # При переходе на безлимит сбрасываем все докупки
            from sqlalchemy import delete

            from app.database.models import TrafficPurchase

            await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
            subscription.purchased_traffic_gb = 0
            subscription.traffic_reset_at = None
        else:
            # add_subscription_traffic уже создаёт TrafficPurchase и обновляет все необходимые поля
            await add_subscription_traffic(db, subscription, traffic_gb, panel=accounting_panel, commit=False)

        # Реактивируем подписку если она была DISABLED/EXPIRED (например, после LIMITED/EXPIRED в RemnaWave)
        await reactivate_subscription(db, subscription, commit=False)
        payment_transaction = await create_transaction(
            db=db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price,
            description=f'Добавление {traffic_gb} ГБ трафика',
            commit=False,
        )
        await db.commit()
        from app.services.traffic_purchase_service import finish_traffic_purchase

        await finish_traffic_purchase(db, subscription, payment_transaction)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        # Явно включаем пользователя на панели (PATCH может не снять LIMITED-статус)
        _en_panel_id = (
            subscription.remnawave_id
            if settings.is_multi_tariff_enabled() and subscription.remnawave_id
            else db_user.remnawave_id
        )
        if _en_panel_id and subscription.status == 'active':
            await subscription_service.enable_remnawave_user(_en_panel_id)

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

        success_text = get_texts(getattr(db_user, 'language', None)).t(
            'SUBSCRIPTION_UI_TRAFFIC_ADDED_TITLE', '✅ Трафик успешно добавлен!\n\n'
        )
        if traffic_gb == 0:
            success_text += get_texts(getattr(db_user, 'language', None)).t(
                'SUBSCRIPTION_UI_NOW_UNLIMITED_TRAFFIC', '🎉 Теперь у вас безлимитный трафик!'
            )
        else:
            success_text += (
                get_texts(getattr(db_user, 'language', None))
                .t('SUBSCRIPTION_UI_TRAFFIC_ADDED_LINE', '📈 Добавлено: {traffic} ГБ\n')
                .format(traffic=traffic_gb)
            )
            success_text += (
                get_texts(getattr(db_user, 'language', None))
                .t('SUBSCRIPTION_UI_NEW_LIMIT_VALUE', 'Новый лимит: {limit}')
                .format(limit=texts.format_traffic(subscription.traffic_limit_gb))
            )

        if price > 0:
            success_text += (
                get_texts(getattr(db_user, 'language', None))
                .t('SUBSCRIPTION_UI_CHARGED_LINE', '\n💰 Списано: {amount}')
                .format(amount=texts.format_price(price))
            )
            if total_discount_value > 0:
                success_text += (
                    get_texts(getattr(db_user, 'language', None))
                    .t('SUBSCRIPTION_UI_DISCOUNT_SUFFIX', ' (скидка {percent}%: -{amount})')
                    .format(percent=traffic_discount_pct, amount=texts.format_price(total_discount_value))
                )

        await callback.message.edit_text(success_text, reply_markup=get_back_keyboard(user_language))

        logger.info('✅ Пользователь добавил ГБ трафика', telegram_id=db_user.telegram_id, traffic_gb=traffic_gb)

    except Exception as e:
        await db.rollback()
        logger.error('Ошибка добавления трафика', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(user_language))

    await callback.answer()


async def handle_no_traffic_packages(callback: types.CallbackQuery, db_user: User):
    await callback.answer(
        get_texts(getattr(db_user, 'language', None)).t(
            'SUBSCRIPTION_UI_NO_TRAFFIC_PACKAGES',
            '⚠️ В данный момент нет доступных пакетов трафика. Обратитесь в техподдержку для получения информации.',
        ),
        show_alert=True,
    )


async def handle_switch_traffic(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        await callback.answer(
            get_texts(getattr(db_user, 'language', None)).t(
                'SUBSCRIPTION_UI_FIXED_TRAFFIC', '⚠️ В текущем режиме трафик фиксированный'
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_PAID_ONLY', '⚠️ Эта функция доступна только для платных подписок'),
            show_alert=True,
        )
        return

    # Проверяем настройку тарифа
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff and not tariff.allow_traffic_topup:
            await callback.answer(
                texts.t(
                    'SUBSCRIPTION_UI_TRAFFIC_SWITCH_UNAVAILABLE', '⚠️ Для вашего тарифа переключение трафика недоступно'
                ),
                show_alert=True,
            )
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
        purchased_info = texts.t(
            'SUBSCRIPTION_UI_BASE_EXTRA_TRAFFIC', '\n📦 Базовый пакет: {base}\n➕ Докуплено: {extra}'
        ).format(base=texts.format_traffic(base_traffic), extra=texts.format_traffic(purchased_traffic))

    await callback.message.edit_text(
        texts.t(
            'SUBSCRIPTION_UI_SWITCH_TRAFFIC_DETAILS',
            '🔄 <b>Переключение лимита трафика</b>\n\nТекущий лимит: {limit}{packages}\nВыберите новый лимит трафика:\n\n💡 <b>Важно:</b>\n• При увеличении - доплата за разницу\n• При уменьшении - возврат средств не производится\n• Докупленный трафик будет сброшен',
        ).format(limit=texts.format_traffic(current_traffic), packages=purchased_info),
        reply_markup=get_traffic_switch_keyboard(
            current_traffic,
            db_user.language,
            subscription.end_date,
            traffic_discount_percent,
            base_traffic_gb=base_traffic,
            back_callback=f'sm:{sub_id}' if settings.is_multi_tariff_enabled() and sub_id else 'subscription_settings',
        ),
    )

    await callback.answer()


async def confirm_switch_traffic(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    new_traffic_gb = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    current_traffic = subscription.traffic_limit_gb

    # Вычисляем базовый трафик (без докупленного) для корректного расчёта цены
    purchased_traffic = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    base_traffic = current_traffic - purchased_traffic

    if new_traffic_gb == current_traffic:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_TRAFFIC_UNCHANGED', 'ℹ️ Лимит трафика не изменился'),
            show_alert=True,
        )
        return

    # Используем базовый трафик для определения текущей цены пакета
    old_price_per_month = settings.get_traffic_price(base_traffic)
    new_price_per_month = settings.get_traffic_price(new_traffic_gb)

    now = datetime.now(UTC)
    days_remaining = max(1, math.ceil((subscription.end_date - now).total_seconds() / 86400))
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

        if total_price_difference > 0 and db_user.balance_kopeks < total_price_difference:
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
                required=texts.t('SUBSCRIPTION_UI_PRICE_FOR_DAYS', '{price} (за {days} дн.)').format(
                    price=texts.format_price(total_price_difference), days=days_remaining
                ),
                balance=texts.format_price(db_user.balance_kopeks, round_kopeks=False),
                missing=texts.format_price(missing_kopeks, round_kopeks=False),
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

        action_text = texts.t('SUBSCRIPTION_UI_INCREASE_TO', 'увеличить до {limit}').format(
            limit=texts.format_traffic(new_traffic_gb)
        )
        cost_text = texts.t('SUBSCRIPTION_UI_EXTRA_CHARGE_DAYS', 'Доплата: {amount} (за {days} дн.)').format(
            amount=texts.format_price(total_price_difference), days=days_remaining
        )
        if discount_savings_per_month > 0:
            total_discount_savings = int(discount_savings_per_month * days_remaining / 30)
            cost_text += texts.t('SUBSCRIPTION_UI_DISCOUNT_SUFFIX', ' (скидка {percent}%: -{amount})').format(
                percent=traffic_discount_percent, amount=texts.format_price(total_discount_savings)
            )
    else:
        total_price_difference = 0
        action_text = texts.t('SUBSCRIPTION_UI_DECREASE_TO', 'уменьшить до {limit}').format(
            limit=texts.format_traffic(new_traffic_gb)
        )
        cost_text = texts.t('SUBSCRIPTION_UI_NO_REFUND', 'Возврат средств не производится')

    confirm_text = texts.t(
        'SUBSCRIPTION_UI_TRAFFIC_SWITCH_CONFIRM_TITLE', '🔄 <b>Подтверждение переключения трафика</b>\n\n'
    )
    confirm_text += texts.t('SUBSCRIPTION_UI_CURRENT_LIMIT_LINE', 'Текущий лимит: {limit}\n').format(
        limit=texts.format_traffic(current_traffic)
    )
    confirm_text += texts.t('SUBSCRIPTION_UI_NEW_LIMIT_LINES', 'Новый лимит: {limit}\n\n').format(
        limit=texts.format_traffic(new_traffic_gb)
    )
    confirm_text += texts.t('SUBSCRIPTION_UI_ACTION_LINE', 'Действие: {action}\n').format(action=action_text)
    confirm_text += f'💰 {cost_text}\n\n'
    confirm_text += texts.t('SUBSCRIPTION_UI_CONFIRM_SWITCH_QUESTION', 'Подтвердить переключение?')

    await callback.message.edit_text(
        confirm_text,
        reply_markup=get_confirm_switch_traffic_keyboard(
            new_traffic_gb,
            total_price_difference,
            db_user.language,
            back_callback=f'sm:{sub_id}' if settings.is_multi_tariff_enabled() and sub_id else 'subscription_settings',
        ),
        parse_mode='HTML',
    )

    await callback.answer()


async def execute_switch_traffic(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    callback_parts = callback.data.split('_')
    new_traffic_gb = int(callback_parts[3])

    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    texts = get_texts(db_user.language)
    # Re-resolve after lock since db_user was refreshed
    subscription, _ = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return
    current_traffic = subscription.traffic_limit_gb

    # Recompute price under lock (callback-baked value may be stale)
    purchased_traffic = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    base_traffic = current_traffic - purchased_traffic
    old_price_per_month = settings.get_traffic_price(base_traffic)
    new_price_per_month = settings.get_traffic_price(new_traffic_gb)
    days_remaining = max(1, math.ceil((subscription.end_date - datetime.now(UTC)).total_seconds() / 86400))
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
                await callback.answer(
                    texts.t('SUBSCRIPTION_UI_DEBIT_WARNING_EMOJI', '⚠️ Ошибка списания средств'),
                    show_alert=True,
                )
                return

            days_remaining = max(1, math.ceil((subscription.end_date - datetime.now(UTC)).total_seconds() / 86400))
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
        _en_panel_id = (
            subscription.remnawave_id
            if settings.is_multi_tariff_enabled() and subscription.remnawave_id
            else db_user.remnawave_id
        )
        if _en_panel_id and subscription.status == 'active':
            await subscription_service.enable_remnawave_user(_en_panel_id)

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
            success_text = texts.t('SUBSCRIPTION_UI_LIMIT_INCREASED', '✅ Лимит трафика увеличен!\n\n')
            success_text += texts.t('SUBSCRIPTION_UI_PREVIOUS_TRAFFIC', '📊 Было: {traffic} → ').format(
                traffic=texts.format_traffic(current_traffic)
            )
            success_text += texts.t('SUBSCRIPTION_UI_UPDATED_TRAFFIC', 'Стало: {traffic}\n').format(
                traffic=texts.format_traffic(new_traffic_gb)
            )
            if price_difference > 0:
                success_text += texts.t('SUBSCRIPTION_UI_CHARGED_AMOUNT', '💰 Списано: {amount}').format(
                    amount=texts.format_price(price_difference)
                )
        elif new_traffic_gb < current_traffic:
            success_text = texts.t('SUBSCRIPTION_UI_LIMIT_DECREASED', '✅ Лимит трафика уменьшен!\n\n')
            success_text += texts.t('SUBSCRIPTION_UI_PREVIOUS_TRAFFIC', '📊 Было: {traffic} → ').format(
                traffic=texts.format_traffic(current_traffic)
            )
            success_text += texts.t('SUBSCRIPTION_UI_UPDATED_TRAFFIC', 'Стало: {traffic}\n').format(
                traffic=texts.format_traffic(new_traffic_gb)
            )
            success_text += texts.t('SUBSCRIPTION_UI_NO_REFUND_INFO', 'ℹ️ Возврат средств не производится')

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
