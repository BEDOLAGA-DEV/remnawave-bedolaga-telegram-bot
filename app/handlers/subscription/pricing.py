import html
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.models import User
from app.utils.pricing_utils import (
    apply_percentage_discount,
    calculate_months_from_days,
    format_period_description,
    validate_pricing_calculation,
)
from app.utils.timezone import format_local_datetime

from .common import _apply_discount_to_monthly_component, _apply_promo_offer_discount, logger
from .countries import _get_available_countries, _get_countries_info, get_countries_price_by_uuids_fallback
from .devices import get_current_devices_count
from .promo import _build_promo_group_discount_text, _get_promo_offer_hint


async def _prepare_subscription_summary(
    db_user: User,
    data: dict[str, Any],
    texts,
) -> tuple[str, dict[str, Any]]:
    summary_data = dict(data)

    if 'period_days' not in summary_data:
        raise KeyError('period_days missing from subscription data — FSM state likely expired')

    countries = await _get_available_countries(db_user.promo_group_id)

    months_in_period = calculate_months_from_days(summary_data['period_days'])
    period_display = format_period_description(summary_data['period_days'], db_user.language)

    base_price_original = PERIOD_PRICES.get(summary_data['period_days'], 0)
    period_discount_percent = db_user.get_promo_discount(
        'period',
        summary_data['period_days'],
    )
    base_price, base_discount_total = apply_percentage_discount(
        base_price_original,
        period_discount_percent,
    )

    if settings.is_traffic_fixed():
        traffic_limit = settings.get_fixed_traffic_limit()
        traffic_price_per_month = settings.get_traffic_price(traffic_limit)
        final_traffic_gb = traffic_limit
    else:
        traffic_gb = summary_data.get('traffic_gb', 0)
        traffic_price_per_month = settings.get_traffic_price(traffic_gb)
        final_traffic_gb = traffic_gb

    traffic_discount_percent = db_user.get_promo_discount(
        'traffic',
        summary_data['period_days'],
    )
    traffic_component = _apply_discount_to_monthly_component(
        traffic_price_per_month,
        traffic_discount_percent,
        months_in_period,
    )
    total_traffic_price = traffic_component['total']

    countries_price_per_month = 0
    selected_countries_names: list[str] = []
    selected_server_prices: list[int] = []
    server_monthly_prices: list[int] = []

    selected_country_ids = set(summary_data.get('countries', []))
    for country in countries:
        if country['uuid'] in selected_country_ids:
            server_price_per_month = country['price_kopeks']
            countries_price_per_month += server_price_per_month
            selected_countries_names.append(html.escape(country['name']))
            server_monthly_prices.append(server_price_per_month)

    servers_discount_percent = db_user.get_promo_discount(
        'servers',
        summary_data['period_days'],
    )
    total_countries_price = 0
    total_servers_discount = 0
    discounted_servers_price_per_month = 0

    for server_price_per_month in server_monthly_prices:
        discounted_per_month, discount_per_month = apply_percentage_discount(
            server_price_per_month,
            servers_discount_percent,
        )
        total_price_for_server = discounted_per_month * months_in_period
        total_discount_for_server = discount_per_month * months_in_period

        discounted_servers_price_per_month += discounted_per_month
        total_countries_price += total_price_for_server
        total_servers_discount += total_discount_for_server
        selected_server_prices.append(total_price_for_server)

    devices_selection_enabled = settings.is_devices_selection_enabled()
    forced_disabled_limit: int | None = None
    if devices_selection_enabled:
        devices_selected = summary_data.get('devices', settings.DEFAULT_DEVICE_LIMIT)
    else:
        forced_disabled_limit = settings.get_disabled_mode_device_limit()
        if forced_disabled_limit is None:
            devices_selected = settings.DEFAULT_DEVICE_LIMIT
        else:
            devices_selected = forced_disabled_limit

    summary_data['devices'] = devices_selected
    additional_devices = max(0, devices_selected - settings.DEFAULT_DEVICE_LIMIT)
    devices_price_per_month = additional_devices * settings.PRICE_PER_DEVICE
    devices_discount_percent = db_user.get_promo_discount(
        'devices',
        summary_data['period_days'],
    )
    devices_component = _apply_discount_to_monthly_component(
        devices_price_per_month,
        devices_discount_percent,
        months_in_period,
    )
    total_devices_price = devices_component['total']

    total_price = base_price + total_traffic_price + total_countries_price + total_devices_price

    discounted_monthly_additions = (
        traffic_component['discounted_per_month']
        + discounted_servers_price_per_month
        + devices_component['discounted_per_month']
    )

    is_valid = validate_pricing_calculation(
        base_price,
        discounted_monthly_additions,
        months_in_period,
        total_price,
    )

    if not is_valid:
        raise ValueError('Subscription price calculation validation failed')

    original_total_price = total_price
    promo_offer_component = _apply_promo_offer_discount(db_user, total_price)
    if promo_offer_component['discount'] > 0:
        total_price = promo_offer_component['discounted']

    summary_data['total_price'] = total_price
    if promo_offer_component['discount'] > 0:
        summary_data['promo_offer_discount_percent'] = promo_offer_component['percent']
        summary_data['promo_offer_discount_value'] = promo_offer_component['discount']
        summary_data['total_price_before_promo_offer'] = original_total_price
    else:
        summary_data.pop('promo_offer_discount_percent', None)
        summary_data.pop('promo_offer_discount_value', None)
        summary_data.pop('total_price_before_promo_offer', None)
    summary_data['server_prices_for_period'] = selected_server_prices
    summary_data['months_in_period'] = months_in_period
    summary_data['base_price'] = base_price
    summary_data['base_price_original'] = base_price_original
    summary_data['base_discount_percent'] = period_discount_percent
    summary_data['base_discount_total'] = base_discount_total
    summary_data['final_traffic_gb'] = final_traffic_gb
    summary_data['traffic_price_per_month'] = traffic_price_per_month
    summary_data['traffic_discount_percent'] = traffic_component['discount_percent']
    summary_data['traffic_discount_total'] = traffic_component['discount_total']
    summary_data['traffic_discounted_price_per_month'] = traffic_component['discounted_per_month']
    summary_data['total_traffic_price'] = total_traffic_price
    summary_data['servers_price_per_month'] = countries_price_per_month
    summary_data['countries_price_per_month'] = countries_price_per_month
    summary_data['servers_discount_percent'] = servers_discount_percent
    summary_data['servers_discount_total'] = total_servers_discount
    summary_data['servers_discounted_price_per_month'] = discounted_servers_price_per_month
    summary_data['total_servers_price'] = total_countries_price
    summary_data['total_countries_price'] = total_countries_price
    summary_data['devices_price_per_month'] = devices_price_per_month
    summary_data['devices_discount_percent'] = devices_component['discount_percent']
    summary_data['devices_discount_total'] = devices_component['discount_total']
    summary_data['devices_discounted_price_per_month'] = devices_component['discounted_per_month']
    summary_data['total_devices_price'] = total_devices_price
    summary_data['discounted_monthly_additions'] = discounted_monthly_additions

    if settings.is_traffic_fixed():
        if final_traffic_gb == 0:
            traffic_display = 'Безлимитный'
        else:
            traffic_display = f'{final_traffic_gb} ГБ'
    elif summary_data.get('traffic_gb', 0) == 0:
        traffic_display = 'Безлимитный'
    else:
        traffic_display = f'{summary_data.get("traffic_gb", 0)} ГБ'

    details_lines = []

    # Добавляем строку базового периода только если цена не равна 0
    if base_discount_total > 0 and base_price > 0:
        base_line = (
            f'- Базовый период: <s>{texts.format_price(base_price_original)}</s> '
            f'{texts.format_price(base_price)}'
            f' (скидка {period_discount_percent}%:'
            f' -{texts.format_price(base_discount_total)})'
        )
        details_lines.append(base_line)
    elif base_price_original > 0:
        base_line = f'- Базовый период: {texts.format_price(base_price_original)}'
        details_lines.append(base_line)

    if total_traffic_price > 0:
        traffic_line = (
            f'- Трафик: {texts.format_price(traffic_price_per_month)}/мес × {months_in_period}'
            f' = {texts.format_price(total_traffic_price)}'
        )
        if traffic_component['discount_total'] > 0:
            traffic_line += (
                f' (скидка {traffic_component["discount_percent"]}%:'
                f' -{texts.format_price(traffic_component["discount_total"])})'
            )
        details_lines.append(traffic_line)
    if total_countries_price > 0:
        servers_line = (
            f'- Серверы: {texts.format_price(countries_price_per_month)}/мес × {months_in_period}'
            f' = {texts.format_price(total_countries_price)}'
        )
        if total_servers_discount > 0:
            servers_line += f' (скидка {servers_discount_percent}%: -{texts.format_price(total_servers_discount)})'
        details_lines.append(servers_line)
    if devices_selection_enabled and total_devices_price > 0:
        devices_line = (
            f'- Доп. устройства: {texts.format_price(devices_price_per_month)}/мес × {months_in_period}'
            f' = {texts.format_price(total_devices_price)}'
        )
        if devices_component['discount_total'] > 0:
            devices_line += (
                f' (скидка {devices_component["discount_percent"]}%:'
                f' -{texts.format_price(devices_component["discount_total"])})'
            )
        details_lines.append(devices_line)

    if promo_offer_component['discount'] > 0:
        details_lines.append(
            texts.t(
                'SUBSCRIPTION_SUMMARY_PROMO_DISCOUNT',
                '- Промо-предложение: -{amount} ({percent}% дополнительно)',
            ).format(
                amount=texts.format_price(promo_offer_component['discount']),
                percent=promo_offer_component['percent'],
            )
        )

    details_text = '\n'.join(details_lines)

    summary_lines = [
        '📋 <b>Сводка заказа</b>',
        '',
        f'📅 <b>Период:</b> {period_display}',
        f'📊 <b>Трафик:</b> {traffic_display}',
        f'🌍 <b>Страны:</b> {", ".join(selected_countries_names)}',
    ]

    if devices_selection_enabled:
        summary_lines.append(f'📱 <b>Устройства:</b> {devices_selected}')

    summary_lines.extend(
        [
            '',
            '💰 <b>Детализация стоимости:</b>',
            details_text,
            '',
            f'💎 <b>Общая стоимость:</b> {texts.format_price(total_price)}',
            '',
            'Подтверждаете покупку?',
        ]
    )

    summary_text = '\n'.join(summary_lines)

    return summary_text, summary_data


async def _build_subscription_period_prompt(
    db_user: User,
    texts,
    db: AsyncSession,
) -> str:
    base_text = texts.BUY_SUBSCRIPTION_START.rstrip()

    lines: list[str] = [base_text]

    promo_offer_hint = await _get_promo_offer_hint(db, db_user, texts)
    if promo_offer_hint:
        lines.extend(['', promo_offer_hint])

    promo_text = await _build_promo_group_discount_text(
        db_user,
        settings.get_available_subscription_periods(),
        texts=texts,
    )

    if promo_text:
        lines.extend(['', promo_text])

    return '\n'.join(lines) + '\n'


async def get_subscription_cost(subscription, db: AsyncSession) -> int:
    try:
        if subscription.is_trial:
            return 0

        from app.config import settings
        from app.database.crud.tariff import get_tariff_by_id
        from app.services.subscription_service import SubscriptionService

        subscription_service = SubscriptionService()

        try:
            owner = subscription.user
        except AttributeError:
            owner = None

        promo_group_id = getattr(owner, 'promo_group_id', None) if owner else None

        # В тарифном режиме цена тарифа уже включает серверы и трафик
        tariff = None
        tariff_price_found = False
        if settings.is_tariffs_mode() and subscription.tariff_id:
            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff and tariff.period_prices:
                base_cost_original = tariff.period_prices.get('30', 0) or tariff.period_prices.get(30, 0)
                if base_cost_original > 0:
                    tariff_price_found = True

        if not tariff_price_found:
            base_cost_original = PERIOD_PRICES.get(30, 0)

        if tariff_price_found:
            # Тарифный режим: серверы и трафик включены в цену.
            # Порядок: база + устройства → скидка на полную сумму (как в calculate_renewal_price).
            from app.utils.promo_offer import get_user_active_promo_discount_percent

            original_price = base_cost_original

            tariff_device_limit = tariff.device_limit if tariff.device_limit is not None else 0
            device_limit = subscription.device_limit if subscription.device_limit is not None else tariff_device_limit
            extra_devices = max(0, device_limit - tariff_device_limit)
            device_price_per_unit = (
                tariff.device_price_kopeks
                if tariff and tariff.device_price_kopeks is not None
                else settings.PRICE_PER_DEVICE
            )
            devices_price = extra_devices * device_price_per_unit
            original_price += devices_price

            # Скидка промогруппы на полную сумму (база + устройства)
            period_discount_percent = 0
            if owner:
                try:
                    period_discount_percent = owner.get_promo_discount('period', 30)
                except AttributeError:
                    pass
            discount_total = original_price * period_discount_percent // 100
            total_cost = original_price - discount_total

            # Promo-offer скидка (временная)
            promo_offer_percent = get_user_active_promo_discount_percent(owner)
            if promo_offer_percent > 0:
                promo_offer_discount = total_cost * promo_offer_percent // 100
                total_cost = total_cost - promo_offer_discount
        else:
            # Классический режим: серверы + трафик + устройства считаются отдельно
            period_discount_percent = 0
            if owner:
                try:
                    period_discount_percent = owner.get_promo_discount('period', 30)
                except AttributeError:
                    period_discount_percent = 0

            base_cost, _ = apply_percentage_discount(
                base_cost_original,
                period_discount_percent,
            )

            try:
                servers_cost, _ = await subscription_service.get_countries_price_by_uuids(
                    subscription.connected_squads,
                    db,
                    promo_group_id=promo_group_id,
                )
            except AttributeError:
                servers_cost, _ = await get_countries_price_by_uuids_fallback(
                    subscription.connected_squads,
                    db,
                    promo_group_id=promo_group_id,
                )

            traffic_cost = settings.get_traffic_price(subscription.traffic_limit_gb)
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

            devices_cost = max(0, (device_limit or 0) - settings.DEFAULT_DEVICE_LIMIT) * settings.PRICE_PER_DEVICE

            total_cost = base_cost + servers_cost + traffic_cost + devices_cost

        logger.info('Месячная стоимость подписки', subscription_id=subscription.id, total_cost_kopeks=total_cost)

        return total_cost

    except Exception as e:
        logger.error('Ошибка расчета стоимости подписки', error=e)
        return 0


async def get_subscription_info_text(subscription, texts, db_user, db: AsyncSession):
    devices_selection_enabled = settings.is_devices_selection_enabled()

    if devices_selection_enabled:
        devices_used = await get_current_devices_count(db_user)
    else:
        devices_used = 0
    countries_info = await _get_countries_info(subscription.connected_squads)
    ', '.join([c['name'] for c in countries_info]) if countries_info else 'Нет'

    subscription_url = getattr(subscription, 'subscription_url', None) or 'Генерируется...'

    if subscription.is_trial:
        status_text = '🎁 Тестовая'
        type_text = 'Триал'
    else:
        if subscription.is_active:
            status_text = '✅ Оплачена'
        else:
            status_text = '⌛ Истекла'
        type_text = 'Платная подписка'

    traffic_limit = subscription.traffic_limit_gb or 0
    if traffic_limit == 0:
        if settings.is_traffic_fixed():
            traffic_text = '∞ Безлимитный'
        else:
            traffic_text = '∞ Безлимитный'
    elif settings.is_traffic_fixed():
        traffic_text = f'{traffic_limit} ГБ'
    else:
        traffic_text = f'{traffic_limit} ГБ'

    subscription_cost = await get_subscription_cost(subscription, db)

    info_template = texts.SUBSCRIPTION_INFO

    if not devices_selection_enabled:
        info_template = info_template.replace(
            '\n📱 <b>Устройства:</b> {devices_used} / {devices_limit}',
            '',
        ).replace(
            '\n📱 <b>Devices:</b> {devices_used} / {devices_limit}',
            '',
        )

    info_text = info_template.format(
        status=status_text,
        type=type_text,
        end_date=format_local_datetime(subscription.end_date, '%d.%m.%Y %H:%M'),
        days_left=max(0, subscription.days_left),
        traffic_used=texts.format_traffic(subscription.traffic_used_gb, is_limit=False),
        traffic_limit=traffic_text,
        countries_count=len(subscription.connected_squads or []),
        devices_used=devices_used,
        devices_limit=subscription.device_limit,
        autopay_status='✅ Включен' if subscription.autopay_enabled else '⌛ Выключен',
    )

    if subscription_cost > 0:
        info_text += f'\n💰 <b>Стоимость подписки в месяц:</b> {texts.format_price(subscription_cost)}'

    # Отображаем докупленный трафик
    if (subscription.traffic_limit_gb or 0) > 0:  # Только для лимитированных тарифов
        from sqlalchemy import select as sql_select

        from app.database.models import TrafficPurchase

        now = datetime.now(UTC)
        purchases_query = (
            sql_select(TrafficPurchase)
            .where(TrafficPurchase.subscription_id == subscription.id)
            .where(TrafficPurchase.expires_at > now)
            .order_by(TrafficPurchase.expires_at.asc())
        )
        purchases_result = await db.execute(purchases_query)
        purchases = purchases_result.scalars().all()

        if purchases:
            info_text += '\n\n📦 <b>Докупленный трафик:</b>'

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

                info_text += f'\n• {purchase.traffic_gb} ГБ — {time_text}'
                info_text += f'\n  {bar} {progress_percent:.0f}% | до {expire_date}'

    if subscription_url and subscription_url != 'Генерируется...' and not settings.should_hide_subscription_link():
        info_text += f'\n\n🔗 <b>Ваша ссылка для импорта в VPN приложениe:</b>\n<code>{subscription_url}</code>'

    return info_text
