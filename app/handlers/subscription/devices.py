import html as html_mod
from datetime import UTC, datetime
from io import BytesIO

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import add_subscription_devices
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import TransactionType, User
from app.keyboards.inline import (
    get_android_tv_qr_wait_keyboard,
    get_app_selection_keyboard,
    get_back_keyboard,
    get_change_devices_keyboard,
    get_confirm_change_devices_keyboard,
    get_connection_guide_keyboard,
    get_device_management_help_keyboard,
    get_devices_management_keyboard,
    get_insufficient_balance_keyboard,
    get_specific_app_keyboard,
    get_tv_selection_keyboard,
)
from app.localization.texts import get_texts
from app.services.android_tv_import_service import (
    AndroidTvQrDecodeError,
    AndroidTvStreamVaultError,
    decode_android_tv_qr_key,
    publish_streamvault_subscription,
)
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service
from app.states import SubscriptionStates
from app.utils.pagination import paginate_list
from app.utils.pricing_utils import (
    apply_percentage_discount,
    calculate_prorated_price,
    get_remaining_months,
)
from app.utils.subscription_utils import (
    get_display_subscription_link,
)

from .common import (
    _get_addon_discount_percent_for_user,
    _get_period_hint_from_subscription,
    get_apps_for_platform_async,
    get_device_name,
    logger,
    render_guide_blocks,
)
from .countries import _get_available_countries


async def get_current_devices_detailed(db_user: User) -> dict:
    try:
        if not db_user.remnawave_uuid:
            return {'count': 0, 'devices': []}

        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()

        async with service.get_api_client() as api:
            response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if response and 'response' in response:
                devices_info = response['response']
                total_devices = devices_info.get('total', 0)
                devices_list = devices_info.get('devices', [])

                return {'count': total_devices, 'devices': devices_list[:5]}
            return {'count': 0, 'devices': []}

    except Exception as e:
        logger.error('Ошибка получения детальной информации об устройствах', error=e)
        return {'count': 0, 'devices': []}


async def get_servers_display_names(squad_uuids: list[str]) -> str:
    if not squad_uuids:
        return 'Нет серверов'

    try:
        from app.database.crud.server_squad import get_server_squad_by_uuid
        from app.database.database import AsyncSessionLocal

        server_names = []

        async with AsyncSessionLocal() as db:
            for uuid in squad_uuids:
                server = await get_server_squad_by_uuid(db, uuid)
                if server:
                    server_names.append(server.display_name)
                    logger.debug('Найден сервер в БД', uuid=uuid, display_name=server.display_name)
                else:
                    logger.warning('Сервер с UUID не найден в БД', uuid=uuid)

        if not server_names:
            countries = await _get_available_countries()
            for uuid in squad_uuids:
                for country in countries:
                    if country['uuid'] == uuid:
                        server_names.append(country['name'])
                        logger.debug('Найден сервер в кэше', uuid=uuid, country=country['name'])
                        break

        if not server_names:
            if len(squad_uuids) == 1:
                return '🎯 Тестовый сервер'
            return f'{len(squad_uuids)} стран'

        if len(server_names) > 6:
            displayed = ', '.join(server_names[:6])
            remaining = len(server_names) - 6
            return f'{displayed} и ещё {remaining}'
        return ', '.join(server_names)

    except Exception as e:
        logger.error('Ошибка получения названий серверов', error=e)
        if len(squad_uuids) == 1:
            return '🎯 Тестовый сервер'
        return f'{len(squad_uuids)} стран'


async def get_current_devices_count(db_user: User) -> str:
    try:
        if not db_user.remnawave_uuid:
            return '—'

        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()

        async with service.get_api_client() as api:
            response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if response and 'response' in response:
                total_devices = response['response'].get('total', 0)
                return str(total_devices)
            return '—'

    except Exception as e:
        logger.error('Ошибка получения количества устройств', error=e)
        return '—'


async def handle_change_devices(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t('PAID_FEATURE_ONLY', '⚠️ Эта функция доступна только для платных подписок'),
            show_alert=True,
        )
        return

    # Проверяем тариф подписки
    tariff = None
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Для тарифов - проверяем разрешено ли изменение устройств
    tariff_device_price = getattr(tariff, 'device_price_kopeks', None) if tariff else None
    if tariff:
        if tariff_device_price is None or tariff_device_price <= 0:
            await callback.answer(
                texts.t('TARIFF_DEVICES_DISABLED', '⚠️ Изменение устройств недоступно для вашего тарифа'),
                show_alert=True,
            )
            return
    # Для обычных подписок проверяем глобальную настройку
    elif not settings.is_devices_selection_enabled():
        await callback.answer(
            texts.t('DEVICES_SELECTION_DISABLED', '⚠️ Изменение количества устройств недоступно'),
            show_alert=True,
        )
        return

    current_devices = subscription.device_limit

    period_hint_days = _get_period_hint_from_subscription(subscription)
    devices_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        'devices',
        period_hint_days,
    )

    # Для тарифов показываем цену из тарифа
    if tariff:
        price_per_device = tariff_device_price
        price_text = texts.format_price(price_per_device)
        tariff_min_devices = getattr(tariff, 'device_limit', 1) or 1

        # Добавляем информацию о минимальном лимите если он больше 1
        min_devices_info = ''
        if tariff_min_devices > 1:
            min_devices_info = texts.t(
                'CHANGE_DEVICES_MIN_LIMIT_INFO',
                '\nМинимум для тарифа: {min_devices} устройств\n',
            ).format(min_devices=tariff_min_devices)

        prompt_text = texts.t(
            'CHANGE_DEVICES_PROMPT_TARIFF',
            (
                '📱 <b>Изменение количества устройств</b>\n\n'
                'Текущий лимит: {current_devices} устройств\n'
                'Цена за доп. устройство: {price}/мес\n'
                '{min_devices_info}'
                'Выберите новое количество устройств:\n\n'
                '💡 <b>Важно:</b>\n'
                '• При увеличении - доплата пропорционально оставшемуся времени\n'
                '• При уменьшении - возврат средств не производится'
            ),
        ).format(current_devices=current_devices, price=price_text, min_devices_info=min_devices_info)
    else:
        prompt_text = texts.t(
            'CHANGE_DEVICES_PROMPT',
            (
                '📱 <b>Изменение количества устройств</b>\n\n'
                'Текущий лимит: {current_devices} устройств\n'
                'Выберите новое количество устройств:\n\n'
                '💡 <b>Важно:</b>\n'
                '• При увеличении - доплата пропорционально оставшемуся времени\n'
                '• При уменьшении - возврат средств не производится'
            ),
        ).format(current_devices=current_devices)

    await callback.message.edit_text(
        prompt_text,
        reply_markup=get_change_devices_keyboard(
            current_devices,
            db_user.language,
            subscription.end_date,
            devices_discount_percent,
            tariff=tariff,
        ),
        parse_mode='HTML',
    )

    await callback.answer()


async def confirm_change_devices(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    new_devices_count = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    # Проверяем тариф подписки
    tariff = None
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Для тарифов - проверяем разрешено ли изменение устройств
    tariff_device_price = getattr(tariff, 'device_price_kopeks', None) if tariff else None
    if tariff:
        if tariff_device_price is None or tariff_device_price <= 0:
            await callback.answer(
                texts.t('TARIFF_DEVICES_DISABLED', '⚠️ Изменение устройств недоступно для вашего тарифа'),
                show_alert=True,
            )
            return
        price_per_device = tariff_device_price
    else:
        if not settings.is_devices_selection_enabled():
            await callback.answer(
                texts.t('DEVICES_SELECTION_DISABLED', '⚠️ Изменение количества устройств недоступно'),
                show_alert=True,
            )
            return
        price_per_device = settings.PRICE_PER_DEVICE

    current_devices = subscription.device_limit

    if new_devices_count == current_devices:
        await callback.answer(
            texts.t('DEVICES_NO_CHANGE', 'ℹ️ Количество устройств не изменилось'),
            show_alert=True,
        )
        return

    if settings.MAX_DEVICES_LIMIT > 0 and new_devices_count > settings.MAX_DEVICES_LIMIT:
        await callback.answer(
            texts.t(
                'DEVICES_LIMIT_EXCEEDED',
                '⚠️ Превышен максимальный лимит устройств ({limit})',
            ).format(limit=settings.MAX_DEVICES_LIMIT),
            show_alert=True,
        )
        return

    # Проверяем минимальное количество устройств на тарифе
    tariff_min_devices = (getattr(tariff, 'device_limit', 1) or 1) if tariff else 1
    if new_devices_count < tariff_min_devices:
        await callback.answer(
            texts.t(
                'DEVICES_MIN_LIMIT_REACHED',
                '⚠️ Минимальное количество устройств для вашего тарифа: {limit}',
            ).format(limit=tariff_min_devices),
            show_alert=True,
        )
        return

    devices_difference = new_devices_count - current_devices

    if devices_difference > 0:
        additional_devices = devices_difference

        # Для тарифов - все устройства платные (нет бесплатного лимита)
        if tariff:
            chargeable_devices = additional_devices
        elif current_devices < settings.DEFAULT_DEVICE_LIMIT:
            free_devices = settings.DEFAULT_DEVICE_LIMIT - current_devices
            chargeable_devices = max(0, additional_devices - free_devices)
        else:
            chargeable_devices = additional_devices

        devices_price_per_month = chargeable_devices * price_per_device

        # Проверяем является ли тариф суточным
        is_daily_tariff = tariff and getattr(tariff, 'is_daily', False)

        if is_daily_tariff:
            # Для суточных тарифов считаем по дням (как в кабинете)
            now = datetime.now(UTC)
            days_left = max(1, (subscription.end_date - now).days)
            period_hint_days = days_left

            devices_discount_percent = _get_addon_discount_percent_for_user(
                db_user,
                'devices',
                period_hint_days,
            )
            discounted_per_month, discount_per_month = apply_percentage_discount(
                devices_price_per_month,
                devices_discount_percent,
            )
            # Цена = месячная_цена * days_left / 30
            price = int(discounted_per_month * days_left / 30)
            price = max(100, price)  # Минимум 1 рубль
            total_discount = int(discount_per_month * days_left / 30)
            period_label = f'{days_left} дн.' if days_left > 1 else '1 день'
        else:
            # Для обычных тарифов - по месяцам
            months_hint = get_remaining_months(subscription.end_date)
            period_hint_days = months_hint * 30 if months_hint > 0 else None

            devices_discount_percent = _get_addon_discount_percent_for_user(
                db_user,
                'devices',
                period_hint_days,
            )
            discounted_per_month, discount_per_month = apply_percentage_discount(
                devices_price_per_month,
                devices_discount_percent,
            )
            price, charged_months = calculate_prorated_price(
                discounted_per_month,
                subscription.end_date,
            )
            total_discount = discount_per_month * charged_months
            period_label = f'{charged_months} мес'

        if price > 0 and db_user.balance_kopeks < price:
            missing_kopeks = price - db_user.balance_kopeks
            required_text = f'{texts.format_price(price)} (за {period_label})'
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

            # Сохраняем корзину для автопокупки после пополнения баланса
            await user_cart_service.save_user_cart(
                user_id=db_user.id,
                cart_data={
                    'cart_mode': 'add_devices',
                    'devices_to_add': devices_difference,
                    'price_kopeks': price,
                },
            )
            logger.info(
                'Сохранена корзина add_devices для пользователя : + устройств, цена коп.',
                telegram_id=db_user.telegram_id,
                devices_difference=devices_difference,
                price=price,
            )

            await callback.message.answer(
                message_text,
                reply_markup=get_insufficient_balance_keyboard(
                    db_user.language,
                    amount_kopeks=missing_kopeks,
                    has_saved_cart=True,
                ),
                parse_mode='HTML',
            )
            await callback.answer()
            return

        action_text = texts.t(
            'DEVICE_CHANGE_ACTION_INCREASE',
            'увеличить до {count}',
        ).format(count=new_devices_count)
        if price > 0:
            cost_text = texts.t(
                'DEVICE_CHANGE_EXTRA_COST',
                'Доплата: {amount} (за {period})',
            ).format(
                amount=texts.format_price(price),
                period=period_label,
                months=period_label,
            )
            if total_discount > 0:
                cost_text += texts.t(
                    'DEVICE_CHANGE_DISCOUNT_INFO',
                    ' (скидка {percent}%: -{amount})',
                ).format(
                    percent=devices_discount_percent,
                    amount=texts.format_price(total_discount),
                )
        else:
            cost_text = texts.t('DEVICE_CHANGE_FREE', 'Бесплатно')

    else:
        price = 0
        action_text = texts.t(
            'DEVICE_CHANGE_ACTION_DECREASE',
            'уменьшить до {count}',
        ).format(count=new_devices_count)
        cost_text = texts.t('DEVICE_CHANGE_NO_REFUND', 'Возврат средств не производится')

    # Проверяем количество подключённых устройств для предупреждения
    devices_warning = ''
    if new_devices_count < current_devices and db_user.remnawave_uuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')
                if response and 'response' in response:
                    connected_count = response['response'].get('total', 0)
                    if connected_count > new_devices_count:
                        devices_warning = texts.t(
                            'DEVICE_CHANGE_RESET_WARNING',
                            (
                                '\n⚠️ <b>Внимание!</b>\n'
                                'У вас подключено {connected} устройств.\n'
                                'При уменьшении лимита до {new} все устройства будут сброшены.\n'
                                'Вам нужно будет заново подключить нужные устройства.\n'
                            ),
                        ).format(connected=connected_count, new=new_devices_count)
        except Exception as e:
            logger.error('Ошибка проверки устройств', error=e)

    confirm_text = texts.t(
        'DEVICE_CHANGE_CONFIRMATION',
        (
            '📱 <b>Подтверждение изменения</b>\n\n'
            'Текущее количество: {current} устройств\n'
            'Новое количество: {new} устройств\n\n'
            'Действие: {action}\n'
            '💰 {cost}\n\n'
            'Подтвердить изменение?'
        ),
    ).format(
        current=current_devices,
        new=new_devices_count,
        action=action_text,
        cost=cost_text,
    )

    if devices_warning:
        confirm_text += devices_warning

    await callback.message.edit_text(
        confirm_text,
        reply_markup=get_confirm_change_devices_keyboard(new_devices_count, price, db_user.language),
        parse_mode='HTML',
    )

    await callback.answer()


async def execute_change_devices(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    callback_parts = callback.data.split('_')
    new_devices_count = int(callback_parts[3])
    price = int(callback_parts[4])

    texts = get_texts(db_user.language)
    subscription = db_user.subscription
    current_devices = subscription.device_limit

    # Проверяем тариф подписки
    tariff = None
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Для тарифов - проверяем разрешено ли изменение устройств
    if tariff:
        tariff_device_price = getattr(tariff, 'device_price_kopeks', None)
        if tariff_device_price is None or tariff_device_price <= 0:
            await callback.answer(
                texts.t('TARIFF_DEVICES_DISABLED', '⚠️ Изменение устройств недоступно для вашего тарифа'),
                show_alert=True,
            )
            return
    elif not settings.is_devices_selection_enabled():
        await callback.answer(
            texts.t('DEVICES_SELECTION_DISABLED', '⚠️ Изменение количества устройств недоступно'),
            show_alert=True,
        )
        return

    # Проверяем минимальное количество устройств на тарифе
    tariff_min_devices = (getattr(tariff, 'device_limit', 1) or 1) if tariff else 1
    if new_devices_count < tariff_min_devices:
        await callback.answer(
            texts.t(
                'DEVICES_MIN_LIMIT_REACHED',
                '⚠️ Минимальное количество устройств для вашего тарифа: {limit}',
            ).format(limit=tariff_min_devices),
            show_alert=True,
        )
        return

    try:
        if price > 0:
            success = await subtract_user_balance(
                db, db_user, price, f'Изменение количества устройств с {current_devices} до {new_devices_count}'
            )

            if not success:
                await callback.answer(
                    texts.t('PAYMENT_CHARGE_ERROR', '⚠️ Ошибка списания средств'),
                    show_alert=True,
                )
                return

            charged_months = get_remaining_months(subscription.end_date)
            await create_transaction(
                db=db,
                user_id=db_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=price,
                description=f'Изменение устройств с {current_devices} до {new_devices_count} на {charged_months} мес',
            )

        subscription.device_limit = new_devices_count
        subscription.updated_at = datetime.now(UTC)

        await db.commit()

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        # При уменьшении лимита - удалить лишние устройства (последние подключённые)
        devices_reset_count = 0
        if new_devices_count < current_devices and db_user.remnawave_uuid:
            try:
                service = RemnaWaveService()
                async with service.get_api_client() as api:
                    response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')
                    if response and 'response' in response:
                        devices_list = response['response'].get('devices', [])
                        connected_count = len(devices_list)

                        # Если подключённых устройств больше чем новый лимит - удалить лишние
                        if connected_count > new_devices_count:
                            devices_to_remove = connected_count - new_devices_count
                            logger.info(
                                '🔧 Удаление лишних устройств при уменьшении лимита: подключено новый лимит удаляем',
                                connected_count=connected_count,
                                new_devices_count=new_devices_count,
                                devices_to_remove=devices_to_remove,
                            )

                            # Сортируем по дате (последние в конце) и удаляем последние
                            sorted_devices = sorted(
                                devices_list,
                                key=lambda d: d.get('updatedAt') or d.get('createdAt') or '',
                            )
                            devices_to_delete = sorted_devices[-devices_to_remove:]

                            for device in devices_to_delete:
                                device_hwid = device.get('hwid')
                                if device_hwid:
                                    try:
                                        delete_data = {'userUuid': db_user.remnawave_uuid, 'hwid': device_hwid}
                                        await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
                                        devices_reset_count += 1
                                        logger.info('✅ Удалено устройство', device_hwid=device_hwid)
                                    except Exception as del_error:
                                        logger.error(
                                            'Ошибка удаления устройства', device_hwid=device_hwid, del_error=del_error
                                        )
            except Exception as reset_error:
                logger.error('Ошибка удаления устройств при уменьшении лимита', reset_error=reset_error)

        await db.refresh(db_user)
        await db.refresh(subscription)

        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_update_notification(
                db, db_user, subscription, 'devices', current_devices, new_devices_count, price
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления об изменении устройств', error=e)

        if new_devices_count > current_devices:
            success_text = texts.t(
                'DEVICE_CHANGE_INCREASE_SUCCESS',
                '✅ Количество устройств увеличено!\n\n',
            )
            success_text += texts.t(
                'DEVICE_CHANGE_RESULT_LINE',
                '📱 Было: {old} → Стало: {new}\n',
            ).format(old=current_devices, new=new_devices_count)
            if price > 0:
                success_text += texts.t(
                    'DEVICE_CHANGE_CHARGED',
                    '💰 Списано: {amount}',
                ).format(amount=texts.format_price(price))
        else:
            success_text = texts.t(
                'DEVICE_CHANGE_DECREASE_SUCCESS',
                '✅ Количество устройств уменьшено!\n\n',
            )
            success_text += texts.t(
                'DEVICE_CHANGE_RESULT_LINE',
                '📱 Было: {old} → Стало: {new}\n',
            ).format(old=current_devices, new=new_devices_count)
            if devices_reset_count > 0:
                success_text += texts.t(
                    'DEVICE_CHANGE_DEVICES_REMOVED',
                    '\n🗑 Удалено устройств: {count}\n',
                ).format(count=devices_reset_count)
            success_text += texts.t(
                'DEVICE_CHANGE_NO_REFUND_INFO',
                'ℹ️ Возврат средств не производится',
            )

        await callback.message.edit_text(success_text, reply_markup=get_back_keyboard(db_user.language))

        logger.info(
            '✅ Пользователь изменил количество устройств с на доплата: ₽',
            telegram_id=db_user.telegram_id,
            current_devices=current_devices,
            new_devices_count=new_devices_count,
            price=price / 100,
        )

    except Exception as e:
        logger.error('Ошибка изменения количества устройств', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()


async def handle_device_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t('PAID_FEATURE_ONLY', '⚠️ Эта функция доступна только для платных подписок'),
            show_alert=True,
        )
        return

    if not db_user.remnawave_uuid:
        await callback.answer(
            texts.t('DEVICE_UUID_NOT_FOUND', '❌ UUID пользователя не найден'),
            show_alert=True,
        )
        return

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()

        async with service.get_api_client() as api:
            response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if response and 'response' in response:
                devices_info = response['response']
                total_devices = devices_info.get('total', 0)
                devices_list = devices_info.get('devices', [])

                if total_devices == 0:
                    await callback.message.edit_text(
                        texts.t('DEVICE_NONE_CONNECTED', 'ℹ️ У вас нет подключенных устройств'),
                        reply_markup=get_back_keyboard(db_user.language),
                    )
                    await callback.answer()
                    return

                await show_devices_page(callback, db_user, devices_list, page=1)
            else:
                await callback.answer(
                    texts.t(
                        'DEVICE_FETCH_INFO_ERROR',
                        '❌ Ошибка получения информации об устройствах',
                    ),
                    show_alert=True,
                )

    except Exception as e:
        logger.error('Ошибка получения списка устройств', error=e)
        await callback.answer(
            texts.t(
                'DEVICE_FETCH_INFO_ERROR',
                '❌ Ошибка получения информации об устройствах',
            ),
            show_alert=True,
        )

    await callback.answer()


async def show_devices_page(callback: types.CallbackQuery, db_user: User, devices_list: list[dict], page: int = 1):
    texts = get_texts(db_user.language)
    devices_per_page = 5

    pagination = paginate_list(devices_list, page=page, per_page=devices_per_page)

    devices_text = texts.t(
        'DEVICE_MANAGEMENT_OVERVIEW',
        (
            '🔄 <b>Управление устройствами</b>\n\n'
            '📊 Всего подключено: {total} устройств\n'
            '📄 Страница {page} из {pages}\n\n'
        ),
    ).format(total=len(devices_list), page=pagination.page, pages=pagination.total_pages)

    if pagination.items:
        devices_text += texts.t(
            'DEVICE_MANAGEMENT_CONNECTED_HEADER',
            '<b>Подключенные устройства:</b>\n',
        )
        for i, device in enumerate(pagination.items, 1):
            platform = device.get('platform', 'Unknown')
            device_model = device.get('deviceModel', 'Unknown')
            device_info = f'{platform} - {device_model}'

            if len(device_info) > 35:
                device_info = device_info[:32] + '...'

            devices_text += texts.t(
                'DEVICE_MANAGEMENT_LIST_ITEM',
                '• {device}\n',
            ).format(device=device_info)

    devices_text += texts.t(
        'DEVICE_MANAGEMENT_ACTIONS',
        ('\n💡 <b>Действия:</b>\n• Выберите устройство для сброса\n• Или сбросьте все устройства сразу'),
    )

    await callback.message.edit_text(
        devices_text,
        reply_markup=get_devices_management_keyboard(pagination.items, pagination, db_user.language),
        parse_mode='HTML',
    )


async def handle_devices_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()

        async with service.get_api_client() as api:
            response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if response and 'response' in response:
                devices_list = response['response'].get('devices', [])
                await show_devices_page(callback, db_user, devices_list, page=page)
            else:
                await callback.answer(
                    texts.t('DEVICE_FETCH_ERROR', '❌ Ошибка получения устройств'),
                    show_alert=True,
                )

    except Exception as e:
        logger.error('Ошибка перехода на страницу устройств', error=e)
        await callback.answer(
            texts.t('DEVICE_PAGE_LOAD_ERROR', '❌ Ошибка загрузки страницы'),
            show_alert=True,
        )


async def handle_single_device_reset(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        callback_parts = callback.data.split('_')
        if len(callback_parts) < 4:
            logger.error('Некорректный формат callback_data', callback_data=callback.data)
            await callback.answer(
                texts.t('DEVICE_RESET_INVALID_REQUEST', '❌ Ошибка: некорректный запрос'),
                show_alert=True,
            )
            return

        device_index = int(callback_parts[2])
        page = int(callback_parts[3])

        logger.info('🔧 Сброс устройства: index=, page', device_index=device_index, page=page)

    except (ValueError, IndexError) as e:
        logger.error('❌ Ошибка парсинга callback_data', callback_data=callback.data, error=e)
        await callback.answer(
            texts.t('DEVICE_RESET_PARSE_ERROR', '❌ Ошибка обработки запроса'),
            show_alert=True,
        )
        return

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()

        async with service.get_api_client() as api:
            response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if response and 'response' in response:
                devices_list = response['response'].get('devices', [])

                devices_per_page = 5
                pagination = paginate_list(devices_list, page=page, per_page=devices_per_page)

                if device_index < len(pagination.items):
                    device = pagination.items[device_index]
                    device_hwid = device.get('hwid')

                    if device_hwid:
                        delete_data = {'userUuid': db_user.remnawave_uuid, 'hwid': device_hwid}

                        await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)

                        platform = device.get('platform', 'Unknown')
                        device_model = device.get('deviceModel', 'Unknown')
                        device_info = f'{platform} - {device_model}'

                        await callback.answer(
                            texts.t(
                                'DEVICE_RESET_SUCCESS',
                                '✅ Устройство {device} успешно сброшено!',
                            ).format(device=device_info),
                            show_alert=True,
                        )

                        updated_response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')
                        if updated_response and 'response' in updated_response:
                            updated_devices = updated_response['response'].get('devices', [])

                            if updated_devices:
                                updated_pagination = paginate_list(
                                    updated_devices, page=page, per_page=devices_per_page
                                )
                                if not updated_pagination.items and page > 1:
                                    page = page - 1

                                await show_devices_page(callback, db_user, updated_devices, page=page)
                            else:
                                await callback.message.edit_text(
                                    texts.t(
                                        'DEVICE_RESET_ALL_DONE',
                                        'ℹ️ Все устройства сброшены',
                                    ),
                                    reply_markup=get_back_keyboard(db_user.language),
                                )

                        logger.info(
                            '✅ Пользователь сбросил устройство',
                            telegram_id=db_user.telegram_id,
                            device_info=device_info,
                        )
                    else:
                        await callback.answer(
                            texts.t(
                                'DEVICE_RESET_ID_FAILED',
                                '❌ Не удалось получить ID устройства',
                            ),
                            show_alert=True,
                        )
                else:
                    await callback.answer(
                        texts.t('DEVICE_RESET_NOT_FOUND', '❌ Устройство не найдено'),
                        show_alert=True,
                    )
            else:
                await callback.answer(
                    texts.t('DEVICE_FETCH_ERROR', '❌ Ошибка получения устройств'),
                    show_alert=True,
                )

    except Exception as e:
        logger.error('Ошибка сброса устройства', error=e)
        await callback.answer(
            texts.t('DEVICE_RESET_ERROR', '❌ Ошибка сброса устройства'),
            show_alert=True,
        )


async def handle_all_devices_reset_from_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    if not db_user.remnawave_uuid:
        await callback.answer(
            texts.t('DEVICE_UUID_NOT_FOUND', '❌ UUID пользователя не найден'),
            show_alert=True,
        )
        return

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()

        async with service.get_api_client() as api:
            devices_response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if not devices_response or 'response' not in devices_response:
                await callback.answer(
                    texts.t(
                        'DEVICE_LIST_FETCH_ERROR',
                        '❌ Ошибка получения списка устройств',
                    ),
                    show_alert=True,
                )
                return

            devices_list = devices_response['response'].get('devices', [])

            if not devices_list:
                await callback.answer(
                    texts.t('DEVICE_NONE_CONNECTED', 'ℹ️ У вас нет подключенных устройств'),
                    show_alert=True,
                )
                return

            logger.info('🔧 Найдено устройств для сброса', devices_list_count=len(devices_list))

            success_count = 0
            failed_count = 0

            for device in devices_list:
                device_hwid = device.get('hwid')
                if device_hwid:
                    try:
                        delete_data = {'userUuid': db_user.remnawave_uuid, 'hwid': device_hwid}

                        await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
                        success_count += 1
                        logger.info('✅ Устройство удалено', device_hwid=device_hwid)

                    except Exception as device_error:
                        failed_count += 1
                        logger.error(
                            '❌ Ошибка удаления устройства', device_hwid=device_hwid, device_error=device_error
                        )
                else:
                    failed_count += 1
                    logger.warning('⚠️ У устройства нет HWID', device=device)

            if success_count > 0:
                if failed_count == 0:
                    await callback.message.edit_text(
                        texts.t(
                            'DEVICE_RESET_ALL_SUCCESS_MESSAGE',
                            (
                                '✅ <b>Все устройства успешно сброшены!</b>\n\n'
                                '🔄 Сброшено: {count} устройств\n'
                                '📱 Теперь вы можете заново подключить свои устройства\n\n'
                                "💡 Используйте ссылку из раздела 'Моя подписка' для повторного подключения"
                            ),
                        ).format(count=success_count),
                        reply_markup=get_back_keyboard(db_user.language),
                        parse_mode='HTML',
                    )
                    logger.info(
                        '✅ Пользователь успешно сбросил устройств',
                        telegram_id=db_user.telegram_id,
                        success_count=success_count,
                    )
                else:
                    await callback.message.edit_text(
                        texts.t(
                            'DEVICE_RESET_PARTIAL_MESSAGE',
                            (
                                '⚠️ <b>Частичный сброс устройств</b>\n\n'
                                '✅ Удалено: {success} устройств\n'
                                '❌ Не удалось удалить: {failed} устройств\n\n'
                                'Попробуйте еще раз или обратитесь в поддержку.'
                            ),
                        ).format(success=success_count, failed=failed_count),
                        reply_markup=get_back_keyboard(db_user.language),
                        parse_mode='HTML',
                    )
                    logger.warning(
                        '⚠️ Частичный сброс у пользователя /',
                        telegram_id=db_user.telegram_id,
                        success_count=success_count,
                        devices_list_count=len(devices_list),
                    )
            else:
                await callback.message.edit_text(
                    texts.t(
                        'DEVICE_RESET_ALL_FAILED_MESSAGE',
                        (
                            '❌ <b>Не удалось сбросить устройства</b>\n\n'
                            'Попробуйте еще раз позже или обратитесь в техподдержку.\n\n'
                            'Всего устройств: {total}'
                        ),
                    ).format(total=len(devices_list)),
                    reply_markup=get_back_keyboard(db_user.language),
                    parse_mode='HTML',
                )
                logger.error(
                    '❌ Не удалось сбросить ни одного устройства у пользователя', telegram_id=db_user.telegram_id
                )

    except Exception as e:
        logger.error('Ошибка сброса всех устройств', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()


async def confirm_add_devices(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    devices_count = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    # Проверяем тариф подписки
    tariff = None
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Для тарифов - проверяем разрешено ли добавление устройств
    tariff_device_price = getattr(tariff, 'device_price_kopeks', None) if tariff else None
    if tariff:
        if tariff_device_price is None or tariff_device_price <= 0:
            await callback.answer(
                texts.t('TARIFF_DEVICES_DISABLED', '⚠️ Добавление устройств недоступно для вашего тарифа'),
                show_alert=True,
            )
            return
        price_per_device = tariff_device_price
    else:
        if not settings.is_devices_selection_enabled():
            await callback.answer(
                texts.t('DEVICES_SELECTION_DISABLED', '⚠️ Изменение количества устройств недоступно'),
                show_alert=True,
            )
            return
        price_per_device = settings.PRICE_PER_DEVICE

    resume_callback = None

    new_total_devices = subscription.device_limit + devices_count

    if settings.MAX_DEVICES_LIMIT > 0 and new_total_devices > settings.MAX_DEVICES_LIMIT:
        await callback.answer(
            f'⚠️ Превышен максимальный лимит устройств ({settings.MAX_DEVICES_LIMIT}). '
            f'У вас: {subscription.device_limit}, добавляете: {devices_count}',
            show_alert=True,
        )
        return

    devices_price_per_month = devices_count * price_per_device

    # Проверяем является ли тариф суточным
    is_daily_tariff = tariff and getattr(tariff, 'is_daily', False)

    if is_daily_tariff:
        # Для суточных тарифов считаем по дням (как в кабинете)
        now = datetime.now(UTC)
        days_left = max(1, (subscription.end_date - now).days)
        period_hint_days = days_left

        devices_discount_percent = _get_addon_discount_percent_for_user(
            db_user,
            'devices',
            period_hint_days,
        )
        discounted_per_month, discount_per_month = apply_percentage_discount(
            devices_price_per_month,
            devices_discount_percent,
        )
        # Цена = месячная_цена * days_left / 30
        price = int(discounted_per_month * days_left / 30)
        price = max(100, price)  # Минимум 1 рубль
        total_discount = int(discount_per_month * days_left / 30)
        period_label = f'{days_left} дн.' if days_left > 1 else '1 день'
    else:
        # Для обычных тарифов - по месяцам
        months_hint = get_remaining_months(subscription.end_date)
        period_hint_days = months_hint * 30 if months_hint > 0 else None

        devices_discount_percent = _get_addon_discount_percent_for_user(
            db_user,
            'devices',
            period_hint_days,
        )
        discounted_per_month, discount_per_month = apply_percentage_discount(
            devices_price_per_month,
            devices_discount_percent,
        )
        price, charged_months = calculate_prorated_price(
            discounted_per_month,
            subscription.end_date,
        )
        total_discount = discount_per_month * charged_months
        period_label = f'{charged_months} мес'

    logger.info(
        'Добавление устройств: ₽/мес × = ₽ (скидка ₽)',
        devices_count=devices_count,
        discounted_per_month=discounted_per_month / 100,
        period_label=period_label,
        price=price / 100,
        total_discount=total_discount / 100,
    )

    if db_user.balance_kopeks < price:
        missing_kopeks = price - db_user.balance_kopeks
        required_text = f'{texts.format_price(price)} (за {period_label})'
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

        # Сохраняем корзину для автопокупки после пополнения баланса
        await user_cart_service.save_user_cart(
            user_id=db_user.id,
            cart_data={
                'cart_mode': 'add_devices',
                'devices_to_add': devices_count,
                'price_kopeks': price,
            },
        )
        logger.info(
            'Сохранена корзина add_devices для пользователя : + устройств, цена коп.',
            telegram_id=db_user.telegram_id,
            devices_count=devices_count,
            price=price,
        )

        await callback.message.edit_text(
            message_text,
            reply_markup=get_insufficient_balance_keyboard(
                db_user.language,
                resume_callback=resume_callback,
                amount_kopeks=missing_kopeks,
                has_saved_cart=True,
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    try:
        success = await subtract_user_balance(
            db, db_user, price, f'Добавление {devices_count} устройств на {period_label}'
        )

        if not success:
            await callback.answer('⚠️ Ошибка списания средств', show_alert=True)
            return

        await add_subscription_devices(db, subscription, devices_count)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        await create_transaction(
            db=db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price,
            description=f'Добавление {devices_count} устройств на {period_label}',
        )

        await db.refresh(db_user)
        await db.refresh(subscription)

        # Отправляем уведомление админам о докупке устройств
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(callback.bot)
            old_device_limit = subscription.device_limit - devices_count
            await notification_service.send_subscription_update_notification(
                db, db_user, subscription, 'devices', old_device_limit, subscription.device_limit, price
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления о докупке устройств', error=e)

        success_text = (
            '✅ Устройства успешно добавлены!\n\n'
            f'📱 Добавлено: {devices_count} устройств\n'
            f'Новый лимит: {subscription.device_limit} устройств\n'
        )
        success_text += f'💰 Списано: {texts.format_price(price)} (за {period_label})'
        if total_discount > 0:
            success_text += f' (скидка {devices_discount_percent}%: -{texts.format_price(total_discount)})'

        await callback.message.edit_text(success_text, reply_markup=get_back_keyboard(db_user.language))

        logger.info(
            '✅ Пользователь добавил устройств за ₽',
            telegram_id=db_user.telegram_id,
            devices_count=devices_count,
            price=price / 100,
        )

    except Exception as e:
        logger.error('Ошибка добавления устройств', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()


async def handle_reset_devices(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await handle_device_management(callback, db_user, db)


async def confirm_reset_devices(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await handle_device_management(callback, db_user, db)


async def handle_device_guide(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    device_type = callback.data.split('_')[2]
    texts = get_texts(db_user.language)
    if device_type == 'tvs':
        await callback.message.edit_text(
            texts.t(
                'DEVICE_GUIDE_TV_GROUP_MESSAGE',
                '📺 <b>Телевизоры</b>\n\nВыберите платформу вашего ТВ:',
            ),
            reply_markup=get_tv_selection_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    subscription = db_user.subscription
    subscription_link = get_display_subscription_link(subscription)

    if not subscription_link:
        await callback.answer(
            texts.t('SUBSCRIPTION_LINK_UNAVAILABLE', '❌ Ссылка подписки недоступна'),
            show_alert=True,
        )
        return

    apps = await get_apps_for_platform_async(device_type, db_user.language)

    hide_subscription_link = settings.should_hide_subscription_link()

    if not apps:
        await callback.answer(
            texts.t('SUBSCRIPTION_DEVICE_APPS_NOT_FOUND', '❌ Приложения для этого устройства не найдены'),
            show_alert=True,
        )
        return

    featured_app = next((app for app in apps if app.get('isFeatured', False)), apps[0])
    featured_app_id = featured_app.get('id')
    other_apps = [app for app in apps if isinstance(app, dict) and app.get('id') and app.get('id') != featured_app_id]

    if device_type == 'tv':
        featured_app_name = html_mod.escape(str(featured_app.get('name') or 'v2rayTun'))
        android_tv_guide_text = texts.t(
            'ANDROID_TV_GUIDE_MESSAGE',
            (
                '📺 <b>Настройка для Android TV</b>\n\n'
                '📋 <b>Рекомендуемое приложение:</b> {app_name}\n\n'
                '<b>Шаг 1 - Установка:</b>\n'
                'Установите {app_name} на Android TV (есть в Google Play).\n\n'
                '<b>Шаг 2 - Добавление подписки:</b>\n'
                'Нажмите «Управление» — «Импорт с телефона» и СФОТОГРАФИРУЙТЕ QR-код на экране телевизора.\n\n'
                '<b>Шаг 3 - Подключение:</b>\n'
                'Если приложение уже установлено, нажмите кнопку «Подключиться».'
            ),
        ).format(app_name=featured_app_name)

        await callback.message.edit_text(
            android_tv_guide_text,
            reply_markup=get_connection_guide_keyboard(
                subscription_link,
                featured_app,
                device_type,
                db_user.language,
                has_other_apps=False,
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    other_app_names = ', '.join(
        html_mod.escape(str(app.get('name')).strip())
        for app in other_apps
        if isinstance(app.get('name'), str) and app.get('name').strip()
    )

    if hide_subscription_link:
        link_section = (
            texts.t('SUBSCRIPTION_DEVICE_LINK_TITLE', '🔗 <b>Ссылка подписки:</b>')
            + '\n'
            + texts.t(
                'SUBSCRIPTION_LINK_HIDDEN_NOTICE',
                'ℹ️ Ссылка подписки доступна по кнопкам ниже или в разделе "Моя подписка".',
            )
            + '\n\n'
        )
    else:
        link_section = (
            texts.t('SUBSCRIPTION_DEVICE_LINK_TITLE', '🔗 <b>Ссылка подписки:</b>')
            + f'\n<code>{html_mod.escape(subscription_link)}</code>\n\n'
        )

    guide_text = (
        texts.t(
            'SUBSCRIPTION_DEVICE_GUIDE_TITLE',
            '📱 <b>Настройка для {device_name}</b>',
        ).format(device_name=html_mod.escape(get_device_name(device_type, db_user.language)))
        + '\n\n'
        + link_section
        + texts.t(
            'SUBSCRIPTION_DEVICE_FEATURED_APP',
            '📋 <b>Рекомендуемое приложение:</b> {app_name}',
        ).format(app_name=html_mod.escape(featured_app.get('name', '')))
    )

    if other_app_names:
        guide_text += '\n\n' + texts.t(
            'SUBSCRIPTION_DEVICE_OTHER_APPS',
            '📦 <b>Другие приложения:</b> {app_list}',
        ).format(app_list=other_app_names)
        guide_text += '\n' + texts.t(
            'SUBSCRIPTION_DEVICE_OTHER_APPS_HINT',
            'Нажмите кнопку "Другие приложения" ниже, чтобы выбрать приложение.',
        )

    blocks_text = render_guide_blocks(featured_app.get('blocks', []), db_user.language)
    if blocks_text:
        guide_text += '\n\n' + blocks_text

    guide_text += '\n\n' + texts.t('SUBSCRIPTION_DEVICE_HOW_TO_TITLE', '💡 <b>Как подключить:</b>')
    guide_text += '\n' + '\n'.join(
        [
            texts.t(
                'SUBSCRIPTION_DEVICE_HOW_TO_STEP1',
                '1. Установите приложение по ссылке выше',
            ),
            texts.t(
                'SUBSCRIPTION_DEVICE_HOW_TO_STEP2',
                '2. Нажмите кнопку "Подключиться" ниже',
            ),
            texts.t(
                'SUBSCRIPTION_DEVICE_HOW_TO_STEP3',
                '3. Откройте приложение и вставьте ссылку',
            ),
            texts.t(
                'SUBSCRIPTION_DEVICE_HOW_TO_STEP4',
                '4. Подключитесь к серверу',
            ),
        ]
    )

    await callback.message.edit_text(
        guide_text,
        reply_markup=get_connection_guide_keyboard(
            subscription_link,
            featured_app,
            device_type,
            db_user.language,
            has_other_apps=bool(other_apps),
        ),
        parse_mode='HTML',
    )
    await callback.answer()


async def handle_android_tv_connect(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    subscription_link = get_display_subscription_link(db_user.subscription)

    if not subscription_link:
        await callback.answer(
            texts.t('SUBSCRIPTION_LINK_UNAVAILABLE', '❌ Ссылка подписки недоступна'),
            show_alert=True,
        )
        return

    await state.update_data(android_tv_subscription_url=subscription_link)
    await state.set_state(SubscriptionStates.waiting_for_android_tv_qr_photo)

    await callback.message.answer(
        texts.t(
            'ANDROID_TV_QR_PROMPT',
            (
                '📸 Сфотографируй QR-код с экрана телевизора в разделе '
                '«Управление» - «Импорт с телефона» и пришли мне.'
            ),
        ),
        reply_markup=get_android_tv_qr_wait_keyboard(db_user.language),
    )
    await callback.answer()


async def handle_android_tv_qr_cancel(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    await state.clear()
    await callback.message.edit_text(
        texts.t('ANDROID_TV_QR_CANCELLED', '❌ Ожидание QR-кода отменено.'),
        reply_markup=get_back_keyboard(db_user.language),
    )
    await callback.answer()


async def _download_photo_bytes(message: types.Message) -> bytes:
    if not message.photo:
        return b''

    file = await message.bot.get_file(message.photo[-1].file_id)
    buffer = BytesIO()
    await message.bot.download_file(file.file_path, destination=buffer)
    return buffer.getvalue()


async def handle_android_tv_qr_photo(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    state_data = await state.get_data()
    subscription_link = state_data.get('android_tv_subscription_url')
    if not isinstance(subscription_link, str) or not subscription_link.strip():
        subscription_link = get_display_subscription_link(db_user.subscription)

    if not subscription_link:
        await state.clear()
        await message.answer(
            texts.t('SUBSCRIPTION_LINK_UNAVAILABLE', '❌ Ссылка подписки недоступна'),
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    try:
        image_bytes = await _download_photo_bytes(message)
    except Exception as exc:
        logger.error('Ошибка загрузки фото QR для Android TV', error=exc)
        await message.answer(
            texts.t(
                'ANDROID_TV_QR_DOWNLOAD_ERROR',
                '❌ Не удалось загрузить фото. Отправьте изображение QR-кода ещё раз.',
            ),
            reply_markup=get_android_tv_qr_wait_keyboard(db_user.language),
        )
        return

    try:
        streamvault_key = decode_android_tv_qr_key(image_bytes)
    except AndroidTvQrDecodeError as exc:
        logger.warning('Не удалось декодировать Android TV QR', error=exc)
        await message.answer(
            texts.t(
                'ANDROID_TV_QR_DECODE_ERROR',
                (
                    '❌ Не удалось распознать QR-код. Убедись, что QR-код целиком в кадре, '
                    'и отправь фото ещё раз.'
                ),
            ),
            reply_markup=get_android_tv_qr_wait_keyboard(db_user.language),
        )
        return
    except Exception as exc:
        logger.error('Ошибка декодирования QR для Android TV', error=exc)
        await message.answer(
            texts.t(
                'ANDROID_TV_QR_DECODE_ERROR',
                (
                    '❌ Не удалось распознать QR-код. Убедись, что QR-код целиком в кадре, '
                    'и отправь фото ещё раз.'
                ),
            ),
            reply_markup=get_android_tv_qr_wait_keyboard(db_user.language),
        )
        return

    try:
        response = await publish_streamvault_subscription(streamvault_key, subscription_link)
        logger.info(
            'Android TV import sent',
            telegram_id=db_user.telegram_id,
            key_prefix=streamvault_key[:6],
            key_suffix=streamvault_key[-4:],
            response=response,
        )
    except AndroidTvStreamVaultError as exc:
        logger.warning('Не удалось отправить Android TV импорт', error=exc)
        await message.answer(
            texts.t(
                'ANDROID_TV_STREAMVAULT_ERROR',
                (
                    '❌ QR-код распознан, но не удалось отправить подписку на телевизор. '
                    'Попробуй отправить фото ещё раз.'
                ),
            ),
            reply_markup=get_android_tv_qr_wait_keyboard(db_user.language),
        )
        return
    except Exception as exc:
        logger.error('Неожиданная ошибка отправки Android TV импорта', error=exc)
        await message.answer(
            texts.t(
                'ANDROID_TV_STREAMVAULT_ERROR',
                (
                    '❌ QR-код распознан, но не удалось отправить подписку на телевизор. '
                    'Попробуй отправить фото ещё раз.'
                ),
            ),
            reply_markup=get_android_tv_qr_wait_keyboard(db_user.language),
        )
        return

    await state.clear()
    await message.answer(
        texts.t(
            'ANDROID_TV_IMPORT_SUCCESS',
            (
                '✅ Подписка отправлена на телевизор.\n\n'
                'Открой {app_name} на Android TV и дождись импорта.'
            ),
        ).format(app_name='v2rayTun'),
        reply_markup=get_back_keyboard(db_user.language),
    )


async def handle_android_tv_qr_non_photo(message: types.Message, db_user: User):
    texts = get_texts(db_user.language)
    await message.answer(
        texts.t(
            'ANDROID_TV_EXPECT_PHOTO',
            (
                '📸 Сейчас ожидаю фотографию QR-кода с экрана телевизора.\n'
                'Отправь фото в этот чат.'
            ),
        ),
        reply_markup=get_android_tv_qr_wait_keyboard(db_user.language),
    )


async def handle_app_selection(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    device_type = callback.data.split('_')[2]
    texts = get_texts(db_user.language)

    apps = await get_apps_for_platform_async(device_type, db_user.language)

    if not apps:
        await callback.answer(
            texts.t('SUBSCRIPTION_DEVICE_APPS_NOT_FOUND', '❌ Приложения для этого устройства не найдены'),
            show_alert=True,
        )
        return

    app_text = (
        texts.t(
            'SUBSCRIPTION_APPS_TITLE',
            '📱 <b>Приложения для {device_name}</b>',
        ).format(device_name=html_mod.escape(get_device_name(device_type, db_user.language)))
        + '\n\n'
        + texts.t('SUBSCRIPTION_APPS_PROMPT', 'Выберите приложение для подключения:')
    )

    await callback.message.edit_text(
        app_text, reply_markup=get_app_selection_keyboard(device_type, apps, db_user.language), parse_mode='HTML'
    )
    await callback.answer()


async def handle_specific_app_guide(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_', 2)
    if len(parts) < 3:
        await callback.answer('Invalid callback data', show_alert=True)
        return
    _, device_type, app_id = parts
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    subscription_link = get_display_subscription_link(subscription)

    if not subscription_link:
        await callback.answer(
            texts.t('SUBSCRIPTION_LINK_UNAVAILABLE', '❌ Ссылка подписки недоступна'),
            show_alert=True,
        )
        return

    apps = await get_apps_for_platform_async(device_type, db_user.language)
    app = next((a for a in apps if a.get('id') == app_id), None) if apps else None

    if not app:
        await callback.answer(
            texts.t('SUBSCRIPTION_APP_NOT_FOUND', '❌ Приложение не найдено'),
            show_alert=True,
        )
        return

    hide_subscription_link = settings.should_hide_subscription_link()

    if hide_subscription_link:
        link_section = (
            texts.t('SUBSCRIPTION_DEVICE_LINK_TITLE', '🔗 <b>Ссылка подписки:</b>')
            + '\n'
            + texts.t(
                'SUBSCRIPTION_LINK_HIDDEN_NOTICE',
                'ℹ️ Ссылка подписки доступна по кнопкам ниже или в разделе "Моя подписка".',
            )
            + '\n\n'
        )
    else:
        link_section = (
            texts.t('SUBSCRIPTION_DEVICE_LINK_TITLE', '🔗 <b>Ссылка подписки:</b>')
            + f'\n<code>{html_mod.escape(subscription_link)}</code>\n\n'
        )

    guide_text = (
        texts.t(
            'SUBSCRIPTION_SPECIFIC_APP_TITLE',
            '📱 <b>{app_name} - {device_name}</b>',
        ).format(
            app_name=html_mod.escape(app.get('name', '')),
            device_name=html_mod.escape(get_device_name(device_type, db_user.language)),
        )
        + '\n\n'
        + link_section
    )

    blocks_text = render_guide_blocks(app.get('blocks', []), db_user.language)
    if blocks_text:
        guide_text += blocks_text + '\n\n'

    await callback.message.edit_text(
        guide_text,
        reply_markup=get_specific_app_keyboard(
            subscription_link,
            app,
            device_type,
            db_user.language,
        ),
        parse_mode='HTML',
    )
    await callback.answer()


async def show_device_connection_help(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    subscription = db_user.subscription
    subscription_link = get_display_subscription_link(subscription)

    if not subscription_link:
        await callback.answer('❌ Ссылка подписки недоступна', show_alert=True)
        return

    help_text = f"""
📱 <b>Как подключить устройство заново</b>

После сброса устройства вам нужно:

<b>1. Получить ссылку подписки:</b>
📋 Скопируйте ссылку ниже или найдите её в разделе "Моя подписка"

<b>2. Настроить VPN приложение:</b>
• Откройте ваше VPN приложение
• Найдите функцию "Добавить подписку" или "Import"
• Вставьте скопированную ссылку

<b>3. Подключиться:</b>
• Выберите сервер
• Нажмите "Подключить"

<b>🔗 Ваша ссылка подписки:</b>
<code>{html_mod.escape(subscription_link)}</code>

💡 <b>Совет:</b> Сохраните эту ссылку - она понадобится для подключения новых устройств
"""

    await callback.message.edit_text(
        help_text, reply_markup=get_device_management_help_keyboard(db_user.language), parse_mode='HTML'
    )
    await callback.answer()
