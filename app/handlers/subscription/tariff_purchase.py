"""Покупка подписки по тарифам."""

import html
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    create_paid_subscription,
    extend_subscription,
    get_active_subscriptions_by_user_id,
    get_subscription_by_id_for_user,
    get_subscription_by_user_id,
)
from app.database.crud.tariff import get_tariff_by_id, get_tariffs_for_user
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.database import AsyncSessionLocal
from app.database.models import Tariff, Transaction, TransactionType, User
from app.localization.texts import Texts, get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.subscription_service import SubscriptionService
from app.services.tariff_switch_policy import remaining_days_for_switch, should_reset_used_traffic
from app.services.user_cart_service import user_cart_service
from app.utils.decorators import error_handler
from app.utils.formatting import format_period, format_price_kopeks, format_traffic
from app.utils.promo_offer import get_user_active_promo_discount_percent


logger = structlog.get_logger(__name__)


async def _persist_failed_refund(
    user_id: int,
    amount_kopeks: int,
    reason: str,
    error: Exception,
) -> None:
    """Persist a failed refund record via a fresh DB session so it can be retried later.

    Uses AsyncSessionLocal directly because the caller's session may be in a broken state
    (e.g. after a rolled-back transaction or connection error).
    """
    try:
        async with AsyncSessionLocal() as session:
            record = Transaction(
                user_id=user_id,
                type=TransactionType.FAILED_REFUND.value,
                amount_kopeks=amount_kopeks,
                description=f'{reason} | error: {error}',
                is_completed=False,
                created_at=datetime.now(UTC),
            )
            session.add(record)
            await session.commit()
            logger.warning(
                'Записан failed_refund для последующей обработки',
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                transaction_id=record.id,
            )
    except Exception as persist_error:
        # Last resort: if even persisting the record fails, log everything needed for manual recovery
        logger.critical(
            'НЕВОЗМОЖНО сохранить failed_refund — требуется ручное вмешательство',
            user_id=user_id,
            amount_kopeks=amount_kopeks,
            reason=reason,
            original_error=str(error),
            persist_error=persist_error,
        )


async def _resolve_subscription(callback, db_user, db, state=None):
    """Resolve subscription — delegates to shared resolve_subscription_from_context."""
    from .common import resolve_subscription_from_context

    return await resolve_subscription_from_context(callback, db_user, db, state)


async def _resolve_switch_subscription(callback, db_user, db, state=None):
    """Resolve the subscription for tariff-switch flows (issue #3012).

    Unlike the generic resolver, this NEVER reads the trailing callback segment as
    a subscription_id — switch callbacks end in tariff_id/period, which the generic
    resolver would mistake for a sub_id (and renew/switch the WRONG subscription
    when that number equals one of the user's subscription ids). The switch entry
    (show_tariff_switch_list / show_instant_switch_list) stores the chosen
    subscription in FSM ``active_subscription_id``, so it is authoritative here.
    """
    from app.database.crud.subscription import (
        get_active_subscriptions_by_user_id,
        get_subscription_by_id_for_user,
        get_subscription_by_user_id,
    )

    if not settings.is_multi_tariff_enabled():
        sub = await get_subscription_by_user_id(db, db_user.id)
        return sub, (sub.id if sub else None)

    if state:
        try:
            data = await state.get_data()
            fsm_sub_id = data.get('active_subscription_id')
            if fsm_sub_id:
                sub = await get_subscription_by_id_for_user(db, fsm_sub_id, db_user.id)
                if sub:
                    return sub, fsm_sub_id
        except Exception:
            pass

    active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
    if len(active_subs) == 1:
        return active_subs[0], active_subs[0].id

    await callback.answer(
        get_texts(db_user.language).t('TARIFF_PURCHASE_SELECT_SUBSCRIPTION', 'Выберите подписку'), show_alert=True
    )
    return None, None


def _apply_promo_discount(price: int, group_pct: int, offer_pct: int = 0) -> int:
    """Применяет стекинг скидок к цене (sequential floor division, как PricingEngine)."""
    from app.services.pricing_engine import PricingEngine

    final, _, _ = PricingEngine.apply_stacked_discounts(price, group_pct, offer_pct)
    return final


def _get_user_period_discount(db_user: User, period_days: int) -> tuple[int, int, int]:
    """Получает скидку пользователя на период из промогруппы + промо-оффер.

    Returns:
        (group_pct, offer_pct, display_combined_pct) — отдельные проценты для
        корректного расчёта цены и комбинированный процент для отображения в UI.
    """
    promo_group = db_user.get_primary_promo_group()
    group_discount = promo_group.get_discount_percent('period', period_days) if promo_group else 0
    personal_discount = get_user_active_promo_discount_percent(db_user)

    if group_discount <= 0 and personal_discount <= 0:
        return 0, 0, 0

    # Комбинированный процент для отображения
    remaining = (100 - group_discount) * (100 - personal_discount)
    display_combined = 100 - remaining // 100

    return group_discount, personal_discount, display_combined


def format_tariffs_list_text(
    tariffs: list[Tariff],
    db_user: User | None = None,
    has_period_discounts: bool = False,
    purchased_tariff_ids: set[int] | None = None,
) -> str:
    """Форматирует текст со списком тарифов для отображения."""
    texts = get_texts(db_user.language if db_user else 'ru')
    lines = [texts.t('TARIFF_PURCHASE_LIST_TITLE', '📦 <b>Выберите тариф</b>')]
    if purchased_tariff_ids is None:
        purchased_tariff_ids = set()

    if has_period_discounts:
        lines.append(texts.t('TARIFF_PURCHASE_PERIOD_DISCOUNTS_HINT', '🎁 <i>Скидки по периодам</i>'))

    lines.append('')

    for tariff in tariffs:
        # Трафик компактно
        traffic_gb = tariff.traffic_limit_gb
        traffic = (
            '∞' if traffic_gb == 0 else texts.t('TARIFF_PURCHASE_TRAFFIC_GB', '{traffic} ГБ').format(traffic=traffic_gb)
        )

        # Цена
        is_daily = getattr(tariff, 'is_daily', False)
        price_text = ''
        discount_icon = ''

        if is_daily:
            # Для суточных тарифов показываем цену за день с учётом скидки промогруппы
            daily_price = getattr(tariff, 'daily_price_kopeks', 0)
            if db_user:
                group_pct, offer_pct, daily_discount = _get_user_period_discount(db_user, 1)
                if daily_discount > 0:
                    daily_price = _apply_promo_discount(daily_price, group_pct, offer_pct)
                    discount_icon = '🔥'
            price_text = texts.t('TARIFF_PURCHASE_PRICE_PER_DAY', '🔄 {price}/день{icon}').format(
                price=format_price_kopeks(daily_price, compact=True), icon=discount_icon
            )
        else:
            # Для периодных тарифов показываем минимальную цену
            prices = tariff.period_prices or {}
            if prices:
                min_period = min(prices.keys(), key=int)
                min_price = prices[min_period]
                group_pct, offer_pct, discount_percent = 0, 0, 0
                if db_user:
                    group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, int(min_period))
                if discount_percent > 0:
                    min_price = _apply_promo_discount(min_price, group_pct, offer_pct)
                    discount_icon = '🔥'
                price_text = texts.t('TARIFF_PURCHASE_PRICE_FROM', 'от {price}{icon}').format(
                    price=format_price_kopeks(min_price, compact=True), icon=discount_icon
                )

        # Компактный формат: Название — 250 ГБ / 10 📱 от 179₽🔥
        purchased_mark = ' ✅' if tariff.id in purchased_tariff_ids else ''
        lines.append(
            f'<b>{html.escape(tariff.name)}</b>{purchased_mark} — {traffic} / {tariff.device_limit} 📱 {price_text}'
        )

        # Описание тарифа если есть
        if tariff.description:
            lines.append(f'<i>{html.escape(tariff.description)}</i>')

        lines.append('')

    return '\n'.join(lines)


def get_tariffs_keyboard(
    tariffs: list[Tariff],
    language: str,
    purchased_tariff_ids: set[int] | None = None,
) -> InlineKeyboardMarkup:
    """Создает компактную клавиатуру выбора тарифов (только названия)."""
    texts = get_texts(language)
    if purchased_tariff_ids is None:
        purchased_tariff_ids = set()
    buttons = []

    badge = texts.t('TARIFF_BEST_VALUE_BADGE', '⭐ выгодно')
    for tariff in tariffs:
        if tariff.id in purchased_tariff_ids:
            # Уже купленный тариф важнее подсказки: галочка отвечает на вопрос
            # «а этот у меня есть?», отметка выгоды тут уже ничего не решает.
            title = f'✅ {tariff.name}'
        elif getattr(tariff, 'is_highlighted', False):
            title = f'{badge} · {tariff.name}'
        else:
            title = tariff.name
        buttons.append([InlineKeyboardButton(text=title, callback_data=f'tariff_select:{tariff.id}')])

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _period_button_text(tariff: Tariff, period: int, price_text: str, texts) -> str:
    """Единый текст кнопки периода: подпись, цена и отметка выгодного периода.

    Собирался в четырёх клавиатурах подряд одинаковой строкой — отметку пришлось
    бы добавлять четыре раза и держать одинаковой на глаз.

    В инлайн-кнопку рамку не нарисовать, поэтому выделение — подпись в начале
    текста: она видна до цены и не теряется среди остальных строк.
    """
    label = f'{format_period(period)} — {price_text}'
    if tariff.highlight_period_days is not None and int(tariff.highlight_period_days) == period:
        return f'{texts.t("TARIFF_PERIOD_BEST_VALUE_BADGE", "⭐ выгодно")} · {label}'
    return label


def get_tariff_periods_keyboard(
    tariff: Tariff,
    language: str,
    db_user: User | None = None,
    back_callback: str = 'menu_buy',
) -> InlineKeyboardMarkup:
    """Создает клавиатуру выбора периода для тарифа с учетом скидок по периодам."""
    texts = get_texts(language)
    buttons = []

    prices = tariff.period_prices or {}
    for period_str in sorted(prices.keys(), key=int):
        period = int(period_str)
        price = prices[period_str]

        # Получаем скидку для конкретного периода
        group_pct, offer_pct, discount_percent = 0, 0, 0
        if db_user:
            group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, period)

        if discount_percent > 0:
            price = _apply_promo_discount(price, group_pct, offer_pct)
            price_text = f'{format_price_kopeks(price)} 🔥−{discount_percent}%'
        else:
            price_text = format_price_kopeks(price)

        button_text = _period_button_text(tariff, period, price_text, texts)
        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f'tariff_period:{tariff.id}:{period}')])

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_tariff_periods_keyboard_with_traffic(
    tariff: Tariff,
    language: str,
    db_user: User | None = None,
    back_callback: str = 'menu_buy',
) -> InlineKeyboardMarkup:
    """Клавиатура выбора периода для тарифа с кастомным трафиком (переход к настройке трафика)."""
    texts = get_texts(language)
    buttons = []

    prices = tariff.period_prices or {}
    for period_str in sorted(prices.keys(), key=int):
        period = int(period_str)
        price = prices[period_str]

        # Получаем скидку для конкретного периода
        group_pct, offer_pct, discount_percent = 0, 0, 0
        if db_user:
            group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, period)

        if discount_percent > 0:
            price = _apply_promo_discount(price, group_pct, offer_pct)
            price_text = f'{format_price_kopeks(price)} 🔥−{discount_percent}%'
        else:
            price_text = format_price_kopeks(price)

        button_text = _period_button_text(tariff, period, price_text, texts)
        # Используем другой callback для перехода к настройке трафика
        buttons.append(
            [InlineKeyboardButton(text=button_text, callback_data=f'tariff_period_traffic:{tariff.id}:{period}')]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_tariff_confirm_keyboard(
    tariff_id: int,
    period: int,
    language: str,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру подтверждения покупки тарифа."""
    texts = get_texts(language)
    buttons = [
        [
            InlineKeyboardButton(
                text=texts.t('TARIFF_PURCHASE_CONFIRM_BUTTON', '✅ Подтвердить покупку'),
                callback_data=f'tariff_confirm:{tariff_id}:{period}',
            )
        ],
    ]
    # Альтернатива оплате с баланса: оформление через СБП-автопродление
    # Platega (первое списание = подтверждение привязки в банке, дальше —
    # автосписания по каденсу тарифа).
    if settings.is_platega_recurrent_enabled():
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('SBP_PURCHASE_BUTTON', '⚡ Оформить с автооплатой СБП'),
                    callback_data=f'tariff_sbp:{tariff_id}',
                )
            ]
        )
    if settings.is_lava_recurrent_enabled():
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('LAVA_PURCHASE_BUTTON', '⚡ Оформить с автооплатой Lava'),
                    callback_data=f'tariff_lava:{tariff_id}',
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'tariff_select:{tariff_id}')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_tariff_insufficient_balance_keyboard(
    tariff_id: int,
    period: int,
    language: str,
    missing_kopeks: int = 0,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру при недостаточном балансе.

    Если включена автопокупка после пополнения (AUTO_PURCHASE_AFTER_TOPUP_ENABLED),
    показываем способы оплаты сразу, предзаполненные ровно недостающей суммой: после
    оплаты подписка оформится автоматически, без отдельного шага «Пополнить баланс».
    Иначе оставляем классический переход в пополнение баланса.

    СБП-оформление показываем и здесь: привязке баланс не нужен (первое списание
    Platega = подтверждение в банке), а без кнопки на этом экране фича была
    недостижима для пользователей без денег на балансе.
    """
    texts = get_texts(language)
    back_button = InlineKeyboardButton(text=texts.BACK, callback_data=f'tariff_select:{tariff_id}')
    sbp_rows = _sbp_purchase_rows(tariff_id, texts)

    if settings.is_auto_purchase_after_topup_enabled() and missing_kopeks > 0:
        from app.keyboards.inline import get_payment_methods_keyboard

        # Оставляем только кнопки прямой оплаты (topup_amount|метод|сумма), отбрасывая
        # навигацию клавиатуры пополнения — возврат ведём к выбору тарифа.
        payment_rows = [
            row
            for row in get_payment_methods_keyboard(missing_kopeks, language).inline_keyboard
            if row and all((button.callback_data or '').startswith('topup_amount|') for button in row)
        ]
        if payment_rows:
            return InlineKeyboardMarkup(inline_keyboard=[*payment_rows, *sbp_rows, [back_button]])

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'), callback_data='balance_topup')],
            *sbp_rows,
            [back_button],
        ]
    )


def get_tariff_extend_insufficient_balance_keyboard(
    tariff_id: int,
    subscription_id: int | None,
    period: int,
    language: str,
    missing_kopeks: int = 0,
) -> InlineKeyboardMarkup:
    """Клавиатура «Недостаточно средств» при продлении тарифа.

    Зеркало ``get_tariff_insufficient_balance_keyboard`` для buy-flow: при
    ``AUTO_PURCHASE_AFTER_TOPUP_ENABLED`` встраивает кнопки прямой оплаты на
    недостающую сумму (``topup_amount|метод|сумма``), иначе — классический
    переход ``balance_topup``. «Назад» всегда ``subscription_extend`` (не
    ``tariff_select``).

    ``tariff_id`` / ``period`` — контекст вызывающего (симметрия с
    insufficient-веткой). ``subscription_id`` определяет адрес «Назад»: в
    мультитарифе — ``se:{id}``, потому что голый ``subscription_extend`` на
    экране возврата после пополнения упирается в «Выберите подписку» и ничего
    не перерисовывает (FSM к тому моменту мог быть очищен, а активная подписка
    там не закрепляется). Идиома та же, что в monitoring_service и
    recurrent_payment_service.

    SBP/Lava-строки (``tariff_sbp`` / ``tariff_lava``) не добавляем: их
    обработчики оформляют новую подписку с привязкой провайдера, а не продление
    существующей — отдельных extend-callback'ов нет.
    """
    texts = get_texts(language)
    back_callback = (
        f'se:{subscription_id}' if settings.is_multi_tariff_enabled() and subscription_id else 'subscription_extend'
    )
    back_button = InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)

    if settings.is_auto_purchase_after_topup_enabled() and missing_kopeks > 0:
        from app.keyboards.inline import get_payment_methods_keyboard

        payment_rows = [
            row
            for row in get_payment_methods_keyboard(missing_kopeks, language).inline_keyboard
            if row and all((button.callback_data or '').startswith('topup_amount|') for button in row)
        ]
        if payment_rows:
            return InlineKeyboardMarkup(inline_keyboard=[*payment_rows, [back_button]])

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'), callback_data='balance_topup')],
            [back_button],
        ]
    )


def _sbp_purchase_rows(tariff_id: int, texts) -> list[list[InlineKeyboardButton]]:
    """Ряды оформления привязкой провайдера — те же, что на confirm-экранах."""
    rows: list[list[InlineKeyboardButton]] = []
    if settings.is_platega_recurrent_enabled():
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('SBP_PURCHASE_BUTTON', '⚡ Оформить с автооплатой СБП'),
                    callback_data=f'tariff_sbp:{tariff_id}',
                )
            ]
        )
    if settings.is_lava_recurrent_enabled():
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('LAVA_PURCHASE_BUTTON', '⚡ Оформить с автооплатой Lava'),
                    callback_data=f'tariff_lava:{tariff_id}',
                )
            ]
        )
    return rows


def format_tariff_info_for_user(
    tariff: Tariff,
    language: str,
    discount_percent: int = 0,
) -> str:
    """Форматирует информацию о тарифе для пользователя."""
    texts = get_texts(language)

    traffic = format_traffic(tariff.traffic_limit_gb)

    text = texts.t(
        'TARIFF_PURCHASE_INFO',
        '📦 <b>{name}</b>\n\n<b>Параметры:</b>\n• Трафик: {traffic}\n• Устройств: {devices}\n',
    ).format(name=html.escape(tariff.name), traffic=traffic, devices=tariff.device_limit)

    if tariff.description:
        text += f'\n📝 {html.escape(tariff.description)}\n'

    if discount_percent > 0:
        text += texts.t('TARIFF_PURCHASE_YOUR_DISCOUNT', '\n🎁 <b>Ваша скидка: {percent}%</b>\n').format(
            percent=discount_percent
        )

    # Для суточных тарифов не показываем выбор периода
    is_daily = getattr(tariff, 'is_daily', False)
    if not is_daily:
        text += texts.t('TARIFF_PURCHASE_CHOOSE_PERIOD', '\nВыберите период подписки:')

    return text


def get_daily_tariff_confirm_keyboard(
    tariff_id: int,
    language: str,
    back_callback: str = 'menu_buy',
) -> InlineKeyboardMarkup:
    """Создает клавиатуру подтверждения покупки суточного тарифа."""
    texts = get_texts(language)
    buttons = [
        [
            InlineKeyboardButton(
                text=texts.t('TARIFF_PURCHASE_CONFIRM_BUTTON', '✅ Подтвердить покупку'),
                callback_data=f'daily_tariff_confirm:{tariff_id}',
            )
        ],
    ]
    if settings.is_platega_recurrent_enabled():
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('SBP_PURCHASE_BUTTON', '⚡ Оформить с автооплатой СБП'),
                    callback_data=f'tariff_sbp:{tariff_id}',
                )
            ]
        )
    if settings.is_lava_recurrent_enabled():
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('LAVA_PURCHASE_BUTTON', '⚡ Оформить с автооплатой Lava'),
                    callback_data=f'tariff_lava:{tariff_id}',
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_daily_tariff_insufficient_balance_keyboard(
    tariff_id: int,
    language: str,
    back_callback: str = 'menu_buy',
) -> InlineKeyboardMarkup:
    """Создает клавиатуру при недостаточном балансе для суточного тарифа."""
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'), callback_data='balance_topup')],
            *_sbp_purchase_rows(tariff_id, texts),
            [InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)],
        ]
    )


# ==================== Кастомные дни/трафик ====================


def get_custom_tariff_keyboard(
    tariff_id: int,
    language: str,
    days: int,
    traffic_gb: int,
    can_custom_days: bool,
    can_custom_traffic: bool,
    min_days: int = 1,
    max_days: int = 365,
    min_traffic: int = 1,
    max_traffic: int = 1000,
    back_callback: str = 'menu_buy',
) -> InlineKeyboardMarkup:
    """Создает клавиатуру для настройки кастомных дней и трафика."""
    texts = get_texts(language)
    buttons = []

    # Кнопки изменения дней
    if can_custom_days:
        days_row = []
        # -30 / -7 / -1
        if days > min_days:
            if days - 30 >= min_days:
                days_row.append(InlineKeyboardButton(text='-30', callback_data=f'custom_days:{tariff_id}:-30'))
            if days - 7 >= min_days:
                days_row.append(InlineKeyboardButton(text='-7', callback_data=f'custom_days:{tariff_id}:-7'))
            days_row.append(InlineKeyboardButton(text='-1', callback_data=f'custom_days:{tariff_id}:-1'))

        # Текущее значение
        days_row.append(
            InlineKeyboardButton(
                text=texts.t('TARIFF_PURCHASE_DAYS_BUTTON', '📅 {days} дн.').format(days=days), callback_data='noop'
            )
        )

        # +1 / +7 / +30
        if days < max_days:
            days_row.append(InlineKeyboardButton(text='+1', callback_data=f'custom_days:{tariff_id}:1'))
            if days + 7 <= max_days:
                days_row.append(InlineKeyboardButton(text='+7', callback_data=f'custom_days:{tariff_id}:7'))
            if days + 30 <= max_days:
                days_row.append(InlineKeyboardButton(text='+30', callback_data=f'custom_days:{tariff_id}:30'))

        if days_row:
            buttons.append(days_row)

    # Кнопки изменения трафика
    if can_custom_traffic:
        traffic_row = []
        # -100 / -10 / -1
        if traffic_gb > min_traffic:
            if traffic_gb - 100 >= min_traffic:
                traffic_row.append(InlineKeyboardButton(text='-100', callback_data=f'custom_traffic:{tariff_id}:-100'))
            if traffic_gb - 10 >= min_traffic:
                traffic_row.append(InlineKeyboardButton(text='-10', callback_data=f'custom_traffic:{tariff_id}:-10'))
            traffic_row.append(InlineKeyboardButton(text='-1', callback_data=f'custom_traffic:{tariff_id}:-1'))

        # Текущее значение
        traffic_row.append(
            InlineKeyboardButton(
                text=texts.t('TARIFF_PURCHASE_TRAFFIC_BUTTON', '📊 {traffic} ГБ').format(traffic=traffic_gb),
                callback_data='noop',
            )
        )

        # +1 / +10 / +100
        if traffic_gb < max_traffic:
            traffic_row.append(InlineKeyboardButton(text='+1', callback_data=f'custom_traffic:{tariff_id}:1'))
            if traffic_gb + 10 <= max_traffic:
                traffic_row.append(InlineKeyboardButton(text='+10', callback_data=f'custom_traffic:{tariff_id}:10'))
            if traffic_gb + 100 <= max_traffic:
                traffic_row.append(InlineKeyboardButton(text='+100', callback_data=f'custom_traffic:{tariff_id}:100'))

        if traffic_row:
            buttons.append(traffic_row)

    # Кнопка подтверждения
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('TARIFF_PURCHASE_CONFIRM_BUTTON', '✅ Подтвердить покупку'),
                callback_data=f'custom_confirm:{tariff_id}',
            )
        ]
    )

    # Кнопка назад
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _calculate_custom_tariff_price(
    tariff: Tariff,
    days: int,
    traffic_gb: int,
) -> tuple[int, int, int]:
    """
    Рассчитывает цену для кастомного тарифа.

    Логика (как в веб-кабинете):
    1. Цена периода: из period_prices ИЛИ price_per_day * дни (если custom_days)
    2. Трафик: добавляется СВЕРХУ к цене периода (если custom_traffic)

    Returns:
        tuple: (period_price, traffic_price, total_price)
    """
    period_price = 0
    traffic_price = 0

    # Цена за период
    if tariff.can_purchase_custom_days():
        # Кастомные дни - используем price_per_day
        period_price = tariff.get_price_for_custom_days(days) or 0
    else:
        # Фиксированные периоды - берём из period_prices
        period_price = tariff.get_price_for_period(days) or 0

    # Цена за трафик (добавляется сверху)
    if tariff.can_purchase_custom_traffic():
        traffic_price = tariff.get_price_for_custom_traffic(traffic_gb) or 0

    total_price = period_price + traffic_price
    return period_price, traffic_price, total_price


async def format_custom_tariff_preview(
    tariff: Tariff,
    days: int,
    traffic_gb: int,
    user_balance: int,
    db_user: User | None = None,
    discount_percent: int = 0,
    group_pct: int = 0,
    offer_pct: int = 0,
) -> str:
    """Форматирует предпросмотр покупки с кастомными параметрами.

    Uses PricingEngine when db_user is provided for accurate per-category discounts
    (period, traffic addon). Falls back to manual calculation otherwise.
    """
    if db_user is not None:
        # Use PricingEngine — single source of truth for all discounts
        from app.services.pricing_engine import pricing_engine

        result = await pricing_engine.calculate_tariff_purchase_price(
            tariff,
            days,
            device_limit=tariff.device_limit,
            custom_traffic_gb=traffic_gb if tariff.can_purchase_custom_traffic() else None,
            user=db_user,
        )
        period_price = result.base_price
        traffic_price = result.traffic_price
        total_price = result.final_total
        has_discount = result.promo_group_discount > 0 or result.promo_offer_discount > 0
    else:
        # Fallback: raw prices without discounts
        period_price, traffic_price, total_price = _calculate_custom_tariff_price(tariff, days, traffic_gb)
        has_discount = discount_percent > 0
        if has_discount:
            total_price = _apply_promo_discount(total_price, group_pct, offer_pct)

    texts = get_texts(db_user.language if db_user else 'ru')
    traffic_display = (
        texts.t('TARIFF_PURCHASE_TRAFFIC_GB', '{traffic} ГБ').format(traffic=traffic_gb)
        if traffic_gb > 0
        else format_traffic(tariff.traffic_limit_gb)
    )

    text = texts.t('TARIFF_PURCHASE_CUSTOM_TITLE', '📦 <b>{name}</b>\n\n<b>Настройте параметры:</b>\n').format(
        name=html.escape(tariff.name)
    )

    if tariff.can_purchase_custom_days():
        text += texts.t('TARIFF_PURCHASE_CUSTOM_DAYS_LINE', '📅 Дней: <b>{days}</b> (от {min} до {max})\n').format(
            days=days, min=tariff.min_days, max=tariff.max_days
        )
        text += f'   💰 {format_price_kopeks(period_price)}\n'
    else:
        # Фиксированный период - показываем без возможности изменения
        text += texts.t('TARIFF_PURCHASE_CUSTOM_PERIOD_LINE', '📅 Период: <b>{period}</b>\n').format(
            period=format_period(days)
        )
        text += f'   💰 {format_price_kopeks(period_price)}\n'

    if tariff.can_purchase_custom_traffic():
        text += texts.t(
            'TARIFF_PURCHASE_CUSTOM_TRAFFIC_LINE', '📊 Трафик: <b>{traffic} ГБ</b> (от {min} до {max})\n'
        ).format(traffic=traffic_gb, min=tariff.min_traffic_gb, max=tariff.max_traffic_gb)
        text += f'   💰 +{format_price_kopeks(traffic_price)}\n'
    else:
        text += texts.t('TARIFF_PURCHASE_TRAFFIC_LINE', '📊 Трафик: {traffic}\n').format(traffic=traffic_display)

    text += texts.t('TARIFF_PURCHASE_DEVICES_LINE', '📱 Устройств: {devices}\n').format(
        devices=Texts.format_device_limit(tariff.device_limit)
    )

    if has_discount:
        text += texts.t('TARIFF_PURCHASE_DISCOUNT_LINE', '\n🎁 <b>Скидка: {percent}%</b>\n').format(
            percent=discount_percent
        )

    text += texts.t(
        'TARIFF_PURCHASE_TOTAL_AND_BALANCE',
        '\n<b>💰 Итого: {total}</b>\n\n💳 Ваш баланс: {balance}',
    ).format(total=format_price_kopeks(total_price), balance=format_price_kopeks(user_balance))

    if user_balance < total_price:
        missing = total_price - user_balance
        text += texts.t('TARIFF_PURCHASE_MISSING_LINE', '\n⚠️ <b>Не хватает: {amount}</b>').format(
            amount=format_price_kopeks(missing)
        )
    else:
        text += texts.t('TARIFF_PURCHASE_AFTER_PAYMENT_LINE', '\nПосле оплаты: {amount}').format(
            amount=format_price_kopeks(user_balance - total_price)
        )

    return text


@error_handler
async def show_tariffs_list(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Показывает список тарифов для покупки."""
    texts = get_texts(db_user.language)
    await state.clear()

    # Получаем доступные тарифы
    promo_group_id = getattr(db_user, 'promo_group_id', None)
    tariffs = await get_tariffs_for_user(db, promo_group_id)

    if not tariffs:
        await callback.message.edit_text(
            texts.t(
                'TARIFF_PURCHASE_NO_TARIFFS',
                '😔 <b>Нет доступных тарифов</b>\n\nК сожалению, сейчас нет тарифов для покупки.',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')]]
            ),
        )
        await callback.answer()
        return

    # Один доступный тариф — сразу к периоду/подтверждению, без экрана «выбора из одной кнопки».
    # Кроме случая, когда покупать нечего: в мультитарифе `_proceed_with_selected_tariff`
    # на уже активном тарифе только показывает всплывающее уведомление и выходит,
    # ничего не перерисовывая, — кнопка «Купить подписку» выглядела бы мёртвой.
    # Тогда показываем список: там тариф помечен галочкой и понятно, почему.
    if len(tariffs) == 1:
        _already_owned = False
        if settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_active_subscriptions_by_user_id

            _active = await get_active_subscriptions_by_user_id(db, db_user.id)
            _already_owned = any(s.tariff_id == tariffs[0].id and not s.is_trial for s in _active)
        if not _already_owned:
            await _proceed_with_selected_tariff(callback, db_user, db, state, tariffs[0].id, skip_selection=True)
            return

    # В мульти-тарифе определяем какие тарифы уже куплены
    purchased_tariff_ids: set[int] = set()
    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
        purchased_tariff_ids = {s.tariff_id for s in active_subs if s.tariff_id and not s.is_trial}

    # Проверяем есть ли у пользователя скидки по периодам
    promo_group = db_user.get_primary_promo_group() if hasattr(db_user, 'get_primary_promo_group') else None
    if promo_group is None:
        promo_group = getattr(db_user, 'promo_group', None)
    has_period_discounts = False
    if promo_group:
        period_discounts = getattr(promo_group, 'period_discounts', None)
        if period_discounts and isinstance(period_discounts, dict) and len(period_discounts) > 0:
            has_period_discounts = True

    # Формируем текст со списком тарифов и их характеристиками
    tariffs_text = format_tariffs_list_text(tariffs, db_user, has_period_discounts, purchased_tariff_ids)

    await callback.message.edit_text(
        tariffs_text,
        reply_markup=get_tariffs_keyboard(tariffs, db_user.language, purchased_tariff_ids),
    )

    await callback.answer()


async def _tariff_back_callback(state: FSMContext) -> str:
    """Куда ведёт «Назад» на экранах покупки тарифа.

    Экран списка мог быть пропущен (единственный доступный тариф) — тогда
    возвращать надо в главное меню. Признак живёт в состоянии, потому что
    перерисовки (изменение дней, трафика, выбор периода) происходят в других
    хендлерах, у которых этого контекста нет. Без него «Назад» после первого же
    +/- деградировал до `menu_buy`, тот снова попадал на пропуск и возвращал на
    тот же экран, попутно сбрасывая уже выбранные дни.
    """
    data = await state.get_data()
    return 'back_to_menu' if data.get('tariff_selection_skipped') else 'menu_buy'


async def _proceed_with_selected_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    tariff_id: int,
    *,
    skip_selection: bool = False,
) -> None:
    """Переход к периоду/подтверждению выбранного тарифа (первая покупка).

    Вызывается и из ``select_tariff`` (кнопка в списке), и из ``show_tariffs_list``
    при единственном доступном тарифе — без дублирования логики.

    ``skip_selection=True``: экран списка тарифов был пропущен, поэтому «Назад»
    ведёт в главное меню (``back_to_menu``), а не в ``menu_buy`` / список.
    """
    texts = get_texts(db_user.language)
    back_callback = 'back_to_menu' if skip_selection else 'menu_buy'
    # Признак нужен последующим перерисовкам в других хендлерах.
    await state.update_data(tariff_selection_skipped=skip_selection)
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    # В мульти-тарифе проверяем не куплен ли уже этот тариф
    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        _active = await get_active_subscriptions_by_user_id(db, db_user.id)
        _existing = next((s for s in _active if s.tariff_id == tariff_id and not s.is_trial), None)
        if _existing:
            days_left = max(0, (_existing.end_date - datetime.now(UTC)).days) if _existing.end_date else 0
            await callback.answer(
                texts.t(
                    'TARIFF_PURCHASE_ALREADY_ACTIVE',
                    'Тариф «{name}» уже активен ({days} дн.). Продлите через "Мои подписки".',
                ).format(name=tariff.name, days=days_left),
                show_alert=True,
            )
            return

    # Проверяем, суточный ли это тариф
    is_daily = getattr(tariff, 'is_daily', False)

    if is_daily:
        # Для суточного тарифа показываем подтверждение без выбора периода
        raw_daily_price = getattr(tariff, 'daily_price_kopeks', 0)
        group_pct, offer_pct, daily_discount = _get_user_period_discount(db_user, 1)
        daily_price = (
            _apply_promo_discount(raw_daily_price, group_pct, offer_pct) if daily_discount > 0 else raw_daily_price
        )
        discount_text = (
            texts.t('TARIFF_PURCHASE_DAILY_DISCOUNT_LINE', '\n💎 Скидка: {percent}%').format(percent=daily_discount)
            if daily_discount > 0
            else ''
        )
        user_balance = db_user.balance_kopeks or 0
        traffic = format_traffic(tariff.traffic_limit_gb)

        if user_balance >= daily_price:
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_PURCHASE_DAILY_CONFIRM',
                    '✅ <b>Подтверждение покупки</b>\n\n'
                    '📦 Тариф: <b>{name}</b>\n'
                    '📊 Трафик: {traffic}\n'
                    '📱 Устройств: {devices}\n'
                    '🔄 Тип: <b>Суточный</b>\n\n'
                    '💰 <b>Цена: {price}/день</b>{discount}\n\n'
                    '💳 Ваш баланс: {balance}\n\n'
                    'ℹ️ Средства будут списываться автоматически раз в сутки.\n'
                    'Вы можете приостановить подписку в любой момент.',
                ).format(
                    name=html.escape(tariff.name),
                    traffic=traffic,
                    devices=tariff.device_limit,
                    price=format_price_kopeks(daily_price),
                    discount=discount_text,
                    balance=format_price_kopeks(user_balance),
                ),
                reply_markup=get_daily_tariff_confirm_keyboard(
                    tariff_id, db_user.language, back_callback=back_callback
                ),
                parse_mode='HTML',
            )
        else:
            missing = daily_price - user_balance

            # Ищем существующую подписку для передачи subscription_id в корзину
            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_subscription_by_user_and_tariff

                _daily_existing_sub = await get_subscription_by_user_and_tariff(db, db_user.id, tariff_id)
            else:
                _daily_existing_sub = await get_subscription_by_user_id(db, db_user.id)

            # Сохраняем данные корзины для автопокупки суточного тарифа
            cart_data = {
                'cart_mode': 'daily_tariff_purchase',
                'tariff_id': tariff_id,
                'is_daily': True,
                'daily_price_kopeks': daily_price,
                'total_price': daily_price,
                'user_id': db_user.id,
                'saved_cart': True,
                'missing_amount': missing,
                'return_to_cart': True,
                'description': f'Покупка суточного тарифа {tariff.name}',
                'traffic_limit_gb': tariff.traffic_limit_gb,
                'device_limit': tariff.device_limit,
                'allowed_squads': tariff.allowed_squads or [],
                'subscription_id': _daily_existing_sub.id if _daily_existing_sub else None,
            }
            await user_cart_service.save_user_cart(db_user.id, cart_data)

            await callback.message.edit_text(
                texts.t(
                    'TARIFF_PURCHASE_DAILY_INSUFFICIENT',
                    '❌ <b>Недостаточно средств</b>\n\n'
                    '📦 Тариф: <b>{name}</b>\n'
                    '🔄 Тип: Суточный\n'
                    '💰 Цена: {price}/день{discount}\n\n'
                    '💳 Ваш баланс: {balance}\n'
                    '⚠️ Не хватает: <b>{missing}</b>',
                ).format(
                    name=html.escape(tariff.name),
                    price=format_price_kopeks(daily_price),
                    discount=discount_text,
                    balance=format_price_kopeks(user_balance),
                    missing=format_price_kopeks(missing),
                )
                + texts.t(
                    'TARIFF_PURCHASE_CART_SAVED_HINT',
                    '\n\n🛒 <i>Корзина сохранена! После пополнения баланса подписка будет оформлена автоматически.</i>',
                ),
                reply_markup=get_daily_tariff_insufficient_balance_keyboard(
                    tariff_id, db_user.language, back_callback=back_callback
                ),
                parse_mode='HTML',
            )
    else:
        # Проверяем, есть ли кастомные дни или трафик
        can_custom_days = tariff.can_purchase_custom_days()
        can_custom_traffic = tariff.can_purchase_custom_traffic()

        if can_custom_days:
            # Кастомные дни - показываем экран с +/- для дней (и опционально трафика)
            user_balance = db_user.balance_kopeks or 0

            initial_days = tariff.min_days
            initial_traffic = tariff.min_traffic_gb if can_custom_traffic else tariff.traffic_limit_gb

            # Вычисляем скидку для начального периода
            group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, initial_days)

            await state.update_data(
                selected_tariff_id=tariff_id,
                custom_days=initial_days,
                custom_traffic_gb=initial_traffic,
                period_discount_percent=discount_percent,
                period_group_pct=group_pct,
                period_offer_pct=offer_pct,
            )

            preview_text = await format_custom_tariff_preview(
                tariff=tariff,
                days=initial_days,
                traffic_gb=initial_traffic,
                user_balance=user_balance,
                db_user=db_user,
                discount_percent=discount_percent,
            )

            await callback.message.edit_text(
                preview_text,
                reply_markup=get_custom_tariff_keyboard(
                    tariff_id=tariff_id,
                    language=db_user.language,
                    days=initial_days,
                    traffic_gb=initial_traffic,
                    can_custom_days=can_custom_days,
                    can_custom_traffic=can_custom_traffic,
                    min_days=tariff.min_days,
                    max_days=tariff.max_days,
                    min_traffic=tariff.min_traffic_gb,
                    max_traffic=tariff.max_traffic_gb,
                    back_callback=back_callback,
                ),
                parse_mode='HTML',
            )
        elif can_custom_traffic:
            # Только кастомный трафик - сначала выбираем период из period_prices
            # Показываем обычный выбор периода, трафик будет на следующем шаге
            await callback.message.edit_text(
                format_tariff_info_for_user(tariff, db_user.language)
                + texts.t(
                    'TARIFF_PURCHASE_TRAFFIC_SETUP_HINT',
                    '\n\n📊 <i>После выбора периода вы сможете настроить трафик</i>',
                ),
                reply_markup=get_tariff_periods_keyboard_with_traffic(
                    tariff, db_user.language, db_user=db_user, back_callback=back_callback
                ),
                parse_mode='HTML',
            )
        else:
            # Для обычного тарифа показываем выбор периода
            await callback.message.edit_text(
                format_tariff_info_for_user(tariff, db_user.language),
                reply_markup=get_tariff_periods_keyboard(
                    tariff, db_user.language, db_user=db_user, back_callback=back_callback
                ),
                parse_mode='HTML',
            )

    await state.update_data(selected_tariff_id=tariff_id)
    await callback.answer()


@error_handler
async def select_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает выбор тарифа."""
    tariff_id = int(callback.data.split(':')[1])
    await _proceed_with_selected_tariff(callback, db_user, db, state, tariff_id)


@error_handler
async def handle_custom_days_change(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает изменение количества дней."""
    # Экран списка мог быть пропущен — «Назад» обязан помнить об этом
    # и при перерисовке, иначе он вернёт на этот же экран.
    back_callback = await _tariff_back_callback(state)
    texts = get_texts(db_user.language)
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    delta = int(parts[2])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    state_data = await state.get_data()
    current_days = state_data.get('custom_days', tariff.min_days)
    current_traffic = state_data.get('custom_traffic_gb', tariff.min_traffic_gb)

    # Применяем изменение
    new_days = current_days + delta
    new_days = max(tariff.min_days, min(tariff.max_days, new_days))

    # При изменении дней пересчитываем скидку для нового периода
    group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, new_days)

    await state.update_data(
        custom_days=new_days,
        period_discount_percent=discount_percent,
        period_group_pct=group_pct,
        period_offer_pct=offer_pct,
    )

    user_balance = db_user.balance_kopeks or 0

    preview_text = await format_custom_tariff_preview(
        tariff=tariff,
        days=new_days,
        traffic_gb=current_traffic,
        user_balance=user_balance,
        db_user=db_user,
        discount_percent=discount_percent,
    )

    await callback.message.edit_text(
        preview_text,
        reply_markup=get_custom_tariff_keyboard(
            tariff_id=tariff_id,
            language=db_user.language,
            days=new_days,
            traffic_gb=current_traffic,
            can_custom_days=tariff.can_purchase_custom_days(),
            can_custom_traffic=tariff.can_purchase_custom_traffic(),
            min_days=tariff.min_days,
            max_days=tariff.max_days,
            min_traffic=tariff.min_traffic_gb,
            max_traffic=tariff.max_traffic_gb,
            back_callback=back_callback,
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def handle_custom_traffic_change(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает изменение количества трафика."""
    # Экран списка мог быть пропущен — «Назад» обязан помнить об этом
    # и при перерисовке, иначе он вернёт на этот же экран.
    back_callback = await _tariff_back_callback(state)
    texts = get_texts(db_user.language)
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    delta = int(parts[2])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    state_data = await state.get_data()
    current_days = state_data.get('custom_days', tariff.min_days)
    current_traffic = state_data.get('custom_traffic_gb', tariff.min_traffic_gb)
    discount_percent = state_data.get('period_discount_percent', 0)

    # Применяем изменение
    new_traffic = current_traffic + delta
    new_traffic = max(tariff.min_traffic_gb, min(tariff.max_traffic_gb, new_traffic))

    await state.update_data(custom_traffic_gb=new_traffic)

    user_balance = db_user.balance_kopeks or 0

    preview_text = await format_custom_tariff_preview(
        tariff=tariff,
        days=current_days,
        traffic_gb=new_traffic,
        user_balance=user_balance,
        db_user=db_user,
        discount_percent=discount_percent,
    )

    await callback.message.edit_text(
        preview_text,
        reply_markup=get_custom_tariff_keyboard(
            tariff_id=tariff_id,
            language=db_user.language,
            days=current_days,
            traffic_gb=new_traffic,
            can_custom_days=tariff.can_purchase_custom_days(),
            can_custom_traffic=tariff.can_purchase_custom_traffic(),
            min_days=tariff.min_days,
            max_days=tariff.max_days,
            min_traffic=tariff.min_traffic_gb,
            max_traffic=tariff.max_traffic_gb,
            back_callback=back_callback,
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def handle_custom_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Подтверждает покупку тарифа с кастомными параметрами."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    # Lock user BEFORE price computation to prevent TOCTOU on promo offer
    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    state_data = await state.get_data()
    custom_days = state_data.get('custom_days', tariff.min_days)
    custom_traffic = state_data.get('custom_traffic_gb', tariff.min_traffic_gb)

    # Calculate price via PricingEngine (single source of truth for all discounts)
    from app.services.pricing_engine import pricing_engine

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        custom_days,
        device_limit=tariff.device_limit,
        custom_traffic_gb=custom_traffic if tariff.can_purchase_custom_traffic() else None,
        user=db_user,
    )
    total_price = result.final_total

    # Проверяем, что цена за период валидна (original_total — цена до скидок)
    if result.original_total == 0 and not tariff.can_purchase_custom_days():
        await callback.answer(
            texts.t('TARIFF_PURCHASE_PERIOD_UNAVAILABLE_FOR_TARIFF', 'Выбранный период недоступен для этого тарифа'),
            show_alert=True,
        )
        return

    # Проверяем баланс (при 100% скидке — пропускаем)
    user_balance = db_user.balance_kopeks or 0
    if total_price > 0 and user_balance < total_price:
        await callback.answer(
            texts.t('MINIAPP_PURCHASE_STATUS_INSUFFICIENT', 'Недостаточно средств на балансе'), show_alert=True
        )
        return

    # Отвечаем на callback СРАЗУ — до тяжёлых операций (панель, транзакции),
    # иначе Telegram инвалидирует query через 30 сек → TelegramBadRequest
    try:
        await callback.answer()
    except Exception:
        pass

    # Save promo offer state before deduction (for restore on failure)
    consume_promo = result.promo_offer_discount > 0
    saved_promo_percent = int(getattr(db_user, 'promo_offer_discount_percent', 0) or 0) if consume_promo else 0
    saved_promo_source = getattr(db_user, 'promo_offer_discount_source', None) if consume_promo else None
    saved_promo_expires = getattr(db_user, 'promo_offer_discount_expires_at', None) if consume_promo else None

    try:
        # Списываем баланс
        success = await subtract_user_balance(
            db,
            db_user,
            total_price,
            f'Покупка тарифа {tariff.name} на {custom_days} дней',
            consume_promo_offer=consume_promo,
            mark_as_paid_subscription=True,
        )
        if not success:
            try:
                await callback.message.edit_text(texts.t('TARIFF_PURCHASE_CHARGE_ERROR', '❌ Ошибка списания баланса'))
            except Exception:
                pass
            return
    except Exception as e:
        logger.error('Ошибка списания баланса при покупке кастомного тарифа', error=e, exc_info=True)
        try:
            await callback.message.edit_text(texts.t('TARIFF_PURCHASE_CHARGE_ERROR', '❌ Ошибка списания баланса'))
        except Exception:
            pass
        return

    # Получаем список серверов из тарифа
    squads = tariff.allowed_squads or []

    # Если allowed_squads пустой - значит "все серверы", получаем их
    if not squads:
        from app.database.crud.server_squad import get_all_server_squads

        all_servers, _ = await get_all_server_squads(db, available_only=True)
        squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

    # Определяем трафик
    traffic_limit = custom_traffic if tariff.can_purchase_custom_traffic() else tariff.traffic_limit_gb

    # Проверяем есть ли уже подписка
    if settings.is_multi_tariff_enabled():
        active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
        existing_subscription = next((s for s in active_subs if s.tariff_id == tariff.id), None)
    else:
        existing_subscription = await get_subscription_by_user_id(db, db_user.id)

    try:
        if existing_subscription:
            # Продлеваем существующую подписку и обновляем параметры тарифа
            # Сохраняем докупленные устройства при продлении того же тарифа
            if existing_subscription.tariff_id == tariff.id:
                effective_device_limit = max(tariff.device_limit or 0, existing_subscription.device_limit or 0)
            else:
                effective_device_limit = tariff.device_limit
            subscription = await extend_subscription(
                db,
                existing_subscription,
                days=custom_days,
                tariff_id=tariff.id,
                traffic_limit_gb=traffic_limit,
                device_limit=effective_device_limit,
                connected_squads=squads,
            )
        else:
            # Создаем новую подписку
            subscription = await create_paid_subscription(
                db=db,
                user_id=db_user.id,
                duration_days=custom_days,
                traffic_limit_gb=traffic_limit,
                device_limit=tariff.device_limit,
                connected_squads=squads,
                tariff_id=tariff.id,
            )
    except Exception as e:
        logger.error('Ошибка создания/продления подписки при покупке кастомного тарифа', error=e, exc_info=True)
        await db.rollback()
        # Compensating refund: balance was already committed by subtract_user_balance
        try:
            from app.database.crud.user import add_user_balance

            refund_success = await add_user_balance(
                db,
                db_user,
                total_price,
                'Возврат: ошибка покупки кастомного тарифа',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
                commit=False,
            )
            if not refund_success:
                await _persist_failed_refund(
                    user_id=db_user.id,
                    amount_kopeks=total_price,
                    reason='Возврат: ошибка покупки кастомного тарифа',
                    error=Exception('add_user_balance returned False'),
                )
            # Restore promo offer if consumed
            if consume_promo and saved_promo_percent > 0:
                db_user.promo_offer_discount_percent = saved_promo_percent
                db_user.promo_offer_discount_source = saved_promo_source
                db_user.promo_offer_discount_expires_at = saved_promo_expires
            await db.commit()
        except Exception as refund_error:
            logger.critical(
                'CRITICAL: не удалось вернуть средства после ошибки покупки кастомного тарифа',
                user_id=db_user.id,
                price_kopeks=total_price,
                refund_error=refund_error,
            )
        try:
            await callback.message.edit_text(
                texts.t('TARIFF_PURCHASE_ERROR', '❌ Произошла ошибка при оформлении подписки')
            )
        except Exception:
            pass
        return

    try:
        # Обновляем пользователя в Remnawave
        # При покупке тарифа ВСЕГДА сбрасываем трафик в панели
        if settings.is_multi_tariff_enabled():
            _should_create = not subscription.remnawave_id
        else:
            _should_create = not getattr(db_user, 'remnawave_id', None)
        try:
            subscription_service = SubscriptionService()
            if _should_create:
                await subscription_service.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=True,
                    reset_reason='покупка тарифа',
                )
            else:
                await subscription_service.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=True,
                    reset_reason='покупка тарифа',
                )
        except Exception as e:
            logger.error('Ошибка обновления Remnawave', error=e)
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            # То же mode-aware решение, что и синк выше: конвертированный из
            # триала ретраится как 'update', а не плодит дубль панельного юзера.
            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=db_user.id,
                action='create' if _should_create else 'update',
            )

        # Создаем транзакцию
        await create_transaction(
            db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=total_price,
            description=f'Покупка тарифа {tariff.name} на {custom_days} дней',
        )

        # Отправляем уведомление админу
        try:
            admin_notification_service = AdminNotificationService(callback.bot)
            await admin_notification_service.send_subscription_purchase_notification(
                db,
                db_user,
                subscription,
                None,
                custom_days,
                # Маркер ставит extend_subscription при конверсии живого триала
                was_trial_conversion=bool(getattr(subscription, '_converted_from_trial', False)),
                amount_kopeks=total_price,
                purchase_type='renewal' if existing_subscription else 'first_purchase',
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления админу', error=e)

        # Очищаем корзину после успешной покупки (per-subscription в multi-tariff)
        try:
            _cart_sub_id = getattr(subscription, 'id', None) if subscription else None
            if _cart_sub_id and settings.is_multi_tariff_enabled():
                await user_cart_service.delete_subscription_cart(db_user.id, _cart_sub_id)
            else:
                await user_cart_service.delete_user_cart(db_user.id)
        except Exception as e:
            logger.error('Ошибка очистки корзины', error=e)

        await state.clear()

        traffic_display = format_traffic(traffic_limit)

        await callback.message.edit_text(
            texts.t(
                'TARIFF_PURCHASE_SUCCESS',
                '🎉 <b>Подписка успешно оформлена!</b>\n\n'
                '📦 Тариф: <b>{name}</b>\n'
                '📊 Трафик: {traffic}\n'
                '📱 Устройств: {devices}\n'
                '📅 Период: {period}\n'
                '💰 Списано: {price}\n\n'
                'Перейдите в раздел «Подписка» для подключения.',
            ).format(
                name=html.escape(tariff.name),
                traffic=traffic_display,
                devices=tariff.device_limit,
                period=format_period(custom_days),
                price=format_price_kopeks(total_price),
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                            callback_data=f'sm:{subscription.id}'
                            if settings.is_multi_tariff_enabled() and subscription
                            else 'menu_subscription',
                        )
                    ],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
                ]
            ),
            parse_mode='HTML',
        )
    except Exception as e:
        logger.error('Ошибка при покупке тарифа с кастомными параметрами', error=e, exc_info=True)
        try:
            await callback.message.edit_text(
                texts.t('TARIFF_PURCHASE_ERROR', '❌ Произошла ошибка при оформлении подписки')
            )
        except Exception:
            pass


@error_handler
async def select_tariff_period_with_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает выбор периода для тарифа с кастомным трафиком - показывает экран настройки трафика."""
    # Экран списка мог быть пропущен — «Назад» обязан помнить об этом
    # и при перерисовке, иначе он вернёт на этот же экран.
    back_callback = await _tariff_back_callback(state)
    texts = get_texts(db_user.language)
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    period = int(parts[2])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    if not tariff.can_purchase_custom_traffic():
        await callback.answer(
            texts.t('TARIFF_PURCHASE_CUSTOM_TRAFFIC_UNAVAILABLE', 'Кастомный трафик недоступен для этого тарифа'),
            show_alert=True,
        )
        return

    user_balance = db_user.balance_kopeks or 0
    initial_traffic = tariff.min_traffic_gb

    # Получаем скидку для выбранного периода
    group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, period)

    # Сохраняем выбранный период и скидку в состояние
    await state.update_data(
        selected_tariff_id=tariff_id,
        custom_days=period,  # Фиксированный период из period_prices
        custom_traffic_gb=initial_traffic,
        period_discount_percent=discount_percent,
        period_group_pct=group_pct,
        period_offer_pct=offer_pct,
    )

    preview_text = await format_custom_tariff_preview(
        tariff=tariff,
        days=period,
        traffic_gb=initial_traffic,
        user_balance=user_balance,
        db_user=db_user,
        discount_percent=discount_percent,
    )

    await callback.message.edit_text(
        preview_text,
        reply_markup=get_custom_tariff_keyboard(
            tariff_id=tariff_id,
            language=db_user.language,
            days=period,
            traffic_gb=initial_traffic,
            can_custom_days=False,  # Период уже выбран, менять нельзя
            can_custom_traffic=True,
            min_days=period,
            max_days=period,
            min_traffic=tariff.min_traffic_gb,
            max_traffic=tariff.max_traffic_gb,
            back_callback=back_callback,
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def select_tariff_period(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает выбор периода для тарифа."""
    texts = get_texts(db_user.language)
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    period = int(parts[2])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    # Существующая подписка этого тарифа нужна ДО расчёта цены: confirm передаёт
    # её device_limit в движок, и превью обязано считать так же.
    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_subscription_by_user_and_tariff

        _existing_sub = await get_subscription_by_user_and_tariff(db, db_user.id, tariff_id)
    else:
        _existing_sub = await get_subscription_by_user_id(db, db_user.id)

    device_limit = None
    if _existing_sub and _existing_sub.tariff_id == tariff.id:
        device_limit = _existing_sub.device_limit

    # Цена — тем же PricingEngine, что и confirm_tariff_purchase: ручной расчёт от
    # period_prices не видел доплату за устройства сверх включённых в тариф, превью
    # показывало заниженную цену («После оплаты: 0»), а подтверждение отбивало
    # «Недостаточно средств».
    from app.services.pricing_engine import pricing_engine

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period,
        device_limit=device_limit,
        user=db_user,
    )
    final_price = result.final_total
    original_price = result.original_total
    total_discount = result.promo_group_discount + result.promo_offer_discount
    discount_percent = (
        round((1 - final_price / original_price) * 100) if original_price > 0 and total_discount > 0 else 0
    )
    shown_device_limit = device_limit if device_limit is not None else tariff.device_limit

    # Проверяем баланс
    user_balance = db_user.balance_kopeks or 0

    traffic = format_traffic(tariff.traffic_limit_gb)

    if user_balance >= final_price:
        # Показываем подтверждение
        discount_text = ''
        if discount_percent > 0:
            discount_text = texts.t(
                'TARIFF_PURCHASE_DISCOUNT_SHORT_LINE', '\n🎁 Скидка: {percent}% (-{amount})'
            ).format(percent=discount_percent, amount=format_price_kopeks(total_discount))

        await callback.message.edit_text(
            texts.t(
                'TARIFF_PURCHASE_CONFIRM',
                '✅ <b>Подтверждение покупки</b>\n\n'
                '📦 Тариф: <b>{name}</b>\n'
                '📊 Трафик: {traffic}\n'
                '📱 Устройств: {devices}\n'
                '📅 Период: {period}\n'
                '{discount}\n'
                '💰 <b>Итого: {total}</b>\n\n'
                '💳 Ваш баланс: {balance}\n'
                'После оплаты: {after}',
            ).format(
                name=html.escape(tariff.name),
                traffic=traffic,
                devices=shown_device_limit,
                period=format_period(period),
                discount=discount_text,
                total=format_price_kopeks(final_price),
                balance=format_price_kopeks(user_balance),
                after=format_price_kopeks(user_balance - final_price),
            ),
            reply_markup=get_tariff_confirm_keyboard(tariff_id, period, db_user.language),
            parse_mode='HTML',
        )
    else:
        # Недостаточно средств - сохраняем корзину для автопокупки
        missing = final_price - user_balance

        # Сохраняем данные корзины для автопокупки после пополнения
        cart_data = {
            'cart_mode': 'tariff_purchase',
            'tariff_id': tariff_id,
            'period_days': period,
            'total_price': final_price,
            'user_id': db_user.id,
            'saved_cart': True,
            'missing_amount': missing,
            'return_to_cart': True,
            'description': f'Покупка тарифа {tariff.name} на {period} дней',
            'traffic_limit_gb': tariff.traffic_limit_gb,
            'device_limit': shown_device_limit,
            'allowed_squads': tariff.allowed_squads or [],
            'discount_percent': discount_percent,
            'subscription_id': _existing_sub.id if _existing_sub else None,
        }
        await user_cart_service.save_user_cart(db_user.id, cart_data)

        await callback.message.edit_text(
            texts.t(
                'TARIFF_PURCHASE_INSUFFICIENT',
                '❌ <b>Недостаточно средств</b>\n\n'
                '📦 Тариф: <b>{name}</b>\n'
                '📅 Период: {period}\n'
                '💰 Стоимость: {price}\n\n'
                '💳 Ваш баланс: {balance}\n'
                '⚠️ Не хватает: <b>{missing}</b>',
            ).format(
                name=html.escape(tariff.name),
                period=format_period(period),
                price=format_price_kopeks(final_price),
                balance=format_price_kopeks(user_balance),
                missing=format_price_kopeks(missing),
            )
            + texts.t(
                'TARIFF_PURCHASE_CART_SAVED_HINT',
                '\n\n🛒 <i>Корзина сохранена! После пополнения баланса подписка будет оформлена автоматически.</i>',
            ),
            reply_markup=get_tariff_insufficient_balance_keyboard(
                tariff_id, period, db_user.language, missing_kopeks=missing
            ),
            parse_mode='HTML',
        )

    # Pin the resolved subscription_id in FSM (resolved above, before pricing).
    # Without this, ``confirm_tariff_purchase`` re-queries by
    # ``(user_id, tariff_id)`` and can race with concurrent panel
    # webhooks that briefly flip the active sub's status — falling
    # through to ``create_paid_subscription`` and hitting the partial
    # UNIQUE ``uq_subscriptions_user_tariff_active`` (logs "Тариф уже
    # активен", refunds, leaves user confused).
    target_subscription_id: int | None = None
    if settings.is_multi_tariff_enabled():
        target_subscription_id = _existing_sub.id if _existing_sub else None

    await state.update_data(
        selected_tariff_id=tariff_id,
        selected_period=period,
        final_price=final_price,
        tariff_discount_percent=discount_percent,
        target_subscription_id=target_subscription_id,
    )
    await callback.answer()


@error_handler
async def confirm_tariff_purchase(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Подтверждает покупку тарифа и создает подписку."""
    texts = get_texts(db_user.language)
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    period = int(parts[2])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    # Validate period is available for this tariff
    if str(period) not in (tariff.period_prices or {}):
        await callback.answer(texts.t('TARIFF_PURCHASE_PERIOD_UNAVAILABLE', 'Период недоступен'), show_alert=True)
        return

    # Lock user BEFORE price computation to prevent TOCTOU on promo offer
    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    # Calculate price via PricingEngine (single source of truth)
    from app.services.pricing_engine import pricing_engine

    # In multi-tariff mode, prefer the subscription_id pinned in FSM at
    # preview time — that's the EXACT row the user clicked Renew/Buy on.
    # Re-querying by ``(user_id, tariff_id)`` here is race-vulnerable:
    # if a concurrent panel webhook briefly flips the active sub's
    # status between preview and confirm, this query returns None,
    # the code falls through to ``create_paid_subscription``, and the
    # partial UNIQUE ``uq_subscriptions_user_tariff_active`` raises
    # IntegrityError → user sees "Тариф уже активен" in logs, money
    # debited then refunded, subscription not extended.
    #
    # We fall back to the tariff-level lookup only if FSM has no
    # pinned ID (old session / direct deep-link / state lost) so
    # legacy flows continue to work.
    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_subscription_by_user_and_tariff

        _state_data = await state.get_data() if state else {}
        _pinned_sub_id = _state_data.get('target_subscription_id')

        existing_sub = None
        if _pinned_sub_id:
            existing_sub = await get_subscription_by_id_for_user(db, int(_pinned_sub_id), db_user.id)
            # Defence: if admin/user switched tariff between preview
            # and confirm, the pinned sub may no longer match —
            # ignore it and fall back to fresh tariff lookup.
            if existing_sub and existing_sub.tariff_id != tariff_id:
                logger.warning(
                    'FSM-pinned subscription tariff diverged from confirm tariff; falling back',
                    pinned_sub_id=_pinned_sub_id,
                    pinned_tariff_id=existing_sub.tariff_id,
                    confirm_tariff_id=tariff_id,
                    user_id=db_user.id,
                )
                existing_sub = None
        if existing_sub is None:
            existing_sub = await get_subscription_by_user_and_tariff(db, db_user.id, tariff_id)
    else:
        existing_sub = await get_subscription_by_user_id(db, db_user.id)

    device_limit = None
    if existing_sub and existing_sub.tariff_id == tariff.id:
        device_limit = existing_sub.device_limit

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period,
        device_limit=device_limit,
        user=db_user,
    )
    final_price = result.final_total

    # Проверяем баланс (user already locked, balance is fresh)
    user_balance = db_user.balance_kopeks or 0
    if final_price > 0 and user_balance < final_price:
        await callback.answer(
            texts.t('MINIAPP_PURCHASE_STATUS_INSUFFICIENT', 'Недостаточно средств на балансе'), show_alert=True
        )
        return

    # Отвечаем на callback СРАЗУ — до тяжёлых операций (панель, транзакции),
    # иначе Telegram инвалидирует query через 30 сек → TelegramBadRequest
    try:
        await callback.answer()
    except Exception:
        pass

    # Списываем баланс
    consume_promo = result.promo_offer_discount > 0
    # Save promo offer state before deduction (for restore on failure)
    saved_promo_percent = int(getattr(db_user, 'promo_offer_discount_percent', 0) or 0) if consume_promo else 0
    saved_promo_source = getattr(db_user, 'promo_offer_discount_source', None) if consume_promo else None
    saved_promo_expires = getattr(db_user, 'promo_offer_discount_expires_at', None) if consume_promo else None
    try:
        success = await subtract_user_balance(
            db,
            db_user,
            final_price,
            f'Покупка тарифа {tariff.name} на {period} дней',
            consume_promo_offer=consume_promo,
            mark_as_paid_subscription=True,
        )
        if not success:
            try:
                await callback.message.edit_text(texts.t('TARIFF_PURCHASE_CHARGE_ERROR', '❌ Ошибка списания баланса'))
            except Exception:
                pass
            return
    except Exception as e:
        logger.error('Ошибка списания баланса при покупке тарифа', error=e, exc_info=True)
        try:
            await callback.message.edit_text(texts.t('TARIFF_PURCHASE_CHARGE_ERROR', '❌ Ошибка списания баланса'))
        except Exception:
            pass
        return

    # Получаем список серверов из тарифа
    squads = tariff.allowed_squads or []

    # Если allowed_squads пустой - значит "все серверы", получаем их
    if not squads:
        from app.database.crud.server_squad import get_all_server_squads

        all_servers, _ = await get_all_server_squads(db, available_only=True)
        squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

    # Reuse existing_sub fetched above for device pricing
    existing_subscription = existing_sub

    try:
        if settings.is_multi_tariff_enabled():
            if existing_subscription and existing_subscription.tariff_id == tariff.id:
                # Extend existing subscription for this tariff
                effective_device_limit = max(tariff.device_limit or 0, existing_subscription.device_limit or 0)
                subscription = await extend_subscription(
                    db,
                    existing_subscription,
                    days=period,
                    tariff_id=tariff.id,
                    traffic_limit_gb=tariff.traffic_limit_gb,
                    device_limit=effective_device_limit,
                    connected_squads=squads,
                )
            else:
                # Guard: enforce MAX_ACTIVE_SUBSCRIPTIONS limit.
                # Живой триал не считаем: create_paid_subscription конвертирует
                # его на месте (строка переиспользуется), количество подписок не
                # растёт — блокировать такую покупку лимитом нельзя, иначе юзер
                # на пределе лимита не может превратить триал в платный тариф.
                _alive_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
                active_count = len([s for s in _alive_subs if not s.is_trial])
                if active_count >= settings.get_max_active_subscriptions():
                    from app.database.crud.user import add_user_balance

                    refund_success = await add_user_balance(
                        db,
                        db_user,
                        final_price,
                        'Возврат: превышен лимит подписок',
                        create_transaction=True,
                        transaction_type=TransactionType.REFUND,
                        commit=False,
                    )
                    if not refund_success:
                        await _persist_failed_refund(
                            user_id=db_user.id,
                            amount_kopeks=final_price,
                            reason='Возврат: превышен лимит подписок',
                            error=Exception('add_user_balance returned False'),
                        )
                    # Restore promo offer if consumed
                    if consume_promo and saved_promo_percent > 0:
                        db_user.promo_offer_discount_percent = saved_promo_percent
                        db_user.promo_offer_discount_source = saved_promo_source
                        db_user.promo_offer_discount_expires_at = saved_promo_expires
                    await db.commit()
                    try:
                        await callback.message.edit_text(
                            texts.t('TARIFF_PURCHASE_MAX_SUBSCRIPTIONS', '❌ Максимум подписок: {max}').format(
                                max=settings.get_max_active_subscriptions()
                            )
                        )
                    except Exception:
                        pass
                    return

                # Create NEW subscription for this tariff (multi-tariff: new Remnawave user)
                subscription = await create_paid_subscription(
                    db=db,
                    user_id=db_user.id,
                    duration_days=period,
                    traffic_limit_gb=tariff.traffic_limit_gb,
                    device_limit=tariff.device_limit,
                    connected_squads=squads,
                    tariff_id=tariff.id,
                )
        elif existing_subscription:
            # Legacy single-subscription: extend or switch
            # Сохраняем докупленные устройства при продлении того же тарифа
            if existing_subscription.tariff_id == tariff.id:
                effective_device_limit = max(tariff.device_limit or 0, existing_subscription.device_limit or 0)
            else:
                effective_device_limit = tariff.device_limit
            subscription = await extend_subscription(
                db,
                existing_subscription,
                days=period,
                tariff_id=tariff.id,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=effective_device_limit,
                connected_squads=squads,
            )
        else:
            # Создаем новую подписку
            subscription = await create_paid_subscription(
                db=db,
                user_id=db_user.id,
                duration_days=period,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=squads,
                tariff_id=tariff.id,
            )
    except IntegrityError as e:
        # Partial unique index violation: user already has active subscription for this tariff
        logger.warning('Тариф уже активен у пользователя', tariff_id=tariff_id, user_id=db_user.id, error=e)
        await db.rollback()
        try:
            from app.database.crud.user import add_user_balance

            refund_success = await add_user_balance(
                db,
                db_user,
                final_price,
                'Возврат: тариф уже активен',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
                commit=False,
            )
            if not refund_success:
                await _persist_failed_refund(
                    user_id=db_user.id,
                    amount_kopeks=final_price,
                    reason='Возврат: тариф уже активен (add_user_balance returned False)',
                    error=Exception('add_user_balance returned False'),
                )
            # Restore promo offer if consumed (atomic with refund)
            if consume_promo and saved_promo_percent > 0:
                db_user.promo_offer_discount_percent = saved_promo_percent
                db_user.promo_offer_discount_source = saved_promo_source
                db_user.promo_offer_discount_expires_at = saved_promo_expires
            await db.commit()
        except Exception as refund_error:
            logger.critical('CRITICAL: не удалось вернуть средства', user_id=db_user.id, refund_error=refund_error)
            await _persist_failed_refund(
                user_id=db_user.id,
                amount_kopeks=final_price,
                reason='Возврат: тариф уже активен',
                error=refund_error,
            )
        try:
            await callback.message.edit_text(
                texts.t('TARIFF_PURCHASE_ALREADY_ACTIVE_SUB', '❌ У вас уже есть активная подписка на этот тариф')
            )
        except Exception:
            pass
        return
    except Exception as e:
        logger.error('Ошибка создания/продления подписки при покупке тарифа', error=e, exc_info=True)
        await db.rollback()
        # Compensating refund: balance was already committed by subtract_user_balance
        try:
            from app.database.crud.user import add_user_balance

            refund_success = await add_user_balance(
                db,
                db_user,
                final_price,
                'Возврат: ошибка покупки тарифа',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
                commit=False,
            )
            if not refund_success:
                await _persist_failed_refund(
                    user_id=db_user.id,
                    amount_kopeks=final_price,
                    reason='Возврат: ошибка покупки тарифа (add_user_balance returned False)',
                    error=Exception('add_user_balance returned False'),
                )
            # Restore promo offer if consumed (atomic with refund)
            if consume_promo and saved_promo_percent > 0:
                db_user.promo_offer_discount_percent = saved_promo_percent
                db_user.promo_offer_discount_source = saved_promo_source
                db_user.promo_offer_discount_expires_at = saved_promo_expires
            await db.commit()
        except Exception as refund_error:
            logger.critical(
                'CRITICAL: не удалось вернуть средства после ошибки покупки тарифа',
                user_id=db_user.id,
                price_kopeks=final_price,
                refund_error=refund_error,
            )
            await _persist_failed_refund(
                user_id=db_user.id,
                amount_kopeks=final_price,
                reason='Возврат: ошибка покупки тарифа',
                error=refund_error,
            )
        try:
            await callback.message.edit_text(
                texts.t('TARIFF_PURCHASE_ERROR', '❌ Произошла ошибка при оформлении подписки')
            )
        except Exception:
            pass
        return

    # Обновляем пользователя в Remnawave
    # При покупке тарифа ВСЕГДА сбрасываем трафик в панели
    # In multi-tariff mode, each subscription has its own panel user.
    # A new subscription has no remnawave_id yet, so always CREATE.
    # In single-tariff mode, reuse the user-level panel id if available.
    if settings.is_multi_tariff_enabled():
        _should_create = not subscription.remnawave_id
    else:
        _should_create = not getattr(db_user, 'remnawave_id', None)
    try:
        subscription_service = SubscriptionService()
        if _should_create:
            await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=True,
                reset_reason='покупка тарифа',
            )
        else:
            await subscription_service.update_remnawave_user(
                db,
                subscription,
                reset_traffic=True,
                reset_reason='покупка тарифа',
            )
    except Exception as e:
        logger.error('Ошибка обновления Remnawave', error=e)
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        # Ретрай повторяет то же mode-aware решение, что и синк выше: у
        # конвертированного из триала (или реанимированной #3004) подписки уже
        # есть панельный юзер — его надо ОБНОВИТЬ, а не создать дубль. Хардкод
        # 'update' по subscription.remnawave_id здесь не годится: в
        # single-tariff вебхук панели чистит user.remnawave_id при удалении
        # юзера, а на подписке остаётся стухший id.
        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=db_user.id,
            action='create' if _should_create else 'update',
        )

    # Создаем транзакцию
    try:
        await create_transaction(
            db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=final_price,
            description=f'Покупка тарифа {tariff.name} на {period} дней',
        )
    except Exception as e:
        logger.error('Ошибка создания транзакции', error=e)

    # Отправляем уведомление админу
    try:
        admin_notification_service = AdminNotificationService(callback.bot)
        await admin_notification_service.send_subscription_purchase_notification(
            db,
            db_user,
            subscription,
            None,  # Транзакция отсутствует, оплата с баланса
            period,
            # Маркер выставляют extend_subscription/_convert_trial_subscription_to_paid,
            # когда покупка конвертировала живой триал (та же строка, тот же панельный юзер).
            was_trial_conversion=bool(getattr(subscription, '_converted_from_trial', False)),
            amount_kopeks=final_price,
            purchase_type='renewal' if existing_subscription else 'first_purchase',
        )
    except Exception as e:
        logger.error('Ошибка отправки уведомления админу', error=e)

    # Очищаем корзину после успешной покупки (per-subscription в multi-tariff)
    try:
        _cart_sub_id = getattr(subscription, 'id', None) if subscription else None
        if _cart_sub_id and settings.is_multi_tariff_enabled():
            await user_cart_service.delete_subscription_cart(db_user.id, _cart_sub_id)
        else:
            await user_cart_service.delete_user_cart(db_user.id)
        logger.info('Корзина очищена после покупки тарифа для пользователя', telegram_id=db_user.telegram_id)
    except Exception as e:
        logger.error('Ошибка очистки корзины', error=e)

    await state.clear()

    traffic = format_traffic(tariff.traffic_limit_gb)

    await callback.message.edit_text(
        texts.t(
            'TARIFF_PURCHASE_SUCCESS',
            '🎉 <b>Подписка успешно оформлена!</b>\n\n'
            '📦 Тариф: <b>{name}</b>\n'
            '📊 Трафик: {traffic}\n'
            '📱 Устройств: {devices}\n'
            '📅 Период: {period}\n'
            '💰 Списано: {price}\n\n'
            'Перейдите в раздел «Подписка» для подключения.',
        ).format(
            name=html.escape(tariff.name),
            traffic=traffic,
            devices=tariff.device_limit,
            period=format_period(period),
            price=format_price_kopeks(final_price),
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                        callback_data=f'sm:{subscription.id}'
                        if settings.is_multi_tariff_enabled() and subscription
                        else 'menu_subscription',
                    )
                ],
                [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
            ]
        ),
        parse_mode='HTML',
    )


# ==================== Покупка суточного тарифа ====================


@error_handler
async def confirm_daily_tariff_purchase(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Подтверждает покупку суточного тарифа."""
    texts = get_texts(db_user.language)

    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    is_daily = getattr(tariff, 'is_daily', False)
    if not is_daily:
        await callback.answer(texts.t('TARIFF_PURCHASE_NOT_DAILY', 'Это не суточный тариф'), show_alert=True)
        return

    daily_price = getattr(tariff, 'daily_price_kopeks', 0)
    if daily_price <= 0:
        await callback.answer(texts.t('TARIFF_PURCHASE_INVALID_PRICE', 'Некорректная цена тарифа'), show_alert=True)
        return

    # Lock user BEFORE price computation to prevent TOCTOU on promo offer
    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    # Apply group + promo-offer discounts via PricingEngine (single source of truth)
    from app.services.pricing_engine import pricing_engine

    pricing_result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period_days=1,
        device_limit=tariff.device_limit,
        user=db_user,
    )
    final_daily_price = pricing_result.final_total
    consume_promo = pricing_result.breakdown.get('offer_discount_pct', 0) > 0

    # Проверяем баланс (user already locked, balance is fresh)
    user_balance = db_user.balance_kopeks or 0
    if final_daily_price > 0 and user_balance < final_daily_price:
        await callback.answer(
            texts.t('MINIAPP_PURCHASE_STATUS_INSUFFICIENT', 'Недостаточно средств на балансе'), show_alert=True
        )
        return

    # Отвечаем на callback СРАЗУ — до тяжёлых операций (панель, транзакции),
    # иначе Telegram инвалидирует query через 30 сек → TelegramBadRequest
    try:
        await callback.answer()
    except Exception:
        pass

    try:
        # Списываем первый день сразу
        success = await subtract_user_balance(
            db,
            db_user,
            final_daily_price,
            f'Покупка суточного тарифа {tariff.name} (первый день)',
            consume_promo_offer=consume_promo,
            mark_as_paid_subscription=True,
        )
        if not success:
            try:
                await callback.message.edit_text(texts.t('TARIFF_PURCHASE_CHARGE_ERROR', '❌ Ошибка списания баланса'))
            except Exception:
                pass
            return
    except Exception as e:
        logger.error('Ошибка списания баланса при покупке суточного тарифа', error=e, exc_info=True)
        try:
            await callback.message.edit_text(texts.t('TARIFF_PURCHASE_CHARGE_ERROR', '❌ Ошибка списания баланса'))
        except Exception:
            pass
        return

    # Получаем список серверов из тарифа
    squads = tariff.allowed_squads or []

    # Если allowed_squads пустой - значит "все серверы", получаем их
    if not squads:
        from app.database.crud.server_squad import get_all_server_squads

        all_servers, _ = await get_all_server_squads(db, available_only=True)
        squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

    # Проверяем есть ли уже подписка
    if settings.is_multi_tariff_enabled():
        active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
        existing_subscription = next((s for s in active_subs if s.tariff_id == tariff.id), None)
    else:
        existing_subscription = await get_subscription_by_user_id(db, db_user.id)

    try:
        if existing_subscription:
            # Обновляем существующую подписку на суточный тариф
            # Сбрасываем лимит устройств на базу нового тарифа (докупленные не переносятся)
            from app.database.crud.subscription import calc_device_limit_on_tariff_switch

            old_tariff = (
                await get_tariff_by_id(db, existing_subscription.tariff_id) if existing_subscription.tariff_id else None
            )
            existing_subscription.tariff_id = tariff.id
            existing_subscription.traffic_limit_gb = tariff.traffic_limit_gb
            existing_subscription.device_limit = calc_device_limit_on_tariff_switch(
                current_device_limit=existing_subscription.device_limit,
                old_tariff_device_limit=old_tariff.device_limit if old_tariff else None,
                new_tariff_device_limit=tariff.device_limit,
                max_device_limit=getattr(tariff, 'max_device_limit', None),
            )
            existing_subscription.connected_squads = squads
            existing_subscription.status = 'active'
            existing_subscription.is_trial = False  # Сбрасываем триальный статус
            existing_subscription.is_daily_paused = False
            existing_subscription.last_daily_charge_at = datetime.now(UTC)
            # Для суточного тарифа ставим срок на 1 день
            existing_subscription.end_date = datetime.now(UTC) + timedelta(days=1)

            # Сбрасываем докупленный трафик при смене тарифа
            from sqlalchemy import delete as sql_delete

            from app.database.models import TrafficPurchase

            await db.execute(
                sql_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == existing_subscription.id)
            )
            existing_subscription.purchased_traffic_gb = 0
            existing_subscription.traffic_reset_at = None

            await db.commit()
            await db.refresh(existing_subscription)
            subscription = existing_subscription
        else:
            # Создаем новую подписку на 1 день
            subscription = await create_paid_subscription(
                db=db,
                user_id=db_user.id,
                duration_days=1,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=squads,
                tariff_id=tariff.id,
            )
            # Устанавливаем время последнего списания
            subscription.last_daily_charge_at = datetime.now(UTC)
            subscription.is_daily_paused = False
            await db.commit()
            await db.refresh(subscription)
    except Exception as e:
        logger.error('Ошибка создания/продления подписки при покупке суточного тарифа', error=e, exc_info=True)
        await db.rollback()
        # Compensating refund: balance was already committed by subtract_user_balance
        try:
            from app.database.crud.user import add_user_balance

            refund_success = await add_user_balance(
                db,
                db_user,
                final_daily_price,
                'Возврат: ошибка покупки суточного тарифа',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
                commit=False,
            )
            if not refund_success:
                await _persist_failed_refund(
                    user_id=db_user.id,
                    amount_kopeks=final_daily_price,
                    reason='Возврат: ошибка покупки суточного тарифа',
                    error=Exception('add_user_balance returned False'),
                )
            await db.commit()
        except Exception as refund_error:
            logger.critical(
                'CRITICAL: не удалось вернуть средства после ошибки покупки суточного тарифа',
                user_id=db_user.id,
                price_kopeks=final_daily_price,
                refund_error=refund_error,
            )
        try:
            await callback.message.edit_text(
                texts.t('TARIFF_PURCHASE_ERROR', '❌ Произошла ошибка при оформлении подписки')
            )
        except Exception:
            pass
        return

    # Обновляем пользователя в Remnawave
    # При покупке тарифа ВСЕГДА сбрасываем трафик в панели
    try:
        subscription_service = SubscriptionService()
        if settings.is_multi_tariff_enabled():
            _should_create = not subscription.remnawave_id
        else:
            _should_create = not getattr(db_user, 'remnawave_id', None)

        if _should_create:
            await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=True,
                reset_reason='покупка суточного тарифа',
            )
        else:
            await subscription_service.update_remnawave_user(
                db,
                subscription,
                reset_traffic=True,
                reset_reason='покупка суточного тарифа',
            )
    except Exception as e:
        logger.error('Ошибка обновления Remnawave', error=e)
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=db_user.id,
            action='create',
        )

    # Создаем транзакцию
    await create_transaction(
        db,
        user_id=db_user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=final_daily_price,
        description=f'Покупка суточного тарифа {tariff.name} (первый день)',
    )

    # Отправляем уведомление админу
    try:
        admin_notification_service = AdminNotificationService(callback.bot)
        await admin_notification_service.send_subscription_purchase_notification(
            db,
            db_user,
            subscription,
            None,
            1,  # 1 день
            was_trial_conversion=False,
            amount_kopeks=final_daily_price,
            purchase_type='renewal' if existing_subscription else 'first_purchase',
        )
    except Exception as e:
        logger.error('Ошибка отправки уведомления админу', error=e)

    # Очищаем корзину после успешной покупки (per-subscription в multi-tariff)
    try:
        _cart_sub_id = getattr(subscription, 'id', None) if subscription else None
        if _cart_sub_id and settings.is_multi_tariff_enabled():
            await user_cart_service.delete_subscription_cart(db_user.id, _cart_sub_id)
        else:
            await user_cart_service.delete_user_cart(db_user.id)
        logger.info('Корзина очищена после покупки суточного тарифа для пользователя', telegram_id=db_user.telegram_id)
    except Exception as e:
        logger.error('Ошибка очистки корзины', error=e)

    await state.clear()

    traffic = format_traffic(tariff.traffic_limit_gb)

    await callback.message.edit_text(
        texts.t(
            'TARIFF_PURCHASE_DAILY_SUCCESS',
            '🎉 <b>Суточная подписка оформлена!</b>\n\n'
            '📦 Тариф: <b>{name}</b>\n'
            '📊 Трафик: {traffic}\n'
            '📱 Устройств: {devices}\n'
            '🔄 Тип: Суточный\n'
            '💰 Списано: {price}\n\n'
            'ℹ️ Следующее списание через 24 часа.\n'
            'Перейдите в раздел «Подписка» для подключения.',
        ).format(
            name=html.escape(tariff.name),
            traffic=traffic,
            devices=tariff.device_limit,
            price=format_price_kopeks(final_daily_price),
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                        callback_data=f'sm:{subscription.id}'
                        if settings.is_multi_tariff_enabled() and subscription
                        else 'menu_subscription',
                    )
                ],
                [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
            ]
        ),
        parse_mode='HTML',
    )


# ==================== Продление по тарифу ====================


def _calc_extra_devices_cost(tariff: Tariff, subscription_device_limit: int, period_days: int) -> int:
    """Рассчитывает стоимость дополнительных устройств сверх тарифа для периода."""
    additional = max(0, subscription_device_limit - (tariff.device_limit or 1))
    if additional <= 0:
        return 0
    device_price = getattr(tariff, 'device_price_kopeks', None) or 0
    if device_price <= 0:
        return 0
    months = max(1, round(period_days / 30))
    return additional * device_price * months


def get_tariff_extend_keyboard(
    tariff: Tariff,
    language: str,
    db_user: User | None = None,
    subscription_device_limit: int | None = None,
    subscription_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру выбора периода для продления по тарифу с учетом скидок по периодам."""
    from app.services.pricing_engine import PricingEngine

    texts = get_texts(language)
    buttons = []

    promo_group = PricingEngine.resolve_promo_group(db_user) if db_user else None

    prices = tariff.period_prices or {}
    for period_str in sorted(prices.keys(), key=int):
        period = int(period_str)
        base_price = prices[period_str]

        # Стоимость дополнительных устройств
        devices_cost = 0
        if subscription_device_limit is not None:
            devices_cost = _calc_extra_devices_cost(tariff, subscription_device_limit, period)

        # Per-category group discounts (period + devices separately, like PricingEngine)
        period_pct = promo_group.get_discount_percent('period', period) if promo_group else 0
        devices_pct = promo_group.get_discount_percent('devices', period) if promo_group else 0
        offer_pct = get_user_active_promo_discount_percent(db_user) if db_user else 0

        discounted_base = PricingEngine.apply_discount(base_price, period_pct)
        discounted_devices = PricingEngine.apply_discount(devices_cost, devices_pct)
        subtotal = discounted_base + discounted_devices
        price = PricingEngine.apply_discount(subtotal, offer_pct)

        # Combined display discount
        total_original = base_price + devices_cost
        has_discount = price < total_original and total_original > 0
        if has_discount:
            combined_pct = round((1 - price / total_original) * 100)
            price_text = f'{format_price_kopeks(price)} 🔥−{combined_pct}%'
        else:
            price_text = format_price_kopeks(price)

        button_text = _period_button_text(tariff, period, price_text, texts)
        # subscription_id ОБЯЗАН быть первым сегментом: иначе резолвер по callback
        # принял бы хвостовой {period} за subscription_id (см. issue #3012 —
        # период совпадал с id чужой подписки и продлевалась не та подписка).
        buttons.append(
            [
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f'tariff_extend:{subscription_id}:{tariff.id}:{period}',
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_tariff_extend_confirm_keyboard(
    subscription_id: int,
    tariff_id: int,
    period: int,
    language: str,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру подтверждения продления по тарифу."""
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('TARIFF_RENEW_CONFIRM_BUTTON', '✅ Подтвердить продление'),
                    callback_data=f'tariff_ext_confirm:{subscription_id}:{tariff_id}:{period}',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='subscription_extend')],
        ]
    )


async def show_tariff_extend(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    subscription_id: int | None = None,
):
    """Показывает экран продления по текущему тарифу.

    ``subscription_id`` передаётся явно из кнопки «Назад» (где хвост callback —
    это tariff_id, а не id подписки). Если не задан — берём из ``se:{id}`` (хвост
    = реальный id подписки).
    """
    texts = get_texts(db_user.language)

    if settings.is_multi_tariff_enabled():
        sub_id = subscription_id
        if sub_id is None:
            parts = (callback.data or '').split(':')
            if len(parts) >= 2:
                try:
                    sub_id = int(parts[-1])
                except (ValueError, TypeError):
                    pass
        if sub_id:
            subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
        else:
            active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
            if len(active_subs) > 1:
                # Show subscription picker for extending
                keyboard = []
                for sub in sorted(active_subs, key=lambda s: s.id):
                    tariff_name = ''
                    if sub.tariff_id:
                        _t = await get_tariff_by_id(db, sub.tariff_id)
                        tariff_name = _t.name if _t else f'#{sub.id}'
                    else:
                        tariff_name = texts.t('TARIFF_PURCHASE_SUBSCRIPTION_FALLBACK', 'Подписка #{id}').format(
                            id=sub.id
                        )
                    days_left = max(0, (sub.end_date - datetime.now(UTC)).days) if sub.end_date else 0
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                text=texts.t('TARIFF_RENEW_SUB_BUTTON', '🔄 {name} ({days}д.)').format(
                                    name=tariff_name, days=days_left
                                ),
                                callback_data=f'se:{sub.id}',
                            )
                        ]
                    )
                keyboard.append(
                    [InlineKeyboardButton(text=texts.t('BACK_BUTTON', '◀️ Назад'), callback_data='back_to_menu')]
                )
                await callback.message.edit_text(
                    texts.t(
                        'TARIFF_RENEW_PICK_SUBSCRIPTION',
                        '🔄 <b>Продление подписки</b>\n\nВыберите подписку для продления:',
                    ),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
                    parse_mode='HTML',
                )
                await callback.answer()
                return
            if active_subs:
                subscription = active_subs[0]
            else:
                subscription = None
    else:
        subscription = await get_subscription_by_user_id(db, db_user.id)
    if not subscription:
        await callback.answer(texts.t('SUBSCRIPTION_NOT_FOUND', '❌ Подписка не найдена'), show_alert=True)
        return

    if not subscription.tariff_id:
        # Legacy user without tariff — show tariff selection for upgrade
        promo_group_id = getattr(db_user, 'promo_group_id', None)
        tariffs = await get_tariffs_for_user(db, promo_group_id)
        if not tariffs:
            await callback.answer(texts.t('TARIFF_PURCHASE_NO_TARIFFS_ALERT', 'Нет доступных тарифов'), show_alert=True)
            return

        keyboard = []
        for t in tariffs:
            if t.is_daily:
                continue
            keyboard.append([InlineKeyboardButton(text=f'📦 {t.name}', callback_data=f'tariff_select:{t.id}')])
        if not keyboard:
            await callback.answer(
                texts.t('TARIFF_RENEW_NO_TARIFFS', 'Нет доступных тарифов для продления'), show_alert=True
            )
            return
        keyboard.append([InlineKeyboardButton(text=texts.t('BACK_BUTTON', '◀️ Назад'), callback_data='back_to_menu')])

        await callback.message.edit_text(
            texts.t(
                'TARIFF_RENEW_CHOOSE_TARIFF',
                '🔄 <b>Выберите тариф для продления</b>\n\n'
                'Для продления подписки необходимо выбрать тариф.\n'
                'Подписка будет обновлена с параметрами выбранного тарифа.',
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    tariff = await get_tariff_by_id(db, subscription.tariff_id)
    if not tariff:
        await callback.answer(texts.t('TARIFF_PURCHASE_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    # Скрытый/неактивный тариф (например, триальный после промокода) —
    # показываем список доступных тарифов вместо продления скрытого
    if not tariff.is_active:
        promo_group_id = getattr(db_user, 'promo_group_id', None)
        tariffs = await get_tariffs_for_user(db, promo_group_id)
        active_tariffs = [t for t in tariffs if not t.is_daily]
        if not active_tariffs:
            await callback.answer(
                texts.t('TARIFF_RENEW_NO_TARIFFS', 'Нет доступных тарифов для продления'), show_alert=True
            )
            return

        keyboard = []
        for t in active_tariffs:
            keyboard.append([InlineKeyboardButton(text=f'📦 {t.name}', callback_data=f'tariff_select:{t.id}')])
        keyboard.append([InlineKeyboardButton(text=texts.t('BACK_BUTTON', '◀️ Назад'), callback_data='back_to_menu')])

        await callback.message.edit_text(
            texts.t(
                'TARIFF_RENEW_CHOOSE_TARIFF',
                '🔄 <b>Выберите тариф для продления</b>\n\n'
                'Для продления подписки необходимо выбрать тариф.\n'
                'Подписка будет обновлена с параметрами выбранного тарифа.',
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    traffic = format_traffic(tariff.traffic_limit_gb)

    # Проверяем есть ли у пользователя скидки по периодам
    promo_group = db_user.get_primary_promo_group() if hasattr(db_user, 'get_primary_promo_group') else None
    if promo_group is None:
        promo_group = getattr(db_user, 'promo_group', None)
    has_period_discounts = False
    if promo_group:
        period_discounts = getattr(promo_group, 'period_discounts', None)
        if period_discounts and isinstance(period_discounts, dict) and len(period_discounts) > 0:
            has_period_discounts = True

    discount_hint = ''
    if has_period_discounts:
        discount_hint = texts.t(
            'TARIFF_RENEW_PERIOD_DISCOUNTS_HINT', '\n🎁 <i>Скидки зависят от выбранного периода</i>'
        )

    actual_device_limit = subscription.device_limit or tariff.device_limit

    await callback.message.edit_text(
        texts.t(
            'TARIFF_RENEW_TITLE',
            '🔄 <b>Продление подписки</b>{discount_hint}\n\n'
            '📦 Тариф: <b>{name}</b>\n'
            '📊 Трафик: {traffic}\n'
            '📱 Устройств: {devices}\n\n'
            'Выберите период продления:',
        ).format(
            discount_hint=discount_hint,
            name=html.escape(tariff.name),
            traffic=traffic,
            devices=actual_device_limit,
        ),
        reply_markup=get_tariff_extend_keyboard(
            tariff,
            db_user.language,
            db_user=db_user,
            subscription_device_limit=actual_device_limit,
            subscription_id=subscription.id,
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def select_tariff_extend_period(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает выбор периода для продления."""
    texts = get_texts(db_user.language)
    # tariff_extend:{sub_id}:{tariff_id}[:{period}]
    parts = callback.data.split(':')
    sub_id = int(parts[1])
    tariff_id = int(parts[2])

    # Кнопка «Назад» шлёт tariff_extend:{sub_id}:{tariff_id} без периода — экран выбора периода
    if len(parts) < 4:
        await show_tariff_extend(callback, db_user, db, subscription_id=sub_id)
        return

    period = int(parts[3])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    # Грузим подписку СТРОГО по id из callback + user_id, без резолвера (он принял
    # бы хвостовой период за id подписки — см. issue #3012).
    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer(texts.t('SUBSCRIPTION_NOT_FOUND', '❌ Подписка не найдена'), show_alert=True)
        return
    actual_device_limit = subscription.device_limit or tariff.device_limit

    # Calculate price via PricingEngine (per-category discounts: period + devices)
    from app.services.pricing_engine import pricing_engine

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period,
        device_limit=actual_device_limit,
        user=db_user,
    )
    final_price = result.final_total
    original_price = result.original_total
    total_discount = result.promo_group_discount + result.promo_offer_discount
    discount_percent = (
        round((1 - final_price / original_price) * 100) if original_price > 0 and total_discount > 0 else 0
    )

    # Проверяем баланс
    user_balance = db_user.balance_kopeks or 0

    traffic = format_traffic(tariff.traffic_limit_gb)

    if user_balance >= final_price:
        discount_text = ''
        if discount_percent > 0:
            discount_text = texts.t(
                'TARIFF_PURCHASE_DISCOUNT_SHORT_LINE', '\n🎁 Скидка: {percent}% (-{amount})'
            ).format(percent=discount_percent, amount=format_price_kopeks(total_discount))

        await callback.message.edit_text(
            texts.t(
                'TARIFF_RENEW_CONFIRM',
                '✅ <b>Подтверждение продления</b>\n\n'
                '📦 Тариф: <b>{name}</b>\n'
                '📊 Трафик: {traffic}\n'
                '📱 Устройств: {devices}\n'
                '📅 Период: {period}\n'
                '{discount}\n'
                '💰 <b>К оплате: {total}</b>\n\n'
                '💳 Ваш баланс: {balance}\n'
                'После оплаты: {after}',
            ).format(
                name=html.escape(tariff.name),
                traffic=traffic,
                devices=actual_device_limit,
                period=format_period(period),
                discount=discount_text,
                total=format_price_kopeks(final_price),
                balance=format_price_kopeks(user_balance),
                after=format_price_kopeks(user_balance - final_price),
            ),
            reply_markup=get_tariff_extend_confirm_keyboard(subscription.id, tariff_id, period, db_user.language),
            parse_mode='HTML',
        )
    else:
        missing = final_price - user_balance

        # Сохраняем данные корзины для автопокупки после пополнения
        cart_data = {
            'cart_mode': 'extend',
            'tariff_id': tariff_id,
            'subscription_id': subscription.id if subscription else None,
            'period_days': period,
            'total_price': final_price,
            'user_id': db_user.id,
            'saved_cart': True,
            'missing_amount': missing,
            'return_to_cart': True,
            'description': f'Продление тарифа {tariff.name} на {period} дней',
            'traffic_limit_gb': tariff.traffic_limit_gb,
            'device_limit': actual_device_limit,
            'allowed_squads': tariff.allowed_squads or [],
            'discount_percent': discount_percent,
        }
        await user_cart_service.save_user_cart(db_user.id, cart_data)

        await callback.message.edit_text(
            texts.t(
                'TARIFF_PURCHASE_INSUFFICIENT',
                '❌ <b>Недостаточно средств</b>\n\n'
                '📦 Тариф: <b>{name}</b>\n'
                '📅 Период: {period}\n'
                '💰 Стоимость: {price}\n\n'
                '💳 Ваш баланс: {balance}\n'
                '⚠️ Не хватает: <b>{missing}</b>',
            ).format(
                name=html.escape(tariff.name),
                period=format_period(period),
                price=format_price_kopeks(final_price),
                balance=format_price_kopeks(user_balance),
                missing=format_price_kopeks(missing),
            )
            + texts.t(
                'TARIFF_RENEW_CART_SAVED_HINT',
                '\n\n🛒 <i>Корзина сохранена! После пополнения баланса подписка будет продлена автоматически.</i>',
            ),
            reply_markup=get_tariff_extend_insufficient_balance_keyboard(
                tariff_id,
                subscription.id if subscription else None,
                period,
                db_user.language,
                missing_kopeks=missing,
            ),
            parse_mode='HTML',
        )

    await state.update_data(
        extend_tariff_id=tariff_id,
        extend_period=period,
        extend_discount_percent=discount_percent,
        active_subscription_id=subscription.id if subscription else None,
    )
    await callback.answer()


@error_handler
async def confirm_tariff_extend(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Подтверждает продление по тарифу."""
    texts = get_texts(db_user.language)

    # tariff_ext_confirm:{sub_id}:{tariff_id}:{period}
    parts = callback.data.split(':')
    try:
        sub_id = int(parts[1])
        tariff_id = int(parts[2])
        period = int(parts[3])
    except (IndexError, ValueError):
        # Устаревшая/обрезанная кнопка (старый формат без subscription_id) — НЕ
        # списываем деньги, просим начать заново (issue #3012).
        await callback.answer(
            texts.t('TARIFF_RENEW_SESSION_EXPIRED', 'Сессия устарела, откройте продление заново'), show_alert=True
        )
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    # Validate period is available for this tariff
    if str(period) not in (tariff.period_prices or {}):
        await callback.answer(texts.t('TARIFF_PURCHASE_PERIOD_UNAVAILABLE', 'Период недоступен'), show_alert=True)
        return

    # Грузим подписку СТРОГО по id из callback + user_id (см. issue #3012: резолвер
    # по хвосту callback принимал период за id подписки и продлевал чужую).
    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer(texts.t('SUBSCRIPTION_NOT_FOUND', '❌ Подписка не найдена'), show_alert=True)
        return

    # Защита от рассинхрона до списания: продлеваемая подписка должна
    # соответствовать тарифу из callback.
    if subscription.tariff_id and subscription.tariff_id != tariff_id:
        logger.warning(
            'tariff_ext_confirm: подписка не соответствует тарифу из callback — отмена',
            subscription_id=sub_id,
            subscription_tariff_id=subscription.tariff_id,
            callback_tariff_id=tariff_id,
            user_id=db_user.id,
        )
        await callback.answer(
            texts.t('TARIFF_RENEW_MISMATCH', 'Несоответствие подписки и тарифа, откройте продление заново'),
            show_alert=True,
        )
        return

    actual_device_limit = subscription.device_limit or tariff.device_limit

    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    # Calculate price via PricingEngine (handles per-category discounts: period + devices)
    from app.services.pricing_engine import pricing_engine

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period,
        device_limit=actual_device_limit,
        user=db_user,
    )
    final_price = result.final_total
    consume_promo = result.promo_offer_discount > 0

    # Проверяем баланс
    user_balance = db_user.balance_kopeks or 0
    if final_price > 0 and user_balance < final_price:
        await callback.answer(
            texts.t('MINIAPP_PURCHASE_STATUS_INSUFFICIENT', 'Недостаточно средств на балансе'), show_alert=True
        )
        return

    # Отвечаем на callback СРАЗУ — до тяжёлых операций (панель, транзакции),
    # иначе Telegram инвалидирует query через 30 сек → TelegramBadRequest
    try:
        await callback.answer()
    except Exception:
        pass

    try:
        # Списываем баланс
        success = await subtract_user_balance(
            db,
            db_user,
            final_price,
            f'Продление тарифа {tariff.name} на {period} дней',
            consume_promo_offer=consume_promo,
            mark_as_paid_subscription=True,
        )
        if not success:
            try:
                await callback.message.edit_text(texts.t('TARIFF_PURCHASE_CHARGE_ERROR', '❌ Ошибка списания баланса'))
            except Exception:
                pass
            return

        # Запоминаем, был ли триал ДО продления
        was_trial = subscription.is_trial

        # Продлеваем подписку; для триала передаём tariff_id чтобы сбросить is_trial
        subscription = await extend_subscription(
            db,
            subscription,
            days=period,
            tariff_id=tariff.id if was_trial else None,
            traffic_limit_gb=tariff.traffic_limit_gb if was_trial else None,
            device_limit=actual_device_limit if was_trial else None,
        )

        # Обновляем пользователя в Remnawave
        try:
            subscription_service = SubscriptionService()
            if settings.is_multi_tariff_enabled():
                _should_create = not subscription.remnawave_id
            else:
                _should_create = not getattr(db_user, 'remnawave_id', None)

            if _should_create:
                await subscription_service.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT or was_trial,
                    reset_reason='конвертация триала' if was_trial else 'продление тарифа',
                )
            else:
                await subscription_service.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT or was_trial,
                    reset_reason='конвертация триала' if was_trial else 'продление тарифа',
                )
        except Exception as e:
            logger.error('Ошибка обновления Remnawave', error=e)
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=db_user.id,
                action='create',
            )

        # Создаем транзакцию
        await create_transaction(
            db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=final_price,
            description=f'Продление тарифа {tariff.name} на {period} дней',
        )

        # Отправляем уведомление админу
        try:
            admin_notification_service = AdminNotificationService(callback.bot)
            await admin_notification_service.send_subscription_purchase_notification(
                db,
                db_user,
                subscription,
                None,  # Транзакция отсутствует, оплата с баланса
                period,
                was_trial_conversion=was_trial,
                amount_kopeks=final_price,
                purchase_type='renewal',
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления админу', error=e)

        # Очищаем корзину после успешной покупки (per-subscription в multi-tariff)
        try:
            _cart_sub_id = getattr(subscription, 'id', None) if subscription else None
            if _cart_sub_id and settings.is_multi_tariff_enabled():
                await user_cart_service.delete_subscription_cart(db_user.id, _cart_sub_id)
            else:
                await user_cart_service.delete_user_cart(db_user.id)
            logger.info('Корзина очищена после продления тарифа для пользователя', telegram_id=db_user.telegram_id)
        except Exception as e:
            logger.error('Ошибка очистки корзины', error=e)

        await state.clear()

        traffic = format_traffic(tariff.traffic_limit_gb)

        await callback.message.edit_text(
            texts.t(
                'TARIFF_RENEW_SUCCESS',
                '🎉 <b>Подписка успешно продлена!</b>\n\n'
                '📦 Тариф: <b>{name}</b>\n'
                '📊 Трафик: {traffic}\n'
                '📱 Устройств: {devices}\n'
                '📅 Добавлено: {period}\n'
                '💰 Списано: {price}',
            ).format(
                name=html.escape(tariff.name),
                traffic=traffic,
                devices=actual_device_limit,
                period=format_period(period),
                price=format_price_kopeks(final_price),
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                            callback_data=f'sm:{subscription.id}'
                            if settings.is_multi_tariff_enabled() and subscription
                            else 'menu_subscription',
                        )
                    ],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
                ]
            ),
            parse_mode='HTML',
        )
    except Exception as e:
        logger.error('Ошибка при продлении тарифа', error=e, exc_info=True)
        try:
            await callback.message.edit_text(
                texts.t('TARIFF_RENEW_ERROR', '❌ Произошла ошибка при продлении подписки')
            )
        except Exception:
            pass


# ==================== Переключение тарифов ====================


def format_tariff_switch_list_text(
    tariffs: list[Tariff],
    current_tariff_id: int | None,
    current_tariff_name: str,
    db_user: User | None = None,
    has_period_discounts: bool = False,
) -> str:
    """Форматирует текст со списком тарифов для переключения."""
    texts = get_texts(db_user.language if db_user else 'ru')
    lines = [
        texts.t('TARIFF_SWITCH_LIST_TITLE', '📦 <b>Смена тарифа</b>'),
        texts.t('TARIFF_SWITCH_CURRENT_LINE', '📌 Текущий: <b>{name}</b>').format(name=current_tariff_name),
    ]

    if has_period_discounts:
        lines.append(texts.t('TARIFF_PURCHASE_PERIOD_DISCOUNTS_HINT', '🎁 <i>Скидки по периодам</i>'))

    lines.append('')
    lines.append(texts.t('TARIFF_SWITCH_FULL_PRICE_NOTE', '⚠️ Оплачивается полная стоимость.'))
    lines.append('')

    for tariff in tariffs:
        if tariff.id == current_tariff_id:
            continue

        traffic_gb = tariff.traffic_limit_gb
        traffic = (
            '∞' if traffic_gb == 0 else texts.t('TARIFF_PURCHASE_TRAFFIC_GB', '{traffic} ГБ').format(traffic=traffic_gb)
        )

        # Проверяем суточный ли тариф
        is_daily = getattr(tariff, 'is_daily', False)
        price_text = ''
        discount_icon = ''

        if is_daily:
            # Для суточных тарифов показываем цену за день с учётом скидки промогруппы
            daily_price = getattr(tariff, 'daily_price_kopeks', 0)
            if db_user:
                group_pct, offer_pct, daily_discount = _get_user_period_discount(db_user, 1)
                if daily_discount > 0:
                    daily_price = _apply_promo_discount(daily_price, group_pct, offer_pct)
                    discount_icon = '🔥'
            price_text = texts.t('TARIFF_PURCHASE_PRICE_PER_DAY', '🔄 {price}/день{icon}').format(
                price=format_price_kopeks(daily_price, compact=True), icon=discount_icon
            )
        else:
            prices = tariff.period_prices or {}
            if prices:
                min_period = min(prices.keys(), key=int)
                min_price = prices[min_period]
                group_pct, offer_pct, discount_percent = 0, 0, 0
                if db_user:
                    group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, int(min_period))
                if discount_percent > 0:
                    min_price = _apply_promo_discount(min_price, group_pct, offer_pct)
                    discount_icon = '🔥'
                price_text = texts.t('TARIFF_PURCHASE_PRICE_FROM', 'от {price}{icon}').format(
                    price=format_price_kopeks(min_price, compact=True), icon=discount_icon
                )

        lines.append(f'<b>{html.escape(tariff.name)}</b> — {traffic} / {tariff.device_limit} 📱 {price_text}')

        if tariff.description:
            lines.append(f'<i>{html.escape(tariff.description)}</i>')

        lines.append('')

    return '\n'.join(lines)


def get_tariff_switch_keyboard(
    tariffs: list[Tariff],
    current_tariff_id: int | None,
    language: str,
) -> InlineKeyboardMarkup:
    """Создает компактную клавиатуру выбора тарифа для переключения."""
    texts = get_texts(language)
    buttons = []

    for tariff in tariffs:
        if tariff.id == current_tariff_id:
            continue

        buttons.append([InlineKeyboardButton(text=tariff.name, callback_data=f'tariff_sw_select:{tariff.id}')])

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_tariff_switch_periods_keyboard(
    tariff: Tariff,
    language: str,
    db_user: User | None = None,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру выбора периода для переключения тарифа с учетом скидок по периодам."""
    texts = get_texts(language)
    buttons = []

    prices = tariff.period_prices or {}
    for period_str in sorted(prices.keys(), key=int):
        period = int(period_str)
        price = prices[period_str]

        # Получаем скидку для конкретного периода
        group_pct, offer_pct, discount_percent = 0, 0, 0
        if db_user:
            group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, period)

        if discount_percent > 0:
            price = _apply_promo_discount(price, group_pct, offer_pct)
            price_text = f'{format_price_kopeks(price)} 🔥−{discount_percent}%'
        else:
            price_text = format_price_kopeks(price)

        button_text = _period_button_text(tariff, period, price_text, texts)
        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f'tariff_sw_period:{tariff.id}:{period}')])

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='tariff_switch')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_tariff_switch_confirm_keyboard(
    tariff_id: int,
    period: int,
    language: str,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру подтверждения переключения тарифа."""
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('TARIFF_SWITCH_CONFIRM_BUTTON', '✅ Подтвердить переключение'),
                    callback_data=f'tariff_sw_confirm:{tariff_id}:{period}',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data=f'tariff_sw_select:{tariff_id}')],
        ]
    )


def get_tariff_switch_insufficient_balance_keyboard(
    tariff_id: int,
    period: int,
    language: str,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру при недостаточном балансе для переключения."""
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'), callback_data='balance_topup')],
            [InlineKeyboardButton(text=texts.BACK, callback_data=f'tariff_sw_select:{tariff_id}')],
        ]
    )


@error_handler
async def show_tariff_switch_list(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Показывает список тарифов для переключения."""
    texts = get_texts(db_user.language)
    await state.clear()

    # Проверяем наличие активной подписки
    subscription, _sub_id = await _resolve_switch_subscription(callback, db_user, db, state)
    if not subscription:
        return

    # Истёкшая подписка: смены тарифа нет, предлагаем купить новый тариф с нуля
    # (раньше кнопка «Тариф» вела сюда в тупик на истёкшей подписке).
    if not subscription.end_date or subscription.end_date <= datetime.now(UTC):
        await callback.message.edit_text(
            texts.t(
                'TARIFF_SWITCH_EXPIRED',
                '❌ <b>Переключение недоступно</b>\n\n'
                'У вашей подписки не осталось активных дней.\n'
                'Оформите новый тариф с нуля.',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('BUY_TARIFF_BUTTON', '📦 Купить тариф'), callback_data='menu_buy'
                        )
                    ],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')],
                ]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    current_tariff_id = subscription.tariff_id

    # Проверяем, разрешена ли смена тарифа хотя бы в одном направлении
    if not settings.TARIFF_SWITCH_UPGRADE_ENABLED and not settings.TARIFF_SWITCH_DOWNGRADE_ENABLED:
        await callback.message.edit_text(
            texts.t(
                'TARIFF_SWITCH_DISABLED',
                '🚫 <b>Смена тарифа недоступна</b>\n\nАдминистратор отключил возможность смены тарифа.',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')]]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Получаем доступные тарифы
    promo_group_id = getattr(db_user, 'promo_group_id', None)
    tariffs = await get_tariffs_for_user(db, promo_group_id)

    # Filter out ALL tariffs user already has active subscriptions for
    if settings.is_multi_tariff_enabled():
        _all_active = await get_active_subscriptions_by_user_id(db, db_user.id)
        _purchased_ids = {s.tariff_id for s in _all_active if s.tariff_id}
        available_tariffs = [t for t in tariffs if t.id not in _purchased_ids]
    else:
        available_tariffs = [t for t in tariffs if t.id != current_tariff_id]

    # Фильтруем по разрешённым направлениям (upgrade/downgrade)
    current_tariff = await get_tariff_by_id(db, current_tariff_id) if current_tariff_id else None
    if current_tariff:
        remaining_days = remaining_days_for_switch(subscription.end_date)
        available_tariffs = _filter_tariffs_by_switch_direction(
            available_tariffs, current_tariff, remaining_days, db_user
        )

    if not available_tariffs:
        await callback.message.edit_text(
            texts.t(
                'TARIFF_SWITCH_NO_TARIFFS',
                '😔 <b>Нет доступных тарифов для переключения</b>\n\nВы уже используете единственный доступный тариф.',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')]]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Получаем текущий тариф для отображения
    current_tariff_name = texts.t('SUBSCRIPTION_STATUS_UNKNOWN', 'Неизвестно')
    if current_tariff_id:
        current_tariff = await get_tariff_by_id(db, current_tariff_id)
        if current_tariff:
            current_tariff_name = html.escape(current_tariff.name)

    # Проверяем есть ли у пользователя скидки по периодам
    promo_group = db_user.get_primary_promo_group() if hasattr(db_user, 'get_primary_promo_group') else None
    if promo_group is None:
        promo_group = getattr(db_user, 'promo_group', None)
    has_period_discounts = False
    if promo_group:
        period_discounts = getattr(promo_group, 'period_discounts', None)
        if period_discounts and isinstance(period_discounts, dict) and len(period_discounts) > 0:
            has_period_discounts = True

    # Формируем текст со списком тарифов
    switch_text = format_tariff_switch_list_text(
        available_tariffs, current_tariff_id, current_tariff_name, db_user, has_period_discounts
    )

    await callback.message.edit_text(
        switch_text,
        reply_markup=get_tariff_switch_keyboard(available_tariffs, current_tariff_id, db_user.language),
        parse_mode='HTML',
    )

    await state.update_data(
        current_tariff_id=current_tariff_id,
        active_subscription_id=subscription.id,
    )
    await callback.answer()


@error_handler
async def select_tariff_switch(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает выбор тарифа для переключения."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    # Проверяем разрешение на смену в данном направлении
    current_subscription_sw, _sw_sub_id_check = await _resolve_switch_subscription(callback, db_user, db, state)
    if current_subscription_sw and current_subscription_sw.tariff_id:
        cur_tariff_sw = await get_tariff_by_id(db, current_subscription_sw.tariff_id)
        if cur_tariff_sw:
            rem_days = remaining_days_for_switch(current_subscription_sw.end_date)
            _, is_up = _calculate_instant_switch_cost(cur_tariff_sw, tariff, rem_days, db_user)
            if is_up and not settings.TARIFF_SWITCH_UPGRADE_ENABLED:
                await callback.answer(
                    texts.t('TARIFF_SWITCH_UPGRADE_DISABLED', 'Повышение тарифа недоступно'), show_alert=True
                )
                return
            if not is_up and not settings.TARIFF_SWITCH_DOWNGRADE_ENABLED:
                await callback.answer(
                    texts.t('TARIFF_SWITCH_DOWNGRADE_DISABLED', 'Понижение тарифа недоступно'), show_alert=True
                )
                return

    traffic = format_traffic(tariff.traffic_limit_gb)

    # Проверяем, суточный ли это тариф
    is_daily = getattr(tariff, 'is_daily', False)

    if is_daily:
        # Для суточного тарифа показываем подтверждение без выбора периода
        raw_daily_price = getattr(tariff, 'daily_price_kopeks', 0)
        group_pct, offer_pct, daily_discount = _get_user_period_discount(db_user, 1)
        daily_price = (
            _apply_promo_discount(raw_daily_price, group_pct, offer_pct) if daily_discount > 0 else raw_daily_price
        )
        discount_text = (
            texts.t('TARIFF_PURCHASE_DAILY_DISCOUNT_LINE', '\n💎 Скидка: {percent}%').format(percent=daily_discount)
            if daily_discount > 0
            else ''
        )
        user_balance = db_user.balance_kopeks or 0

        # Проверяем текущую подписку на оставшиеся дни (switched FROM, not TO)
        current_subscription, _sw_sub_id = await _resolve_switch_subscription(callback, db_user, db, state)
        days_warning = ''
        if current_subscription and current_subscription.end_date:
            # Предупреждение показывает ПОЛНЫЕ оставшиеся дни, а не расчётный остаток:
            # «осталось 0 дн.» в тексте про потерю дней смысла не имеет.
            whole_days_left = max(0, (current_subscription.end_date - datetime.now(UTC)).days)
            if whole_days_left > 1:
                days_warning = '\n\n' + texts.t(
                    'DAILY_SWITCH_WARNING',
                    '⚠️ <b>Внимание!</b> У вас осталось {days} дн. подписки.\nПри смене на суточный тариф они будут утеряны!',
                ).format(days=whole_days_left)

        if user_balance >= daily_price:
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_SWITCH_DAILY_CONFIRM',
                    '✅ <b>Подтверждение смены тарифа</b>\n\n'
                    '📦 Новый тариф: <b>{name}</b>\n'
                    '📊 Трафик: {traffic}\n'
                    '📱 Устройств: {devices}\n'
                    '🔄 Тип: <b>Суточный</b>\n\n'
                    '💰 <b>Цена: {price}/день</b>{discount}\n\n'
                    '💳 Ваш баланс: {balance}{warning}\n\n'
                    'ℹ️ Средства будут списываться автоматически раз в сутки.\n'
                    'Вы можете приостановить подписку в любой момент.',
                ).format(
                    name=html.escape(tariff.name),
                    traffic=traffic,
                    devices=tariff.device_limit,
                    price=format_price_kopeks(daily_price),
                    discount=discount_text,
                    balance=format_price_kopeks(user_balance),
                    warning=days_warning,
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('TARIFF_SWITCH_CONFIRM_BUTTON', '✅ Подтвердить переключение'),
                                callback_data=f'daily_tariff_switch_confirm:{tariff_id}',
                            )
                        ],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='tariff_switch')],
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            missing = daily_price - user_balance
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_PURCHASE_DAILY_INSUFFICIENT',
                    '❌ <b>Недостаточно средств</b>\n\n'
                    '📦 Тариф: <b>{name}</b>\n'
                    '🔄 Тип: Суточный\n'
                    '💰 Цена: {price}/день{discount}\n\n'
                    '💳 Ваш баланс: {balance}\n'
                    '⚠️ Не хватает: <b>{missing}</b>',
                ).format(
                    name=html.escape(tariff.name),
                    price=format_price_kopeks(daily_price),
                    discount=discount_text,
                    balance=format_price_kopeks(user_balance),
                    missing=format_price_kopeks(missing),
                )
                + days_warning,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'), callback_data='balance_topup'
                            )
                        ],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='tariff_switch')],
                    ]
                ),
                parse_mode='HTML',
            )
    else:
        # Для обычного тарифа показываем выбор периода
        info_text = texts.t(
            'TARIFF_SWITCH_NEW_TARIFF_INFO',
            '📦 <b>{name}</b>\n\n<b>Параметры нового тарифа:</b>\n• Трафик: {traffic}\n• Устройств: {devices}\n',
        ).format(name=html.escape(tariff.name), traffic=traffic, devices=tariff.device_limit)

        if tariff.description:
            info_text += f'\n📝 {html.escape(tariff.description)}\n'

        info_text += texts.t(
            'TARIFF_SWITCH_FULL_PRICE_CHOOSE_PERIOD', '\n⚠️ Оплачивается полная стоимость тарифа.\nВыберите период:'
        )

        await callback.message.edit_text(
            info_text,
            reply_markup=get_tariff_switch_periods_keyboard(tariff, db_user.language, db_user=db_user),
            parse_mode='HTML',
        )

    await state.update_data(switch_tariff_id=tariff_id)
    await callback.answer()


@error_handler
async def select_tariff_switch_period(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает выбор периода для переключения тарифа."""
    texts = get_texts(db_user.language)

    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    period = int(parts[2])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    data = await state.get_data()
    current_tariff_id = data.get('current_tariff_id')

    # Calculate price via PricingEngine (per-category discounts: period + devices for new tariff)
    from app.services.pricing_engine import pricing_engine

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period,
        device_limit=tariff.device_limit or 0,
        user=db_user,
    )
    final_price = result.final_total
    original_price = result.original_total
    total_discount = result.promo_group_discount + result.promo_offer_discount
    discount_percent = (
        round((1 - final_price / original_price) * 100) if original_price > 0 and total_discount > 0 else 0
    )

    # Проверяем баланс
    user_balance = db_user.balance_kopeks or 0

    traffic = format_traffic(tariff.traffic_limit_gb)

    # Получаем текущий тариф для отображения
    current_tariff_name = texts.t('SUBSCRIPTION_STATUS_UNKNOWN', 'Неизвестно')
    if current_tariff_id:
        current_tariff = await get_tariff_by_id(db, current_tariff_id)
        if current_tariff:
            current_tariff_name = html.escape(current_tariff.name)

    # Получаем текущую подписку (switched FROM, not TO) для расчёта оставшегося времени
    subscription, _sw_period_sub_id = await _resolve_switch_subscription(callback, db_user, db, state)
    if subscription and subscription.end_date:
        max(0, (subscription.end_date - datetime.now(UTC)).days)

    # При смене тарифа устанавливается ровно оплаченный период
    time_info = texts.t('TARIFF_SWITCH_WILL_SET_LINE', '⏰ Будет установлено: {days} дней').format(days=period)

    if user_balance >= final_price:
        discount_text = ''
        if discount_percent > 0:
            discount_text = texts.t(
                'TARIFF_PURCHASE_DISCOUNT_SHORT_LINE', '\n🎁 Скидка: {percent}% (-{amount})'
            ).format(percent=discount_percent, amount=format_price_kopeks(total_discount))

        await callback.message.edit_text(
            texts.t(
                'TARIFF_SWITCH_CONFIRM',
                '✅ <b>Подтверждение переключения тарифа</b>\n\n'
                '📌 Текущий тариф: <b>{current}</b>\n'
                '📦 Новый тариф: <b>{name}</b>\n'
                '📊 Трафик: {traffic}\n'
                '📱 Устройств: {devices}\n'
                '{time_info}\n'
                '{discount}\n'
                '💰 <b>К оплате: {total}</b>\n\n'
                '💳 Ваш баланс: {balance}\n'
                'После оплаты: {after}',
            ).format(
                current=current_tariff_name,
                name=html.escape(tariff.name),
                traffic=traffic,
                devices=tariff.device_limit,
                time_info=time_info,
                discount=discount_text,
                total=format_price_kopeks(final_price),
                balance=format_price_kopeks(user_balance),
                after=format_price_kopeks(user_balance - final_price),
            ),
            reply_markup=get_tariff_switch_confirm_keyboard(tariff_id, period, db_user.language),
            parse_mode='HTML',
        )
    else:
        missing = final_price - user_balance
        await callback.message.edit_text(
            texts.t(
                'TARIFF_PURCHASE_INSUFFICIENT',
                '❌ <b>Недостаточно средств</b>\n\n'
                '📦 Тариф: <b>{name}</b>\n'
                '📅 Период: {period}\n'
                '💰 Стоимость: {price}\n\n'
                '💳 Ваш баланс: {balance}\n'
                '⚠️ Не хватает: <b>{missing}</b>',
            ).format(
                name=html.escape(tariff.name),
                period=format_period(period),
                price=format_price_kopeks(final_price),
                balance=format_price_kopeks(user_balance),
                missing=format_price_kopeks(missing),
            ),
            reply_markup=get_tariff_switch_insufficient_balance_keyboard(tariff_id, period, db_user.language),
            parse_mode='HTML',
        )

    await state.update_data(
        switch_tariff_id=tariff_id,
        switch_period=period,
        switch_final_price=final_price,
    )
    await callback.answer()


@error_handler
async def confirm_tariff_switch(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Подтверждает переключение тарифа."""
    texts = get_texts(db_user.language)
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    period = int(parts[2])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    # Validate period is available for this tariff
    if str(period) not in (tariff.period_prices or {}):
        await callback.answer(texts.t('TARIFF_PURCHASE_PERIOD_UNAVAILABLE', 'Период недоступен'), show_alert=True)
        return

    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    # Проверяем наличие подписки (switched FROM — resolved via FSM state)
    subscription, _sw_confirm_sub_id = await _resolve_switch_subscription(callback, db_user, db, state)
    if not subscription:
        await callback.answer(texts.t('NO_SUBSCRIPTION_ERROR', '❌ У вас нет активной подписки'), show_alert=True)
        return

    # Проверяем разрешение на смену в данном направлении
    if subscription.tariff_id and subscription.tariff_id != tariff_id:
        cur_tariff_obj = await get_tariff_by_id(db, subscription.tariff_id)
        if cur_tariff_obj:
            rem_days = remaining_days_for_switch(subscription.end_date)
            _, is_up = _calculate_instant_switch_cost(cur_tariff_obj, tariff, rem_days, db_user)
            if is_up and not settings.TARIFF_SWITCH_UPGRADE_ENABLED:
                await callback.answer(
                    texts.t('TARIFF_SWITCH_UPGRADE_DISABLED', 'Повышение тарифа недоступно'), show_alert=True
                )
                return
            if not is_up and not settings.TARIFF_SWITCH_DOWNGRADE_ENABLED:
                await callback.answer(
                    texts.t('TARIFF_SWITCH_DOWNGRADE_DISABLED', 'Понижение тарифа недоступно'), show_alert=True
                )
                return

    # Calculate price via PricingEngine (handles per-category discounts + extra devices)
    from app.services.pricing_engine import pricing_engine

    # New tariff device_limit applies on switch (extra devices not transferred)
    effective_device_limit = tariff.device_limit or 0
    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period,
        device_limit=effective_device_limit,
        user=db_user,
    )
    final_price = result.final_total
    consume_promo = result.promo_offer_discount > 0

    # Проверяем баланс
    user_balance = db_user.balance_kopeks or 0
    if final_price > 0 and user_balance < final_price:
        await callback.answer(
            texts.t('MINIAPP_PURCHASE_STATUS_INSUFFICIENT', 'Недостаточно средств на балансе'), show_alert=True
        )
        return

    # Отвечаем на callback СРАЗУ — до тяжёлых операций (панель, транзакции),
    # иначе Telegram инвалидирует query через 30 сек → TelegramBadRequest
    try:
        await callback.answer()
    except Exception:
        pass

    try:
        # Списываем баланс
        success = await subtract_user_balance(
            db,
            db_user,
            final_price,
            f'Смена тарифа на {tariff.name} ({period} дней)',
            consume_promo_offer=consume_promo,
            mark_as_paid_subscription=True,
        )
        if not success:
            try:
                await callback.message.edit_text(texts.t('TARIFF_PURCHASE_CHARGE_ERROR', '❌ Ошибка списания баланса'))
            except Exception:
                pass
            return

        # Получаем список серверов из тарифа
        squads = tariff.allowed_squads or []

        # Если allowed_squads пустой - значит "все серверы", получаем их
        if not squads:
            from app.database.crud.server_squad import get_all_server_squads

            all_servers, _ = await get_all_server_squads(db, available_only=True)
            squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

        # При смене тарифа пользователь получает оплаченный период + оставшиеся дни
        # (остаток добавляется в extend_subscription автоматически)
        days_for_new_tariff = period

        # Обновляем подписку с новыми параметрами тарифа
        # Сохраняем докупленные устройства при продлении того же тарифа
        if subscription.tariff_id == tariff.id:
            effective_device_limit = max(tariff.device_limit or 0, subscription.device_limit or 0)
        else:
            effective_device_limit = tariff.device_limit
        subscription = await extend_subscription(
            db,
            subscription,
            days=days_for_new_tariff,  # Даем ровно оплаченный период
            tariff_id=tariff.id,
            traffic_limit_gb=tariff.traffic_limit_gb,
            device_limit=effective_device_limit,
            connected_squads=squads,
        )

        # Обновляем пользователя в Remnawave
        try:
            subscription_service = SubscriptionService()
            if settings.is_multi_tariff_enabled():
                _should_create = not subscription.remnawave_id
            else:
                _should_create = not getattr(db_user, 'remnawave_id', None)

            reset_used_traffic = should_reset_used_traffic(final_price)
            if _should_create:
                await subscription_service.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=reset_used_traffic,
                    reset_reason='переключение тарифа',
                )
            else:
                await subscription_service.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=reset_used_traffic,
                    reset_reason='переключение тарифа',
                )
        except Exception as e:
            logger.error('Ошибка обновления Remnawave при переключении тарифа', error=e)
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=db_user.id,
                action='create',
            )

        # Гарантированный сброс устройств при смене тарифа
        await db.refresh(db_user)
        _reset_panel_id = (
            subscription.remnawave_id
            if settings.is_multi_tariff_enabled() and subscription.remnawave_id
            else db_user.remnawave_id
        )
        if settings.is_multi_tariff_enabled() and not getattr(subscription, 'remnawave_id', None):
            logger.warning(
                'Multi-tariff: subscription missing remnawave_id, using user fallback',
                subscription_id=getattr(subscription, 'id', None),
            )
        if _reset_panel_id:
            try:
                from app.services.remnawave_service import RemnaWaveService

                service = RemnaWaveService()
                async with service.get_api_client() as api:
                    # reset_user_devices больше не бросает при отказе панели —
                    # он ловит ошибку внутри и возвращает False. Без проверки
                    # результата лог утверждал бы, что сброс прошёл, когда он
                    # провалился, и старые HWID остались бы за новым лимитом.
                    if await api.reset_user_devices(_reset_panel_id):
                        logger.info('🔧 Сброшены устройства при смене тарифа для user_id', db_user_id=db_user.id)
                    else:
                        logger.error('Не удалось сбросить устройства при смене тарифа', db_user_id=db_user.id)
            except Exception as e:
                logger.error('Ошибка сброса устройств при смене тарифа', error=e)

        # Создаем транзакцию
        await create_transaction(
            db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=final_price,
            description=f'Смена тарифа на {tariff.name}',
        )

        # Отправляем уведомление админу
        try:
            admin_notification_service = AdminNotificationService(callback.bot)
            await admin_notification_service.send_subscription_purchase_notification(
                db,
                db_user,
                subscription,
                None,  # Транзакция отсутствует, оплата с баланса
                days_for_new_tariff,  # Итоговый срок подписки
                was_trial_conversion=False,
                amount_kopeks=final_price,
                purchase_type='tariff_switch',
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления админу', error=e)

        # Очищаем корзину после успешной покупки (per-subscription в multi-tariff)
        try:
            _cart_sub_id = getattr(subscription, 'id', None) if subscription else None
            if _cart_sub_id and settings.is_multi_tariff_enabled():
                await user_cart_service.delete_subscription_cart(db_user.id, _cart_sub_id)
            else:
                await user_cart_service.delete_user_cart(db_user.id)
            logger.info('Корзина очищена после смены тарифа для пользователя', telegram_id=db_user.telegram_id)
        except Exception as e:
            logger.error('Ошибка очистки корзины', error=e)

        await state.clear()

        traffic = format_traffic(tariff.traffic_limit_gb)

        # При смене тарифа устанавливается оплаченный период
        time_info = texts.t('TARIFF_SWITCH_PERIOD_LINE', '📅 Период: {days} дней').format(days=days_for_new_tariff)

        await callback.message.edit_text(
            texts.t(
                'TARIFF_SWITCH_SUCCESS',
                '🎉 <b>Тариф успешно изменён!</b>\n\n'
                '📦 Новый тариф: <b>{name}</b>\n'
                '📊 Трафик: {traffic}\n'
                '📱 Устройств: {devices}\n'
                '💰 Списано: {price}\n'
                '{time_info}\n\n'
                'Перейдите в раздел «Подписка» для просмотра деталей.',
            ).format(
                name=html.escape(tariff.name),
                traffic=traffic,
                devices=tariff.device_limit,
                price=format_price_kopeks(final_price),
                time_info=time_info,
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                            callback_data=f'sm:{subscription.id}'
                            if settings.is_multi_tariff_enabled() and subscription
                            else 'menu_subscription',
                        )
                    ],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
                ]
            ),
            parse_mode='HTML',
        )

    except Exception as e:
        logger.error('Ошибка при переключении тарифа', error=e, exc_info=True)
        try:
            await callback.message.edit_text(
                texts.t('TARIFF_SWITCH_ERROR', '❌ Произошла ошибка при переключении тарифа')
            )
        except Exception:
            pass


# ==================== Смена на суточный тариф ====================


@error_handler
async def confirm_daily_tariff_switch(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Подтверждает смену на суточный тариф."""
    texts = get_texts(db_user.language)

    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    is_daily = getattr(tariff, 'is_daily', False)
    if not is_daily:
        await callback.answer(texts.t('TARIFF_PURCHASE_NOT_DAILY', 'Это не суточный тариф'), show_alert=True)
        return

    daily_price = getattr(tariff, 'daily_price_kopeks', 0)
    if daily_price <= 0:
        await callback.answer(texts.t('TARIFF_PURCHASE_INVALID_PRICE', 'Некорректная цена тарифа'), show_alert=True)
        return

    # Lock user BEFORE price computation to prevent TOCTOU on promo offer
    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    # Apply group + promo-offer discounts via PricingEngine (single source of truth)
    from app.services.pricing_engine import pricing_engine

    pricing_result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period_days=1,
        device_limit=tariff.device_limit,
        user=db_user,
    )
    final_daily_price = pricing_result.final_total
    consume_promo = pricing_result.breakdown.get('offer_discount_pct', 0) > 0

    # Проверяем баланс (user already locked, balance is fresh)
    user_balance = db_user.balance_kopeks or 0
    if final_daily_price > 0 and user_balance < final_daily_price:
        await callback.answer(
            texts.t('MINIAPP_PURCHASE_STATUS_INSUFFICIENT', 'Недостаточно средств на балансе'), show_alert=True
        )
        return

    # Проверяем наличие подписки — ищем подписку FROM (текущую), не TO (новый тариф)
    subscription, _sub_id = await _resolve_switch_subscription(callback, db_user, db, state)
    if not subscription:
        await callback.answer(texts.t('NO_SUBSCRIPTION_ERROR', '❌ У вас нет активной подписки'), show_alert=True)
        return

    # Проверяем разрешение на смену в данном направлении
    if subscription.tariff_id and subscription.tariff_id != tariff_id:
        cur_tariff_daily = await get_tariff_by_id(db, subscription.tariff_id)
        if cur_tariff_daily:
            rem_days = remaining_days_for_switch(subscription.end_date)
            _, is_up = _calculate_instant_switch_cost(cur_tariff_daily, tariff, rem_days, db_user)
            if is_up and not settings.TARIFF_SWITCH_UPGRADE_ENABLED:
                await callback.answer(
                    texts.t('TARIFF_SWITCH_UPGRADE_DISABLED', 'Повышение тарифа недоступно'), show_alert=True
                )
                return
            if not is_up and not settings.TARIFF_SWITCH_DOWNGRADE_ENABLED:
                await callback.answer(
                    texts.t('TARIFF_SWITCH_DOWNGRADE_DISABLED', 'Понижение тарифа недоступно'), show_alert=True
                )
                return

    # Отвечаем на callback СРАЗУ — до тяжёлых операций (панель, транзакции),
    # иначе Telegram инвалидирует query через 30 сек → TelegramBadRequest
    try:
        await callback.answer()
    except Exception:
        pass

    try:
        # Списываем первый день сразу
        success = await subtract_user_balance(
            db,
            db_user,
            final_daily_price,
            f'Смена на суточный тариф {tariff.name} (первый день)',
            consume_promo_offer=consume_promo,
            mark_as_paid_subscription=True,
        )
        if not success:
            try:
                await callback.message.edit_text(texts.t('TARIFF_PURCHASE_CHARGE_ERROR', '❌ Ошибка списания баланса'))
            except Exception:
                pass
            return

        # Получаем список серверов из тарифа
        squads = tariff.allowed_squads or []

        # Если allowed_squads пустой - значит "все серверы", получаем их
        if not squads:
            from app.database.crud.server_squad import get_all_server_squads

            all_servers, _ = await get_all_server_squads(db, available_only=True)
            squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

        # Обновляем подписку на суточный тариф
        # Сбрасываем лимит устройств на базу нового тарифа (докупленные не переносятся)
        from app.database.crud.subscription import calc_device_limit_on_tariff_switch

        old_tariff = await get_tariff_by_id(db, subscription.tariff_id) if subscription.tariff_id else None
        subscription.tariff_id = tariff.id
        subscription.traffic_limit_gb = tariff.traffic_limit_gb
        subscription.device_limit = calc_device_limit_on_tariff_switch(
            current_device_limit=subscription.device_limit,
            old_tariff_device_limit=old_tariff.device_limit if old_tariff else None,
            new_tariff_device_limit=tariff.device_limit,
            max_device_limit=getattr(tariff, 'max_device_limit', None),
        )
        subscription.connected_squads = squads
        subscription.status = 'active'
        subscription.is_trial = False  # Сбрасываем триальный статус
        subscription.is_daily_paused = False
        subscription.last_daily_charge_at = datetime.now(UTC)
        # Для суточного тарифа ставим срок на 1 день
        subscription.end_date = datetime.now(UTC) + timedelta(days=1)

        # Сбрасываем докупленный трафик при смене тарифа
        from sqlalchemy import delete as sql_delete

        from app.database.models import TrafficPurchase

        await db.execute(sql_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
        subscription.purchased_traffic_gb = 0
        subscription.traffic_reset_at = None

        # Счётчик трафика обнуляет только ОПЛАЧЕННОЕ переключение (см. tariff_switch_policy).
        reset_used_traffic = should_reset_used_traffic(final_daily_price)
        if reset_used_traffic:
            subscription.traffic_used_gb = 0.0

        await db.commit()
        await db.refresh(subscription)

        # Обновляем пользователя в Remnawave (сброс трафика по админ-настройке)
        try:
            subscription_service = SubscriptionService()
            if settings.is_multi_tariff_enabled():
                _should_create = not subscription.remnawave_id
            else:
                _should_create = not getattr(db_user, 'remnawave_id', None)

            if _should_create:
                await subscription_service.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=reset_used_traffic,
                    reset_reason='смена на суточный тариф',
                )
            else:
                await subscription_service.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=reset_used_traffic,
                    reset_reason='смена на суточный тариф',
                )
        except Exception as e:
            logger.error('Ошибка обновления Remnawave', error=e)
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=db_user.id,
                action='create',
            )

        # Гарантированный сброс устройств при смене тарифа
        await db.refresh(db_user)
        _reset_panel_id_daily = (
            subscription.remnawave_id
            if settings.is_multi_tariff_enabled() and subscription.remnawave_id
            else db_user.remnawave_id
        )
        if settings.is_multi_tariff_enabled() and not getattr(subscription, 'remnawave_id', None):
            logger.warning(
                'Multi-tariff: subscription missing remnawave_id, using user fallback',
                subscription_id=getattr(subscription, 'id', None),
            )
        if _reset_panel_id_daily:
            try:
                from app.services.remnawave_service import RemnaWaveService

                service = RemnaWaveService()
                async with service.get_api_client() as api:
                    # reset_user_devices больше не бросает при отказе панели —
                    # он ловит ошибку внутри и возвращает False. Без проверки
                    # результата лог утверждал бы, что сброс прошёл, когда он
                    # провалился, и старые HWID остались бы за новым лимитом.
                    if await api.reset_user_devices(_reset_panel_id_daily):
                        logger.info(
                            '🔧 Сброшены устройства при смене на суточный тариф для user_id', db_user_id=db_user.id
                        )
                    else:
                        logger.error(
                            'Не удалось сбросить устройства при смене на суточный тариф', db_user_id=db_user.id
                        )
            except Exception as e:
                logger.error('Ошибка сброса устройств при смене тарифа', error=e)

        # Создаем транзакцию
        await create_transaction(
            db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=final_daily_price,
            description=f'Смена на суточный тариф {tariff.name} (первый день)',
        )

        # Отправляем уведомление админу
        try:
            admin_notification_service = AdminNotificationService(callback.bot)
            await admin_notification_service.send_subscription_purchase_notification(
                db,
                db_user,
                subscription,
                None,
                1,  # 1 день
                was_trial_conversion=False,
                amount_kopeks=final_daily_price,
                purchase_type='tariff_switch',
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления админу', error=e)

        await state.clear()

        traffic = format_traffic(tariff.traffic_limit_gb)

        await callback.message.edit_text(
            texts.t(
                'TARIFF_SWITCH_DAILY_SUCCESS',
                '🎉 <b>Тариф успешно изменён!</b>\n\n'
                '📦 Новый тариф: <b>{name}</b>\n'
                '📊 Трафик: {traffic}\n'
                '📱 Устройств: {devices}\n'
                '🔄 Тип: Суточный\n'
                '💰 Списано: {price}\n\n'
                'ℹ️ Следующее списание через 24 часа.',
            ).format(
                name=html.escape(tariff.name),
                traffic=traffic,
                devices=tariff.device_limit,
                price=format_price_kopeks(final_daily_price),
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                            callback_data=f'sm:{subscription.id}'
                            if settings.is_multi_tariff_enabled() and subscription
                            else 'menu_subscription',
                        )
                    ],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
                ]
            ),
            parse_mode='HTML',
        )

    except Exception as e:
        logger.error('Ошибка при смене на суточный тариф', error=e, exc_info=True)
        await db.rollback()
        # Compensating refund: balance was already committed by subtract_user_balance
        try:
            from app.database.crud.user import add_user_balance

            refund_success = await add_user_balance(
                db,
                db_user,
                final_daily_price,
                'Возврат: ошибка смены на суточный тариф',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
                commit=False,
            )
            if not refund_success:
                await _persist_failed_refund(
                    user_id=db_user.id,
                    amount_kopeks=final_daily_price,
                    reason='Возврат: ошибка смены на суточный тариф',
                    error=Exception('add_user_balance returned False'),
                )
            await db.commit()
        except Exception as refund_error:
            logger.critical(
                'CRITICAL: не удалось вернуть средства после ошибки смены на суточный тариф',
                user_id=db_user.id,
                price_kopeks=final_daily_price,
                refund_error=refund_error,
            )
        try:
            await callback.message.edit_text(
                texts.t('TARIFF_SWITCH_ERROR', '❌ Произошла ошибка при переключении тарифа')
            )
        except Exception:
            pass


# ==================== Мгновенное переключение тарифов (без выбора периода) ====================


def _calculate_instant_switch_cost(
    current_tariff: Tariff,
    new_tariff: Tariff,
    remaining_days: int,
    db_user: User | None = None,
) -> tuple[int, bool]:
    """Рассчитывает стоимость мгновенного переключения тарифа.

    Делегирует расчёт в PricingEngine.calculate_tariff_switch_cost().
    Returns:
        (upgrade_cost_kopeks, is_upgrade)
    """
    from app.services.pricing_engine import pricing_engine

    result = pricing_engine.calculate_tariff_switch_cost(
        current_tariff,
        new_tariff,
        remaining_days,
        user=db_user,
    )
    return result.upgrade_cost, result.is_upgrade


def _filter_tariffs_by_switch_direction(
    tariffs: list[Tariff],
    current_tariff: Tariff,
    remaining_days: int,
    db_user: User | None = None,
) -> list[Tariff]:
    """Фильтрует тарифы по разрешённым направлениям смены (upgrade/downgrade)."""
    upgrade_ok = settings.TARIFF_SWITCH_UPGRADE_ENABLED
    downgrade_ok = settings.TARIFF_SWITCH_DOWNGRADE_ENABLED

    if upgrade_ok and downgrade_ok:
        return tariffs

    filtered = []
    for tariff in tariffs:
        if tariff.id == current_tariff.id:
            filtered.append(tariff)
            continue
        _, is_upgrade = _calculate_instant_switch_cost(current_tariff, tariff, remaining_days, db_user)
        if (is_upgrade and upgrade_ok) or (not is_upgrade and downgrade_ok):
            filtered.append(tariff)
    return filtered


def format_instant_switch_list_text(
    tariffs: list[Tariff],
    current_tariff: Tariff,
    remaining_days: int,
    db_user: User | None = None,
) -> str:
    """Форматирует текст со списком тарифов для мгновенного переключения."""
    upgrade_ok = settings.TARIFF_SWITCH_UPGRADE_ENABLED
    downgrade_ok = settings.TARIFF_SWITCH_DOWNGRADE_ENABLED

    texts = get_texts(db_user.language if db_user else 'ru')
    lines = [
        texts.t('TARIFF_SWITCH_INSTANT_TITLE', '📦 <b>Мгновенная смена тарифа</b>'),
        texts.t('TARIFF_SWITCH_CURRENT_LINE', '📌 Текущий: <b>{name}</b>').format(
            name=html.escape(current_tariff.name)
        ),
        texts.t('TARIFF_SWITCH_DAYS_LEFT_LINE', '⏰ Осталось: <b>{days} дн.</b>').format(days=remaining_days),
        '',
        texts.t('TARIFF_SWITCH_KEEP_DAYS_NOTE', '💡 При переключении остаток дней сохраняется.'),
    ]
    if upgrade_ok:
        lines.append(texts.t('TARIFF_SWITCH_UPGRADE_NOTE', '⬆️ Повышение тарифа = доплата за разницу'))
    if downgrade_ok:
        lines.append(texts.t('TARIFF_SWITCH_DOWNGRADE_NOTE', '⬇️ Понижение = бесплатно'))
    lines.append('')

    for tariff in tariffs:
        if tariff.id == current_tariff.id:
            continue

        traffic_gb = tariff.traffic_limit_gb
        traffic = (
            '∞' if traffic_gb == 0 else texts.t('TARIFF_PURCHASE_TRAFFIC_GB', '{traffic} ГБ').format(traffic=traffic_gb)
        )

        # Рассчитываем стоимость переключения
        cost, is_upgrade = _calculate_instant_switch_cost(current_tariff, tariff, remaining_days, db_user)

        if is_upgrade:
            cost_text = f'⬆️ +{format_price_kopeks(cost, compact=True)}'
        else:
            cost_text = texts.t('TARIFF_SWITCH_FREE_LABEL', '⬇️ Бесплатно')

        lines.append(f'<b>{html.escape(tariff.name)}</b> — {traffic} / {tariff.device_limit} 📱 {cost_text}')

        if tariff.description:
            lines.append(f'<i>{html.escape(tariff.description)}</i>')

        lines.append('')

    return '\n'.join(lines)


def get_instant_switch_keyboard(
    tariffs: list[Tariff],
    current_tariff: Tariff,
    remaining_days: int,
    language: str,
    db_user: User | None = None,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру для мгновенного переключения тарифа."""
    texts = get_texts(language)
    buttons = []

    for tariff in tariffs:
        if tariff.id == current_tariff.id:
            continue

        # Рассчитываем стоимость
        cost, is_upgrade = _calculate_instant_switch_cost(current_tariff, tariff, remaining_days, db_user)

        if is_upgrade:
            btn_text = f'{tariff.name} (+{format_price_kopeks(cost, compact=True)})'
        else:
            btn_text = texts.t('TARIFF_SWITCH_BUTTON_FREE', '{name} (бесплатно)').format(name=tariff.name)

        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f'instant_sw_preview:{tariff.id}')])

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_instant_switch_confirm_keyboard(
    tariff_id: int,
    language: str,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру подтверждения мгновенного переключения."""
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('TARIFF_SWITCH_CONFIRM_BUTTON', '✅ Подтвердить переключение'),
                    callback_data=f'instant_sw_confirm:{tariff_id}',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='instant_switch')],
        ]
    )


def get_instant_switch_insufficient_balance_keyboard(
    tariff_id: int,
    language: str,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру при недостаточном балансе для мгновенного переключения."""
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'), callback_data='balance_topup')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='instant_switch')],
        ]
    )


@error_handler
async def show_instant_switch_list(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Показывает список тарифов для мгновенного переключения."""

    texts = get_texts(db_user.language)
    await state.clear()

    # Проверяем наличие активной подписки
    subscription, _sub_id = await _resolve_switch_subscription(callback, db_user, db, state)
    if not subscription:
        return

    if not subscription.tariff_id:
        # Legacy subscription without tariff — redirect to tariff_switch migration flow
        await show_tariff_switch_list(callback, db_user, db, state)
        return

    # Получаем текущий тариф
    current_tariff = await get_tariff_by_id(db, subscription.tariff_id)
    if not current_tariff:
        await callback.answer(texts.t('TARIFF_SWITCH_CURRENT_NOT_FOUND', 'Текущий тариф не найден'), show_alert=True)
        return

    # Смена С бесплатного (0₽) тарифа: prorated instant-switch неприменим —
    # доплата считалась бы за весь наспамленный/подаренный остаток (сотни дней),
    # а сами дни переносились бы на платный тариф вопреки
    # TARIFF_SWITCH_RESET_FREE_DAYS. Отправляем в флоу с выбором периода:
    # там оплачивается полная стоимость периода, а extend_subscription
    # сбрасывает бесплатный остаток.
    if settings.TARIFF_SWITCH_RESET_FREE_DAYS and current_tariff.is_free:
        await show_tariff_switch_list(callback, db_user, db, state)
        return

    now = datetime.now(UTC)
    remaining_days = remaining_days_for_switch(subscription.end_date, now)

    if not subscription.end_date or subscription.end_date <= now:
        await callback.message.edit_text(
            texts.t(
                'TARIFF_SWITCH_EXPIRED',
                '❌ <b>Переключение недоступно</b>\n\n'
                'У вашей подписки не осталось активных дней.\n'
                'Оформите новый тариф с нуля.',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('BUY_TARIFF_BUTTON', '📦 Купить тариф'), callback_data='menu_buy'
                        )
                    ],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')],
                ]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Проверяем, разрешена ли смена тарифа хотя бы в одном направлении
    if not settings.TARIFF_SWITCH_UPGRADE_ENABLED and not settings.TARIFF_SWITCH_DOWNGRADE_ENABLED:
        await callback.message.edit_text(
            texts.t(
                'TARIFF_SWITCH_DISABLED',
                '🚫 <b>Смена тарифа недоступна</b>\n\nАдминистратор отключил возможность смены тарифа.',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')]]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Получаем доступные тарифы
    promo_group_id = getattr(db_user, 'promo_group_id', None)
    tariffs = await get_tariffs_for_user(db, promo_group_id)

    # Filter out ALL tariffs user already has active subscriptions for
    if settings.is_multi_tariff_enabled():
        _all_active_instant = await get_active_subscriptions_by_user_id(db, db_user.id)
        _purchased_ids_instant = {s.tariff_id for s in _all_active_instant if s.tariff_id}
        available_tariffs = [t for t in tariffs if t.id not in _purchased_ids_instant]
    else:
        available_tariffs = [t for t in tariffs if t.id != current_tariff.id]

    # Фильтруем по разрешённым направлениям (upgrade/downgrade)
    available_tariffs = _filter_tariffs_by_switch_direction(available_tariffs, current_tariff, remaining_days, db_user)

    if not available_tariffs:
        await callback.message.edit_text(
            texts.t(
                'TARIFF_SWITCH_NO_TARIFFS',
                '😔 <b>Нет доступных тарифов для переключения</b>\n\nВы уже используете единственный доступный тариф.',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')]]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Формируем текст со списком тарифов
    switch_text = format_instant_switch_list_text(available_tariffs, current_tariff, remaining_days, db_user)

    await callback.message.edit_text(
        switch_text,
        reply_markup=get_instant_switch_keyboard(
            available_tariffs, current_tariff, remaining_days, db_user.language, db_user
        ),
        parse_mode='HTML',
    )

    await state.update_data(
        current_tariff_id=current_tariff.id,
        remaining_days=remaining_days,
        active_subscription_id=subscription.id,
    )
    await callback.answer()


@error_handler
async def preview_instant_switch(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Показывает превью мгновенного переключения тарифа."""
    texts = get_texts(db_user.language)

    tariff_id = int(callback.data.split(':')[1])
    new_tariff = await get_tariff_by_id(db, tariff_id)

    if not new_tariff or not new_tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    # Получаем данные из состояния
    data = await state.get_data()
    current_tariff_id = data.get('current_tariff_id')
    remaining_days = data.get('remaining_days', 0)

    # Resolve the subscription being switched FROM (via FSM state active_subscription_id)
    subscription, _isw_sub_id = await _resolve_switch_subscription(callback, db_user, db, state)
    if not subscription or not subscription.tariff_id:
        await callback.answer(texts.t('SUBSCRIPTION_NOT_FOUND', '❌ Подписка не найдена'), show_alert=True)
        return

    current_tariff_id = current_tariff_id or subscription.tariff_id
    current_tariff = await get_tariff_by_id(db, current_tariff_id)
    if not current_tariff:
        await callback.answer(texts.t('TARIFF_SWITCH_CURRENT_NOT_FOUND', 'Текущий тариф не найден'), show_alert=True)
        return

    # Бесплатный (0₽) исходный тариф не переключается через prorated instant-switch
    # (см. show_instant_switch_list) — кнопка могла остаться в старом сообщении.
    if settings.TARIFF_SWITCH_RESET_FREE_DAYS and current_tariff.is_free:
        await show_tariff_switch_list(callback, db_user, db, state)
        return

    if not remaining_days and subscription.end_date:
        remaining_days = remaining_days_for_switch(subscription.end_date)

    # Рассчитываем стоимость переключения
    upgrade_cost, is_upgrade = _calculate_instant_switch_cost(current_tariff, new_tariff, remaining_days, db_user)

    # Проверяем разрешение на смену в данном направлении
    if is_upgrade and not settings.TARIFF_SWITCH_UPGRADE_ENABLED:
        await callback.answer(texts.t('TARIFF_SWITCH_UPGRADE_DISABLED', 'Повышение тарифа недоступно'), show_alert=True)
        return
    if not is_upgrade and not settings.TARIFF_SWITCH_DOWNGRADE_ENABLED:
        await callback.answer(
            texts.t('TARIFF_SWITCH_DOWNGRADE_DISABLED', 'Понижение тарифа недоступно'), show_alert=True
        )
        return

    # Проверяем баланс
    user_balance = db_user.balance_kopeks or 0

    traffic = format_traffic(new_tariff.traffic_limit_gb)
    current_traffic = format_traffic(current_tariff.traffic_limit_gb)

    # Проверяем, суточный ли новый тариф
    is_new_daily = getattr(new_tariff, 'is_daily', False)
    daily_warning = ''
    if is_new_daily and remaining_days > 1:
        daily_warning = '\n\n' + texts.t(
            'DAILY_SWITCH_WARNING',
            '⚠️ <b>Внимание!</b> У вас осталось {days} дн. подписки.\nПри смене на суточный тариф они будут утеряны!',
        ).format(days=remaining_days)

    # Для суточного тарифа особая логика показа
    if is_new_daily:
        raw_daily_price = getattr(new_tariff, 'daily_price_kopeks', 0)
        # Применяем групповую скидку + promo-offer для отображения
        daily_group_pct, daily_offer_pct, daily_discount = _get_user_period_discount(db_user, 1)
        daily_price = (
            _apply_promo_discount(raw_daily_price, daily_group_pct, daily_offer_pct)
            if daily_discount > 0
            else raw_daily_price
        )
        discount_text = (
            texts.t('TARIFF_PURCHASE_DAILY_DISCOUNT_LINE', '\n💎 Скидка: {percent}%').format(percent=daily_discount)
            if daily_discount > 0
            else ''
        )
        user_balance = db_user.balance_kopeks or 0

        if user_balance >= daily_price:
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_SWITCH_DAILY_PREVIEW',
                    '🔄 <b>Переключение на суточный тариф</b>\n\n'
                    '📌 Текущий: <b>{current}</b>\n'
                    '   • Трафик: {current_traffic}\n'
                    '   • Устройств: {current_devices}\n\n'
                    '📦 Новый: <b>{name}</b>\n'
                    '   • Трафик: {traffic}\n'
                    '   • Устройств: {devices}\n'
                    '   • Тип: 🔄 Суточный\n\n'
                    '💰 <b>Цена: {price}/день</b>{discount}\n\n'
                    '💳 Ваш баланс: {balance}{warning}\n\n'
                    'ℹ️ Средства будут списываться автоматически раз в сутки.',
                ).format(
                    current=html.escape(current_tariff.name),
                    current_traffic=current_traffic,
                    current_devices=current_tariff.device_limit,
                    name=html.escape(new_tariff.name),
                    traffic=traffic,
                    devices=new_tariff.device_limit,
                    price=format_price_kopeks(daily_price),
                    discount=discount_text,
                    balance=format_price_kopeks(user_balance),
                    warning=daily_warning,
                ),
                reply_markup=get_instant_switch_confirm_keyboard(tariff_id, db_user.language),
                parse_mode='HTML',
            )
        else:
            missing = daily_price - user_balance
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_PURCHASE_DAILY_INSUFFICIENT',
                    '❌ <b>Недостаточно средств</b>\n\n'
                    '📦 Тариф: <b>{name}</b>\n'
                    '🔄 Тип: Суточный\n'
                    '💰 Цена: {price}/день{discount}\n\n'
                    '💳 Ваш баланс: {balance}\n'
                    '⚠️ Не хватает: <b>{missing}</b>',
                ).format(
                    name=html.escape(new_tariff.name),
                    price=format_price_kopeks(daily_price),
                    discount=discount_text,
                    balance=format_price_kopeks(user_balance),
                    missing=format_price_kopeks(missing),
                )
                + daily_warning,
                reply_markup=get_instant_switch_insufficient_balance_keyboard(tariff_id, db_user.language),
                parse_mode='HTML',
            )

        await state.update_data(
            switch_tariff_id=tariff_id,
            upgrade_cost=0,
            is_upgrade=False,
            current_tariff_id=current_tariff_id,
            remaining_days=remaining_days,
        )
        await callback.answer()
        return

    if is_upgrade:
        # Upgrade - нужна доплата
        if user_balance >= upgrade_cost:
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_SWITCH_UPGRADE_PREVIEW',
                    '⬆️ <b>Повышение тарифа</b>\n\n'
                    '📌 Текущий: <b>{current}</b>\n'
                    '   • Трафик: {current_traffic}\n'
                    '   • Устройств: {current_devices}\n\n'
                    '📦 Новый: <b>{name}</b>\n'
                    '   • Трафик: {traffic}\n'
                    '   • Устройств: {devices}\n\n'
                    '⏰ Осталось дней: <b>{days}</b>\n'
                    '💰 <b>Доплата: {cost}</b>\n\n'
                    '💳 Ваш баланс: {balance}\n'
                    'После оплаты: {after}',
                ).format(
                    current=html.escape(current_tariff.name),
                    current_traffic=current_traffic,
                    current_devices=current_tariff.device_limit,
                    name=html.escape(new_tariff.name),
                    traffic=traffic,
                    devices=new_tariff.device_limit,
                    days=remaining_days,
                    cost=format_price_kopeks(upgrade_cost),
                    balance=format_price_kopeks(user_balance),
                    after=format_price_kopeks(user_balance - upgrade_cost),
                ),
                reply_markup=get_instant_switch_confirm_keyboard(tariff_id, db_user.language),
                parse_mode='HTML',
            )
        else:
            missing = upgrade_cost - user_balance
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_SWITCH_UPGRADE_INSUFFICIENT',
                    '❌ <b>Недостаточно средств</b>\n\n'
                    '📦 Новый тариф: <b>{name}</b>\n'
                    '💰 Требуется доплата: {cost}\n\n'
                    '💳 Ваш баланс: {balance}\n'
                    '⚠️ Не хватает: <b>{missing}</b>',
                ).format(
                    name=html.escape(new_tariff.name),
                    cost=format_price_kopeks(upgrade_cost),
                    balance=format_price_kopeks(user_balance),
                    missing=format_price_kopeks(missing),
                ),
                reply_markup=get_instant_switch_insufficient_balance_keyboard(tariff_id, db_user.language),
                parse_mode='HTML',
            )
    else:
        # Downgrade или тот же уровень - бесплатно
        await callback.message.edit_text(
            texts.t(
                'TARIFF_SWITCH_DOWNGRADE_PREVIEW',
                '⬇️ <b>Переключение тарифа</b>\n\n'
                '📌 Текущий: <b>{current}</b>\n'
                '   • Трафик: {current_traffic}\n'
                '   • Устройств: {current_devices}\n\n'
                '📦 Новый: <b>{name}</b>\n'
                '   • Трафик: {traffic}\n'
                '   • Устройств: {devices}\n\n'
                '⏰ Осталось дней: <b>{days}</b>\n'
                '💰 <b>Бесплатно</b> (понижение/равный тариф)',
            ).format(
                current=html.escape(current_tariff.name),
                current_traffic=current_traffic,
                current_devices=current_tariff.device_limit,
                name=html.escape(new_tariff.name),
                traffic=traffic,
                devices=new_tariff.device_limit,
                days=remaining_days,
            ),
            reply_markup=get_instant_switch_confirm_keyboard(tariff_id, db_user.language),
            parse_mode='HTML',
        )

    await state.update_data(
        switch_tariff_id=tariff_id,
        upgrade_cost=upgrade_cost,
        is_upgrade=is_upgrade,
        current_tariff_id=current_tariff_id,
        remaining_days=remaining_days,
    )
    await callback.answer()


@error_handler
async def purchase_tariff_with_lava(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Оформление подписки на тариф через автопродление Lava.

    Зеркало ``purchase_tariff_with_sbp``: первое списание оплачивается по
    ссылке Lava и оживляет подписку (для нового тарифа создаётся заготовка).
    Выбранный период игнорируется — каденс задан продуктом в кабинете Lava.
    """
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    from app.services.payment.lava import purchase_tariff_with_lava_recurring

    try:
        result = await purchase_tariff_with_lava_recurring(db, user=db_user, tariff=tariff)
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        return
    except Exception:
        await callback.answer(
            texts.t('LAVA_RECURRING_ENABLE_ERROR', '❌ Не удалось подключить автопродление Lava. Попробуйте позже.'),
            show_alert=True,
        )
        return

    redirect_url = result.get('redirect_url')
    text = texts.t(
        'LAVA_RECURRING_ENABLE_SUCCESS',
        '⚡ <b>Автопродление Lava</b>\n\nОплатите первый счёт по кнопке ниже.\nПосле оплаты автопродление станет активным.',
    )
    text += '\n\n' + texts.t(
        'LAVA_PURCHASE_PENDING_NOTE',
        'ℹ️ Подписка активируется после оплаты первого счёта.',
    )

    buttons = []
    if redirect_url:
        buttons.append(
            [InlineKeyboardButton(text=texts.t('LAVA_RECURRING_PAY_BUTTON', '💳 Оплатить'), url=redirect_url)]
        )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='tariff_list')])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@error_handler
async def confirm_instant_switch(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Подтверждает мгновенное переключение тарифа."""
    texts = get_texts(db_user.language)

    tariff_id = int(callback.data.split(':')[1])
    new_tariff = await get_tariff_by_id(db, tariff_id)

    if not new_tariff or not new_tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    # Проверяем подписку (switched FROM — resolved via FSM state)
    subscription, _isw_confirm_sub_id = await _resolve_switch_subscription(callback, db_user, db, state)
    if not subscription:
        await callback.answer(texts.t('SUBSCRIPTION_NOT_FOUND', '❌ Подписка не найдена'), show_alert=True)
        return

    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    # Recompute upgrade_cost under lock (FSM-stored value may be stale)
    current_tariff = await get_tariff_by_id(db, subscription.tariff_id) if subscription.tariff_id else None
    if not current_tariff:
        await callback.answer(texts.t('TARIFF_SWITCH_CURRENT_NOT_FOUND', 'Текущий тариф не найден'), show_alert=True)
        return

    # Бесплатный (0₽) исходный тариф: prorated-списание запрещено — переключение
    # идёт только через флоу с выбором периода (полная цена, сброс бесплатных дней).
    if settings.TARIFF_SWITCH_RESET_FREE_DAYS and current_tariff.is_free:
        await show_tariff_switch_list(callback, db_user, db, state)
        return

    remaining_days = remaining_days_for_switch(subscription.end_date)

    # Use full TariffSwitchResult to access offer_discount_pct for consume_promo_offer flag
    from app.services.pricing_engine import pricing_engine

    switch_result = pricing_engine.calculate_tariff_switch_cost(
        current_tariff,
        new_tariff,
        remaining_days,
        user=db_user,
    )
    upgrade_cost = switch_result.upgrade_cost
    is_upgrade = switch_result.is_upgrade
    consume_promo = switch_result.offer_discount_pct > 0

    # Проверяем разрешение на смену в данном направлении
    if is_upgrade and not settings.TARIFF_SWITCH_UPGRADE_ENABLED:
        await callback.answer(texts.t('TARIFF_SWITCH_UPGRADE_DISABLED', 'Повышение тарифа недоступно'), show_alert=True)
        return
    if not is_upgrade and not settings.TARIFF_SWITCH_DOWNGRADE_ENABLED:
        await callback.answer(
            texts.t('TARIFF_SWITCH_DOWNGRADE_DISABLED', 'Понижение тарифа недоступно'), show_alert=True
        )
        return

    # Проверяем баланс если это upgrade (use locked user's fresh balance)
    user_balance = db_user.balance_kopeks or 0
    if is_upgrade and user_balance < upgrade_cost:
        await callback.answer(
            texts.t('MINIAPP_PURCHASE_STATUS_INSUFFICIENT', 'Недостаточно средств на балансе'), show_alert=True
        )
        return

    # Отвечаем на callback СРАЗУ — до тяжёлых операций (панель, транзакции),
    # иначе Telegram инвалидирует query через 30 сек → TelegramBadRequest
    try:
        await callback.answer()
    except Exception:
        pass

    try:
        # Списываем баланс если это upgrade
        # upgrade_cost includes both group + offer discounts from PricingEngine
        if is_upgrade and upgrade_cost > 0:
            success = await subtract_user_balance(
                db,
                db_user,
                upgrade_cost,
                f'Переключение на тариф {new_tariff.name}',
                consume_promo_offer=consume_promo,
                mark_as_paid_subscription=True,
            )
            if not success:
                try:
                    await callback.message.edit_text(
                        texts.t('TARIFF_PURCHASE_CHARGE_ERROR', '❌ Ошибка списания баланса')
                    )
                except Exception:
                    pass
                return

        # Получаем список серверов из нового тарифа
        squads = new_tariff.allowed_squads or []

        # Если allowed_squads пустой - значит "все серверы", получаем их
        if not squads:
            from app.database.crud.server_squad import get_all_server_squads

            all_servers, _ = await get_all_server_squads(db, available_only=True)
            squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

        # Проверяем, суточный ли новый тариф
        is_new_daily = getattr(new_tariff, 'is_daily', False)

        # Обновляем подписку с новыми параметрами тарифа
        # Сбрасываем лимит устройств на базу нового тарифа (докупленные не переносятся)
        from app.database.crud.subscription import calc_device_limit_on_tariff_switch

        old_tariff = await get_tariff_by_id(db, subscription.tariff_id) if subscription.tariff_id else None
        subscription.tariff_id = new_tariff.id
        subscription.traffic_limit_gb = new_tariff.traffic_limit_gb
        subscription.device_limit = calc_device_limit_on_tariff_switch(
            current_device_limit=subscription.device_limit,
            old_tariff_device_limit=old_tariff.device_limit if old_tariff else None,
            new_tariff_device_limit=new_tariff.device_limit,
            max_device_limit=getattr(new_tariff, 'max_device_limit', None),
        )
        subscription.connected_squads = squads

        # Сбрасываем докупленный трафик при смене тарифа
        from sqlalchemy import delete as sql_delete

        from app.database.models import TrafficPurchase

        await db.execute(sql_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
        subscription.purchased_traffic_gb = 0
        subscription.traffic_reset_at = None

        # Счётчик трафика обнуляет только ОПЛАЧЕННОЕ переключение (см. tariff_switch_policy).
        reset_used_traffic = should_reset_used_traffic(upgrade_cost)
        if reset_used_traffic:
            subscription.traffic_used_gb = 0.0

        if is_new_daily:
            # Для суточного тарифа - сбрасываем на 1 день и настраиваем суточные параметры
            # Apply group + promo-offer discounts via PricingEngine (single source of truth)
            daily_pricing = await pricing_engine.calculate_tariff_purchase_price(
                new_tariff,
                period_days=1,
                device_limit=new_tariff.device_limit,
                user=db_user,
            )
            daily_price = daily_pricing.final_total
            consume_promo_for_daily = daily_pricing.breakdown.get('offer_discount_pct', 0) > 0

            # Списываем первый день если ещё не списано (upgrade_cost был 0)
            if upgrade_cost == 0 and daily_price > 0:
                if user_balance >= daily_price:
                    success = await subtract_user_balance(
                        db,
                        db_user,
                        daily_price,
                        f'Переключение на суточный тариф {new_tariff.name} (первый день)',
                        consume_promo_offer=consume_promo_for_daily,
                        mark_as_paid_subscription=True,
                    )
                    if not success:
                        try:
                            await callback.message.edit_text(
                                texts.t('TARIFF_PURCHASE_INSUFFICIENT_FUNDS', '❌ Недостаточно средств')
                            )
                        except Exception:
                            pass
                        return
                    await create_transaction(
                        db,
                        user_id=db_user.id,
                        type=TransactionType.SUBSCRIPTION_PAYMENT,
                        amount_kopeks=daily_price,
                        description=f'Переключение на суточный тариф {new_tariff.name} (первый день)',
                    )

                    # Уведомление админу о списании за первый день суточного тарифа
                    try:
                        admin_notification_service = AdminNotificationService(callback.bot)
                        await admin_notification_service.send_subscription_purchase_notification(
                            db,
                            db_user,
                            subscription,
                            None,
                            1,
                            was_trial_conversion=False,
                            amount_kopeks=daily_price,
                            purchase_type='tariff_switch',
                        )
                    except Exception as e:
                        logger.error('Ошибка отправки уведомления админу', error=e)

            subscription.end_date = datetime.now(UTC) + timedelta(days=1)
            subscription.is_trial = False
            subscription.is_daily_paused = False
            subscription.last_daily_charge_at = datetime.now(UTC)

        await db.commit()
        await db.refresh(subscription)

        # Обновляем пользователя в Remnawave (сброс трафика по админ-настройке)
        try:
            subscription_service = SubscriptionService()
            if settings.is_multi_tariff_enabled():
                _should_create = not subscription.remnawave_id
            else:
                _should_create = not getattr(db_user, 'remnawave_id', None)

            if _should_create:
                await subscription_service.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=reset_used_traffic,
                    reset_reason='мгновенное переключение тарифа',
                )
            else:
                await subscription_service.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=reset_used_traffic,
                    reset_reason='мгновенное переключение тарифа',
                )
        except Exception as e:
            logger.error('Ошибка обновления Remnawave при мгновенном переключении', error=e)
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=db_user.id,
                action='create',
            )

        # Гарантированный сброс устройств при смене тарифа
        await db.refresh(db_user)
        _reset_panel_id_instant = (
            subscription.remnawave_id
            if settings.is_multi_tariff_enabled() and subscription.remnawave_id
            else db_user.remnawave_id
        )
        if settings.is_multi_tariff_enabled() and not getattr(subscription, 'remnawave_id', None):
            logger.warning(
                'Multi-tariff: subscription missing remnawave_id, using user fallback',
                subscription_id=getattr(subscription, 'id', None),
            )
        if _reset_panel_id_instant:
            try:
                from app.services.remnawave_service import RemnaWaveService

                service = RemnaWaveService()
                async with service.get_api_client() as api:
                    # reset_user_devices больше не бросает при отказе панели —
                    # он ловит ошибку внутри и возвращает False. Без проверки
                    # результата лог утверждал бы, что сброс прошёл, когда он
                    # провалился, и старые HWID остались бы за новым лимитом.
                    if await api.reset_user_devices(_reset_panel_id_instant):
                        logger.info(
                            '🔧 Сброшены устройства при мгновенном переключении тарифа для user_id',
                            db_user_id=db_user.id,
                        )
                    else:
                        logger.error(
                            'Не удалось сбросить устройства при мгновенном переключении тарифа',
                            db_user_id=db_user.id,
                        )
            except Exception as e:
                logger.error('Ошибка сброса устройств при переключении тарифа', error=e)

        # Создаем транзакцию если была оплата
        if is_upgrade and upgrade_cost > 0:
            await create_transaction(
                db,
                user_id=db_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=upgrade_cost,
                description=f'Переключение на тариф {new_tariff.name}',
            )

            # Отправляем уведомление админу
            try:
                admin_notification_service = AdminNotificationService(callback.bot)
                await admin_notification_service.send_subscription_purchase_notification(
                    db,
                    db_user,
                    subscription,
                    None,
                    remaining_days,
                    was_trial_conversion=False,
                    amount_kopeks=upgrade_cost,
                    purchase_type='tariff_switch',
                )
            except Exception as e:
                logger.error('Ошибка отправки уведомления админу', error=e)

        await state.clear()

        traffic = format_traffic(new_tariff.traffic_limit_gb)

        # Для суточного тарифа другое сообщение об успехе
        if is_new_daily:
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_SWITCH_DAILY_SUCCESS',
                    '🎉 <b>Тариф успешно изменён!</b>\n\n'
                    '📦 Новый тариф: <b>{name}</b>\n'
                    '📊 Трафик: {traffic}\n'
                    '📱 Устройств: {devices}\n'
                    '🔄 Тип: Суточный\n'
                    '💰 Списано: {price}\n\n'
                    'ℹ️ Следующее списание через 24 часа.',
                ).format(
                    name=html.escape(new_tariff.name),
                    traffic=traffic,
                    devices=new_tariff.device_limit,
                    price=format_price_kopeks(daily_price),
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                                callback_data=f'sm:{subscription.id}'
                                if settings.is_multi_tariff_enabled() and subscription
                                else 'menu_subscription',
                            )
                        ],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            if is_upgrade:
                cost_text = texts.t('TARIFF_SWITCH_CHARGED_LINE', '💰 Списано: {price}').format(
                    price=format_price_kopeks(upgrade_cost)
                )
            else:
                cost_text = texts.t('TARIFF_SWITCH_FREE_COST_LINE', '💰 Бесплатно')

            await callback.message.edit_text(
                texts.t(
                    'TARIFF_SWITCH_INSTANT_SUCCESS',
                    '🎉 <b>Тариф успешно изменён!</b>\n\n'
                    '📦 Новый тариф: <b>{name}</b>\n'
                    '📊 Трафик: {traffic}\n'
                    '📱 Устройств: {devices}\n'
                    '⏰ Осталось дней: {days}\n'
                    '{cost_text}',
                ).format(
                    name=html.escape(new_tariff.name),
                    traffic=traffic,
                    devices=new_tariff.device_limit,
                    days=remaining_days,
                    cost_text=cost_text,
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                                callback_data=f'sm:{subscription.id}'
                                if settings.is_multi_tariff_enabled() and subscription
                                else 'menu_subscription',
                            )
                        ],
                        [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
                    ]
                ),
                parse_mode='HTML',
            )

    except Exception as e:
        logger.error('Ошибка при мгновенном переключении тарифа', error=e, exc_info=True)
        try:
            await callback.message.edit_text(
                texts.t('TARIFF_SWITCH_ERROR', '❌ Произошла ошибка при переключении тарифа')
            )
        except Exception:
            pass


async def return_to_saved_tariff_cart(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
    cart_data: dict,
):
    """Восстанавливает сохраненную корзину тарифа после пополнения баланса."""
    texts = get_texts(db_user.language)
    cart_mode = cart_data.get('cart_mode')
    tariff_id = cart_data.get('tariff_id')

    if not tariff_id:
        await callback.answer(
            texts.t('TARIFF_PURCHASE_CART_CORRUPTED', '❌ Данные корзины повреждены'), show_alert=True
        )
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(
            texts.t('TARIFF_PURCHASE_NO_LONGER_AVAILABLE', '❌ Тариф больше недоступен'), show_alert=True
        )
        # Очищаем корзину (per-subscription в multi-tariff)
        _cart_sub_id = cart_data.get('subscription_id')
        if _cart_sub_id and settings.is_multi_tariff_enabled():
            await user_cart_service.delete_subscription_cart(db_user.id, _cart_sub_id)
        else:
            await user_cart_service.delete_user_cart(db_user.id)
        return

    total_price = cart_data.get('total_price', 0)
    user_balance = db_user.balance_kopeks or 0
    traffic = format_traffic(tariff.traffic_limit_gb)

    # Проверяем баланс (при 100% скидке — пропускаем)
    if total_price > 0 and user_balance < total_price:
        missing = total_price - user_balance

        if cart_mode == 'daily_tariff_purchase':
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_PURCHASE_STILL_INSUFFICIENT_DAILY',
                    '❌ <b>Все еще недостаточно средств</b>\n\n'
                    '📦 Тариф: <b>{name}</b>\n'
                    '🔄 Тип: Суточный\n'
                    '💰 Стоимость: {price}\n\n'
                    '💳 Ваш баланс: {balance}\n'
                    '⚠️ Не хватает: <b>{missing}</b>',
                ).format(
                    name=html.escape(tariff.name),
                    price=format_price_kopeks(total_price),
                    balance=format_price_kopeks(user_balance),
                    missing=format_price_kopeks(missing),
                ),
                reply_markup=get_daily_tariff_insufficient_balance_keyboard(tariff_id, db_user.language),
                parse_mode='HTML',
            )
        elif cart_mode == 'extend':
            period = cart_data.get('period_days', 30)
            # Метка «пополняю ради корзины» живёт 30 минут, сама корзина — час.
            # Экран возврата рисует кнопки оплаты на недостающую сумму, поэтому
            # без продления метки пользователь мог оплатить ровно столько,
            # сколько нужно, уже после её истечения — и автопродление молча не
            # срабатывало. Пересохранение обновляет оба срока.
            await user_cart_service.save_user_cart(db_user.id, cart_data)
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_PURCHASE_STILL_INSUFFICIENT',
                    '❌ <b>Все еще недостаточно средств</b>\n\n'
                    '📦 Тариф: <b>{name}</b>\n'
                    '📅 Период: {period}\n'
                    '💰 Стоимость: {price}\n\n'
                    '💳 Ваш баланс: {balance}\n'
                    '⚠️ Не хватает: <b>{missing}</b>',
                ).format(
                    name=html.escape(tariff.name),
                    period=format_period(period),
                    price=format_price_kopeks(total_price),
                    balance=format_price_kopeks(user_balance),
                    missing=format_price_kopeks(missing),
                ),
                reply_markup=get_tariff_extend_insufficient_balance_keyboard(
                    tariff_id,
                    cart_data.get('subscription_id'),
                    period,
                    db_user.language,
                    missing_kopeks=missing,
                ),
                parse_mode='HTML',
            )
        else:  # tariff_purchase
            period = cart_data.get('period_days', 30)
            await callback.message.edit_text(
                texts.t(
                    'TARIFF_PURCHASE_STILL_INSUFFICIENT',
                    '❌ <b>Все еще недостаточно средств</b>\n\n'
                    '📦 Тариф: <b>{name}</b>\n'
                    '📅 Период: {period}\n'
                    '💰 Стоимость: {price}\n\n'
                    '💳 Ваш баланс: {balance}\n'
                    '⚠️ Не хватает: <b>{missing}</b>',
                ).format(
                    name=html.escape(tariff.name),
                    period=format_period(period),
                    price=format_price_kopeks(total_price),
                    balance=format_price_kopeks(user_balance),
                    missing=format_price_kopeks(missing),
                ),
                reply_markup=get_tariff_insufficient_balance_keyboard(
                    tariff_id, period, db_user.language, missing_kopeks=missing
                ),
                parse_mode='HTML',
            )
        await callback.answer()
        return

    # Баланс достаточен - показываем подтверждение
    discount_percent = cart_data.get('discount_percent', 0)

    # Pin FSM keys read by confirm_tariff_purchase before showing the
    # confirm keyboard. Without this, the cart-restore-after-topup path
    # bypasses select_tariff_period (the normal preview) and confirm
    # falls back to the race-vulnerable (user_id, tariff_id) lookup —
    # which is exactly the scenario that produced the user-reported
    # "Тариф уже активен" bug in the cart-restore flow.
    if cart_mode in ('tariff_purchase', 'extend'):
        _period_for_pin = cart_data.get('period_days', 30)
        await state.update_data(
            selected_tariff_id=tariff_id,
            selected_period=_period_for_pin,
            final_price=total_price,
            tariff_discount_percent=discount_percent,
            target_subscription_id=cart_data.get('subscription_id'),
        )

    if cart_mode == 'daily_tariff_purchase':
        daily_price = cart_data.get('daily_price_kopeks', total_price)

        await callback.message.edit_text(
            texts.t(
                'TARIFF_PURCHASE_DAILY_CART_CONFIRM',
                '✅ <b>Подтверждение покупки</b>\n\n'
                '📦 Тариф: <b>{name}</b>\n'
                '📊 Трафик: {traffic}\n'
                '📱 Устройств: {devices}\n'
                '🔄 Тип: Суточный\n'
                '💰 <b>Стоимость в день: {price}</b>\n\n'
                '💳 Ваш баланс: {balance}\n'
                'После оплаты: {after}',
            ).format(
                name=html.escape(tariff.name),
                traffic=traffic,
                devices=tariff.device_limit,
                price=format_price_kopeks(daily_price),
                balance=format_price_kopeks(user_balance),
                after=format_price_kopeks(user_balance - daily_price),
            ),
            reply_markup=get_daily_tariff_confirm_keyboard(tariff_id, db_user.language),
            parse_mode='HTML',
        )
    elif cart_mode == 'extend':
        period = cart_data.get('period_days', 30)

        discount_text = ''
        if discount_percent > 0:
            original_price = int(total_price / (1 - discount_percent / 100))
            discount_text = texts.t(
                'TARIFF_PURCHASE_DISCOUNT_SHORT_LINE', '\n🎁 Скидка: {percent}% (-{amount})'
            ).format(percent=discount_percent, amount=format_price_kopeks(original_price - total_price))

        # subscription_id обязателен в callback продления (issue #3012), берём из корзины
        _extend_sub_id = cart_data.get('subscription_id')

        await callback.message.edit_text(
            texts.t(
                'TARIFF_RENEW_CONFIRM',
                '✅ <b>Подтверждение продления</b>\n\n'
                '📦 Тариф: <b>{name}</b>\n'
                '📊 Трафик: {traffic}\n'
                '📱 Устройств: {devices}\n'
                '📅 Период: {period}\n'
                '{discount}\n'
                '💰 <b>К оплате: {total}</b>\n\n'
                '💳 Ваш баланс: {balance}\n'
                'После оплаты: {after}',
            ).format(
                name=html.escape(tariff.name),
                traffic=traffic,
                devices=tariff.device_limit,
                period=format_period(period),
                discount=discount_text,
                total=format_price_kopeks(total_price),
                balance=format_price_kopeks(user_balance),
                after=format_price_kopeks(user_balance - total_price),
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('TARIFF_RENEW_CONFIRM_BUTTON', '✅ Подтвердить продление'),
                            callback_data=f'tariff_ext_confirm:{_extend_sub_id}:{tariff_id}:{period}',
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=texts.BACK, callback_data=f'tariff_extend:{_extend_sub_id}:{tariff_id}'
                        )
                    ],
                ]
            ),
            parse_mode='HTML',
        )
    else:  # tariff_purchase
        period = cart_data.get('period_days', 30)

        discount_text = ''
        if discount_percent > 0:
            original_price = int(total_price / (1 - discount_percent / 100))
            discount_text = texts.t(
                'TARIFF_PURCHASE_DISCOUNT_SHORT_LINE', '\n🎁 Скидка: {percent}% (-{amount})'
            ).format(percent=discount_percent, amount=format_price_kopeks(original_price - total_price))

        await callback.message.edit_text(
            texts.t(
                'TARIFF_PURCHASE_CONFIRM',
                '✅ <b>Подтверждение покупки</b>\n\n'
                '📦 Тариф: <b>{name}</b>\n'
                '📊 Трафик: {traffic}\n'
                '📱 Устройств: {devices}\n'
                '📅 Период: {period}\n'
                '{discount}\n'
                '💰 <b>Итого: {total}</b>\n\n'
                '💳 Ваш баланс: {balance}\n'
                'После оплаты: {after}',
            ).format(
                name=html.escape(tariff.name),
                traffic=traffic,
                devices=tariff.device_limit,
                period=format_period(period),
                discount=discount_text,
                total=format_price_kopeks(total_price),
                balance=format_price_kopeks(user_balance),
                after=format_price_kopeks(user_balance - total_price),
            ),
            reply_markup=get_tariff_confirm_keyboard(tariff_id, period, db_user.language),
            parse_mode='HTML',
        )

    await callback.answer(texts.t('TARIFF_PURCHASE_CART_RESTORED', '✅ Корзина восстановлена!'))


async def purchase_tariff_with_sbp(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Оформление подписки на тариф через СБП-автопродление Platega.

    Кнопка на экране подтверждения покупки — альтернатива оплате с баланса.
    Первое списание = подтверждение привязки в банке; для нового тарифа
    создаётся EXPIRED-заготовка, которую активирует первый чардж (см.
    ``purchase_tariff_with_sbp_recurring``). Выбранный период игнорируется:
    списания идут по каденс-правилу тарифа.
    """
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return

    from app.services.payment.platega import purchase_tariff_with_sbp_recurring

    try:
        result = await purchase_tariff_with_sbp_recurring(db, user=db_user, tariff=tariff)
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        return
    except RuntimeError:
        await callback.answer(
            texts.t(
                'SBP_RECURRING_ENABLE_ERROR', '❌ Не удалось подключить автопродление через СБП. Попробуйте позже.'
            ),
            show_alert=True,
        )
        return

    redirect_url = result.get('redirect_url')
    text = texts.t(
        'SBP_RECURRING_ENABLE_SUCCESS',
        '⚡ <b>Автопродление через СБП</b>\n\nПодтвердите подключение в банковском приложении по кнопке ниже.\nПосле подтверждения автопродление станет активным.',
    )
    text += '\n\n' + texts.t(
        'SBP_PURCHASE_PENDING_NOTE',
        'ℹ️ Подписка активируется после подтверждения и первого списания.',
    )

    buttons = []
    if redirect_url:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('SBP_RECURRING_CONFIRM_BUTTON', '🏦 Подтвердить в банке'), url=redirect_url
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='tariff_list')])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


def register_tariff_purchase_handlers(dp: Dispatcher):
    """Регистрирует обработчики покупки по тарифам."""
    # Список тарифов (для режима tariffs)
    dp.callback_query.register(show_tariffs_list, F.data == 'tariff_list')
    dp.callback_query.register(show_tariffs_list, F.data == 'buy_subscription_tariffs')

    # Выбор тарифа
    dp.callback_query.register(select_tariff, F.data.startswith('tariff_select:'))

    # Выбор периода
    dp.callback_query.register(select_tariff_period, F.data.startswith('tariff_period:'))

    # Подтверждение покупки
    dp.callback_query.register(confirm_tariff_purchase, F.data.startswith('tariff_confirm:'))

    # Оформление через СБП-автопродление Platega (альтернатива балансу)
    dp.callback_query.register(purchase_tariff_with_sbp, F.data.startswith('tariff_sbp:'))
    dp.callback_query.register(purchase_tariff_with_lava, F.data.startswith('tariff_lava:'))

    # Подтверждение покупки суточного тарифа
    dp.callback_query.register(confirm_daily_tariff_purchase, F.data.startswith('daily_tariff_confirm:'))

    # Кастомные дни/трафик
    dp.callback_query.register(handle_custom_days_change, F.data.startswith('custom_days:'))
    dp.callback_query.register(handle_custom_traffic_change, F.data.startswith('custom_traffic:'))
    dp.callback_query.register(handle_custom_confirm, F.data.startswith('custom_confirm:'))
    dp.callback_query.register(select_tariff_period_with_traffic, F.data.startswith('tariff_period_traffic:'))

    # Продление по тарифу
    dp.callback_query.register(select_tariff_extend_period, F.data.startswith('tariff_extend:'))
    dp.callback_query.register(confirm_tariff_extend, F.data.startswith('tariff_ext_confirm:'))

    # Переключение тарифов (с выбором периода)
    dp.callback_query.register(show_tariff_switch_list, F.data == 'tariff_switch')
    dp.callback_query.register(select_tariff_switch, F.data.startswith('tariff_sw_select:'))
    dp.callback_query.register(select_tariff_switch_period, F.data.startswith('tariff_sw_period:'))
    dp.callback_query.register(confirm_tariff_switch, F.data.startswith('tariff_sw_confirm:'))

    # Смена на суточный тариф
    dp.callback_query.register(confirm_daily_tariff_switch, F.data.startswith('daily_tariff_switch_confirm:'))

    # Мгновенное переключение тарифов (без выбора периода)
    dp.callback_query.register(show_instant_switch_list, F.data == 'instant_switch')
    dp.callback_query.register(preview_instant_switch, F.data.startswith('instant_sw_preview:'))
    dp.callback_query.register(confirm_instant_switch, F.data.startswith('instant_sw_confirm:'))
