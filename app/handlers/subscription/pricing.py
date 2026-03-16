import html
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.utils.pricing_utils import (
    format_period_description,
)
from app.utils.timezone import format_local_datetime

from .common import logger
from .countries import _get_available_countries, _get_countries_info
from .devices import get_current_devices_count
from .promo import _build_promo_group_discount_text, _get_promo_offer_hint


async def _prepare_subscription_summary(
    db_user: User,
    data: dict[str, Any],
    texts,
) -> tuple[str, dict[str, Any]]:
    from app.database.database import AsyncSessionLocal
    from app.services.pricing_engine import PricingEngine, pricing_engine

    summary_data = dict(data)

    if 'period_days' not in summary_data:
        raise KeyError('period_days missing from subscription data — FSM state likely expired')

    period_days = summary_data['period_days']

    # --- Resolve device limit (same logic as before) ---
    devices_selection_enabled = settings.is_devices_selection_enabled()
    if devices_selection_enabled:
        devices_selected = summary_data.get('devices', settings.DEFAULT_DEVICE_LIMIT)
    else:
        forced_disabled_limit = settings.get_disabled_mode_device_limit()
        if forced_disabled_limit is None:
            devices_selected = settings.DEFAULT_DEVICE_LIMIT
        else:
            devices_selected = forced_disabled_limit
    summary_data['devices'] = devices_selected

    # --- Resolve traffic ---
    if settings.is_traffic_fixed():
        final_traffic_gb = settings.get_fixed_traffic_limit()
    else:
        final_traffic_gb = summary_data.get('traffic_gb', 0)

    # --- Resolve connected squads ---
    connected_squads = list(summary_data.get('countries', []))

    # --- Delegate pricing to PricingEngine ---
    async with AsyncSessionLocal() as db:
        pricing = await pricing_engine.calculate_classic_new_subscription_price(
            db,
            period_days,
            connected_squads,
            final_traffic_gb,
            devices_selected,
            user=db_user,
        )

    # --- Build legacy dict from PricingEngine result ---
    details = PricingEngine.classic_pricing_to_purchase_details(pricing)
    bd = pricing.breakdown

    months_in_period = details['months_in_period']
    base_price = details['base_price']
    base_price_original = details['base_price_original']
    base_discount_total = details['base_discount_total']
    period_discount_percent = details['base_discount_percent']
    traffic_price_per_month = details['traffic_price_per_month']
    traffic_discount_percent = details['traffic_discount_percent']
    traffic_discount_total = details['traffic_discount_total']
    total_traffic_price = details['total_traffic_price']
    servers_price_per_month = details['servers_price_per_month']
    servers_discount_percent = details['servers_discount_percent']
    servers_discount_total = details['servers_discount_total']
    total_servers_price = details['total_servers_price']
    devices_price_per_month = details['devices_price_per_month']
    devices_discount_percent = details['devices_discount_percent']
    devices_discount_total = details['devices_discount_total']
    total_devices_price = details['total_devices_price']

    # Compute discounted per-month values (not in classic_pricing_to_purchase_details)
    traffic_discounted_per_month = PricingEngine.apply_discount(traffic_price_per_month, traffic_discount_percent)
    servers_discounted_per_month = PricingEngine.apply_discount(servers_price_per_month, servers_discount_percent)
    devices_discounted_per_month = PricingEngine.apply_discount(devices_price_per_month, devices_discount_percent)
    discounted_monthly_additions = (
        traffic_discounted_per_month + servers_discounted_per_month + devices_discounted_per_month
    )

    # --- Promo offer discount (already computed by PricingEngine) ---
    promo_offer_discount = pricing.promo_offer_discount
    offer_pct = bd.get('offer_discount_pct', 0)
    # subtotal before promo offer = final_total + promo_offer_discount
    subtotal_before_offer = pricing.final_total + promo_offer_discount
    total_price = pricing.final_total

    summary_data['total_price'] = total_price
    if promo_offer_discount > 0:
        summary_data['promo_offer_discount_percent'] = offer_pct
        summary_data['promo_offer_discount_value'] = promo_offer_discount
        summary_data['total_price_before_promo_offer'] = subtotal_before_offer
    else:
        summary_data.pop('promo_offer_discount_percent', None)
        summary_data.pop('promo_offer_discount_value', None)
        summary_data.pop('total_price_before_promo_offer', None)
    summary_data['server_prices_for_period'] = details['servers_individual_prices']
    summary_data['months_in_period'] = months_in_period
    summary_data['base_price'] = base_price
    summary_data['base_price_original'] = base_price_original
    summary_data['base_discount_percent'] = period_discount_percent
    summary_data['base_discount_total'] = base_discount_total
    summary_data['final_traffic_gb'] = final_traffic_gb
    summary_data['traffic_price_per_month'] = traffic_price_per_month
    summary_data['traffic_discount_percent'] = traffic_discount_percent
    summary_data['traffic_discount_total'] = traffic_discount_total
    summary_data['traffic_discounted_price_per_month'] = traffic_discounted_per_month
    summary_data['total_traffic_price'] = total_traffic_price
    summary_data['servers_price_per_month'] = servers_price_per_month
    summary_data['countries_price_per_month'] = servers_price_per_month
    summary_data['servers_discount_percent'] = servers_discount_percent
    summary_data['servers_discount_total'] = servers_discount_total
    summary_data['servers_discounted_price_per_month'] = servers_discounted_per_month
    summary_data['total_servers_price'] = total_servers_price
    summary_data['total_countries_price'] = total_servers_price
    summary_data['devices_price_per_month'] = devices_price_per_month
    summary_data['devices_discount_percent'] = devices_discount_percent
    summary_data['devices_discount_total'] = devices_discount_total
    summary_data['devices_discounted_price_per_month'] = devices_discounted_per_month
    summary_data['total_devices_price'] = total_devices_price
    summary_data['discounted_monthly_additions'] = discounted_monthly_additions

    # --- Build display text ---
    period_display = format_period_description(period_days, db_user.language)

    if settings.is_traffic_fixed():
        if final_traffic_gb == 0:
            traffic_display = 'Безлимитный'
        else:
            traffic_display = f'{final_traffic_gb} ГБ'
    elif summary_data.get('traffic_gb', 0) == 0:
        traffic_display = 'Безлимитный'
    else:
        traffic_display = f'{summary_data.get("traffic_gb", 0)} ГБ'

    # Resolve country display names (still needed for the summary text)
    countries = await _get_available_countries(db_user.promo_group_id)
    selected_country_ids = set(connected_squads)
    selected_countries_names: list[str] = [
        html.escape(country['name']) for country in countries if country['uuid'] in selected_country_ids
    ]

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
        if traffic_discount_total > 0:
            traffic_line += f' (скидка {traffic_discount_percent}%: -{texts.format_price(traffic_discount_total)})'
        details_lines.append(traffic_line)
    if total_servers_price > 0:
        servers_line = (
            f'- Серверы: {texts.format_price(servers_price_per_month)}/мес × {months_in_period}'
            f' = {texts.format_price(total_servers_price)}'
        )
        if servers_discount_total > 0:
            servers_line += f' (скидка {servers_discount_percent}%: -{texts.format_price(servers_discount_total)})'
        details_lines.append(servers_line)
    if devices_selection_enabled and total_devices_price > 0:
        devices_line = (
            f'- Доп. устройства: {texts.format_price(devices_price_per_month)}/мес × {months_in_period}'
            f' = {texts.format_price(total_devices_price)}'
        )
        if devices_discount_total > 0:
            devices_line += f' (скидка {devices_discount_percent}%: -{texts.format_price(devices_discount_total)})'
        details_lines.append(devices_line)

    if promo_offer_discount > 0:
        details_lines.append(
            texts.t(
                'SUBSCRIPTION_SUMMARY_PROMO_DISCOUNT',
                '- Промо-предложение: -{amount} ({percent}% дополнительно)',
            ).format(
                amount=texts.format_price(promo_offer_discount),
                percent=offer_pct,
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

        from app.services.pricing_engine import pricing_engine

        try:
            owner = subscription.user
        except AttributeError:
            owner = None

        result = await pricing_engine.calculate_renewal_price(db, subscription, 30, user=owner)
        total_cost = result.final_total

        logger.info('Monthly subscription cost', subscription_id=subscription.id, total_cost_kopeks=total_cost)
        return total_cost

    except Exception as e:
        logger.error('Error calculating subscription cost', error=e)
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
