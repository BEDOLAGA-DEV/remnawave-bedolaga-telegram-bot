import logging
from datetime import datetime, timedelta


logger = logging.getLogger(__name__)
from typing import Any

from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.crud.subscription import (
    create_paid_subscription,
    create_pending_trial_subscription,
    create_trial_subscription,
)
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import Subscription, SubscriptionStatus, TransactionType, User
from app.keyboards.inline import (
    get_back_keyboard,
    get_countries_keyboard,
    get_devices_keyboard,
    get_extend_subscription_keyboard_with_prices,
    get_happ_download_button_row,
    get_insufficient_balance_keyboard,
    get_insufficient_balance_keyboard_with_cart,
    get_payment_methods_keyboard_with_cart,
    get_subscription_confirm_keyboard,
    get_subscription_confirm_keyboard_with_cart,
    get_subscription_keyboard,
    get_subscription_period_keyboard,
    get_traffic_packages_keyboard,
    get_trial_keyboard,
    get_updated_subscription_settings_keyboard,
)
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.remnawave_service import RemnaWaveConfigurationError
from app.services.subscription_checkout_service import (
    clear_subscription_checkout_draft,
    get_subscription_checkout_draft,
    save_subscription_checkout_draft,
    should_offer_checkout_resume,
)
from app.services.subscription_service import SubscriptionService
from app.services.trial_activation_service import (
    TrialPaymentChargeFailed,
    TrialPaymentInsufficientFunds,
    charge_trial_activation_if_required,
    revert_trial_activation,
    rollback_trial_subscription_activation,
)
from app.services.user_cart_service import user_cart_service
from app.utils.decorators import error_handler


logger = logging.getLogger(__name__)


def _serialize_markup(markup: InlineKeyboardMarkup | None) -> Any | None:
    if markup is None:
        return None

    model_dump = getattr(markup, 'model_dump', None)
    if callable(model_dump):
        try:
            return model_dump(exclude_none=True)
        except TypeError:
            return model_dump()

    to_python = getattr(markup, 'to_python', None)
    if callable(to_python):
        return to_python()

    return markup


def _message_needs_update(
    message: types.Message,
    new_text: str,
    new_markup: InlineKeyboardMarkup | None,
) -> bool:
    current_text = getattr(message, 'text', None)

    if current_text != new_text:
        return True

    current_markup = getattr(message, 'reply_markup', None)

    return _serialize_markup(current_markup) != _serialize_markup(new_markup)


from app.handlers.simple_subscription import (
    _calculate_simple_subscription_price,
    _get_simple_subscription_payment_keyboard,
)
from app.states import SubscriptionStates
from app.utils.price_display import PriceInfo, calculate_user_price, format_price_text
from app.utils.pricing_utils import (
    apply_percentage_discount,
    calculate_months_from_days,
    format_period_description,
    validate_pricing_calculation,
)
from app.utils.subscription_utils import (
    get_display_subscription_link,
    resolve_simple_subscription_device_limit,
)
from app.utils.timezone import format_local_datetime

from .autopay import (
    handle_autopay_menu,
    handle_subscription_cancel,
    handle_subscription_config_back,
    set_autopay_days,
    show_autopay_days,
    toggle_autopay,
)
from .common import _apply_promo_offer_discount, _get_promo_offer_discount_percent, update_traffic_prices
from .countries import (
    _build_countries_selection_text,
    _get_available_countries,
    _get_preselected_free_countries,
    _should_show_countries_management,
    apply_countries_changes,
    countries_continue,
    handle_add_countries,
    handle_manage_country,
    select_country,
)
from .devices import (
    confirm_add_devices,
    confirm_change_devices,
    confirm_reset_devices,
    execute_change_devices,
    get_current_devices_count,
    get_servers_display_names,
    handle_all_devices_reset_from_management,
    handle_app_selection,
    handle_change_devices,
    handle_device_guide,
    handle_device_management,
    handle_devices_page,
    handle_reset_devices,
    handle_single_device_reset,
    handle_specific_app_guide,
    show_device_connection_help,
)
from .happ import (
    handle_happ_download_back,
    handle_happ_download_close,
    handle_happ_download_platform_choice,
    handle_happ_download_request,
)
from .links import handle_connect_subscription, handle_open_subscription_link
from .pricing import _build_subscription_period_prompt, _prepare_subscription_summary
from .promo import (
    _build_promo_group_discount_text,
    _get_promo_offer_hint,
    claim_discount_offer,
    handle_promo_offer_close,
)
from .summary import present_subscription_summary
from .traffic import (
    confirm_reset_traffic,
    confirm_switch_traffic,
    execute_switch_traffic,
    handle_no_traffic_packages,
    handle_reset_traffic,
    handle_switch_traffic,
    select_traffic,
)


async def show_subscription_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Проверяем, доступно ли сообщение для редактирования
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    await db.refresh(db_user)

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription:
        await callback.message.edit_text(texts.SUBSCRIPTION_NONE, reply_markup=get_back_keyboard(db_user.language))
        await callback.answer()
        return

    from app.database.crud.subscription import check_and_update_subscription_status

    subscription = await check_and_update_subscription_status(db, subscription)

    subscription_service = SubscriptionService()
    await subscription_service.sync_subscription_usage(db, subscription)

    # Проверяем и синхронизируем подписку с RemnaWave если необходимо
    sync_success, sync_error = await subscription_service.ensure_subscription_synced(db, subscription)
    if not sync_success:
        logger.warning(f'Не удалось синхронизировать подписку {subscription.id} с RemnaWave: {sync_error}')

    await db.refresh(subscription)
    await db.refresh(db_user)

    current_time = datetime.utcnow()

    if subscription.status == 'expired' or subscription.end_date <= current_time:
        actual_status = 'expired'
        status_display = texts.t('SUBSCRIPTION_STATUS_EXPIRED', 'Истекла')
        status_emoji = '🔴'
    elif subscription.status == 'active' and subscription.end_date > current_time:
        if subscription.is_trial:
            actual_status = 'trial_active'
            status_display = texts.t('SUBSCRIPTION_STATUS_TRIAL', 'Тестовая')
            status_emoji = '🎯'
        else:
            actual_status = 'paid_active'
            status_display = texts.t('SUBSCRIPTION_STATUS_ACTIVE', 'Активна')
            status_emoji = '💎'
    else:
        actual_status = 'unknown'
        status_display = texts.t('SUBSCRIPTION_STATUS_UNKNOWN', 'Неизвестно')
        status_emoji = '❓'

    if subscription.end_date <= current_time:
        days_left = 0
        time_left_text = texts.t('SUBSCRIPTION_TIME_LEFT_EXPIRED', 'истёк')
        warning_text = ''
    else:
        delta = subscription.end_date - current_time
        days_left = delta.days
        hours_left = delta.seconds // 3600

        if days_left > 1:
            time_left_text = texts.t('SUBSCRIPTION_TIME_LEFT_DAYS', '{days} дн.').format(days=days_left)
            warning_text = ''
        elif days_left == 1:
            time_left_text = texts.t('SUBSCRIPTION_TIME_LEFT_DAYS', '{days} дн.').format(days=days_left)
            warning_text = texts.t('SUBSCRIPTION_WARNING_TOMORROW', '\n⚠️ истекает завтра!')
        elif hours_left > 0:
            time_left_text = texts.t('SUBSCRIPTION_TIME_LEFT_HOURS', '{hours} ч.').format(hours=hours_left)
            warning_text = texts.t('SUBSCRIPTION_WARNING_TODAY', '\n⚠️ истекает сегодня!')
        else:
            minutes_left = (delta.seconds % 3600) // 60
            time_left_text = texts.t('SUBSCRIPTION_TIME_LEFT_MINUTES', '{minutes} мин.').format(minutes=minutes_left)
            warning_text = texts.t(
                'SUBSCRIPTION_WARNING_MINUTES',
                '\n🔴 истекает через несколько минут!',
            )

    subscription_type = (
        texts.t('SUBSCRIPTION_TYPE_TRIAL', 'Триал')
        if subscription.is_trial
        else texts.t('SUBSCRIPTION_TYPE_PAID', 'Платная')
    )

    used_traffic = f'{subscription.traffic_used_gb:.1f}'
    if subscription.traffic_limit_gb == 0:
        traffic_used_display = texts.t(
            'SUBSCRIPTION_TRAFFIC_UNLIMITED',
            '∞ (безлимит) | Использовано: {used} ГБ',
        ).format(used=used_traffic)
    else:
        traffic_used_display = texts.t(
            'SUBSCRIPTION_TRAFFIC_LIMITED',
            '{used} / {limit} ГБ',
        ).format(used=used_traffic, limit=subscription.traffic_limit_gb)

    devices_used_str = '—'
    devices_list = []
    devices_count = 0

    show_devices = settings.is_devices_selection_enabled()
    devices_used_str = ''
    devices_list: list[dict[str, Any]] = []

    if show_devices:
        try:
            if db_user.remnawave_uuid:
                from app.services.remnawave_service import RemnaWaveService

                service = RemnaWaveService()

                async with service.get_api_client() as api:
                    response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

                    if response and 'response' in response:
                        devices_info = response['response']
                        devices_count = devices_info.get('total', 0)
                        devices_list = devices_info.get('devices', [])
                        devices_used_str = str(devices_count)
                        logger.info(f'Найдено {devices_count} устройств для пользователя {db_user.telegram_id}')
                    else:
                        logger.warning(f'Не удалось получить информацию об устройствах для {db_user.telegram_id}')

        except Exception as e:
            logger.error(f'Ошибка получения устройств для отображения: {e}')
            devices_used = await get_current_devices_count(db_user)
            devices_used_str = str(devices_used)

    servers_names = await get_servers_display_names(subscription.connected_squads)
    servers_display = servers_names if servers_names else texts.t('SUBSCRIPTION_NO_SERVERS', 'Нет серверов')

    # Получаем информацию о тарифе для режима тарифов
    tariff_info_block = ''
    tariff = None
    if settings.is_tariffs_mode() and subscription.tariff_id:
        try:
            from app.database.crud.tariff import get_tariff_by_id

            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff:
                # Прикрепляем тариф к подписке для использования в клавиатуре
                subscription.tariff = tariff

                # Формируем блок информации о тарифе
                is_daily = getattr(tariff, 'is_daily', False)
                tariff_type_str = '🔄 Суточный' if is_daily else '📅 Периодный'

                tariff_info_lines = [
                    f'<b>📦 {tariff.name}</b>',
                    f'Тип: {tariff_type_str}',
                    f'Трафик: {tariff.traffic_limit_gb} ГБ' if tariff.traffic_limit_gb > 0 else 'Трафик: ∞ Безлимит',
                    f'Устройства: {tariff.device_limit}',
                ]

                if is_daily:
                    # Для суточного тарифа показываем цену и прогресс-бар
                    daily_price = getattr(tariff, 'daily_price_kopeks', 0) / 100
                    tariff_info_lines.append(f'Цена: {daily_price:.2f} ₽/день')

                    # Прогресс-бар до следующего списания
                    last_charge = getattr(subscription, 'last_daily_charge_at', None)
                    is_paused = getattr(subscription, 'is_daily_paused', False)

                    if is_paused:
                        tariff_info_lines.append('')
                        tariff_info_lines.append('⏸️ <b>Подписка приостановлена</b>')
                        # Показываем оставшееся время даже при паузе
                        if last_charge:
                            from datetime import timedelta

                            next_charge = last_charge + timedelta(hours=24)
                            now = datetime.utcnow()
                            if next_charge > now:
                                time_until = next_charge - now
                                hours_left = time_until.seconds // 3600
                                minutes_left = (time_until.seconds % 3600) // 60
                                tariff_info_lines.append(f'⏳ Осталось: {hours_left}ч {minutes_left}мин')
                                tariff_info_lines.append('💤 Списание приостановлено')
                    elif last_charge:
                        from datetime import timedelta

                        next_charge = last_charge + timedelta(hours=24)
                        now = datetime.utcnow()

                        if next_charge > now:
                            time_until = next_charge - now
                            hours_left = time_until.seconds // 3600
                            minutes_left = (time_until.seconds % 3600) // 60

                            # Процент оставшегося времени (24 часа = 100%)
                            total_seconds = 24 * 3600
                            remaining_seconds = time_until.total_seconds()
                            percent = min(100, max(0, (remaining_seconds / total_seconds) * 100))

                            # Генерируем прогресс-бар
                            bar_length = 10
                            filled = int(bar_length * percent / 100)
                            empty = bar_length - filled
                            progress_bar = '▓' * filled + '░' * empty

                            tariff_info_lines.append('')
                            tariff_info_lines.append(f'⏳ До списания: {hours_left}ч {minutes_left}мин')
                            tariff_info_lines.append(f'[{progress_bar}] {percent:.0f}%')
                    else:
                        tariff_info_lines.append('')
                        tariff_info_lines.append('⏳ Первое списание скоро')

                tariff_info_block = '\n<blockquote expandable>' + '\n'.join(tariff_info_lines) + '</blockquote>'

        except Exception as e:
            logger.warning(f'Ошибка получения тарифа: {e}', exc_info=True)

    # Определяем, суточный ли тариф для выбора шаблона
    is_daily_tariff = tariff and getattr(tariff, 'is_daily', False)

    if is_daily_tariff:
        # Для суточных тарифов другой шаблон без "Действует до" и "Осталось"
        message_template = texts.t(
            'SUBSCRIPTION_DAILY_OVERVIEW_TEMPLATE',
            """👤 {full_name}
💰 Баланс: {balance}
📱 Подписка: {status_emoji} {status_display}{warning}{tariff_info_block}

📱 Информация о подписке
🎭 Тип: {subscription_type}
📈 Трафик: {traffic}
🌍 Серверы: {servers}
📱 Устройства: {devices_used} / {device_limit}""",
        )
    else:
        message_template = texts.t(
            'SUBSCRIPTION_OVERVIEW_TEMPLATE',
            """👤 {full_name}
💰 Баланс: {balance}
📱 Подписка: {status_emoji} {status_display}{warning}{tariff_info_block}

📱 Информация о подписке
🎭 Тип: {subscription_type}
📅 Действует до: {end_date}
⏰ Осталось: {time_left}
📈 Трафик: {traffic}
🌍 Серверы: {servers}
📱 Устройства: {devices_used} / {device_limit}""",
        )

    if not show_devices:
        message_template = message_template.replace(
            '\n📱 Устройства: {devices_used} / {device_limit}',
            '',
        )

    # Формируем отображение лимита устройств с учётом модема
    modem_enabled = getattr(subscription, 'modem_enabled', False) or False
    if modem_enabled and settings.is_modem_enabled():
        # Показываем лимит без модема + модем
        visible_device_limit = (subscription.device_limit or 1) - 1
        device_limit_display = f'{visible_device_limit} + модем'
    else:
        device_limit_display = str(subscription.device_limit)

    message = message_template.format(
        full_name=db_user.full_name,
        balance=settings.format_price(db_user.balance_kopeks),
        status_emoji=status_emoji,
        status_display=status_display,
        warning=warning_text,
        tariff_info_block=tariff_info_block,
        subscription_type=subscription_type,
        end_date=format_local_datetime(subscription.end_date, '%d.%m.%Y %H:%M'),
        time_left=time_left_text,
        traffic=traffic_used_display,
        servers=servers_display,
        devices_used=devices_used_str,
        device_limit=device_limit_display,
    )

    if show_devices and devices_list:
        message += '\n\n' + texts.t(
            'SUBSCRIPTION_CONNECTED_DEVICES_TITLE',
            '<blockquote>📱 <b>Подключенные устройства:</b>\n',
        )
        for device in devices_list[:5]:
            platform = device.get('platform', 'Unknown')
            device_model = device.get('deviceModel', 'Unknown')
            device_info = f'{platform} - {device_model}'

            if len(device_info) > 35:
                device_info = device_info[:32] + '...'
            message += f'• {device_info}\n'
        message += texts.t('SUBSCRIPTION_CONNECTED_DEVICES_FOOTER', '</blockquote>')

    # Отображаем докупленный трафик
    if subscription.traffic_limit_gb > 0:  # Только для лимитированных тарифов
        from sqlalchemy import select as sql_select

        from app.database.models import TrafficPurchase

        now = datetime.utcnow()
        purchases_query = (
            sql_select(TrafficPurchase)
            .where(TrafficPurchase.subscription_id == subscription.id)
            .where(TrafficPurchase.expires_at > now)
            .order_by(TrafficPurchase.expires_at.asc())
        )
        purchases_result = await db.execute(purchases_query)
        purchases = purchases_result.scalars().all()

        if purchases:
            message += '\n\n' + texts.t(
                'SUBSCRIPTION_PURCHASED_TRAFFIC_TITLE',
                '<blockquote>📦 <b>Докупленный трафик:</b>\n',
            )

            for purchase in purchases:
                time_remaining = purchase.expires_at - now
                days_remaining = max(0, int(time_remaining.total_seconds() / 86400))

                # Генерируем прогресс-бар
                total_duration_seconds = (purchase.expires_at - purchase.created_at).total_seconds()
                elapsed_seconds = (now - purchase.created_at).total_seconds()
                progress_percent = min(
                    100.0,
                    max(0.0, (elapsed_seconds / total_duration_seconds * 100) if total_duration_seconds > 0 else 0),
                )

                bar_length = 10
                filled = int((progress_percent / 100) * bar_length)
                bar = '▰' * filled + '▱' * (bar_length - filled)

                # Форматируем дату истечения
                expire_date = purchase.expires_at.strftime('%d.%m.%Y')

                # Формируем текст о времени
                if days_remaining == 0:
                    time_text = 'истекает сегодня'
                elif days_remaining == 1:
                    time_text = 'остался 1 день'
                elif days_remaining < 5:
                    time_text = f'осталось {days_remaining} дня'
                else:
                    time_text = f'осталось {days_remaining} дней'

                message += f'• {purchase.traffic_gb} ГБ — {time_text}\n'
                message += f'  {bar} {progress_percent:.0f}% | до {expire_date}\n'

            message += texts.t('SUBSCRIPTION_PURCHASED_TRAFFIC_FOOTER', '</blockquote>')

    subscription_link = get_display_subscription_link(subscription)
    hide_subscription_link = settings.should_hide_subscription_link()

    if subscription_link and actual_status in ['trial_active', 'paid_active'] and not hide_subscription_link:
        subscription_link_display = subscription_link

        if settings.is_happ_cryptolink_mode():
            subscription_link_display = f'<blockquote expandable><code>{subscription_link}</code></blockquote>'
        else:
            subscription_link_display = f'<code>{subscription_link}</code>'

        message += '\n\n' + texts.t(
            'SUBSCRIPTION_CONNECT_LINK_SECTION',
            '🔗 <b>Ссылка для подключения:</b>\n{subscription_url}',
        ).format(subscription_url=subscription_link_display)
        message += '\n\n' + texts.t(
            'SUBSCRIPTION_CONNECT_LINK_PROMPT',
            '📱 Скопируйте ссылку и добавьте в ваше VPN приложение',
        )

    await callback.message.edit_text(
        message,
        reply_markup=get_subscription_keyboard(
            db_user.language, has_subscription=True, is_trial=subscription.is_trial, subscription=subscription
        ),
        parse_mode='HTML',
    )
    await callback.answer()


async def show_trial_offer(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Проверяем, доступно ли сообщение для редактирования
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)

    # Проверяем, отключён ли триал для этого типа пользователя
    if settings.is_trial_disabled_for_user(getattr(db_user, 'auth_type', 'telegram')):
        await callback.message.edit_text(
            texts.t('TRIAL_DISABLED_FOR_USER_TYPE', 'Пробный период недоступен'),
            reply_markup=get_back_keyboard(db_user.language),
        )
        await callback.answer()
        return

    # Проверяем, использовал ли пользователь триал
    # PENDING триальные подписки не считаются - пользователь может повторить оплату
    trial_blocked = False
    if db_user.has_had_paid_subscription:
        trial_blocked = True
    elif db_user.subscription:
        sub = db_user.subscription
        # Разрешаем если это PENDING триальная подписка (повторная попытка оплаты)
        if not (sub.status == SubscriptionStatus.PENDING.value and sub.is_trial):
            trial_blocked = True

    if trial_blocked:
        await callback.message.edit_text(texts.TRIAL_ALREADY_USED, reply_markup=get_back_keyboard(db_user.language))
        await callback.answer()
        return

    # Получаем параметры триала (из тарифа или из глобальных настроек)
    trial_days = settings.TRIAL_DURATION_DAYS
    trial_traffic = settings.TRIAL_TRAFFIC_LIMIT_GB
    trial_device_limit = settings.TRIAL_DEVICE_LIMIT
    trial_tariff = None
    trial_server_name = texts.t('TRIAL_SERVER_DEFAULT_NAME', '🎯 Тестовый сервер')

    # Проверяем триальный тариф
    if settings.is_tariffs_mode():
        try:
            from app.database.crud.tariff import get_tariff_by_id as get_tariff, get_trial_tariff

            trial_tariff = await get_trial_tariff(db)
            if not trial_tariff:
                trial_tariff_id = settings.get_trial_tariff_id()
                if trial_tariff_id > 0:
                    trial_tariff = await get_tariff(db, trial_tariff_id)
                    if trial_tariff and not trial_tariff.is_active:
                        trial_tariff = None

            if trial_tariff:
                trial_traffic = trial_tariff.traffic_limit_gb
                trial_device_limit = trial_tariff.device_limit
                tariff_trial_days = getattr(trial_tariff, 'trial_duration_days', None)
                if tariff_trial_days:
                    trial_days = tariff_trial_days
                logger.info(f'Показываем триал с тарифом {trial_tariff.name}')
        except Exception as e:
            logger.error(f'Ошибка получения триального тарифа: {e}')

    try:
        from app.database.crud.server_squad import get_trial_eligible_server_squads

        # Для тарифа используем его сервера
        if trial_tariff and trial_tariff.allowed_squads:
            from app.database.crud.server_squad import get_server_squads_by_uuids

            tariff_squads = await get_server_squads_by_uuids(db, trial_tariff.allowed_squads)
            if tariff_squads:
                if len(tariff_squads) == 1:
                    trial_server_name = tariff_squads[0].display_name
                else:
                    trial_server_name = texts.t(
                        'TRIAL_SERVER_RANDOM_POOL',
                        '🎲 Случайный из {count} серверов',
                    ).format(count=len(tariff_squads))
        else:
            trial_squads = await get_trial_eligible_server_squads(db, include_unavailable=True)
            if trial_squads:
                if len(trial_squads) == 1:
                    trial_server_name = trial_squads[0].display_name
                else:
                    trial_server_name = texts.t(
                        'TRIAL_SERVER_RANDOM_POOL',
                        '🎲 Случайный из {count} серверов',
                    ).format(count=len(trial_squads))
            else:
                logger.warning('Не настроены сквады для выдачи триалов')

    except Exception as e:
        logger.error(f'Ошибка получения триального сервера: {e}')

    if not settings.is_devices_selection_enabled():
        forced_limit = settings.get_disabled_mode_device_limit()
        if forced_limit is not None:
            trial_device_limit = forced_limit

    devices_line = ''
    if settings.is_devices_selection_enabled() or trial_tariff:
        devices_line_template = texts.t(
            'TRIAL_AVAILABLE_DEVICES_LINE',
            '\n📱 <b>Устройства:</b> {devices} шт.',
        )
        devices_line = devices_line_template.format(
            devices=trial_device_limit,
        )

    price_line = ''
    if settings.is_trial_paid_activation_enabled():
        trial_price = settings.get_trial_activation_price()
        if trial_price > 0:
            price_line = texts.t(
                'TRIAL_PAYMENT_PRICE_LINE',
                '\n💳 <b>Стоимость активации:</b> {price}',
            ).format(price=settings.format_price(trial_price))

    trial_text = texts.TRIAL_AVAILABLE.format(
        days=trial_days,
        traffic=texts.format_traffic(trial_traffic),
        devices=trial_device_limit if trial_device_limit is not None else '',
        devices_line=devices_line,
        server_name=trial_server_name,
        price_line=price_line,
    )

    await callback.message.edit_text(trial_text, reply_markup=get_trial_keyboard(db_user.language))
    await callback.answer()


def _get_trial_payment_keyboard(language: str, can_pay_from_balance: bool = False) -> types.InlineKeyboardMarkup:
    """Создает клавиатуру с методами оплаты для платного триала."""
    texts = get_texts(language)
    keyboard = []

    # Кнопка оплаты с баланса (если хватает средств)
    if can_pay_from_balance:
        keyboard.append(
            [types.InlineKeyboardButton(text='✅ Оплатить с баланса', callback_data='trial_pay_with_balance')]
        )

    # Добавляем доступные методы оплаты
    if settings.TELEGRAM_STARS_ENABLED:
        keyboard.append([types.InlineKeyboardButton(text='⭐ Telegram Stars', callback_data='trial_payment_stars')])

    if settings.is_yookassa_enabled():
        yookassa_methods = []
        if settings.YOOKASSA_SBP_ENABLED:
            yookassa_methods.append(
                types.InlineKeyboardButton(text='🏦 YooKassa (СБП)', callback_data='trial_payment_yookassa_sbp')
            )
        yookassa_methods.append(
            types.InlineKeyboardButton(text='💳 YooKassa (Карта)', callback_data='trial_payment_yookassa')
        )
        if yookassa_methods:
            keyboard.append(yookassa_methods)

    if settings.is_cryptobot_enabled():
        keyboard.append([types.InlineKeyboardButton(text='🪙 CryptoBot', callback_data='trial_payment_cryptobot')])

    if settings.is_heleket_enabled():
        keyboard.append([types.InlineKeyboardButton(text='🪙 Heleket', callback_data='trial_payment_heleket')])

    if settings.is_mulenpay_enabled():
        mulenpay_name = settings.get_mulenpay_display_name()
        keyboard.append(
            [types.InlineKeyboardButton(text=f'💳 {mulenpay_name}', callback_data='trial_payment_mulenpay')]
        )

    if settings.is_pal24_enabled():
        keyboard.append([types.InlineKeyboardButton(text='💳 PayPalych', callback_data='trial_payment_pal24')])

    if settings.is_wata_enabled():
        keyboard.append([types.InlineKeyboardButton(text='💳 WATA', callback_data='trial_payment_wata')])

    if settings.is_platega_enabled():
        platega_name = settings.get_platega_display_name()
        keyboard.append([types.InlineKeyboardButton(text=f'💳 {platega_name}', callback_data='trial_payment_platega')])

    # Кнопка назад
    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_trial')])

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


async def activate_trial(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.services.trial_activation_service import get_trial_activation_charge_amount

    texts = get_texts(db_user.language)

    # Проверка ограничения на покупку/продление подписки
    if getattr(db_user, 'restriction_subscription', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription')])

        await callback.message.edit_text(
            f'🚫 <b>Активация подписки ограничена</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    # Проверяем, отключён ли триал для этого типа пользователя
    if settings.is_trial_disabled_for_user(getattr(db_user, 'auth_type', 'telegram')):
        await callback.message.edit_text(
            texts.t('TRIAL_DISABLED_FOR_USER_TYPE', 'Пробный период недоступен'),
            reply_markup=get_back_keyboard(db_user.language),
        )
        await callback.answer()
        return

    # Проверяем, использовал ли пользователь триал
    # PENDING триальные подписки не считаются - пользователь может повторить оплату
    trial_blocked = False
    if db_user.has_had_paid_subscription:
        trial_blocked = True
    elif db_user.subscription:
        sub = db_user.subscription
        # Разрешаем если это PENDING триальная подписка (повторная попытка оплаты)
        if not (sub.status == SubscriptionStatus.PENDING.value and sub.is_trial):
            trial_blocked = True

    if trial_blocked:
        await callback.message.edit_text(texts.TRIAL_ALREADY_USED, reply_markup=get_back_keyboard(db_user.language))
        await callback.answer()
        return

    # Проверяем, платный ли триал
    trial_price_kopeks = get_trial_activation_charge_amount()

    if trial_price_kopeks > 0:
        # Платный триал - показываем экран с выбором метода оплаты
        user_balance_kopeks = getattr(db_user, 'balance_kopeks', 0) or 0
        can_pay_from_balance = user_balance_kopeks >= trial_price_kopeks

        traffic_label = 'Безлимит' if settings.TRIAL_TRAFFIC_LIMIT_GB == 0 else f'{settings.TRIAL_TRAFFIC_LIMIT_GB} ГБ'

        message_lines = [
            texts.t('PAID_TRIAL_HEADER', '⚡ <b>Пробная подписка</b>'),
            '',
            f'📅 {texts.t("PERIOD", "Период")}: {settings.TRIAL_DURATION_DAYS} {texts.t("DAYS", "дней")}',
            f'📊 {texts.t("TRAFFIC", "Трафик")}: {traffic_label}',
            f'📱 {texts.t("DEVICES", "Устройства")}: {settings.TRIAL_DEVICE_LIMIT}',
            '',
            f'💰 {texts.t("PRICE", "Стоимость")}: {settings.format_price(trial_price_kopeks)}',
            f'💳 {texts.t("YOUR_BALANCE", "Ваш баланс")}: {settings.format_price(user_balance_kopeks)}',
            '',
        ]

        if can_pay_from_balance:
            message_lines.append(
                texts.t(
                    'PAID_TRIAL_CAN_PAY_BALANCE',
                    'Вы можете оплатить пробную подписку с баланса или выбрать другой способ оплаты.',
                )
            )
        else:
            message_lines.append(texts.t('PAID_TRIAL_SELECT_PAYMENT', 'Выберите подходящий способ оплаты:'))

        message_text = '\n'.join(message_lines)
        keyboard = _get_trial_payment_keyboard(db_user.language, can_pay_from_balance)

        await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')
        await callback.answer()
        return

    # Бесплатный триал - текущее поведение
    charged_amount = 0
    subscription: Subscription | None = None
    remnawave_user = None

    try:
        forced_devices = None
        if not settings.is_devices_selection_enabled():
            forced_devices = settings.get_disabled_mode_device_limit()

        # Проверяем, настроен ли триальный тариф для режима тарифов
        trial_tariff = None
        trial_traffic_limit = None
        trial_device_limit = forced_devices
        trial_squads = None
        tariff_id_for_trial = None
        trial_duration = None  # None = использовать TRIAL_DURATION_DAYS

        if settings.is_tariffs_mode():
            try:
                from app.database.crud.tariff import get_tariff_by_id, get_trial_tariff

                # Сначала проверяем тариф из БД с флагом is_trial_available
                trial_tariff = await get_trial_tariff(db)

                # Если не найден в БД, проверяем настройку TRIAL_TARIFF_ID
                if not trial_tariff:
                    trial_tariff_id = settings.get_trial_tariff_id()
                    if trial_tariff_id > 0:
                        trial_tariff = await get_tariff_by_id(db, trial_tariff_id)
                        if trial_tariff and not trial_tariff.is_active:
                            trial_tariff = None

                if trial_tariff:
                    trial_traffic_limit = trial_tariff.traffic_limit_gb
                    trial_device_limit = trial_tariff.device_limit
                    trial_squads = trial_tariff.allowed_squads or []
                    tariff_id_for_trial = trial_tariff.id
                    tariff_trial_days = getattr(trial_tariff, 'trial_duration_days', None)
                    if tariff_trial_days:
                        trial_duration = tariff_trial_days
                    logger.info(f'Используем триальный тариф {trial_tariff.name} (ID: {trial_tariff.id})')
            except Exception as e:
                logger.error(f'Ошибка получения триального тарифа: {e}')

        subscription = await create_trial_subscription(
            db,
            db_user.id,
            duration_days=trial_duration,
            device_limit=trial_device_limit,
            traffic_limit_gb=trial_traffic_limit,
            connected_squads=trial_squads,
            tariff_id=tariff_id_for_trial,
        )

        await db.refresh(db_user)

        try:
            charged_amount = await charge_trial_activation_if_required(
                db,
                db_user,
                description='Активация триала через бота',
            )
        except TrialPaymentInsufficientFunds as error:
            rollback_success = await rollback_trial_subscription_activation(db, subscription)
            await db.refresh(db_user)
            if not rollback_success:
                await callback.answer(
                    texts.t(
                        'TRIAL_ROLLBACK_FAILED',
                        'Не удалось отменить активацию триала. Попробуйте позже.',
                    ),
                    show_alert=True,
                )
                return

            logger.error(
                'Insufficient funds detected after trial creation for user %s: %s',
                db_user.id,
                error,
            )
            required_label = settings.format_price(error.required_amount)
            balance_label = settings.format_price(error.balance_amount)
            missing_label = settings.format_price(error.missing_amount)
            message = texts.t(
                'TRIAL_PAYMENT_INSUFFICIENT_FUNDS',
                '⚠️ Недостаточно средств для активации триала.\n'
                'Необходимо: {required}\nНа балансе: {balance}\n'
                'Не хватает: {missing}\n\nПополните баланс и попробуйте снова.',
            ).format(
                required=required_label,
                balance=balance_label,
                missing=missing_label,
            )

            await callback.message.edit_text(
                message,
                reply_markup=get_insufficient_balance_keyboard(
                    db_user.language,
                    amount_kopeks=error.required_amount,
                ),
            )
            await callback.answer()
            return
        except TrialPaymentChargeFailed:
            rollback_success = await rollback_trial_subscription_activation(db, subscription)
            await db.refresh(db_user)
            if not rollback_success:
                await callback.answer(
                    texts.t(
                        'TRIAL_ROLLBACK_FAILED',
                        'Не удалось отменить активацию триала. Попробуйте позже.',
                    ),
                    show_alert=True,
                )
                return

            await callback.answer(
                texts.t(
                    'TRIAL_PAYMENT_FAILED',
                    'Не удалось списать средства для активации триала. Попробуйте позже.',
                ),
                show_alert=True,
            )
            return

        subscription_service = SubscriptionService()
        try:
            remnawave_user = await subscription_service.create_remnawave_user(
                db,
                subscription,
            )
        except RemnaWaveConfigurationError as error:
            logger.error('RemnaWave update skipped due to configuration error: %s', error)
            revert_result = await revert_trial_activation(
                db,
                db_user,
                subscription,
                charged_amount,
                refund_description='Возврат оплаты за активацию триала через бота',
            )
            if not revert_result.subscription_rolled_back:
                failure_text = texts.t(
                    'TRIAL_ROLLBACK_FAILED',
                    'Не удалось отменить активацию триала после ошибки списания. Свяжитесь с поддержкой и попробуйте позже.',
                )
            elif charged_amount > 0 and not revert_result.refunded:
                failure_text = texts.t(
                    'TRIAL_REFUND_FAILED',
                    'Не удалось вернуть оплату за активацию триала. Немедленно свяжитесь с поддержкой.',
                )
            else:
                failure_text = texts.t(
                    'TRIAL_PROVISIONING_FAILED',
                    'Не удалось завершить активацию триала. Средства возвращены на баланс. Попробуйте позже.',
                )

            await callback.message.edit_text(
                failure_text,
                reply_markup=get_back_keyboard(db_user.language),
            )
            await callback.answer()
            return
        except Exception as error:
            logger.error(
                'Failed to create RemnaWave user for trial subscription %s: %s',
                getattr(subscription, 'id', '<unknown>'),
                error,
            )
            revert_result = await revert_trial_activation(
                db,
                db_user,
                subscription,
                charged_amount,
                refund_description='Возврат оплаты за активацию триала через бота',
            )
            if not revert_result.subscription_rolled_back:
                failure_text = texts.t(
                    'TRIAL_ROLLBACK_FAILED',
                    'Не удалось отменить активацию триала после ошибки списания. Свяжитесь с поддержкой и попробуйте позже.',
                )
            elif charged_amount > 0 and not revert_result.refunded:
                failure_text = texts.t(
                    'TRIAL_REFUND_FAILED',
                    'Не удалось вернуть оплату за активацию триала. Немедленно свяжитесь с поддержкой.',
                )
            else:
                failure_text = texts.t(
                    'TRIAL_PROVISIONING_FAILED',
                    'Не удалось завершить активацию триала. Средства возвращены на баланс. Попробуйте позже.',
                )

            await callback.message.edit_text(
                failure_text,
                reply_markup=get_back_keyboard(db_user.language),
            )
            await callback.answer()
            return

        await db.refresh(db_user)

        try:
            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_trial_activation_notification(
                db,
                db_user,
                subscription,
                charged_amount_kopeks=charged_amount,
            )
        except Exception as e:
            logger.error(f'Ошибка отправки уведомления о триале: {e}')

        subscription_link = get_display_subscription_link(subscription)
        hide_subscription_link = settings.should_hide_subscription_link()

        payment_note = ''
        if charged_amount > 0:
            payment_note = '\n\n' + texts.t(
                'TRIAL_PAYMENT_CHARGED_NOTE',
                '💳 С вашего баланса списано {amount}.',
            ).format(amount=settings.format_price(charged_amount))

        if remnawave_user and subscription_link:
            if settings.is_happ_cryptolink_mode():
                trial_success_text = (
                    f'{texts.TRIAL_ACTIVATED}\n\n'
                    + texts.t(
                        'SUBSCRIPTION_HAPP_LINK_PROMPT',
                        '🔒 Ссылка на подписку создана. Нажмите кнопку "Подключиться" ниже, чтобы открыть её в Happ.',
                    )
                    + '\n\n'
                    + texts.t(
                        'SUBSCRIPTION_IMPORT_INSTRUCTION_PROMPT',
                        '📱 Нажмите кнопку ниже, чтобы получить инструкцию по настройке VPN на вашем устройстве',
                    )
                )
            elif hide_subscription_link:
                trial_success_text = (
                    f'{texts.TRIAL_ACTIVATED}\n\n'
                    + texts.t(
                        'SUBSCRIPTION_LINK_HIDDEN_NOTICE',
                        'ℹ️ Ссылка подписки доступна по кнопкам ниже или в разделе "Моя подписка".',
                    )
                    + '\n\n'
                    + texts.t(
                        'SUBSCRIPTION_IMPORT_INSTRUCTION_PROMPT',
                        '📱 Нажмите кнопку ниже, чтобы получить инструкцию по настройке VPN на вашем устройстве',
                    )
                )
            else:
                subscription_import_link = texts.t(
                    'SUBSCRIPTION_IMPORT_LINK_SECTION',
                    '🔗 <b>Ваша ссылка для импорта в VPN приложение:</b>\n<code>{subscription_url}</code>',
                ).format(subscription_url=subscription_link)

                trial_success_text = (
                    f'{texts.TRIAL_ACTIVATED}\n\n'
                    f'{subscription_import_link}\n\n'
                    f'{texts.t("SUBSCRIPTION_IMPORT_INSTRUCTION_PROMPT", "📱 Нажмите кнопку ниже, чтобы получить инструкцию по настройке VPN на вашем устройстве")}'
                )

            trial_success_text += payment_note

            connect_mode = settings.CONNECT_BUTTON_MODE

            if connect_mode == 'miniapp_subscription':
                connect_keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                                web_app=types.WebAppInfo(url=subscription_link),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )
            elif connect_mode == 'miniapp_custom':
                if not settings.MINIAPP_CUSTOM_URL:
                    await callback.answer(
                        texts.t(
                            'CUSTOM_MINIAPP_URL_NOT_SET',
                            '⚠ Кастомная ссылка для мини-приложения не настроена',
                        ),
                        show_alert=True,
                    )
                    return

                connect_keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                                web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )
            elif connect_mode == 'link':
                rows = [
                    [
                        InlineKeyboardButton(
                            text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                            url=subscription_link,
                        )
                    ]
                ]
                happ_row = get_happ_download_button_row(texts)
                if happ_row:
                    rows.append(happ_row)
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                            callback_data='back_to_menu',
                        )
                    ]
                )
                connect_keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
            elif connect_mode == 'happ_cryptolink':
                rows = [
                    [
                        InlineKeyboardButton(
                            text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                            callback_data='open_subscription_link',
                        )
                    ]
                ]
                happ_row = get_happ_download_button_row(texts)
                if happ_row:
                    rows.append(happ_row)
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                            callback_data='back_to_menu',
                        )
                    ]
                )
                connect_keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
            else:
                connect_keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                                callback_data='subscription_connect',
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )

            await callback.message.edit_text(
                trial_success_text,
                reply_markup=connect_keyboard,
                parse_mode='HTML',
            )
        else:
            trial_success_text = f"{texts.TRIAL_ACTIVATED}\n\n⚠️ Ссылка генерируется, попробуйте перейти в раздел 'Моя подписка' через несколько секунд."
            trial_success_text += payment_note
            await callback.message.edit_text(
                trial_success_text,
                reply_markup=get_back_keyboard(db_user.language),
            )

        logger.info(f'✅ Активирована тестовая подписка для пользователя {db_user.telegram_id}')

    except Exception as e:
        logger.error(f'Ошибка активации триала: {e}')
        failure_text = texts.ERROR

        if subscription and remnawave_user is None:
            revert_result = await revert_trial_activation(
                db,
                db_user,
                subscription,
                charged_amount,
                refund_description='Возврат оплаты за активацию триала через бота',
            )
            if not revert_result.subscription_rolled_back:
                failure_text = texts.t(
                    'TRIAL_ROLLBACK_FAILED',
                    'Не удалось отменить активацию триала после ошибки списания. Свяжитесь с поддержкой и попробуйте позже.',
                )
            elif charged_amount > 0 and not revert_result.refunded:
                failure_text = texts.t(
                    'TRIAL_REFUND_FAILED',
                    'Не удалось вернуть оплату за активацию триала. Немедленно свяжитесь с поддержкой.',
                )
            else:
                failure_text = texts.t(
                    'TRIAL_PROVISIONING_FAILED',
                    'Не удалось завершить активацию триала. Средства возвращены на баланс. Попробуйте позже.',
                )

        await callback.message.edit_text(failure_text, reply_markup=get_back_keyboard(db_user.language))
        await callback.answer()
        return

    await callback.answer()


async def start_subscription_purchase(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    # Проверяем режим продаж - если tariffs, перенаправляем на выбор тарифов
    if settings.is_tariffs_mode():
        from .tariff_purchase import show_tariffs_list

        await show_tariffs_list(callback, db_user, db, state)
        return

    keyboard = get_subscription_period_keyboard(db_user.language, db_user)
    prompt_text = await _build_subscription_period_prompt(db_user, texts, db)

    await _edit_message_text_or_caption(
        callback.message,
        prompt_text,
        keyboard,
    )

    subscription = getattr(db_user, 'subscription', None)

    if settings.is_devices_selection_enabled():
        initial_devices = settings.DEFAULT_DEVICE_LIMIT
        if subscription and getattr(subscription, 'device_limit', None) is not None:
            initial_devices = max(settings.DEFAULT_DEVICE_LIMIT, subscription.device_limit)
    else:
        forced_limit = settings.get_disabled_mode_device_limit()
        if forced_limit is None:
            initial_devices = settings.DEFAULT_DEVICE_LIMIT
        else:
            initial_devices = forced_limit

    initial_data = {'period_days': None, 'countries': [], 'devices': initial_devices, 'total_price': 0}

    if settings.is_traffic_fixed():
        initial_data['traffic_gb'] = settings.get_fixed_traffic_limit()
    else:
        initial_data['traffic_gb'] = None

    await state.set_data(initial_data)
    await state.set_state(SubscriptionStates.selecting_period)
    await callback.answer()


async def _edit_message_text_or_caption(
    message: types.Message,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    parse_mode: str | None = 'HTML',
) -> None:
    """Edits message text when possible, falls back to caption or re-sends message."""

    # Если сообщение недоступно, отправляем новое
    if isinstance(message, InaccessibleMessage):
        await message.answer(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return

    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramBadRequest as error:
        error_message = str(error).lower()

        if 'message is not modified' in error_message:
            return

        if 'there is no text in the message to edit' in error_message:
            if message.caption is not None:
                await message.edit_caption(
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                return

            await message.delete()
            await message.answer(
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return

        raise


async def save_cart_and_redirect_to_topup(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, missing_amount: int
):
    texts = get_texts(db_user.language)
    data = await state.get_data()

    # Сохраняем данные корзины в Redis
    cart_data = {
        **data,
        'saved_cart': True,
        'missing_amount': missing_amount,
        'return_to_cart': True,
        'user_id': db_user.id,
    }

    await user_cart_service.save_user_cart(db_user.id, cart_data)

    await callback.message.edit_text(
        f'💰 Недостаточно средств для оформления подписки\n\n'
        f'Требуется: {texts.format_price(missing_amount)}\n'
        f'У вас: {texts.format_price(db_user.balance_kopeks)}\n\n'
        f'🛒 Ваша корзина сохранена!\n'
        f'После пополнения баланса вы сможете вернуться к оформлению подписки.\n\n'
        f'Выберите способ пополнения:',
        reply_markup=get_payment_methods_keyboard_with_cart(
            db_user.language,
            missing_amount,
        ),
        parse_mode='HTML',
    )


async def return_to_saved_cart(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    # Получаем данные корзины из Redis
    cart_data = await user_cart_service.get_user_cart(db_user.id)

    if not cart_data:
        await callback.answer('❌ Сохраненная корзина не найдена', show_alert=True)
        return

    texts = get_texts(db_user.language)

    # Проверяем режим корзины - если это тарифная корзина, перенаправляем на соответствующий обработчик
    cart_mode = cart_data.get('cart_mode')
    if cart_mode in ('tariff_purchase', 'daily_tariff_purchase', 'extend') and cart_data.get('tariff_id'):
        from .tariff_purchase import return_to_saved_tariff_cart

        await return_to_saved_tariff_cart(callback, state, db_user, db, cart_data)
        return

    preserved_metadata_keys = {
        'saved_cart',
        'missing_amount',
        'return_to_cart',
        'user_id',
    }
    preserved_metadata = {key: cart_data[key] for key in preserved_metadata_keys if key in cart_data}

    prepared_cart_data = dict(cart_data)

    if 'period_days' not in prepared_cart_data:
        await callback.answer('❌ Корзина повреждена. Оформите подписку заново.', show_alert=True)
        await user_cart_service.delete_user_cart(db_user.id)
        return

    if not settings.is_devices_selection_enabled():
        try:
            from .pricing import _prepare_subscription_summary

            _, recalculated_data = await _prepare_subscription_summary(
                db_user,
                prepared_cart_data,
                texts,
            )
        except ValueError as recalculation_error:
            logger.error(
                'Не удалось пересчитать сохраненную корзину пользователя %s: %s',
                db_user.telegram_id,
                recalculation_error,
            )
            forced_limit = settings.get_disabled_mode_device_limit()
            if forced_limit is None:
                forced_limit = settings.DEFAULT_DEVICE_LIMIT
            prepared_cart_data['devices'] = forced_limit
            removed_devices_total = prepared_cart_data.pop('total_devices_price', 0) or 0
            if removed_devices_total:
                prepared_cart_data['total_price'] = max(
                    0,
                    prepared_cart_data.get('total_price', 0) - removed_devices_total,
                )
            prepared_cart_data.pop('devices_discount_percent', None)
            prepared_cart_data.pop('devices_discount_total', None)
            prepared_cart_data.pop('devices_discounted_price_per_month', None)
            prepared_cart_data.pop('devices_price_per_month', None)
        else:
            normalized_cart_data = dict(prepared_cart_data)
            normalized_cart_data.update(recalculated_data)

            for key, value in preserved_metadata.items():
                normalized_cart_data[key] = value

            prepared_cart_data = normalized_cart_data

        if prepared_cart_data != cart_data:
            await user_cart_service.save_user_cart(db_user.id, prepared_cart_data)

    total_price = prepared_cart_data.get('total_price', 0)

    if db_user.balance_kopeks < total_price:
        missing_amount = total_price - db_user.balance_kopeks
        insufficient_keyboard = get_insufficient_balance_keyboard_with_cart(
            db_user.language,
            missing_amount,
        )
        insufficient_text = (
            f'❌ Все еще недостаточно средств\n\n'
            f'Требуется: {texts.format_price(total_price)}\n'
            f'У вас: {texts.format_price(db_user.balance_kopeks)}\n'
            f'Не хватает: {texts.format_price(missing_amount)}'
        )

        if _message_needs_update(callback.message, insufficient_text, insufficient_keyboard):
            await callback.message.edit_text(
                insufficient_text,
                reply_markup=insufficient_keyboard,
            )
        else:
            await callback.answer('ℹ️ Пополните баланс, чтобы завершить оформление.')
        return

    countries = await _get_available_countries(db_user.promo_group_id)
    selected_countries_names = []

    period_display = format_period_description(prepared_cart_data['period_days'], db_user.language)

    # Проверяем наличие ключа 'countries' в данных корзины
    cart_countries = prepared_cart_data.get('countries', [])
    for country in countries:
        if country['uuid'] in cart_countries:
            selected_countries_names.append(country['name'])

    if settings.is_traffic_fixed():
        traffic_value = prepared_cart_data.get('traffic_gb')
        if traffic_value is None:
            traffic_value = settings.get_fixed_traffic_limit()
        traffic_display = 'Безлимитный' if traffic_value == 0 else f'{traffic_value} ГБ'
    else:
        traffic_value = prepared_cart_data.get('traffic_gb', 0) or 0
        traffic_display = 'Безлимитный' if traffic_value == 0 else f'{traffic_value} ГБ'

    summary_lines = [
        '🛒 Восстановленная корзина',
        '',
        f'📅 Период: {period_display}',
        f'📊 Трафик: {traffic_display}',
        f'🌍 Страны: {", ".join(selected_countries_names)}',
    ]

    if settings.is_devices_selection_enabled():
        devices_value = prepared_cart_data.get('devices')
        if devices_value is not None:
            summary_lines.append(f'📱 Устройства: {devices_value}')

    summary_lines.extend(
        [
            '',
            f'💎 Общая стоимость: {texts.format_price(total_price)}',
            '',
            'Подтверждаете покупку?',
        ]
    )

    summary_text = '\n'.join(summary_lines)

    # Устанавливаем данные в FSM для продолжения процесса
    await state.set_data(prepared_cart_data)
    await state.set_state(SubscriptionStates.confirming_purchase)

    confirm_keyboard = get_subscription_confirm_keyboard_with_cart(db_user.language)

    if _message_needs_update(callback.message, summary_text, confirm_keyboard):
        await callback.message.edit_text(summary_text, reply_markup=confirm_keyboard, parse_mode='HTML')

    await callback.answer('✅ Корзина восстановлена!')


async def handle_extend_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Проверяем, доступно ли сообщение для редактирования
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription or subscription.is_trial:
        await callback.answer('⚠ Продление доступно только для платных подписок', show_alert=True)
        return

    # В режиме тарифов проверяем наличие tariff_id
    if settings.is_tariffs_mode():
        if subscription.tariff_id:
            # У подписки есть тариф - перенаправляем на продление по тарифу
            from .tariff_purchase import show_tariff_extend

            await show_tariff_extend(callback, db_user, db)
            return
        # У подписки нет тарифа - предлагаем выбрать тариф
        await callback.message.edit_text(
            '📦 <b>Выберите тариф для продления</b>\n\n'
            'Ваша текущая подписка была создана до введения тарифов.\n'
            'Для продления необходимо выбрать один из доступных тарифов.\n\n'
            '⚠️ Ваша текущая подписка продолжит действовать до окончания срока.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='📦 Выбрать тариф', callback_data='tariff_switch')],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')],
                ]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    subscription_service = SubscriptionService()

    available_periods = settings.get_available_renewal_periods()
    renewal_prices = {}
    promo_offer_percent = _get_promo_offer_discount_percent(db_user)

    for days in available_periods:
        try:
            months_in_period = calculate_months_from_days(days)

            from app.config import PERIOD_PRICES

            # 1. Calculate period price with promo group discount using unified system
            base_price_original = PERIOD_PRICES.get(days, 0)
            period_price_info = calculate_user_price(db_user, base_price_original, days, 'period')

            # 2. Calculate servers price with promo group discount
            servers_price_per_month, _ = await subscription_service.get_countries_price_by_uuids(
                subscription.connected_squads,
                db,
                promo_group_id=db_user.promo_group_id,
            )
            servers_total_base = servers_price_per_month * months_in_period
            servers_price_info = calculate_user_price(db_user, servers_total_base, days, 'servers')

            # 3. Calculate devices price with promo group discount
            device_limit = subscription.device_limit
            if device_limit is None:
                if settings.is_devices_selection_enabled():
                    device_limit = settings.DEFAULT_DEVICE_LIMIT
                else:
                    forced_limit = settings.get_disabled_mode_device_limit()
                    if forced_limit is None:
                        device_limit = settings.DEFAULT_DEVICE_LIMIT
                    else:
                        device_limit = forced_limit

            # Модем добавляет +1 к device_limit, но оплачивается отдельно,
            # поэтому не должен учитываться как платное устройство при продлении
            if getattr(subscription, 'modem_enabled', False):
                device_limit = max(1, device_limit - 1)

            additional_devices = max(0, (device_limit or 0) - settings.DEFAULT_DEVICE_LIMIT)
            devices_price_per_month = additional_devices * settings.PRICE_PER_DEVICE
            devices_total_base = devices_price_per_month * months_in_period
            devices_price_info = calculate_user_price(db_user, devices_total_base, days, 'devices')

            # 4. Calculate traffic price with promo group discount
            # В режиме fixed_with_topup при продлении трафик сбрасывается до фиксированного лимита
            if settings.is_traffic_fixed():
                renewal_traffic_gb = settings.get_fixed_traffic_limit()
            else:
                renewal_traffic_gb = subscription.traffic_limit_gb
            traffic_price_per_month = settings.get_traffic_price(renewal_traffic_gb)
            traffic_total_base = traffic_price_per_month * months_in_period
            traffic_price_info = calculate_user_price(db_user, traffic_total_base, days, 'traffic')

            # 5. Calculate ORIGINAL price (before ALL discounts)
            total_original_price = (
                period_price_info.base_price
                + servers_price_info.base_price
                + devices_price_info.base_price
                + traffic_price_info.base_price
            )

            # 6. Sum prices with promo group discounts applied
            total_price = (
                period_price_info.final_price
                + servers_price_info.final_price
                + devices_price_info.final_price
                + traffic_price_info.final_price
            )

            # 7. Apply promo offer discount on top of promo group discounts
            promo_component = _apply_promo_offer_discount(db_user, total_price)

            # Store: original = price before discounts, final = price with all discounts
            renewal_prices[days] = {
                'final': promo_component['discounted'],
                'original': total_original_price,
            }

        except Exception as e:
            logger.error(f'Ошибка расчета цены для периода {days}: {e}')
            continue

    if not renewal_prices:
        await callback.answer('⚠ Нет доступных периодов для продления', show_alert=True)
        return

    prices_text = ''

    for days in available_periods:
        if days not in renewal_prices:
            continue

        price_info = renewal_prices[days]

        if isinstance(price_info, dict):
            final_price = price_info.get('final')
            if final_price is None:
                final_price = price_info.get('original', 0)
            original_price = price_info.get('original', final_price)
        else:
            final_price = price_info
            original_price = final_price

        period_display = format_period_description(days, db_user.language)

        # Calculate discount percentage for PriceInfo
        discount_percent = 0
        if original_price > final_price and original_price > 0:
            discount_percent = ((original_price - final_price) * 100) // original_price

        # Create PriceInfo and format text using unified system
        price_info_obj = PriceInfo(
            base_price=original_price, final_price=final_price, discount_percent=discount_percent
        )

        prices_text += (
            format_price_text(
                period_label=period_display, price_info=price_info_obj, format_price_func=texts.format_price
            )
            + '\n'
        )

    promo_discounts_text = await _build_promo_group_discount_text(
        db_user,
        available_periods,
        texts=texts,
    )

    renewal_lines = [
        '⏰ Продление подписки',
        '',
        f'Осталось дней: {subscription.days_left}',
        '',
        '<b>Ваша текущая конфигурация:</b>',
        f'🌍 Серверов: {len(subscription.connected_squads)}',
        f'📊 Трафик: {texts.format_traffic(subscription.traffic_limit_gb)}',
    ]

    if settings.is_devices_selection_enabled():
        renewal_lines.append(f'📱 Устройств: {subscription.device_limit}')

    renewal_lines.extend(
        [
            '',
            '<b>Выберите период продления:</b>',
            prices_text.rstrip(),
            '',
        ]
    )

    message_text = '\n'.join(renewal_lines)

    if promo_discounts_text:
        message_text += f'{promo_discounts_text}\n\n'

    promo_offer_hint = await _get_promo_offer_hint(
        db,
        db_user,
        texts,
        promo_offer_percent,
    )
    if promo_offer_hint:
        message_text += f'{promo_offer_hint}\n\n'

    message_text += '💡 <i>Цена включает все ваши текущие серверы и настройки</i>'

    await callback.message.edit_text(
        message_text,
        reply_markup=get_extend_subscription_keyboard_with_prices(db_user.language, renewal_prices),
        parse_mode='HTML',
    )

    await callback.answer()


async def confirm_extend_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    days = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)

    # Валидация что период доступен для продления
    available_renewal_periods = settings.get_available_renewal_periods()
    if days not in available_renewal_periods:
        await callback.answer(
            texts.t('RENEWAL_PERIOD_NOT_AVAILABLE', '❌ Этот период больше недоступен для продления'), show_alert=True
        )
        return

    subscription = db_user.subscription

    if not subscription:
        await callback.answer('⚠ У вас нет активной подписки', show_alert=True)
        return

    months_in_period = calculate_months_from_days(days)
    old_end_date = subscription.end_date
    server_uuid_prices: dict[str, int] = {}

    try:
        from app.config import PERIOD_PRICES

        base_price_original = PERIOD_PRICES.get(days, 0)
        period_discount_percent = db_user.get_promo_discount('period', days)
        base_price, base_discount_total = apply_percentage_discount(
            base_price_original,
            period_discount_percent,
        )

        subscription_service = SubscriptionService()
        servers_price_per_month, per_server_monthly_prices = await subscription_service.get_countries_price_by_uuids(
            subscription.connected_squads,
            db,
            promo_group_id=db_user.promo_group_id,
        )
        servers_discount_percent = db_user.get_promo_discount(
            'servers',
            days,
        )
        total_servers_price = 0
        total_servers_discount = 0

        for squad_uuid, server_monthly_price in zip(
            subscription.connected_squads, per_server_monthly_prices, strict=False
        ):
            discount_per_month = server_monthly_price * servers_discount_percent // 100
            discounted_per_month = server_monthly_price - discount_per_month
            total_servers_price += discounted_per_month * months_in_period
            total_servers_discount += discount_per_month * months_in_period
            server_uuid_prices[squad_uuid] = discounted_per_month * months_in_period

        discounted_servers_price_per_month = servers_price_per_month - (
            servers_price_per_month * servers_discount_percent // 100
        )

        device_limit = subscription.device_limit
        if device_limit is None:
            if settings.is_devices_selection_enabled():
                device_limit = settings.DEFAULT_DEVICE_LIMIT
            else:
                forced_limit = settings.get_disabled_mode_device_limit()
                if forced_limit is None:
                    device_limit = settings.DEFAULT_DEVICE_LIMIT
                else:
                    device_limit = forced_limit

        # Модем добавляет +1 к device_limit, но оплачивается отдельно,
        # поэтому не должен учитываться как платное устройство при продлении
        if getattr(subscription, 'modem_enabled', False):
            device_limit = max(1, device_limit - 1)

        additional_devices = max(0, (device_limit or 0) - settings.DEFAULT_DEVICE_LIMIT)
        devices_price_per_month = additional_devices * settings.PRICE_PER_DEVICE
        devices_discount_percent = db_user.get_promo_discount(
            'devices',
            days,
        )
        devices_discount_per_month = devices_price_per_month * devices_discount_percent // 100
        discounted_devices_price_per_month = devices_price_per_month - devices_discount_per_month
        total_devices_price = discounted_devices_price_per_month * months_in_period

        # В режиме fixed_with_topup при продлении трафик сбрасывается до фиксированного лимита
        if settings.is_traffic_fixed():
            renewal_traffic_gb = settings.get_fixed_traffic_limit()
        else:
            renewal_traffic_gb = subscription.traffic_limit_gb
        traffic_price_per_month = settings.get_traffic_price(renewal_traffic_gb)
        traffic_discount_percent = db_user.get_promo_discount(
            'traffic',
            days,
        )
        traffic_discount_per_month = traffic_price_per_month * traffic_discount_percent // 100
        discounted_traffic_price_per_month = traffic_price_per_month - traffic_discount_per_month
        total_traffic_price = discounted_traffic_price_per_month * months_in_period

        price = base_price + total_servers_price + total_devices_price + total_traffic_price
        original_price = price
        promo_component = _apply_promo_offer_discount(db_user, price)
        if promo_component['discount'] > 0:
            price = promo_component['discounted']

        monthly_additions = (
            discounted_servers_price_per_month + discounted_devices_price_per_month + discounted_traffic_price_per_month
        )
        is_valid = validate_pricing_calculation(base_price, monthly_additions, months_in_period, original_price)

        if not is_valid:
            logger.error(f'Ошибка в расчете цены продления для пользователя {db_user.telegram_id}')
            await callback.answer('Ошибка расчета цены. Обратитесь в поддержку.', show_alert=True)
            return

        logger.info(f'💰 Расчет продления подписки {subscription.id} на {days} дней ({months_in_period} мес):')
        base_log = f'   📅 Период {days} дней: {base_price_original / 100}₽'
        if base_discount_total > 0:
            base_log += f' → {base_price / 100}₽ (скидка {period_discount_percent}%: -{base_discount_total / 100}₽)'
        logger.info(base_log)
        if total_servers_price > 0:
            logger.info(
                f'   🌐 Серверы: {servers_price_per_month / 100}₽/мес × {months_in_period}'
                f' = {total_servers_price / 100}₽'
                + (
                    f' (скидка {servers_discount_percent}%: -{total_servers_discount / 100}₽)'
                    if total_servers_discount > 0
                    else ''
                )
            )
        if total_devices_price > 0:
            logger.info(
                f'   📱 Устройства: {devices_price_per_month / 100}₽/мес × {months_in_period}'
                f' = {total_devices_price / 100}₽'
                + (
                    f' (скидка {devices_discount_percent}%: -{devices_discount_per_month * months_in_period / 100}₽)'
                    if devices_discount_percent > 0 and devices_discount_per_month > 0
                    else ''
                )
            )
        if total_traffic_price > 0:
            logger.info(
                f'   📊 Трафик: {traffic_price_per_month / 100}₽/мес × {months_in_period}'
                f' = {total_traffic_price / 100}₽'
                + (
                    f' (скидка {traffic_discount_percent}%: -{traffic_discount_per_month * months_in_period / 100}₽)'
                    if traffic_discount_percent > 0 and traffic_discount_per_month > 0
                    else ''
                )
            )
        if promo_component['discount'] > 0:
            logger.info(
                '   🎯 Промо-предложение: -%s₽ (%s%%)',
                promo_component['discount'] / 100,
                promo_component['percent'],
            )
        logger.info(f'   💎 ИТОГО: {price / 100}₽')

    except Exception as e:
        logger.error(f'⚠ ОШИБКА РАСЧЕТА ЦЕНЫ: {e}')
        await callback.answer('⚠ Ошибка расчета стоимости', show_alert=True)
        return

    if db_user.balance_kopeks < price:
        missing_kopeks = price - db_user.balance_kopeks
        required_text = texts.format_price(price)
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
            required=required_text,
            balance=texts.format_price(db_user.balance_kopeks),
            missing=texts.format_price(missing_kopeks),
        )

        # Подготовим данные для сохранения в корзину
        cart_data = {
            'cart_mode': 'extend',
            'subscription_id': subscription.id,
            'period_days': days,
            'total_price': price,
            'user_id': db_user.id,
            'saved_cart': True,
            'missing_amount': missing_kopeks,
            'return_to_cart': True,
            'description': f'Продление подписки на {days} дней',
            'consume_promo_offer': bool(promo_component['discount'] > 0),
        }

        await user_cart_service.save_user_cart(db_user.id, cart_data)

        await callback.message.edit_text(
            message_text,
            reply_markup=get_insufficient_balance_keyboard(
                db_user.language,
                amount_kopeks=missing_kopeks,
                has_saved_cart=True,  # Указываем, что есть сохраненная корзина
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    try:
        success = await subtract_user_balance(
            db,
            db_user,
            price,
            f'Продление подписки на {days} дней',
            consume_promo_offer=promo_component['discount'] > 0,
        )

        if not success:
            await callback.answer('⚠ Ошибка списания средств', show_alert=True)
            return

        current_time = datetime.utcnow()

        if subscription.end_date > current_time:
            new_end_date = subscription.end_date + timedelta(days=days)
        else:
            new_end_date = current_time + timedelta(days=days)

        subscription.end_date = new_end_date

        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.updated_at = current_time

        # В режиме fixed_with_topup при продлении сбрасываем трафик до фиксированного лимита
        traffic_was_reset = False
        old_traffic_limit = subscription.traffic_limit_gb
        if settings.is_traffic_fixed():
            fixed_limit = settings.get_fixed_traffic_limit()
            if subscription.traffic_limit_gb != fixed_limit or (subscription.purchased_traffic_gb or 0) > 0:
                traffic_was_reset = True
                subscription.traffic_limit_gb = fixed_limit
                subscription.purchased_traffic_gb = 0
                subscription.traffic_reset_at = None  # Сбрасываем дату сброса трафика
                logger.info(f'🔄 Сброс трафика при продлении: {old_traffic_limit} ГБ → {fixed_limit} ГБ')

        await db.commit()
        await db.refresh(subscription)
        await db.refresh(db_user)

        # ensure freshly loaded values are available even if SQLAlchemy expires
        # attributes on subsequent access
        refreshed_end_date = subscription.end_date
        refreshed_balance = db_user.balance_kopeks

        from app.database.crud.server_squad import get_server_ids_by_uuids
        from app.database.crud.subscription import add_subscription_servers

        server_ids = await get_server_ids_by_uuids(db, subscription.connected_squads)
        if server_ids:
            from sqlalchemy import select

            from app.database.models import ServerSquad

            result = await db.execute(
                select(ServerSquad.id, ServerSquad.squad_uuid).where(ServerSquad.id.in_(server_ids))
            )
            id_to_uuid = {row.id: row.squad_uuid for row in result}
            default_price = total_servers_price // len(server_ids) if server_ids else 0
            server_prices_for_period = [
                server_uuid_prices.get(id_to_uuid.get(server_id, ''), default_price) for server_id in server_ids
            ]
            await add_subscription_servers(db, subscription, server_ids, server_prices_for_period)

        try:
            remnawave_result = await subscription_service.update_remnawave_user(
                db,
                subscription,
                reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                reset_reason='продление подписки',
            )
            if remnawave_result:
                logger.info('✅ RemnaWave обновлен успешно')
            else:
                logger.error('⚠ ОШИБКА ОБНОВЛЕНИЯ REMNAWAVE')
        except Exception as e:
            logger.error(f'⚠ ИСКЛЮЧЕНИЕ ПРИ ОБНОВЛЕНИИ REMNAWAVE: {e}')

        transaction = await create_transaction(
            db=db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price,
            description=f'Продление подписки на {days} дней ({months_in_period} мес)',
        )

        try:
            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_extension_notification(
                db,
                db_user,
                subscription,
                transaction,
                days,
                old_end_date,
                new_end_date=refreshed_end_date,
                balance_after=refreshed_balance,
            )
        except Exception as e:
            logger.error(f'Ошибка отправки уведомления о продлении: {e}')

        success_message = (
            '✅ Подписка успешно продлена!\n\n'
            f'⏰ Добавлено: {days} дней\n'
            f'Действует до: {format_local_datetime(refreshed_end_date, "%d.%m.%Y %H:%M")}\n\n'
            f'💰 Списано: {texts.format_price(price)}'
        )

        # Добавляем уведомление о сбросе трафика
        if traffic_was_reset:
            fixed_limit = settings.get_fixed_traffic_limit()
            success_message += f'\n\n📊 Трафик сброшен до {fixed_limit} ГБ'

        if promo_component['discount'] > 0:
            success_message += (
                f' (включая доп. скидку {promo_component["percent"]}%:'
                f' -{texts.format_price(promo_component["discount"])})'
            )

        await callback.message.edit_text(success_message, reply_markup=get_back_keyboard(db_user.language))

        logger.info(f'✅ Пользователь {db_user.telegram_id} продлил подписку на {days} дней за {price / 100}₽')

    except Exception as e:
        logger.error(f'⚠ КРИТИЧЕСКАЯ ОШИБКА ПРОДЛЕНИЯ: {e}')
        import traceback

        logger.error(f'TRACEBACK: {traceback.format_exc()}')

        await callback.message.edit_text(
            '⚠ Произошла ошибка при продлении подписки. Обратитесь в поддержку.',
            reply_markup=get_back_keyboard(db_user.language),
        )

    await callback.answer()


async def select_period(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    period_days = int(callback.data.split('_')[1])
    texts = get_texts(db_user.language)

    # Валидация что период доступен
    available_periods = settings.get_available_subscription_periods()
    if period_days not in available_periods:
        await callback.answer(texts.t('PERIOD_NOT_AVAILABLE', '❌ Этот период больше недоступен'), show_alert=True)
        return

    # Получаем цену с защитой от KeyError
    period_price = PERIOD_PRICES.get(period_days, 0)

    data = await state.get_data()
    data['period_days'] = period_days
    data['total_price'] = period_price

    if settings.is_traffic_fixed():
        fixed_traffic_price = settings.get_traffic_price(settings.get_fixed_traffic_limit())
        data['total_price'] += fixed_traffic_price
        data['traffic_gb'] = settings.get_fixed_traffic_limit()

    await state.set_data(data)

    if settings.is_traffic_selectable():
        available_packages = [pkg for pkg in settings.get_traffic_packages() if pkg['enabled']]

        if not available_packages:
            await callback.answer('⚠️ Пакеты трафика не настроены', show_alert=True)
            return

        await callback.message.edit_text(
            texts.SELECT_TRAFFIC, reply_markup=get_traffic_packages_keyboard(db_user.language)
        )
        await state.set_state(SubscriptionStates.selecting_traffic)
        await callback.answer()
        return

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


async def select_devices(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    texts = get_texts(db_user.language)

    if not settings.is_devices_selection_enabled():
        await callback.answer(
            texts.t('DEVICES_SELECTION_DISABLED', '⚠️ Выбор количества устройств недоступен'),
            show_alert=True,
        )
        return

    if not callback.data.startswith('devices_') or callback.data == 'devices_continue':
        await callback.answer(texts.t('DEVICES_INVALID_REQUEST', '❌ Некорректный запрос'), show_alert=True)
        return

    try:
        devices = int(callback.data.split('_')[1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('DEVICES_INVALID_COUNT', '❌ Некорректное количество устройств'), show_alert=True)
        return

    data = await state.get_data()

    # Получаем цену периода с защитой от KeyError
    period_days = data.get('period_days')
    if not period_days or period_days not in PERIOD_PRICES:
        await callback.answer(
            texts.t('PERIOD_NOT_AVAILABLE', '❌ Период больше недоступен, начните заново'), show_alert=True
        )
        return

    base_price = PERIOD_PRICES.get(period_days, 0) + settings.get_traffic_price(data.get('traffic_gb', 0))

    countries = await _get_available_countries(db_user.promo_group_id)
    # Проверяем, что ключ 'countries' существует в данных перед доступом к нему
    selected_countries = data.get('countries', [])
    countries_price = sum(c['price_kopeks'] for c in countries if c['uuid'] in selected_countries)

    devices_price = max(0, devices - settings.DEFAULT_DEVICE_LIMIT) * settings.PRICE_PER_DEVICE

    previous_devices = data.get('devices', settings.DEFAULT_DEVICE_LIMIT)

    data['devices'] = devices
    data['total_price'] = base_price + countries_price + devices_price
    await state.set_data(data)

    if devices != previous_devices:
        try:
            await callback.message.edit_reply_markup(reply_markup=get_devices_keyboard(devices, db_user.language))
        except TelegramBadRequest as error:
            if 'message is not modified' in str(error).lower():
                logger.debug('ℹ️ Пропускаем обновление клавиатуры устройств: содержимое не изменилось')
            else:
                raise

    await callback.answer()


async def devices_continue(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    if callback.data != 'devices_continue':
        await callback.answer('⚠️ Некорректный запрос', show_alert=True)
        return

    if await present_subscription_summary(callback, state, db_user):
        await callback.answer()


async def confirm_purchase(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    # Проверка ограничения на покупку/продление подписки
    if getattr(db_user, 'restriction_subscription', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        texts = get_texts(db_user.language)
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription')])

        await callback.message.edit_text(
            f'🚫 <b>Покупка/продление подписки ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    data = await state.get_data()
    texts = get_texts(db_user.language)

    await save_subscription_checkout_draft(db_user.id, dict(data))
    resume_callback = 'subscription_resume_checkout' if should_offer_checkout_resume(db_user, True) else None

    countries = await _get_available_countries(db_user.promo_group_id)

    period_days = data.get('period_days')
    if period_days is None:
        await callback.message.edit_text(
            texts.t('SUBSCRIPTION_PURCHASE_ERROR', 'Ошибка при оформлении подписки. Попробуйте начать сначала.'),
            reply_markup=get_back_keyboard(db_user.language),
        )
        await callback.answer()
        return
    months_in_period = data.get('months_in_period', calculate_months_from_days(period_days))

    # Всегда пересчитываем base_price из PERIOD_PRICES для безопасности
    # (не доверяем кэшированным значениям из FSM данных)
    base_price_original = PERIOD_PRICES.get(period_days, 0)
    base_discount_percent = db_user.get_promo_discount(
        'period',
        period_days,
    )
    base_price, base_discount_total = apply_percentage_discount(
        base_price_original,
        base_discount_percent,
    )
    server_prices = data.get('server_prices_for_period', [])

    if not server_prices:
        countries_price_per_month = 0
        per_month_prices: list[int] = []
        for country in countries:
            # Проверяем, что ключ 'countries' существует в данных перед доступом к нему
            selected_countries = data.get('countries', [])
            if country['uuid'] in selected_countries:
                server_price_per_month = country['price_kopeks']
                countries_price_per_month += server_price_per_month
                per_month_prices.append(server_price_per_month)

        servers_discount_percent = db_user.get_promo_discount(
            'servers',
            period_days,
        )
        total_servers_price = 0
        total_servers_discount = 0
        discounted_servers_price_per_month = 0
        server_prices = []

        for server_price_per_month in per_month_prices:
            discounted_per_month, discount_per_month = apply_percentage_discount(
                server_price_per_month,
                servers_discount_percent,
            )
            total_price_for_server = discounted_per_month * months_in_period
            total_discount_for_server = discount_per_month * months_in_period

            discounted_servers_price_per_month += discounted_per_month
            total_servers_price += total_price_for_server
            total_servers_discount += total_discount_for_server
            server_prices.append(total_price_for_server)

        total_countries_price = total_servers_price
    else:
        total_countries_price = data.get('total_servers_price', sum(server_prices))
        countries_price_per_month = data.get('servers_price_per_month', 0)
        discounted_servers_price_per_month = data.get('servers_discounted_price_per_month', countries_price_per_month)
        total_servers_discount = data.get('servers_discount_total', 0)
        servers_discount_percent = data.get('servers_discount_percent', 0)

    devices_selection_enabled = settings.is_devices_selection_enabled()
    forced_disabled_limit: int | None = None
    if devices_selection_enabled:
        devices_selected = data.get('devices', settings.DEFAULT_DEVICE_LIMIT)
    else:
        forced_disabled_limit = settings.get_disabled_mode_device_limit()
        if forced_disabled_limit is None:
            devices_selected = settings.DEFAULT_DEVICE_LIMIT
        else:
            devices_selected = forced_disabled_limit

    additional_devices = max(0, devices_selected - settings.DEFAULT_DEVICE_LIMIT)
    devices_price_per_month = data.get('devices_price_per_month', additional_devices * settings.PRICE_PER_DEVICE)

    devices_discount_percent = 0
    discounted_devices_price_per_month = 0
    devices_discount_total = 0
    total_devices_price = 0

    if devices_selection_enabled and additional_devices > 0:
        if 'devices_discount_percent' in data:
            devices_discount_percent = data.get('devices_discount_percent', 0)
            discounted_devices_price_per_month = data.get('devices_discounted_price_per_month', devices_price_per_month)
            devices_discount_total = data.get('devices_discount_total', 0)
            total_devices_price = data.get('total_devices_price', discounted_devices_price_per_month * months_in_period)
        else:
            devices_discount_percent = db_user.get_promo_discount(
                'devices',
                period_days,
            )
            discounted_devices_price_per_month, discount_per_month = apply_percentage_discount(
                devices_price_per_month,
                devices_discount_percent,
            )
            devices_discount_total = discount_per_month * months_in_period
            total_devices_price = discounted_devices_price_per_month * months_in_period

    if settings.is_traffic_fixed():
        final_traffic_gb = settings.get_fixed_traffic_limit()
        traffic_price_per_month = data.get('traffic_price_per_month', settings.get_traffic_price(final_traffic_gb))
    else:
        final_traffic_gb = data.get('final_traffic_gb', data.get('traffic_gb'))
        traffic_gb = data.get('traffic_gb')
        if traffic_gb is not None:
            traffic_price_per_month = data.get('traffic_price_per_month', settings.get_traffic_price(traffic_gb))
        else:
            traffic_price_per_month = data.get('traffic_price_per_month', 0)

    if 'traffic_discount_percent' in data:
        traffic_discount_percent = data.get('traffic_discount_percent', 0)
        discounted_traffic_price_per_month = data.get('traffic_discounted_price_per_month', traffic_price_per_month)
        traffic_discount_total = data.get('traffic_discount_total', 0)
        total_traffic_price = data.get('total_traffic_price', discounted_traffic_price_per_month * months_in_period)
    else:
        traffic_discount_percent = db_user.get_promo_discount(
            'traffic',
            period_days,
        )
        discounted_traffic_price_per_month, discount_per_month = apply_percentage_discount(
            traffic_price_per_month,
            traffic_discount_percent,
        )
        traffic_discount_total = discount_per_month * months_in_period
        total_traffic_price = discounted_traffic_price_per_month * months_in_period

    total_servers_price = data.get('total_servers_price', total_countries_price)

    cached_total_price = data.get('total_price', 0)
    cached_promo_discount_value = data.get('promo_offer_discount_value', 0)

    # Всегда пересчитываем monthly_additions из компонентов для безопасности
    discounted_monthly_additions = (
        discounted_traffic_price_per_month + discounted_servers_price_per_month + discounted_devices_price_per_month
    )

    # Вычисляем ожидаемую цену до промо-скидки из компонентов
    calculated_total_before_promo = base_price + (discounted_monthly_additions * months_in_period)

    # Получаем сохраненную цену до промо-скидки или используем вычисленную
    validation_total_price = data.get('total_price_before_promo_offer')
    if validation_total_price is None and cached_promo_discount_value > 0:
        validation_total_price = cached_total_price + cached_promo_discount_value
    if validation_total_price is None:
        validation_total_price = cached_total_price

    current_promo_offer_percent = _get_promo_offer_discount_percent(db_user)
    if current_promo_offer_percent > 0:
        final_price, promo_offer_discount_value = apply_percentage_discount(
            calculated_total_before_promo,
            current_promo_offer_percent,
        )
        promo_offer_discount_percent = current_promo_offer_percent
    else:
        final_price = calculated_total_before_promo
        promo_offer_discount_value = 0
        promo_offer_discount_percent = 0

    # Валидация: проверяем что cached_total_price соответствует ожидаемой финальной цене
    # Допускаем небольшое расхождение из-за округления (до 5%)
    price_difference = abs(final_price - cached_total_price)
    max_allowed_difference = max(500, int(final_price * 0.05))  # 5% или минимум 5₽

    if price_difference > max_allowed_difference:
        # Слишком большое расхождение - блокируем покупку
        logger.error(
            f'Критическое расхождение цены для пользователя {db_user.telegram_id}: '
            f'кэш={cached_total_price / 100}₽, пересчет={final_price / 100}₽, '
            f'разница={price_difference / 100}₽ (>{max_allowed_difference / 100}₽). '
            f'Покупка заблокирована.'
        )
        await callback.answer('Цена изменилась. Пожалуйста, начните оформление заново.', show_alert=True)
        return
    if price_difference > 100:  # допуск 1₽
        # Небольшое расхождение - логируем предупреждение но продолжаем
        logger.warning(
            f'Расхождение цены для пользователя {db_user.telegram_id}: '
            f'кэш={cached_total_price / 100}₽, пересчет={final_price / 100}₽. '
            f'Используем пересчитанную цену.'
        )

    # Используем пересчитанную цену
    validation_total_price = calculated_total_before_promo

    logger.info(f'Расчет покупки подписки на {data["period_days"]} дней ({months_in_period} мес):')
    base_log = f'   Период: {base_price_original / 100}₽'
    if base_discount_total and base_discount_total > 0:
        base_log += f' → {base_price / 100}₽ (скидка {base_discount_percent}%: -{base_discount_total / 100}₽)'
    logger.info(base_log)
    if total_traffic_price > 0:
        message = f'   Трафик: {traffic_price_per_month / 100}₽/мес × {months_in_period} = {total_traffic_price / 100}₽'
        if traffic_discount_total > 0:
            message += f' (скидка {traffic_discount_percent}%: -{traffic_discount_total / 100}₽)'
        logger.info(message)
    if total_servers_price > 0:
        message = (
            f'   Серверы: {countries_price_per_month / 100}₽/мес × {months_in_period} = {total_servers_price / 100}₽'
        )
        if total_servers_discount > 0:
            message += f' (скидка {servers_discount_percent}%: -{total_servers_discount / 100}₽)'
        logger.info(message)
    if total_devices_price > 0:
        message = (
            f'   Устройства: {devices_price_per_month / 100}₽/мес × {months_in_period} = {total_devices_price / 100}₽'
        )
        if devices_discount_total > 0:
            message += f' (скидка {devices_discount_percent}%: -{devices_discount_total / 100}₽)'
        logger.info(message)
    if promo_offer_discount_value > 0:
        logger.info(
            '   🎯 Промо-предложение: -%s₽ (%s%%)',
            promo_offer_discount_value / 100,
            promo_offer_discount_percent,
        )
    logger.info(f'   ИТОГО: {final_price / 100}₽')

    if db_user.balance_kopeks < final_price:
        missing_kopeks = final_price - db_user.balance_kopeks
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
            required=texts.format_price(final_price),
            balance=texts.format_price(db_user.balance_kopeks),
            missing=texts.format_price(missing_kopeks),
        )

        # Сохраняем данные корзины в Redis перед переходом к пополнению
        cart_data = {
            **data,
            'saved_cart': True,
            'missing_amount': missing_kopeks,
            'return_to_cart': True,
            'user_id': db_user.id,
        }

        await user_cart_service.save_user_cart(db_user.id, cart_data)

        await callback.message.edit_text(
            message_text,
            reply_markup=get_insufficient_balance_keyboard(
                db_user.language,
                resume_callback=resume_callback,
                amount_kopeks=missing_kopeks,
                has_saved_cart=True,  # Указываем, что есть сохраненная корзина
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    purchase_completed = False

    try:
        success = await subtract_user_balance(
            db,
            db_user,
            final_price,
            f'Покупка подписки на {data["period_days"]} дней',
            consume_promo_offer=promo_offer_discount_value > 0,
        )

        if not success:
            missing_kopeks = final_price - db_user.balance_kopeks
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
                required=texts.format_price(final_price),
                balance=texts.format_price(db_user.balance_kopeks),
                missing=texts.format_price(missing_kopeks),
            )

            await callback.message.edit_text(
                message_text,
                reply_markup=get_insufficient_balance_keyboard(
                    db_user.language,
                    resume_callback=resume_callback,
                    amount_kopeks=missing_kopeks,
                ),
                parse_mode='HTML',
            )
            await callback.answer()
            return

        existing_subscription = db_user.subscription
        if devices_selection_enabled:
            selected_devices = devices_selected
        else:
            selected_devices = forced_disabled_limit

        should_update_devices = selected_devices is not None

        was_trial_conversion = False
        current_time = datetime.utcnow()

        if existing_subscription:
            logger.info(f'Обновляем существующую подписку пользователя {db_user.telegram_id}')

            bonus_period = timedelta()

            if existing_subscription.is_trial:
                logger.info(f'Конверсия из триала в платную для пользователя {db_user.telegram_id}')
                was_trial_conversion = True

                trial_duration = (current_time - existing_subscription.start_date).days

                if settings.TRIAL_ADD_REMAINING_DAYS_TO_PAID and existing_subscription.end_date:
                    remaining_trial_delta = existing_subscription.end_date - current_time
                    if remaining_trial_delta.total_seconds() > 0:
                        bonus_period = remaining_trial_delta
                        logger.info(
                            'Добавляем оставшееся время триала (%s) к новой подписке пользователя %s',
                            bonus_period,
                            db_user.telegram_id,
                        )

                try:
                    from app.database.crud.subscription_conversion import create_subscription_conversion

                    await create_subscription_conversion(
                        db=db,
                        user_id=db_user.id,
                        trial_duration_days=trial_duration,
                        payment_method='balance',
                        first_payment_amount_kopeks=final_price,
                        first_paid_period_days=period_days,
                    )
                    logger.info(
                        f'Записана конверсия: {trial_duration} дн. триал → {period_days} дн. платная за {final_price / 100}₽'
                    )
                except Exception as conversion_error:
                    logger.error(f'Ошибка записи конверсии: {conversion_error}')

            existing_subscription.is_trial = False
            existing_subscription.status = SubscriptionStatus.ACTIVE.value
            existing_subscription.traffic_limit_gb = final_traffic_gb
            if should_update_devices:
                existing_subscription.device_limit = selected_devices
            # Проверяем, что при обновлении существующей подписки есть хотя бы одна страна
            selected_countries = data.get('countries')
            if not selected_countries:
                # Иногда после возврата к оформлению из сохраненной корзины список стран не передается.
                # В таком случае повторно используем текущие подключенные страны подписки.
                selected_countries = existing_subscription.connected_squads or []
                if selected_countries:
                    data['countries'] = selected_countries  # чтобы далее использовать фактический список стран

            if not selected_countries:
                texts = get_texts(db_user.language)
                await callback.message.edit_text(
                    texts.t(
                        'COUNTRIES_MINIMUM_REQUIRED',
                        '❌ Нельзя отключить все страны. Должна быть подключена хотя бы одна страна.',
                    ),
                    reply_markup=get_back_keyboard(db_user.language),
                )
                await callback.answer()
                return

            existing_subscription.connected_squads = selected_countries

            # Если подписка еще активна, продлеваем от текущей даты окончания,
            # иначе начинаем новый период с текущего момента
            extension_base_date = current_time
            if existing_subscription.end_date and existing_subscription.end_date > current_time:
                extension_base_date = existing_subscription.end_date
            else:
                existing_subscription.start_date = current_time

            existing_subscription.end_date = extension_base_date + timedelta(days=period_days) + bonus_period
            existing_subscription.updated_at = current_time

            existing_subscription.traffic_used_gb = 0.0

            await db.commit()
            await db.refresh(existing_subscription)
            subscription = existing_subscription

        else:
            logger.info(f'Создаем новую подписку для пользователя {db_user.telegram_id}')
            default_device_limit = getattr(settings, 'DEFAULT_DEVICE_LIMIT', 1)
            resolved_device_limit = selected_devices

            if resolved_device_limit is None:
                if devices_selection_enabled:
                    resolved_device_limit = default_device_limit
                elif forced_disabled_limit is not None:
                    resolved_device_limit = forced_disabled_limit
                else:
                    resolved_device_limit = default_device_limit

            if resolved_device_limit is None and devices_selection_enabled:
                resolved_device_limit = default_device_limit

            # Проверяем, что для новой подписки также есть хотя бы одна страна, если пользователь проходит через интерфейс стран
            new_subscription_countries = data.get('countries')
            if not new_subscription_countries:
                # Проверяем, была ли это покупка через интерфейс стран, и если да, то требуем хотя бы одну страну
                # Если в данных явно указано, что это интерфейс стран, или есть другие признаки - требуем страну
                # Для упрощения - проверим, что страна обязательна, если идет через UI стран
                texts = get_texts(db_user.language)
                await callback.message.edit_text(
                    texts.t(
                        'COUNTRIES_MINIMUM_REQUIRED',
                        '❌ Нельзя отключить все страны. Должна быть подключена хотя бы одна страна.',
                    ),
                    reply_markup=get_back_keyboard(db_user.language),
                )
                await callback.answer()
                return

            subscription = await create_paid_subscription_with_traffic_mode(
                db=db,
                user_id=db_user.id,
                duration_days=period_days,
                device_limit=resolved_device_limit,
                connected_squads=new_subscription_countries,
                traffic_gb=final_traffic_gb,
            )

        from app.utils.user_utils import mark_user_as_had_paid_subscription

        await mark_user_as_had_paid_subscription(db, db_user)

        from app.database.crud.server_squad import add_user_to_servers, get_server_ids_by_uuids
        from app.database.crud.subscription import add_subscription_servers

        server_ids = await get_server_ids_by_uuids(db, data.get('countries', []))

        if server_ids:
            await add_subscription_servers(db, subscription, server_ids, server_prices)
            await add_user_to_servers(db, server_ids)

            logger.info(f'Сохранены цены серверов за весь период: {server_prices}')

        await db.refresh(db_user)

        subscription_service = SubscriptionService()
        # При покупке подписки ВСЕГДА сбрасываем трафик в панели
        if db_user.remnawave_uuid:
            remnawave_user = await subscription_service.update_remnawave_user(
                db,
                subscription,
                reset_traffic=True,
                reset_reason='покупка подписки',
            )
        else:
            remnawave_user = await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=True,
                reset_reason='покупка подписки',
            )

        if not remnawave_user:
            logger.error(f'Не удалось создать/обновить RemnaWave пользователя для {db_user.telegram_id}')
            remnawave_user = await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=True,
                reset_reason='покупка подписки (повторная попытка)',
            )

        transaction = await create_transaction(
            db=db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=final_price,
            description=f'Подписка на {period_days} дней ({months_in_period} мес)',
        )

        try:
            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_purchase_notification(
                db, db_user, subscription, transaction, period_days, was_trial_conversion
            )
        except Exception as e:
            logger.error(f'Ошибка отправки уведомления о покупке: {e}')

        await db.refresh(db_user)
        await db.refresh(subscription)

        subscription_link = get_display_subscription_link(subscription)
        hide_subscription_link = settings.should_hide_subscription_link()

        discount_note = ''
        if promo_offer_discount_value > 0:
            discount_note = texts.t(
                'SUBSCRIPTION_PROMO_DISCOUNT_NOTE',
                '⚡ Доп. скидка {percent}%: -{amount}',
            ).format(
                percent=promo_offer_discount_percent,
                amount=texts.format_price(promo_offer_discount_value),
            )

        if remnawave_user and subscription_link:
            if settings.is_happ_cryptolink_mode():
                success_text = (
                    f'{texts.SUBSCRIPTION_PURCHASED}\n\n'
                    + texts.t(
                        'SUBSCRIPTION_HAPP_LINK_PROMPT',
                        '🔒 Ссылка на подписку создана. Нажмите кнопку "Подключиться" ниже, чтобы открыть её в Happ.',
                    )
                    + '\n\n'
                    + texts.t(
                        'SUBSCRIPTION_IMPORT_INSTRUCTION_PROMPT',
                        '📱 Нажмите кнопку ниже, чтобы получить инструкцию по настройке VPN на вашем устройстве',
                    )
                )
            elif hide_subscription_link:
                success_text = (
                    f'{texts.SUBSCRIPTION_PURCHASED}\n\n'
                    + texts.t(
                        'SUBSCRIPTION_LINK_HIDDEN_NOTICE',
                        'ℹ️ Ссылка подписки доступна по кнопкам ниже или в разделе "Моя подписка".',
                    )
                    + '\n\n'
                    + texts.t(
                        'SUBSCRIPTION_IMPORT_INSTRUCTION_PROMPT',
                        '📱 Нажмите кнопку ниже, чтобы получить инструкцию по настройке VPN на вашем устройстве',
                    )
                )
            else:
                import_link_section = texts.t(
                    'SUBSCRIPTION_IMPORT_LINK_SECTION',
                    '🔗 <b>Ваша ссылка для импорта в VPN приложение:</b>\\n<code>{subscription_url}</code>',
                ).format(subscription_url=subscription_link)

                success_text = (
                    f'{texts.SUBSCRIPTION_PURCHASED}\n\n'
                    f'{import_link_section}\n\n'
                    f'{texts.t("SUBSCRIPTION_IMPORT_INSTRUCTION_PROMPT", "📱 Нажмите кнопку ниже, чтобы получить инструкцию по настройке VPN на вашем устройстве")}'
                )

            if discount_note:
                success_text = f'{success_text}\n\n{discount_note}'

            connect_mode = settings.CONNECT_BUTTON_MODE

            if connect_mode == 'miniapp_subscription':
                connect_keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                                web_app=types.WebAppInfo(url=subscription_link),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )
            elif connect_mode == 'miniapp_custom':
                if not settings.MINIAPP_CUSTOM_URL:
                    await callback.answer(
                        texts.t(
                            'CUSTOM_MINIAPP_URL_NOT_SET',
                            '⚠ Кастомная ссылка для мини-приложения не настроена',
                        ),
                        show_alert=True,
                    )
                    return

                connect_keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                                web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )
            elif connect_mode == 'link':
                rows = [
                    [InlineKeyboardButton(text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'), url=subscription_link)]
                ]
                happ_row = get_happ_download_button_row(texts)
                if happ_row:
                    rows.append(happ_row)
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'), callback_data='back_to_menu'
                        )
                    ]
                )
                connect_keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
            elif connect_mode == 'happ_cryptolink':
                rows = [
                    [
                        InlineKeyboardButton(
                            text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                            callback_data='open_subscription_link',
                        )
                    ]
                ]
                happ_row = get_happ_download_button_row(texts)
                if happ_row:
                    rows.append(happ_row)
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'), callback_data='back_to_menu'
                        )
                    ]
                )
                connect_keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
            else:
                connect_keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'), callback_data='subscription_connect'
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )

            await callback.message.edit_text(success_text, reply_markup=connect_keyboard, parse_mode='HTML')
        else:
            purchase_text = texts.SUBSCRIPTION_PURCHASED
            if discount_note:
                purchase_text = f'{purchase_text}\n\n{discount_note}'
            await callback.message.edit_text(
                texts.t(
                    'SUBSCRIPTION_LINK_GENERATING_NOTICE',
                    "{purchase_text}\n\nСсылка генерируется, перейдите в раздел 'Моя подписка' через несколько секунд.",
                ).format(purchase_text=purchase_text),
                reply_markup=get_back_keyboard(db_user.language),
            )

        purchase_completed = True
        logger.info(
            f'Пользователь {db_user.telegram_id} купил подписку на {data["period_days"]} дней за {final_price / 100}₽'
        )

    except Exception as e:
        logger.error(f'Ошибка покупки подписки: {e}')
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    if purchase_completed:
        await clear_subscription_checkout_draft(db_user.id)

    await state.clear()
    await callback.answer()


async def resume_subscription_checkout(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
):
    texts = get_texts(db_user.language)

    draft = await get_subscription_checkout_draft(db_user.id)

    if not draft:
        await callback.answer(texts.NO_SAVED_SUBSCRIPTION_ORDER, show_alert=True)
        return

    try:
        summary_text, prepared_data = await _prepare_subscription_summary(db_user, draft, texts)
    except ValueError as exc:
        logger.error(f'Ошибка восстановления заказа подписки для пользователя {db_user.telegram_id}: {exc}')
        await clear_subscription_checkout_draft(db_user.id)
        await callback.answer(texts.NO_SAVED_SUBSCRIPTION_ORDER, show_alert=True)
        return

    await state.set_data(prepared_data)
    await state.set_state(SubscriptionStates.confirming_purchase)
    await save_subscription_checkout_draft(db_user.id, prepared_data)

    await callback.message.edit_text(
        summary_text,
        reply_markup=get_subscription_confirm_keyboard(db_user.language),
        parse_mode='HTML',
    )

    await callback.answer()


async def create_paid_subscription_with_traffic_mode(
    db: AsyncSession,
    user_id: int,
    duration_days: int,
    device_limit: int | None,
    connected_squads: list[str],
    traffic_gb: int | None = None,
):
    from app.config import settings

    if traffic_gb is None:
        if settings.is_traffic_fixed():
            traffic_limit_gb = settings.get_fixed_traffic_limit()
        else:
            traffic_limit_gb = 0
    else:
        traffic_limit_gb = traffic_gb

    create_kwargs = dict(
        db=db,
        user_id=user_id,
        duration_days=duration_days,
        traffic_limit_gb=traffic_limit_gb,
        connected_squads=connected_squads,
        update_server_counters=False,
    )

    if device_limit is not None:
        create_kwargs['device_limit'] = device_limit

    subscription = await create_paid_subscription(**create_kwargs)

    logger.info(f'📋 Создана подписка с трафиком: {traffic_limit_gb} ГБ (режим: {settings.TRAFFIC_SELECTION_MODE})')

    return subscription


async def handle_subscription_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Проверяем, доступно ли сообщение для редактирования
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    # Получаем тариф подписки если есть
    tariff = None
    if subscription and subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_SETTINGS_PAID_ONLY',
                '⚠️ Настройки доступны только для платных подписок',
            ),
            show_alert=True,
        )
        return

    show_devices = settings.is_devices_selection_enabled()

    if show_devices:
        devices_used = await get_current_devices_count(db_user)
    else:
        devices_used = 0

    settings_template = texts.t(
        'SUBSCRIPTION_SETTINGS_OVERVIEW',
        (
            '⚙️ <b>Настройки подписки</b>\n\n'
            '📊 <b>Текущие параметры:</b>\n'
            '🌐 Стран: {countries_count}\n'
            '📈 Трафик: {traffic_used} / {traffic_limit}\n'
            '📱 Устройства: {devices_used} / {devices_limit}\n\n'
            'Выберите что хотите изменить:'
        ),
    )

    if not show_devices:
        settings_template = settings_template.replace(
            '\n📱 Устройства: {devices_used} / {devices_limit}',
            '',
        )

    # Формируем отображение лимита устройств с учётом модема
    modem_enabled = getattr(subscription, 'modem_enabled', False) or False
    if modem_enabled and settings.is_modem_enabled():
        visible_device_limit = (subscription.device_limit or 1) - 1
        devices_limit_display = f'{visible_device_limit} + модем'
    else:
        devices_limit_display = str(subscription.device_limit)

    settings_text = settings_template.format(
        countries_count=len(subscription.connected_squads),
        traffic_used=texts.format_traffic(subscription.traffic_used_gb, is_limit=False),
        traffic_limit=texts.format_traffic(subscription.traffic_limit_gb, is_limit=True),
        devices_used=devices_used,
        devices_limit=devices_limit_display,
    )

    show_countries = await _should_show_countries_management(db_user)

    await callback.message.edit_text(
        settings_text,
        reply_markup=get_updated_subscription_settings_keyboard(
            db_user.language, show_countries, tariff=tariff, subscription=subscription
        ),
        parse_mode='HTML',
    )
    await callback.answer()


async def clear_saved_cart(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    # Очищаем как FSM, так и Redis
    await state.clear()
    await user_cart_service.delete_user_cart(db_user.id)

    from app.handlers.menu import show_main_menu

    await show_main_menu(callback, db_user, db)

    await callback.answer('🗑️ Корзина очищена')


# ============== ХЕНДЛЕР ПАУЗЫ СУТОЧНОЙ ПОДПИСКИ ==============


async def handle_toggle_daily_subscription_pause(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Переключает паузу суточной подписки."""
    from app.database.crud.subscription import toggle_daily_subscription_pause
    from app.database.crud.tariff import get_tariff_by_id

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription:
        await callback.answer(texts.t('NO_SUBSCRIPTION_ERROR', '❌ У вас нет активной подписки'), show_alert=True)
        return

    # Проверяем что это суточный тариф
    tariff = None
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    if not tariff or not getattr(tariff, 'is_daily', False):
        await callback.answer(
            texts.t('NOT_DAILY_TARIFF_ERROR', '❌ Эта функция доступна только для суточных тарифов'), show_alert=True
        )
        return

    # Прикрепляем тариф к подписке для CRUD функций
    subscription.tariff = tariff

    # Переключаем статус паузы
    was_paused = getattr(subscription, 'is_daily_paused', False)

    # При возобновлении проверяем баланс
    if was_paused:
        daily_price = getattr(tariff, 'daily_price_kopeks', 0)
        if daily_price > 0 and db_user.balance_kopeks < daily_price:
            await callback.answer(
                texts.t(
                    'INSUFFICIENT_BALANCE_FOR_RESUME',
                    f'❌ Недостаточно средств для возобновления. Требуется: {settings.format_price(daily_price)}',
                ),
                show_alert=True,
            )
            return

    subscription = await toggle_daily_subscription_pause(db, subscription)

    if was_paused:
        # Была пауза, теперь возобновили
        message = texts.t('DAILY_SUBSCRIPTION_RESUMED', '▶️ Подписка возобновлена!')
        # Синхронизируем с Remnawave - активируем пользователя
        try:
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=False,
                reset_reason=None,
            )
            logger.info(f'✅ Синхронизировано с Remnawave после возобновления суточной подписки {subscription.id}')
        except Exception as e:
            logger.error(f'Ошибка синхронизации с Remnawave при возобновлении: {e}')
    else:
        # Была активна, теперь на паузе
        message = texts.t('DAILY_SUBSCRIPTION_PAUSED', '⏸️ Подписка приостановлена!')
        # При паузе можно отключить пользователя в Remnawave (опционально)
        # Пока оставляем активным, т.к. пауза - это только остановка списания

    await callback.answer(message, show_alert=True)

    # Возвращаемся в меню подписки - вызываем show_subscription_info
    await db.refresh(db_user)
    await show_subscription_info(callback, db_user, db)


# ============== ХЕНДЛЕРЫ ПЛАТНОГО ТРИАЛА ==============


@error_handler
async def handle_trial_pay_with_balance(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Обрабатывает оплату триала с баланса."""
    from app.services.trial_activation_service import get_trial_activation_charge_amount

    texts = get_texts(db_user.language)

    # Проверяем права на триал
    # PENDING триальные подписки не считаются - пользователь может повторить оплату
    trial_blocked = False
    if db_user.has_had_paid_subscription:
        trial_blocked = True
    elif db_user.subscription:
        sub = db_user.subscription
        # Разрешаем если это PENDING триальная подписка (повторная попытка оплаты)
        if not (sub.status == SubscriptionStatus.PENDING.value and sub.is_trial):
            trial_blocked = True

    if trial_blocked:
        await callback.message.edit_text(texts.TRIAL_ALREADY_USED, reply_markup=get_back_keyboard(db_user.language))
        await callback.answer()
        return

    trial_price_kopeks = get_trial_activation_charge_amount()
    if trial_price_kopeks <= 0:
        await callback.answer('❌ Ошибка: триал бесплатный', show_alert=True)
        return

    user_balance_kopeks = getattr(db_user, 'balance_kopeks', 0) or 0
    if user_balance_kopeks < trial_price_kopeks:
        await callback.answer(texts.t('INSUFFICIENT_BALANCE', '❌ Недостаточно средств на балансе'), show_alert=True)
        return

    # Списываем с баланса
    success = await subtract_user_balance(
        db,
        db_user,
        trial_price_kopeks,
        texts.t('TRIAL_PAYMENT_DESCRIPTION', 'Оплата пробной подписки'),
    )

    if not success:
        await callback.answer(texts.t('PAYMENT_FAILED', '❌ Не удалось списать средства'), show_alert=True)
        return

    await db.refresh(db_user)

    # Создаем триальную подписку
    subscription: Subscription | None = None
    remnawave_user = None

    try:
        forced_devices = None
        if not settings.is_devices_selection_enabled():
            forced_devices = settings.get_disabled_mode_device_limit()

        subscription = await create_trial_subscription(
            db,
            db_user.id,
            device_limit=forced_devices,
        )

        await db.refresh(db_user)

        subscription_service = SubscriptionService()
        try:
            remnawave_user = await subscription_service.create_remnawave_user(
                db,
                subscription,
            )
        except RemnaWaveConfigurationError as error:
            logger.error('RemnaWave update skipped due to configuration error: %s', error)
            # Откатываем подписку и возвращаем деньги
            await rollback_trial_subscription_activation(db, subscription)
            from app.database.crud.user import add_user_balance

            await add_user_balance(
                db,
                db_user,
                trial_price_kopeks,
                texts.t('TRIAL_REFUND_DESCRIPTION', 'Возврат за неудачную активацию триала'),
                transaction_type=TransactionType.REFUND,
            )
            await db.refresh(db_user)

            await callback.message.edit_text(
                texts.t(
                    'TRIAL_PROVISIONING_FAILED',
                    'Не удалось завершить активацию триала. Средства возвращены на баланс.',
                ),
                reply_markup=get_back_keyboard(db_user.language),
            )
            await callback.answer()
            return
        except Exception as error:
            logger.error(
                'Failed to create RemnaWave user for trial subscription %s: %s',
                getattr(subscription, 'id', '<unknown>'),
                error,
            )
            # Откатываем подписку и возвращаем деньги
            await rollback_trial_subscription_activation(db, subscription)
            from app.database.crud.user import add_user_balance

            await add_user_balance(
                db,
                db_user,
                trial_price_kopeks,
                texts.t('TRIAL_REFUND_DESCRIPTION', 'Возврат за неудачную активацию триала'),
                transaction_type=TransactionType.REFUND,
            )
            await db.refresh(db_user)

            await callback.message.edit_text(
                texts.t(
                    'TRIAL_PROVISIONING_FAILED',
                    'Не удалось завершить активацию триала. Средства возвращены на баланс.',
                ),
                reply_markup=get_back_keyboard(db_user.language),
            )
            await callback.answer()
            return

        # Отправляем уведомление админам
        try:
            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_trial_activation_notification(
                db,
                db_user,
                subscription,
                charged_amount_kopeks=trial_price_kopeks,
            )
        except Exception as e:
            logger.error(f'Ошибка отправки уведомления о триале: {e}')

        # Показываем успешное сообщение с ссылкой
        subscription_link = get_display_subscription_link(subscription)
        hide_subscription_link = settings.should_hide_subscription_link()

        payment_note = '\n\n' + texts.t(
            'TRIAL_PAYMENT_CHARGED_NOTE',
            '💳 С вашего баланса списано {amount}.',
        ).format(amount=settings.format_price(trial_price_kopeks))

        if remnawave_user and subscription_link:
            if settings.is_happ_cryptolink_mode():
                trial_success_text = (
                    f'{texts.TRIAL_ACTIVATED}\n\n'
                    + texts.t(
                        'SUBSCRIPTION_HAPP_LINK_PROMPT',
                        '🔒 Ссылка на подписку создана. Нажмите кнопку "Подключиться" ниже, чтобы открыть её в Happ.',
                    )
                    + '\n\n'
                    + texts.t(
                        'SUBSCRIPTION_IMPORT_INSTRUCTION_PROMPT',
                        '📱 Нажмите кнопку ниже, чтобы получить инструкцию по настройке VPN на вашем устройстве',
                    )
                )
            elif hide_subscription_link:
                trial_success_text = (
                    f'{texts.TRIAL_ACTIVATED}\n\n'
                    + texts.t(
                        'SUBSCRIPTION_LINK_HIDDEN_NOTICE',
                        'ℹ️ Ссылка подписки доступна по кнопкам ниже или в разделе "Моя подписка".',
                    )
                    + '\n\n'
                    + texts.t(
                        'SUBSCRIPTION_IMPORT_INSTRUCTION_PROMPT',
                        '📱 Нажмите кнопку ниже, чтобы получить инструкцию по настройке VPN на вашем устройстве',
                    )
                )
            else:
                subscription_import_link = texts.t(
                    'SUBSCRIPTION_IMPORT_LINK_SECTION',
                    '🔗 <b>Ваша ссылка для импорта в VPN приложение:</b>\n<code>{subscription_url}</code>',
                ).format(subscription_url=subscription_link)

                trial_success_text = (
                    f'{texts.TRIAL_ACTIVATED}\n\n'
                    f'{subscription_import_link}\n\n'
                    f'{texts.t("SUBSCRIPTION_IMPORT_INSTRUCTION_PROMPT", "📱 Нажмите кнопку ниже, чтобы получить инструкцию по настройке VPN на вашем устройстве")}'
                )

            trial_success_text += payment_note

            connect_mode = settings.CONNECT_BUTTON_MODE
            connect_keyboard = _build_trial_success_keyboard(texts, subscription_link, connect_mode)

            await callback.message.edit_text(
                trial_success_text,
                reply_markup=connect_keyboard,
                parse_mode='HTML',
            )
        else:
            trial_success_text = f"{texts.TRIAL_ACTIVATED}\n\n⚠️ Ссылка генерируется, попробуйте перейти в раздел 'Моя подписка' через несколько секунд."
            trial_success_text += payment_note

            await callback.message.edit_text(
                trial_success_text,
                reply_markup=get_back_keyboard(db_user.language),
                parse_mode='HTML',
            )

        await callback.answer()

    except Exception as error:
        logger.error(
            'Unexpected error during paid trial activation for user %s: %s',
            db_user.id,
            error,
        )
        # Пытаемся откатить и вернуть деньги
        if subscription:
            await rollback_trial_subscription_activation(db, subscription)
        from app.database.crud.user import add_user_balance

        await add_user_balance(
            db,
            db_user,
            trial_price_kopeks,
            texts.t('TRIAL_REFUND_DESCRIPTION', 'Возврат за неудачную активацию триала'),
            transaction_type=TransactionType.REFUND,
        )
        await db.refresh(db_user)

        await callback.message.edit_text(
            texts.t(
                'TRIAL_ACTIVATION_ERROR',
                '❌ Произошла ошибка при активации триала. Средства возвращены на баланс.',
            ),
            reply_markup=get_back_keyboard(db_user.language),
        )
        await callback.answer()


def _build_trial_success_keyboard(texts, subscription_link: str, connect_mode: str) -> InlineKeyboardMarkup:
    """Создает клавиатуру успешной активации триала."""

    if connect_mode == 'miniapp_subscription':
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                        web_app=types.WebAppInfo(url=subscription_link),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                        callback_data='back_to_menu',
                    )
                ],
            ]
        )
    if connect_mode == 'miniapp_custom':
        if not settings.MINIAPP_CUSTOM_URL:
            return get_back_keyboard(texts.language if hasattr(texts, 'language') else 'ru')

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                        web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                        callback_data='back_to_menu',
                    )
                ],
            ]
        )
    if connect_mode == 'link':
        rows = [
            [
                InlineKeyboardButton(
                    text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                    url=subscription_link,
                )
            ]
        ]
        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            rows.append(happ_row)
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                    callback_data='back_to_menu',
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)
    if connect_mode == 'happ_cryptolink':
        rows = [
            [
                InlineKeyboardButton(
                    text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                    callback_data='open_subscription_link',
                )
            ]
        ]
        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            rows.append(happ_row)
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                    callback_data='back_to_menu',
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                    callback_data='subscription_connect',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                    callback_data='back_to_menu',
                )
            ],
        ]
    )


@error_handler
async def handle_trial_payment_method(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Обрабатывает выбор метода оплаты для платного триала."""
    from app.services.payment_service import PaymentService
    from app.services.trial_activation_service import get_trial_activation_charge_amount

    texts = get_texts(db_user.language)

    # Проверяем права на триал
    # PENDING триальные подписки не считаются - пользователь может повторить оплату
    trial_blocked = False
    if db_user.has_had_paid_subscription:
        trial_blocked = True
    elif db_user.subscription:
        sub = db_user.subscription
        # Разрешаем если это PENDING триальная подписка (повторная попытка оплаты)
        if not (sub.status == SubscriptionStatus.PENDING.value and sub.is_trial):
            trial_blocked = True

    if trial_blocked:
        await callback.message.edit_text(texts.TRIAL_ALREADY_USED, reply_markup=get_back_keyboard(db_user.language))
        await callback.answer()
        return

    trial_price_kopeks = get_trial_activation_charge_amount()
    if trial_price_kopeks <= 0:
        await callback.answer('❌ Ошибка: триал бесплатный', show_alert=True)
        return

    # Определяем метод оплаты
    payment_method = callback.data.replace('trial_payment_', '')

    try:
        payment_service = PaymentService(callback.bot)

        # Получаем случайный сквад для триала
        from app.database.crud.server_squad import get_random_trial_squad_uuid

        trial_squad_uuid = await get_random_trial_squad_uuid(db)

        # Создаем pending триальную подписку
        pending_subscription = await create_pending_trial_subscription(
            db=db,
            user_id=db_user.id,
            duration_days=settings.TRIAL_DURATION_DAYS,
            traffic_limit_gb=settings.TRIAL_TRAFFIC_LIMIT_GB,
            device_limit=settings.TRIAL_DEVICE_LIMIT,
            connected_squads=[trial_squad_uuid] if trial_squad_uuid else [],
            payment_method=f'trial_{payment_method}',
            total_price_kopeks=trial_price_kopeks,
        )

        if not pending_subscription:
            await callback.answer('❌ Не удалось подготовить заказ. Попробуйте позже.', show_alert=True)
            return

        traffic_label = 'Безлимит' if settings.TRIAL_TRAFFIC_LIMIT_GB == 0 else f'{settings.TRIAL_TRAFFIC_LIMIT_GB} ГБ'

        if payment_method == 'stars':
            # Оплата через Telegram Stars
            stars_count = settings.rubles_to_stars(settings.kopeks_to_rubles(trial_price_kopeks))

            await callback.bot.send_invoice(
                chat_id=callback.from_user.id,
                title=texts.t('PAID_TRIAL_INVOICE_TITLE', 'Пробная подписка на {days} дней').format(
                    days=settings.TRIAL_DURATION_DAYS
                ),
                description=(
                    f'{texts.t("PERIOD", "Период")}: {settings.TRIAL_DURATION_DAYS} {texts.t("DAYS", "дней")}\n'
                    f'{texts.t("DEVICES", "Устройства")}: {settings.TRIAL_DEVICE_LIMIT}\n'
                    f'{texts.t("TRAFFIC", "Трафик")}: {traffic_label}'
                ),
                payload=f'trial_{pending_subscription.id}',
                provider_token='',
                currency='XTR',
                prices=[
                    types.LabeledPrice(label=texts.t('PAID_TRIAL_STARS_LABEL', 'Пробная подписка'), amount=stars_count)
                ],
            )

            await callback.message.edit_text(
                texts.t(
                    'PAID_TRIAL_STARS_WAITING',
                    '⭐ Для оплаты пробной подписки нажмите кнопку оплаты в сообщении выше.\n\n'
                    'После успешной оплаты подписка будет активирована автоматически.',
                ),
                reply_markup=get_back_keyboard(db_user.language),
                parse_mode='HTML',
            )

        elif payment_method == 'yookassa_sbp':
            # Оплата через YooKassa СБП
            payment_result = await payment_service.create_yookassa_sbp_payment(
                db=db,
                amount_kopeks=trial_price_kopeks,
                description=texts.t('PAID_TRIAL_PAYMENT_DESC', 'Пробная подписка на {days} дней').format(
                    days=settings.TRIAL_DURATION_DAYS
                ),
                user_id=db_user.id,
                metadata={
                    'type': 'trial',
                    'subscription_id': pending_subscription.id,
                    'user_id': db_user.id,
                },
            )

            if not payment_result or not payment_result.get('confirmation_url'):
                await callback.answer('❌ Не удалось создать платеж. Попробуйте позже.', show_alert=True)
                return

            qr_url = payment_result.get('qr_code_url') or payment_result.get('confirmation_url')

            await callback.message.edit_text(
                texts.t(
                    'PAID_TRIAL_YOOKASSA_SBP',
                    '🏦 <b>Оплата через СБП</b>\n\n'
                    'Отсканируйте QR-код или перейдите по ссылке для оплаты.\n\n'
                    '💰 Сумма: {amount}',
                ).format(amount=settings.format_price(trial_price_kopeks)),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text='💳 Оплатить', url=qr_url)],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='trial_activate')],
                    ]
                ),
                parse_mode='HTML',
            )

        elif payment_method == 'yookassa':
            # Оплата через YooKassa карта
            payment_result = await payment_service.create_yookassa_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=trial_price_kopeks,
                description=texts.t('PAID_TRIAL_PAYMENT_DESC', 'Пробная подписка на {days} дней').format(
                    days=settings.TRIAL_DURATION_DAYS
                ),
                metadata={
                    'type': 'trial',
                    'subscription_id': pending_subscription.id,
                    'user_id': db_user.id,
                },
            )

            if not payment_result or not payment_result.get('confirmation_url'):
                await callback.answer('❌ Не удалось создать платеж. Попробуйте позже.', show_alert=True)
                return

            await callback.message.edit_text(
                texts.t(
                    'PAID_TRIAL_YOOKASSA_CARD',
                    '💳 <b>Оплата картой</b>\n\nНажмите кнопку ниже для перехода к оплате.\n\n💰 Сумма: {amount}',
                ).format(amount=settings.format_price(trial_price_kopeks)),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text='💳 Оплатить', url=payment_result['confirmation_url'])],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='trial_activate')],
                    ]
                ),
                parse_mode='HTML',
            )

        elif payment_method == 'cryptobot':
            # Оплата через CryptoBot
            # Конвертируем копейки в USD
            from app.utils.currency_converter import currency_converter

            try:
                usd_rate = await currency_converter.get_usd_to_rub_rate()
            except Exception as rate_error:
                logger.warning('Не удалось получить курс USD: %s', rate_error)
                usd_rate = 95.0

            amount_rubles = trial_price_kopeks / 100
            amount_usd = round(amount_rubles / usd_rate, 2)
            if amount_usd < 1:
                amount_usd = 1.0

            payment_result = await payment_service.create_cryptobot_payment(
                db=db,
                user_id=db_user.id,
                amount_usd=amount_usd,
                asset=settings.CRYPTOBOT_DEFAULT_ASSET,
                description=texts.t('PAID_TRIAL_PAYMENT_DESC', 'Пробная подписка на {days} дней').format(
                    days=settings.TRIAL_DURATION_DAYS
                ),
                payload=f'trial_{pending_subscription.id}_{db_user.id}',
            )

            payment_url = (
                (
                    payment_result.get('mini_app_invoice_url')
                    or payment_result.get('bot_invoice_url')
                    or payment_result.get('web_app_invoice_url')
                )
                if payment_result
                else None
            )

            if not payment_result or not payment_url:
                await callback.answer('❌ Не удалось создать платеж. Попробуйте позже.', show_alert=True)
                return

            await callback.message.edit_text(
                texts.t(
                    'PAID_TRIAL_CRYPTOBOT',
                    '🪙 <b>Оплата криптовалютой</b>\n\n'
                    'Нажмите кнопку ниже для перехода к оплате.\n\n'
                    '💰 Сумма: {amount}',
                ).format(amount=settings.format_price(trial_price_kopeks)),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text='🪙 Оплатить', url=payment_url)],
                        [
                            InlineKeyboardButton(
                                text=texts.t('CHECK_PAYMENT', '🔄 Проверить оплату'),
                                callback_data=f'check_trial_cryptobot_{pending_subscription.id}',
                            )
                        ],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='trial_activate')],
                    ]
                ),
                parse_mode='HTML',
            )

        elif payment_method == 'heleket':
            # Оплата через Heleket
            payment_result = await payment_service.create_heleket_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=trial_price_kopeks,
                description=texts.t('PAID_TRIAL_PAYMENT_DESC', 'Пробная подписка на {days} дней').format(
                    days=settings.TRIAL_DURATION_DAYS
                ),
                language=db_user.language,
            )

            if not payment_result or not payment_result.get('payment_url'):
                await callback.answer('❌ Не удалось создать платеж. Попробуйте позже.', show_alert=True)
                return

            await callback.message.edit_text(
                texts.t(
                    'PAID_TRIAL_HELEKET',
                    '🪙 <b>Оплата криптовалютой (Heleket)</b>\n\n'
                    'Нажмите кнопку ниже для перехода к оплате.\n\n'
                    '💰 Сумма: {amount}',
                ).format(amount=settings.format_price(trial_price_kopeks)),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text='🪙 Оплатить', url=payment_result['payment_url'])],
                        [
                            InlineKeyboardButton(
                                text=texts.t('CHECK_PAYMENT', '🔄 Проверить оплату'),
                                callback_data=f'check_trial_heleket_{pending_subscription.id}',
                            )
                        ],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='trial_activate')],
                    ]
                ),
                parse_mode='HTML',
            )

        elif payment_method == 'mulenpay':
            # Оплата через MulenPay
            payment_result = await payment_service.create_mulenpay_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=trial_price_kopeks,
                description=texts.t('PAID_TRIAL_PAYMENT_DESC', 'Пробная подписка на {days} дней').format(
                    days=settings.TRIAL_DURATION_DAYS
                ),
                language=db_user.language,
            )

            if not payment_result or not payment_result.get('payment_url'):
                await callback.answer('❌ Не удалось создать платеж. Попробуйте позже.', show_alert=True)
                return

            mulenpay_name = settings.get_mulenpay_display_name()
            await callback.message.edit_text(
                texts.t(
                    'PAID_TRIAL_MULENPAY',
                    '💳 <b>Оплата через {name}</b>\n\nНажмите кнопку ниже для перехода к оплате.\n\n💰 Сумма: {amount}',
                ).format(name=mulenpay_name, amount=settings.format_price(trial_price_kopeks)),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text='💳 Оплатить', url=payment_result['payment_url'])],
                        [
                            InlineKeyboardButton(
                                text=texts.t('CHECK_PAYMENT', '🔄 Проверить оплату'),
                                callback_data=f'check_trial_mulenpay_{pending_subscription.id}',
                            )
                        ],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='trial_activate')],
                    ]
                ),
                parse_mode='HTML',
            )

        elif payment_method == 'pal24':
            # Оплата через PAL24
            payment_result = await payment_service.create_pal24_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=trial_price_kopeks,
                description=texts.t('PAID_TRIAL_PAYMENT_DESC', 'Пробная подписка на {days} дней').format(
                    days=settings.TRIAL_DURATION_DAYS
                ),
                language=db_user.language,
            )

            if not payment_result or not payment_result.get('payment_url'):
                await callback.answer('❌ Не удалось создать платеж. Попробуйте позже.', show_alert=True)
                return

            await callback.message.edit_text(
                texts.t(
                    'PAID_TRIAL_PAL24',
                    '💳 <b>Оплата через PayPalych</b>\n\n'
                    'Нажмите кнопку ниже для перехода к оплате.\n\n'
                    '💰 Сумма: {amount}',
                ).format(amount=settings.format_price(trial_price_kopeks)),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text='💳 Оплатить', url=payment_result['payment_url'])],
                        [
                            InlineKeyboardButton(
                                text=texts.t('CHECK_PAYMENT', '🔄 Проверить оплату'),
                                callback_data=f'check_trial_pal24_{pending_subscription.id}',
                            )
                        ],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='trial_activate')],
                    ]
                ),
                parse_mode='HTML',
            )

        elif payment_method == 'wata':
            # Оплата через WATA
            payment_result = await payment_service.create_wata_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=trial_price_kopeks,
                description=texts.t('PAID_TRIAL_PAYMENT_DESC', 'Пробная подписка на {days} дней').format(
                    days=settings.TRIAL_DURATION_DAYS
                ),
                language=db_user.language,
            )

            if not payment_result or not payment_result.get('payment_url'):
                await callback.answer('❌ Не удалось создать платеж. Попробуйте позже.', show_alert=True)
                return

            await callback.message.edit_text(
                texts.t(
                    'PAID_TRIAL_WATA',
                    '💳 <b>Оплата через WATA</b>\n\nНажмите кнопку ниже для перехода к оплате.\n\n💰 Сумма: {amount}',
                ).format(amount=settings.format_price(trial_price_kopeks)),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text='💳 Оплатить', url=payment_result['payment_url'])],
                        [
                            InlineKeyboardButton(
                                text=texts.t('CHECK_PAYMENT', '🔄 Проверить оплату'),
                                callback_data=f'check_trial_wata_{pending_subscription.id}',
                            )
                        ],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='trial_activate')],
                    ]
                ),
                parse_mode='HTML',
            )

        elif payment_method == 'platega':
            # Оплата через Platega
            active_methods = settings.get_platega_active_methods()
            if not active_methods:
                await callback.answer('❌ Platega не настроена', show_alert=True)
                return

            # Используем первый активный метод
            method_code = active_methods[0]

            payment_result = await payment_service.create_platega_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=trial_price_kopeks,
                description=texts.t('PAID_TRIAL_PAYMENT_DESC', 'Пробная подписка на {days} дней').format(
                    days=settings.TRIAL_DURATION_DAYS
                ),
                language=db_user.language,
                payment_method_code=method_code,
            )

            if not payment_result or not payment_result.get('redirect_url'):
                await callback.answer('❌ Не удалось создать платеж. Попробуйте позже.', show_alert=True)
                return

            platega_name = settings.get_platega_display_name()
            await callback.message.edit_text(
                texts.t(
                    'PAID_TRIAL_PLATEGA',
                    '💳 <b>Оплата через {provider}</b>\n\n'
                    'Нажмите кнопку ниже для перехода к оплате.\n\n'
                    '💰 Сумма: {amount}',
                ).format(provider=platega_name, amount=settings.format_price(trial_price_kopeks)),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text='💳 Оплатить', url=payment_result['redirect_url'])],
                        [
                            InlineKeyboardButton(
                                text=texts.t('CHECK_PAYMENT', '🔄 Проверить оплату'),
                                callback_data=f'check_trial_platega_{pending_subscription.id}',
                            )
                        ],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='trial_activate')],
                    ]
                ),
                parse_mode='HTML',
            )

        else:
            await callback.answer(f'❌ Неизвестный метод оплаты: {payment_method}', show_alert=True)
            return

        await callback.answer()

    except Exception as error:
        logger.error(f'Error processing trial payment method {payment_method}: {error}')
        await callback.answer('❌ Произошла ошибка при создании платежа. Попробуйте позже.', show_alert=True)


def register_handlers(dp: Dispatcher):
    update_traffic_prices()

    dp.callback_query.register(show_subscription_info, F.data == 'menu_subscription')

    dp.callback_query.register(show_trial_offer, F.data == 'menu_trial')

    dp.callback_query.register(activate_trial, F.data == 'trial_activate')

    # Хендлеры платного триала
    dp.callback_query.register(handle_trial_pay_with_balance, F.data == 'trial_pay_with_balance')

    dp.callback_query.register(handle_trial_payment_method, F.data.startswith('trial_payment_'))

    dp.callback_query.register(
        start_subscription_purchase, F.data.in_(['menu_buy', 'subscription_upgrade', 'subscription_purchase'])
    )

    dp.callback_query.register(handle_add_countries, F.data == 'subscription_add_countries')

    dp.callback_query.register(handle_switch_traffic, F.data == 'subscription_switch_traffic')

    dp.callback_query.register(confirm_switch_traffic, F.data.startswith('switch_traffic_'))

    dp.callback_query.register(execute_switch_traffic, F.data.startswith('confirm_switch_traffic_'))

    dp.callback_query.register(handle_change_devices, F.data == 'subscription_change_devices')

    dp.callback_query.register(confirm_change_devices, F.data.startswith('change_devices_'))

    dp.callback_query.register(execute_change_devices, F.data.startswith('confirm_change_devices_'))

    dp.callback_query.register(handle_extend_subscription, F.data == 'subscription_extend')

    dp.callback_query.register(handle_reset_traffic, F.data == 'subscription_reset_traffic')

    dp.callback_query.register(confirm_add_devices, F.data.startswith('add_devices_'))

    dp.callback_query.register(confirm_extend_subscription, F.data.startswith('extend_period_'))

    dp.callback_query.register(confirm_reset_traffic, F.data == 'confirm_reset_traffic')

    dp.callback_query.register(handle_reset_devices, F.data == 'subscription_reset_devices')

    dp.callback_query.register(confirm_reset_devices, F.data == 'confirm_reset_devices')

    dp.callback_query.register(select_period, F.data.startswith('period_'), SubscriptionStates.selecting_period)

    dp.callback_query.register(select_traffic, F.data.startswith('traffic_'), SubscriptionStates.selecting_traffic)

    dp.callback_query.register(
        select_devices,
        F.data.startswith('devices_') & ~F.data.in_(['devices_continue']),
        SubscriptionStates.selecting_devices,
    )

    dp.callback_query.register(devices_continue, F.data == 'devices_continue', SubscriptionStates.selecting_devices)

    dp.callback_query.register(
        confirm_purchase, F.data == 'subscription_confirm', SubscriptionStates.confirming_purchase
    )

    dp.callback_query.register(
        resume_subscription_checkout,
        F.data == 'subscription_resume_checkout',
    )

    dp.callback_query.register(
        return_to_saved_cart,
        F.data == 'return_to_saved_cart',
    )

    dp.callback_query.register(
        clear_saved_cart,
        F.data == 'clear_saved_cart',
    )

    dp.callback_query.register(handle_autopay_menu, F.data == 'subscription_autopay')

    dp.callback_query.register(toggle_autopay, F.data.in_(['autopay_enable', 'autopay_disable']))

    dp.callback_query.register(show_autopay_days, F.data == 'autopay_set_days')

    dp.callback_query.register(handle_subscription_config_back, F.data == 'subscription_config_back')

    dp.callback_query.register(handle_subscription_cancel, F.data == 'subscription_cancel')

    dp.callback_query.register(set_autopay_days, F.data.startswith('autopay_days_'))

    dp.callback_query.register(select_country, F.data.startswith('country_'), SubscriptionStates.selecting_countries)

    dp.callback_query.register(
        countries_continue, F.data == 'countries_continue', SubscriptionStates.selecting_countries
    )

    dp.callback_query.register(handle_manage_country, F.data.startswith('country_manage_'))

    dp.callback_query.register(apply_countries_changes, F.data == 'countries_apply')

    dp.callback_query.register(claim_discount_offer, F.data.startswith('claim_discount_'))

    dp.callback_query.register(
        handle_promo_offer_close,
        F.data == 'promo_offer_close',
    )

    dp.callback_query.register(handle_happ_download_request, F.data == 'subscription_happ_download')

    dp.callback_query.register(
        handle_happ_download_platform_choice,
        F.data.in_(
            [
                'happ_download_ios',
                'happ_download_android',
                'happ_download_pc',
                'happ_download_macos',
                'happ_download_windows',
            ]
        ),
    )

    dp.callback_query.register(handle_happ_download_close, F.data == 'happ_download_close')

    dp.callback_query.register(handle_happ_download_back, F.data == 'happ_download_back')

    dp.callback_query.register(handle_connect_subscription, F.data == 'subscription_connect')

    dp.callback_query.register(handle_device_guide, F.data.startswith('device_guide_'))

    dp.callback_query.register(handle_app_selection, F.data.startswith('app_list_'))

    dp.callback_query.register(handle_specific_app_guide, F.data.startswith('app_'))

    dp.callback_query.register(handle_open_subscription_link, F.data == 'open_subscription_link')

    dp.callback_query.register(handle_subscription_settings, F.data == 'subscription_settings')

    dp.callback_query.register(handle_toggle_daily_subscription_pause, F.data == 'toggle_daily_subscription_pause')

    dp.callback_query.register(handle_no_traffic_packages, F.data == 'no_traffic_packages')

    dp.callback_query.register(handle_device_management, F.data == 'subscription_manage_devices')

    dp.callback_query.register(handle_devices_page, F.data.startswith('devices_page_'))

    dp.callback_query.register(handle_single_device_reset, F.data.regexp(r'^reset_device_\d+_\d+$'))

    dp.callback_query.register(handle_all_devices_reset_from_management, F.data == 'reset_all_devices')

    dp.callback_query.register(show_device_connection_help, F.data == 'device_connection_help')

    # Регистрируем обработчики модема
    from .modem import register_modem_handlers

    register_modem_handlers(dp)

    # Регистрируем обработчики покупки по тарифам
    from .tariff_purchase import register_tariff_purchase_handlers

    register_tariff_purchase_handlers(dp)

    # Регистрируем обработчик для простой покупки
    dp.callback_query.register(handle_simple_subscription_purchase, F.data == 'simple_subscription_purchase')


async def handle_simple_subscription_purchase(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    """Обрабатывает простую покупку подписки."""
    texts = get_texts(db_user.language)

    if not settings.SIMPLE_SUBSCRIPTION_ENABLED:
        await callback.answer('❌ Простая покупка подписки временно недоступна', show_alert=True)
        return

    # Определяем ограничение по устройствам для текущего режима
    simple_device_limit = resolve_simple_subscription_device_limit()

    # Проверяем, есть ли у пользователя активная подписка
    from app.database.crud.subscription import get_subscription_by_user_id

    current_subscription = await get_subscription_by_user_id(db, db_user.id)

    # Если у пользователя уже есть активная подписка, продлеваем её
    if current_subscription and current_subscription.is_active:
        # При продлении используем текущие устройства подписки, а не дефолтные
        extend_device_limit = current_subscription.device_limit or simple_device_limit
        # Модем добавляет +1 к device_limit, но оплачивается отдельно
        modem_enabled = getattr(current_subscription, 'modem_enabled', False)
        if modem_enabled:
            extend_device_limit = max(1, extend_device_limit - 1)
        # Используем максимум из текущего и дефолтного
        extend_device_limit = max(simple_device_limit, extend_device_limit)

        # Продлеваем существующую подписку
        await _extend_existing_subscription(
            callback=callback,
            db_user=db_user,
            db=db,
            current_subscription=current_subscription,
            period_days=settings.SIMPLE_SUBSCRIPTION_PERIOD_DAYS,
            device_limit=extend_device_limit,
            traffic_limit_gb=settings.SIMPLE_SUBSCRIPTION_TRAFFIC_GB,
            squad_uuid=settings.SIMPLE_SUBSCRIPTION_SQUAD_UUID,
            modem_enabled=modem_enabled,
        )
        return

    # Подготовим параметры простой подписки
    subscription_params = {
        'period_days': settings.SIMPLE_SUBSCRIPTION_PERIOD_DAYS,
        'device_limit': simple_device_limit,
        'traffic_limit_gb': settings.SIMPLE_SUBSCRIPTION_TRAFFIC_GB,
        'squad_uuid': settings.SIMPLE_SUBSCRIPTION_SQUAD_UUID,
    }

    # Сохраняем параметры в состояние
    await state.update_data(subscription_params=subscription_params)

    # Проверяем баланс пользователя
    user_balance_kopeks = getattr(db_user, 'balance_kopeks', 0)
    # Рассчитываем цену подписки
    price_kopeks, price_breakdown = await _calculate_simple_subscription_price(
        db,
        subscription_params,
        user=db_user,
        resolved_squad_uuid=subscription_params.get('squad_uuid'),
    )
    logger.debug(
        'SIMPLE_SUBSCRIPTION_PURCHASE_PRICE | user=%s | total=%s | base=%s | traffic=%s | devices=%s | servers=%s | discount=%s',
        db_user.id,
        price_kopeks,
        price_breakdown.get('base_price', 0),
        price_breakdown.get('traffic_price', 0),
        price_breakdown.get('devices_price', 0),
        price_breakdown.get('servers_price', 0),
        price_breakdown.get('total_discount', 0),
    )
    traffic_text = (
        'Безлимит' if subscription_params['traffic_limit_gb'] == 0 else f'{subscription_params["traffic_limit_gb"]} ГБ'
    )

    if user_balance_kopeks >= price_kopeks:
        # Если баланс достаточный, предлагаем оплатить с баланса
        simple_lines = [
            '⚡ <b>Простая покупка подписки</b>',
            '',
            f'📅 Период: {subscription_params["period_days"]} дней',
        ]

        if settings.is_devices_selection_enabled():
            simple_lines.append(f'📱 Устройства: {subscription_params["device_limit"]}')

        simple_lines.extend(
            [
                f'📊 Трафик: {traffic_text}',
                f'🌍 Сервер: {"Любой доступный" if not subscription_params["squad_uuid"] else "Выбранный"}',
                '',
                f'💰 Стоимость: {settings.format_price(price_kopeks)}',
                f'💳 Ваш баланс: {settings.format_price(user_balance_kopeks)}',
                '',
                'Вы можете оплатить подписку с баланса или выбрать другой способ оплаты.',
            ]
        )

        message_text = '\n'.join(simple_lines)

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='✅ Оплатить с баланса', callback_data='simple_subscription_pay_with_balance'
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text='💳 Другие способы оплаты', callback_data='simple_subscription_other_payment_methods'
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')],
            ]
        )
    else:
        # Если баланс недостаточный, предлагаем внешние способы оплаты
        simple_lines = [
            '⚡ <b>Простая покупка подписки</b>',
            '',
            f'📅 Период: {subscription_params["period_days"]} дней',
        ]

        if settings.is_devices_selection_enabled():
            simple_lines.append(f'📱 Устройства: {subscription_params["device_limit"]}')

        simple_lines.extend(
            [
                f'📊 Трафик: {traffic_text}',
                f'🌍 Сервер: {"Любой доступный" if not subscription_params["squad_uuid"] else "Выбранный"}',
                '',
                f'💰 Стоимость: {settings.format_price(price_kopeks)}',
                f'💳 Ваш баланс: {settings.format_price(user_balance_kopeks)}',
                '',
                'Выберите способ оплаты:',
            ]
        )

        message_text = '\n'.join(simple_lines)

        keyboard = _get_simple_subscription_payment_keyboard(db_user.language)

    await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

    await state.set_state(SubscriptionStates.waiting_for_simple_subscription_payment_method)
    await callback.answer()


async def _extend_existing_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    current_subscription: Subscription,
    period_days: int,
    device_limit: int,
    traffic_limit_gb: int,
    squad_uuid: str,
    modem_enabled: bool = False,
):
    """Продлевает существующую подписку."""
    from datetime import datetime, timedelta

    from app.database.crud.transaction import create_transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import TransactionType
    from app.services.subscription_service import SubscriptionService

    texts = get_texts(db_user.language)

    # Рассчитываем цену подписки
    subscription_params = {
        'period_days': period_days,
        'device_limit': device_limit,
        'traffic_limit_gb': traffic_limit_gb,
        'squad_uuid': squad_uuid,
        'modem_enabled': modem_enabled,
    }
    price_kopeks, price_breakdown = await _calculate_simple_subscription_price(
        db,
        subscription_params,
        user=db_user,
        resolved_squad_uuid=squad_uuid,
    )
    logger.warning(
        'SIMPLE_SUBSCRIPTION_EXTEND_PRICE | user=%s | total=%s | base=%s | traffic=%s | devices=%s | modem=%s | servers=%s | discount=%s | device_limit=%s | modem_enabled=%s',
        db_user.id,
        price_kopeks,
        price_breakdown.get('base_price', 0),
        price_breakdown.get('traffic_price', 0),
        price_breakdown.get('devices_price', 0),
        price_breakdown.get('modem_price', 0),
        price_breakdown.get('servers_price', 0),
        price_breakdown.get('total_discount', 0),
        device_limit,
        modem_enabled,
    )

    # Проверяем баланс пользователя
    if db_user.balance_kopeks < price_kopeks:
        missing_kopeks = price_kopeks - db_user.balance_kopeks
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
            required=texts.format_price(price_kopeks),
            balance=texts.format_price(db_user.balance_kopeks),
            missing=texts.format_price(missing_kopeks),
        )

        # Подготовим данные для сохранения в корзину
        from app.services.user_cart_service import user_cart_service

        cart_data = {
            'cart_mode': 'extend',
            'subscription_id': current_subscription.id,
            'period_days': period_days,
            'total_price': price_kopeks,
            'user_id': db_user.id,
            'saved_cart': True,
            'missing_amount': missing_kopeks,
            'return_to_cart': True,
            'description': f'Продление подписки на {period_days} дней',
            'device_limit': device_limit,
            'traffic_limit_gb': traffic_limit_gb,
            'squad_uuid': squad_uuid,
            'consume_promo_offer': False,
        }

        await user_cart_service.save_user_cart(db_user.id, cart_data)

        await callback.message.edit_text(
            message_text,
            reply_markup=get_insufficient_balance_keyboard(
                db_user.language, amount_kopeks=missing_kopeks, has_saved_cart=True
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Списываем средства
    success = await subtract_user_balance(
        db,
        db_user,
        price_kopeks,
        f'Продление подписки на {period_days} дней',
        consume_promo_offer=False,  # Простая покупка не использует промо-скидки
    )

    if not success:
        await callback.answer('⚠ Ошибка списания средств', show_alert=True)
        return

    # Обновляем параметры подписки
    current_time = datetime.utcnow()
    old_end_date = current_subscription.end_date

    # Обновляем параметры в зависимости от типа текущей подписки
    if current_subscription.is_trial:
        # При продлении триальной подписки переводим её в обычную
        current_subscription.is_trial = False
        current_subscription.status = 'active'
        # Убираем ограничения с триальной подписки
        current_subscription.traffic_limit_gb = traffic_limit_gb
        current_subscription.device_limit = device_limit
        # Если указан squad_uuid, добавляем его к существующим серверам
        if squad_uuid and squad_uuid not in current_subscription.connected_squads:
            # Используем += для безопасного добавления в список SQLAlchemy
            current_subscription.connected_squads = current_subscription.connected_squads + [squad_uuid]
    else:
        # Для обычной подписки просто продлеваем
        # Обновляем трафик и устройства, если нужно
        if traffic_limit_gb != 0:  # Если не безлимит, обновляем
            current_subscription.traffic_limit_gb = traffic_limit_gb
        current_subscription.device_limit = max(current_subscription.device_limit, device_limit)
        # Если указан squad_uuid и его ещё нет в подписке, добавляем
        if squad_uuid and squad_uuid not in current_subscription.connected_squads:
            # Используем += для безопасного добавления в список SQLAlchemy
            current_subscription.connected_squads = current_subscription.connected_squads + [squad_uuid]

    # Продлеваем подписку
    if current_subscription.end_date > current_time:
        # Если подписка ещё активна, добавляем дни к текущей дате окончания
        new_end_date = current_subscription.end_date + timedelta(days=period_days)
    else:
        # Если подписка уже истекла, начинаем от текущего времени
        new_end_date = current_time + timedelta(days=period_days)

    current_subscription.end_date = new_end_date
    current_subscription.updated_at = current_time

    # Сохраняем изменения
    await db.commit()
    await db.refresh(current_subscription)
    await db.refresh(db_user)

    # Обновляем пользователя в Remnawave
    subscription_service = SubscriptionService()
    try:
        remnawave_result = await subscription_service.update_remnawave_user(
            db,
            current_subscription,
            reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
            reset_reason='продление подписки',
        )
        if remnawave_result:
            logger.info('✅ RemnaWave обновлен успешно')
        else:
            logger.error('⚠ ОШИБКА ОБНОВЛЕНИЯ REMNAWAVE')
    except Exception as e:
        logger.error(f'⚠ ИСКЛЮЧЕНИЕ ПРИ ОБНОВЛЕНИИ REMNAWAVE: {e}')

    # Создаём транзакцию
    transaction = await create_transaction(
        db=db,
        user_id=db_user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=price_kopeks,
        description=f'Продление подписки на {period_days} дней',
    )

    # Отправляем уведомление админу
    try:
        notification_service = AdminNotificationService(callback.bot)
        await notification_service.send_subscription_extension_notification(
            db,
            db_user,
            current_subscription,
            transaction,
            period_days,
            old_end_date,
            new_end_date=new_end_date,
            balance_after=db_user.balance_kopeks,
        )
    except Exception as e:
        logger.error(f'Ошибка отправки уведомления о продлении: {e}')

    # Отправляем сообщение пользователю
    success_message = (
        '✅ Подписка успешно продлена!\n\n'
        f'⏰ Добавлено: {period_days} дней\n'
        f'Действует до: {format_local_datetime(new_end_date, "%d.%m.%Y %H:%M")}\n\n'
        f'💰 Списано: {texts.format_price(price_kopeks)}'
    )

    # Если это была триальная подписка, добавляем информацию о преобразовании
    if current_subscription.is_trial:
        success_message += '\n🎯 Триальная подписка преобразована в платную'

    await callback.message.edit_text(success_message, reply_markup=get_back_keyboard(db_user.language))

    logger.info(
        f'✅ Пользователь {db_user.telegram_id} продлил подписку на {period_days} дней за {price_kopeks / 100}₽'
    )
    await callback.answer()
