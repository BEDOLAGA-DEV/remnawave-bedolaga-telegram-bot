from datetime import datetime

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import TransactionType, User
from app.keyboards.inline import (
    get_back_keyboard,
    get_countries_keyboard,
    get_devices_keyboard,
    get_insufficient_balance_keyboard,
    get_manage_countries_keyboard,
)
from app.localization.texts import get_texts
from app.services.subscription_checkout_service import (
    save_subscription_checkout_draft,
    should_offer_checkout_resume,
)
from app.services.subscription_service import SubscriptionService
from app.states import SubscriptionStates
from app.utils.pricing_utils import (
    apply_percentage_discount,
    calculate_prorated_price,
    get_remaining_months,
)

from .common import _get_addon_discount_percent_for_user, _get_period_hint_from_subscription, logger
from .summary import present_subscription_summary


async def handle_add_countries(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    if not await _should_show_countries_management(db_user):
        texts = get_texts(db_user.language)
        await callback.answer(
            texts.t(
                'COUNTRY_MANAGEMENT_UNAVAILABLE',
                'ℹ️ Управление серверами недоступно - доступен только один сервер',
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t('PAID_FEATURE_ONLY', '⚠ Эта функция доступна только для платных подписок'),
            show_alert=True,
        )
        return

    countries = await _get_available_countries(db_user.promo_group_id)
    current_countries = subscription.connected_squads

    period_hint_days = _get_period_hint_from_subscription(subscription)
    servers_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        'servers',
        period_hint_days,
    )

    current_countries_names = []
    for country in countries:
        if country['uuid'] in current_countries:
            current_countries_names.append(country['name'])

    current_list = (
        '\n'.join(f'• {name}' for name in current_countries_names)
        if current_countries_names
        else texts.t('COUNTRY_MANAGEMENT_NONE', 'Нет подключенных стран')
    )

    text = texts.t(
        'COUNTRY_MANAGEMENT_PROMPT',
        (
            '🌍 <b>Управление странами подписки</b>\n\n'
            '📋 <b>Текущие страны ({current_count}):</b>\n'
            '{current_list}\n\n'
            '💡 <b>Инструкция:</b>\n'
            '✅ - страна подключена\n'
            '➕ - будет добавлена (платно)\n'
            '➖ - будет отключена (бесплатно)\n'
            '⚪ - не выбрана\n\n'
            '⚠️ <b>Важно:</b> Повторное подключение отключенных стран будет платным!'
        ),
    ).format(
        current_count=len(current_countries),
        current_list=current_list,
    )

    await state.update_data(countries=current_countries.copy())

    await callback.message.edit_text(
        text,
        reply_markup=get_manage_countries_keyboard(
            countries,
            current_countries.copy(),
            current_countries,
            db_user.language,
            subscription.end_date,
            servers_discount_percent,
        ),
        parse_mode='HTML',
    )

    await callback.answer()


async def get_countries_price_by_uuids_fallback(
    country_uuids: list[str],
    db: AsyncSession,
    promo_group_id: int | None = None,
) -> tuple[int, list[int]]:
    try:
        from app.database.crud.server_squad import get_server_squad_by_uuid

        total_price = 0
        prices_list = []

        for country_uuid in country_uuids:
            try:
                server = await get_server_squad_by_uuid(db, country_uuid)
                is_allowed = True
                if promo_group_id is not None and server:
                    allowed_ids = {pg.id for pg in server.allowed_promo_groups}
                    is_allowed = promo_group_id in allowed_ids

                if server and server.is_available and not server.is_full and is_allowed:
                    price = server.price_kopeks
                    total_price += price
                    prices_list.append(price)
                else:
                    default_price = 0
                    total_price += default_price
                    prices_list.append(default_price)
            except Exception:
                default_price = 0
                total_price += default_price
                prices_list.append(default_price)

        return total_price, prices_list

    except Exception as e:
        logger.error(f'Ошибка fallback функции: {e}')
        default_prices = [0] * len(country_uuids)
        return sum(default_prices), default_prices


async def handle_manage_country(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    logger.info(f'🔍 Управление страной: {callback.data}')

    country_uuid = callback.data.split('_')[2]

    subscription = db_user.subscription
    if not subscription or subscription.is_trial:
        texts = get_texts(db_user.language)
        await callback.answer(
            texts.t('PAID_FEATURE_ONLY_SHORT', '⚠ Только для платных подписок'),
            show_alert=True,
        )
        return

    data = await state.get_data()
    current_selected = data.get('countries', subscription.connected_squads.copy())

    countries = await _get_available_countries(db_user.promo_group_id)
    allowed_country_ids = {country['uuid'] for country in countries}

    if country_uuid not in allowed_country_ids and country_uuid not in current_selected:
        texts = get_texts(db_user.language)
        await callback.answer(
            texts.t(
                'COUNTRY_NOT_AVAILABLE_PROMOGROUP',
                '❌ Сервер недоступен для вашей промогруппы',
            ),
            show_alert=True,
        )
        return

    if country_uuid in current_selected:
        current_selected.remove(country_uuid)
        action = 'removed'
    else:
        current_selected.append(country_uuid)
        action = 'added'

    logger.info(f'🔍 Страна {country_uuid} {action}')

    await state.update_data(countries=current_selected)

    period_hint_days = _get_period_hint_from_subscription(subscription)
    servers_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        'servers',
        period_hint_days,
    )

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_manage_countries_keyboard(
                countries,
                current_selected,
                subscription.connected_squads,
                db_user.language,
                subscription.end_date,
                servers_discount_percent,
            )
        )
        logger.info('✅ Клавиатура обновлена')

    except Exception as e:
        logger.error(f'⚠ Ошибка обновления клавиатуры: {e}')

    await callback.answer()


async def apply_countries_changes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    logger.info('🔧 Применение изменений стран')

    data = await state.get_data()
    texts = get_texts(db_user.language)

    await save_subscription_checkout_draft(db_user.id, dict(data))
    resume_callback = 'subscription_resume_checkout' if should_offer_checkout_resume(db_user, True) else None
    subscription = db_user.subscription

    selected_countries = data.get('countries', [])
    current_countries = subscription.connected_squads

    countries = await _get_available_countries(db_user.promo_group_id)
    allowed_country_ids = {country['uuid'] for country in countries}

    selected_countries = [
        country_uuid
        for country_uuid in selected_countries
        if country_uuid in allowed_country_ids or country_uuid in current_countries
    ]

    added = [c for c in selected_countries if c not in current_countries]
    removed = [c for c in current_countries if c not in selected_countries]

    if not added and not removed:
        await callback.answer(
            texts.t('COUNTRY_CHANGES_NOT_FOUND', '⚠️ Изменения не обнаружены'),
            show_alert=True,
        )
        return

    logger.info(f'🔧 Добавлено: {added}, Удалено: {removed}')

    months_to_pay = get_remaining_months(subscription.end_date)

    period_hint_days = months_to_pay * 30 if months_to_pay > 0 else None
    servers_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        'servers',
        period_hint_days,
    )

    cost_per_month = 0
    added_names = []
    removed_names = []

    added_server_components: list[dict[str, int]] = []

    for country in countries:
        if not country.get('is_available', True):
            continue

        if country['uuid'] in added:
            server_price_per_month = country['price_kopeks']
            discounted_per_month, discount_per_month = apply_percentage_discount(
                server_price_per_month,
                servers_discount_percent,
            )
            cost_per_month += discounted_per_month
            added_names.append(country['name'])
            added_server_components.append(
                {
                    'discounted_per_month': discounted_per_month,
                    'discount_per_month': discount_per_month,
                    'original_per_month': server_price_per_month,
                }
            )
        if country['uuid'] in removed:
            removed_names.append(country['name'])

    total_cost, charged_months = calculate_prorated_price(cost_per_month, subscription.end_date)

    added_server_prices = [component['discounted_per_month'] * charged_months for component in added_server_components]

    total_discount = sum(component['discount_per_month'] * charged_months for component in added_server_components)

    if added_names:
        logger.info(
            'Стоимость новых серверов: %.2f₽/мес × %s мес = %.2f₽ (скидка %.2f₽)',
            cost_per_month / 100,
            charged_months,
            total_cost / 100,
            total_discount / 100,
        )

    if total_cost > 0 and db_user.balance_kopeks < total_cost:
        missing_kopeks = total_cost - db_user.balance_kopeks
        required_text = f'{texts.format_price(total_cost)} (за {charged_months} мес)'
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

        await callback.message.answer(
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

    # Проверяем, что пользователь не пытается отключить все страны (должна остаться хотя бы 1 страна)
    if len(selected_countries) == 0:
        await callback.answer(
            texts.t(
                'COUNTRIES_MINIMUM_REQUIRED',
                '❌ Нельзя отключить все страны. Должна быть подключена хотя бы одна страна.',
            ),
            show_alert=True,
        )
        return

    try:
        if added and total_cost > 0:
            success = await subtract_user_balance(
                db, db_user, total_cost, f'Добавление стран: {", ".join(added_names)} на {charged_months} мес'
            )
            if not success:
                await callback.answer(
                    texts.t('PAYMENT_CHARGE_ERROR', '⚠️ Ошибка списания средств'),
                    show_alert=True,
                )
                return

            await create_transaction(
                db=db,
                user_id=db_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=total_cost,
                description=f'Добавление стран к подписке: {", ".join(added_names)} на {charged_months} мес',
            )

        if added:
            from app.database.crud.server_squad import add_user_to_servers, get_server_ids_by_uuids
            from app.database.crud.subscription import add_subscription_servers

            added_server_ids = await get_server_ids_by_uuids(db, added)

            if added_server_ids:
                await add_subscription_servers(db, subscription, added_server_ids, added_server_prices)
                await add_user_to_servers(db, added_server_ids)

                logger.info(
                    f'📊 Добавлены серверы с ценами за {charged_months} мес: {list(zip(added_server_ids, added_server_prices, strict=False))}'
                )

        subscription.connected_squads = selected_countries
        subscription.updated_at = datetime.utcnow()
        await db.commit()

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        await db.refresh(subscription)

        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_update_notification(
                db, db_user, subscription, 'servers', current_countries, selected_countries, total_cost
            )
        except Exception as e:
            logger.error(f'Ошибка отправки уведомления об изменении серверов: {e}')

        success_text = texts.t(
            'COUNTRY_CHANGES_SUCCESS_HEADER',
            '✅ <b>Страны успешно обновлены!</b>\n\n',
        )

        if added_names:
            success_text += texts.t(
                'COUNTRY_CHANGES_ADDED_HEADER',
                '➕ <b>Добавлены страны:</b>\n',
            )
            success_text += '\n'.join(f'• {name}' for name in added_names)
            if total_cost > 0:
                success_text += '\n' + texts.t(
                    'COUNTRY_CHANGES_CHARGED',
                    '💰 Списано: {amount} (за {months} мес)',
                ).format(
                    amount=texts.format_price(total_cost),
                    months=charged_months,
                )
                if total_discount > 0:
                    success_text += texts.t(
                        'COUNTRY_CHANGES_DISCOUNT_INFO',
                        ' (скидка {percent}%: -{amount})',
                    ).format(
                        percent=servers_discount_percent,
                        amount=texts.format_price(total_discount),
                    )
            success_text += '\n'

        if removed_names:
            success_text += '\n' + texts.t(
                'COUNTRY_CHANGES_REMOVED_HEADER',
                '➖ <b>Отключены страны:</b>\n',
            )
            success_text += '\n'.join(f'• {name}' for name in removed_names)
            success_text += (
                '\n'
                + texts.t(
                    'COUNTRY_CHANGES_REMOVED_WARNING',
                    'ℹ️ Повторное подключение будет платным',
                )
                + '\n'
            )

        success_text += '\n' + texts.t(
            'COUNTRY_CHANGES_ACTIVE_COUNT',
            '🌐 <b>Активных стран:</b> {count}',
        ).format(count=len(selected_countries))

        await callback.message.edit_text(
            success_text, reply_markup=get_back_keyboard(db_user.language), parse_mode='HTML'
        )

        await state.clear()
        logger.info(
            f'✅ Пользователь {db_user.telegram_id} обновил страны. Добавлено: {len(added)}, удалено: {len(removed)}, заплатил: {total_cost / 100}₽'
        )

    except Exception as e:
        logger.error(f'⚠️ Ошибка применения изменений: {e}')
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()


async def select_country(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    country_uuid = callback.data.split('_')[1]
    data = await state.get_data()

    if 'period_days' not in data:
        await callback.answer('❌ Данные подписки устарели. Начните оформление заново.', show_alert=True)
        return

    selected_countries = data.get('countries', [])
    if country_uuid in selected_countries:
        selected_countries.remove(country_uuid)
    else:
        selected_countries.append(country_uuid)

    countries = await _get_available_countries(db_user.promo_group_id)
    allowed_country_ids = {country['uuid'] for country in countries}

    if country_uuid not in allowed_country_ids and country_uuid not in selected_countries:
        await callback.answer('❌ Сервер недоступен для вашей промогруппы', show_alert=True)
        return

    period_base_price = PERIOD_PRICES.get(data['period_days'], 0)
    discounted_base_price, _ = apply_percentage_discount(
        period_base_price,
        db_user.get_promo_discount('period', data['period_days']),
    )

    base_price = discounted_base_price + settings.get_traffic_price(data['traffic_gb'])

    try:
        subscription_service = SubscriptionService()
        countries_price, _ = await subscription_service.get_countries_price_by_uuids(
            selected_countries,
            db,
            promo_group_id=db_user.promo_group_id,
        )
    except AttributeError:
        logger.warning('Используем fallback функцию для расчета цен стран')
        countries_price, _ = await get_countries_price_by_uuids_fallback(
            selected_countries,
            db,
            promo_group_id=db_user.promo_group_id,
        )

    data['countries'] = selected_countries
    data['total_price'] = base_price + countries_price
    await state.set_data(data)

    await callback.message.edit_reply_markup(
        reply_markup=get_countries_keyboard(countries, selected_countries, db_user.language)
    )
    await callback.answer()


async def countries_continue(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    data = await state.get_data()
    texts = get_texts(db_user.language)

    if not data.get('countries'):
        await callback.answer('⚠️ Выберите хотя бы одну страну!', show_alert=True)
        return

    if not settings.is_devices_selection_enabled():
        if await present_subscription_summary(callback, state, db_user, texts):
            await callback.answer()
        return

    selected_devices = data.get('devices', settings.DEFAULT_DEVICE_LIMIT)

    await callback.message.edit_text(
        texts.SELECT_DEVICES, reply_markup=get_devices_keyboard(selected_devices, db_user.language)
    )

    await state.set_state(SubscriptionStates.selecting_devices)
    await callback.answer()


async def _get_available_countries(promo_group_id: int | None = None):
    from app.database.crud.server_squad import get_available_server_squads
    from app.database.database import AsyncSessionLocal
    from app.utils.cache import cache, cache_key

    cache_key_value = cache_key('available_countries', promo_group_id or 'all')
    cached_countries = await cache.get(cache_key_value)
    if cached_countries:
        return cached_countries

    try:
        async with AsyncSessionLocal() as db:
            available_servers = await get_available_server_squads(db, promo_group_id=promo_group_id)

        if promo_group_id is not None and not available_servers:
            logger.info(
                'Промогруппа %s не имеет доступных серверов, возврат пустого списка',
                promo_group_id,
            )
            await cache.set(cache_key_value, [], 60)
            return []

        countries = []
        for server in available_servers:
            countries.append(
                {
                    'uuid': server.squad_uuid,
                    'name': server.display_name,
                    'price_kopeks': server.price_kopeks,
                    'country_code': server.country_code,
                    'is_available': server.is_available and not server.is_full,
                    'description': server.description or '',
                }
            )

        if not countries:
            logger.info('🔄 Серверов в БД нет, получаем из RemnaWave...')
            from app.services.remnawave_service import RemnaWaveService

            service = RemnaWaveService()
            squads = await service.get_all_squads()

            for squad in squads:
                squad_name = squad['name']

                if not any(
                    flag in squad_name for flag in ['🇳🇱', '🇩🇪', '🇺🇸', '🇫🇷', '🇬🇧', '🇮🇹', '🇪🇸', '🇨🇦', '🇯🇵', '🇸🇬', '🇦🇺']
                ):
                    name_lower = squad_name.lower()
                    if 'netherlands' in name_lower or 'нидерланды' in name_lower or 'nl' in name_lower:
                        squad_name = f'🇳🇱 {squad_name}'
                    elif 'germany' in name_lower or 'германия' in name_lower or 'de' in name_lower:
                        squad_name = f'🇩🇪 {squad_name}'
                    elif 'usa' in name_lower or 'сша' in name_lower or 'america' in name_lower or 'us' in name_lower:
                        squad_name = f'🇺🇸 {squad_name}'
                    else:
                        squad_name = f'🌐 {squad_name}'

                countries.append(
                    {
                        'uuid': squad['uuid'],
                        'name': squad_name,
                        'price_kopeks': 0,
                        'is_available': True,
                        'description': '',
                    }
                )

        await cache.set(cache_key_value, countries, 300)
        return countries

    except Exception as e:
        logger.error(f'Ошибка получения списка стран: {e}')
        fallback_countries = [
            {
                'uuid': 'default-free',
                'name': '🆓 Бесплатный сервер',
                'price_kopeks': 0,
                'is_available': True,
                'description': '',
            },
        ]

        await cache.set(cache_key_value, fallback_countries, 60)
        return fallback_countries


async def _get_countries_info(squad_uuids):
    countries = await _get_available_countries()
    return [c for c in countries if c['uuid'] in squad_uuids]


def _get_preselected_free_countries(countries: list[dict]) -> list[str]:
    """Получить UUID бесплатных серверов для автоматического предвыбора."""
    return [c['uuid'] for c in countries if c.get('is_available', True) and c.get('price_kopeks', 0) == 0]


def _build_countries_selection_text(countries: list[dict], base_text: str) -> str:
    """
    Формирует текст выбора серверов с описаниями.

    Если у серверов есть description — добавляет их под базовым текстом.
    """
    descriptions = []
    for country in countries:
        if not country.get('is_available', True):
            continue
        desc = country.get('description', '').strip()
        if desc:
            name = country.get('name', '')
            descriptions.append(f'<b>{name}</b>\n{desc}')

    if not descriptions:
        return base_text

    return f'{base_text}\n\n' + '\n\n'.join(descriptions)


async def handle_add_country_to_subscription(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    logger.info(f'🔍 handle_add_country_to_subscription вызван для {db_user.telegram_id}')
    logger.info(f'🔍 Callback data: {callback.data}')

    current_state = await state.get_state()
    logger.info(f'🔍 Текущее состояние: {current_state}')

    country_uuid = callback.data.split('_')[1]
    data = await state.get_data()
    logger.info(f'🔍 Данные состояния: {data}')

    selected_countries = data.get('countries', [])
    countries = await _get_available_countries(db_user.promo_group_id)
    allowed_country_ids = {country['uuid'] for country in countries}

    if country_uuid not in allowed_country_ids and country_uuid not in selected_countries:
        await callback.answer('❌ Сервер недоступен для вашей промогруппы', show_alert=True)
        return

    if country_uuid in selected_countries:
        selected_countries.remove(country_uuid)
        logger.info(f'🔍 Удалена страна: {country_uuid}')
    else:
        selected_countries.append(country_uuid)
        logger.info(f'🔍 Добавлена страна: {country_uuid}')

    total_price = 0
    subscription = db_user.subscription
    period_hint_days = _get_period_hint_from_subscription(subscription)
    servers_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        'servers',
        period_hint_days,
    )

    for country in countries:
        if not country.get('is_available', True):
            continue

        if country['uuid'] in selected_countries and country['uuid'] not in subscription.connected_squads:
            server_price = country['price_kopeks']
            if servers_discount_percent > 0 and server_price > 0:
                discounted_price, _ = apply_percentage_discount(
                    server_price,
                    servers_discount_percent,
                )
            else:
                discounted_price = server_price
            total_price += discounted_price

    data['countries'] = selected_countries
    data['total_price'] = total_price
    await state.set_data(data)

    logger.info(f'🔍 Новые выбранные страны: {selected_countries}')
    logger.info(f'🔍 Общая стоимость: {total_price}')

    try:
        from app.keyboards.inline import get_manage_countries_keyboard

        await callback.message.edit_reply_markup(
            reply_markup=get_manage_countries_keyboard(
                countries,
                selected_countries,
                subscription.connected_squads,
                db_user.language,
                subscription.end_date,
                servers_discount_percent,
            )
        )
        logger.info('✅ Клавиатура обновлена')
    except Exception as e:
        logger.error(f'❌ Ошибка обновления клавиатуры: {e}')

    await callback.answer()


async def _should_show_countries_management(user: User | None = None) -> bool:
    try:
        promo_group_id = user.promo_group_id if user else None

        promo_group = getattr(user, 'promo_group', None) if user else None
        if promo_group and getattr(promo_group, 'server_squads', None):
            allowed_servers = [
                server for server in promo_group.server_squads if server.is_available and not server.is_full
            ]

            if allowed_servers:
                if len(allowed_servers) > 1:
                    logger.debug(
                        'Промогруппа %s имеет %s доступных серверов, показываем управление странами',
                        promo_group.id,
                        len(allowed_servers),
                    )
                    return True

                logger.debug(
                    'Промогруппа %s имеет всего %s доступный сервер, пропускаем шаг выбора стран',
                    promo_group.id,
                    len(allowed_servers),
                )
                return False

        countries = await _get_available_countries(promo_group_id)
        available_countries = [c for c in countries if c.get('is_available', True)]
        return len(available_countries) > 1
    except Exception as e:
        logger.error(f'Ошибка проверки доступных серверов: {e}')
        return True


async def confirm_add_countries_to_subscription(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    data = await state.get_data()
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    selected_countries = data.get('countries', [])
    current_countries = subscription.connected_squads

    countries = await _get_available_countries(db_user.promo_group_id)
    allowed_country_ids = {country['uuid'] for country in countries}

    selected_countries = [
        country_uuid
        for country_uuid in selected_countries
        if country_uuid in allowed_country_ids or country_uuid in current_countries
    ]

    new_countries = [c for c in selected_countries if c not in current_countries]
    removed_countries = [c for c in current_countries if c not in selected_countries]

    if not new_countries and not removed_countries:
        await callback.answer('⚠️ Изменения не обнаружены', show_alert=True)
        return

    total_price = 0
    new_countries_names = []
    removed_countries_names = []

    period_hint_days = _get_period_hint_from_subscription(subscription)
    servers_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        'servers',
        period_hint_days,
    )
    total_discount_value = 0

    for country in countries:
        if not country.get('is_available', True):
            continue

        if country['uuid'] in new_countries:
            server_price = country['price_kopeks']
            if servers_discount_percent > 0 and server_price > 0:
                discounted_per_month, discount_per_month = apply_percentage_discount(
                    server_price,
                    servers_discount_percent,
                )
            else:
                discounted_per_month = server_price
                discount_per_month = 0

            charged_price, charged_months = calculate_prorated_price(
                discounted_per_month,
                subscription.end_date,
            )

            total_price += charged_price
            total_discount_value += discount_per_month * charged_months
            new_countries_names.append(country['name'])
        if country['uuid'] in removed_countries:
            removed_countries_names.append(country['name'])

    if new_countries and db_user.balance_kopeks < total_price:
        missing_kopeks = total_price - db_user.balance_kopeks
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
            required=texts.format_price(total_price),
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
        await state.clear()
        await callback.answer()
        return

    try:
        # Проверяем, что пользователь не пытается отключить все страны (должна остаться хотя бы 1 страна)
        if len(selected_countries) == 0:
            await callback.answer(
                texts.t(
                    'COUNTRIES_MINIMUM_REQUIRED',
                    '❌ Нельзя отключить все страны. Должна быть подключена хотя бы одна страна.',
                ),
                show_alert=True,
            )
            return

        if new_countries and total_price > 0:
            success = await subtract_user_balance(
                db, db_user, total_price, f'Добавление стран к подписке: {", ".join(new_countries_names)}'
            )

            if not success:
                await callback.answer('❌ Ошибка списания средств', show_alert=True)
                return

            await create_transaction(
                db=db,
                user_id=db_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=total_price,
                description=f'Добавление стран к подписке: {", ".join(new_countries_names)}',
            )

        subscription.connected_squads = selected_countries
        subscription.updated_at = datetime.utcnow()
        await db.commit()

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        await db.refresh(db_user)
        await db.refresh(subscription)

        success_text = '✅ Страны успешно обновлены!\n\n'

        if new_countries_names:
            success_text += f'➕ Добавлены страны:\n{chr(10).join(f"• {name}" for name in new_countries_names)}\n'
            if total_price > 0:
                success_text += f'💰 Списано: {texts.format_price(total_price)}'
                if total_discount_value > 0:
                    success_text += (
                        f' (скидка {servers_discount_percent}%: -{texts.format_price(total_discount_value)})'
                    )
                success_text += '\n'

        if removed_countries_names:
            success_text += f'\n➖ Отключены страны:\n{chr(10).join(f"• {name}" for name in removed_countries_names)}\n'
            success_text += 'ℹ️ Повторное подключение будет платным\n'

        success_text += f'\n🌍 Активных стран: {len(selected_countries)}'

        await callback.message.edit_text(success_text, reply_markup=get_back_keyboard(db_user.language))

        logger.info(
            f'✅ Пользователь {db_user.telegram_id} обновил страны подписки. Добавлено: {len(new_countries)}, убрано: {len(removed_countries)}'
        )

    except Exception as e:
        logger.error(f'Ошибка обновления стран подписки: {e}')
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await state.clear()
    await callback.answer()
