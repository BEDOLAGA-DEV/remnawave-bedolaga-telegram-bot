"""Automatic subscription purchase from a saved cart after balance top-up."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.crud.transaction import create_transaction
from app.database.crud.user import get_user_by_id, subtract_user_balance
from app.database.models import Subscription, TransactionType, User
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.subscription_checkout_service import clear_subscription_checkout_draft
from app.services.subscription_purchase_service import (
    MiniAppSubscriptionPurchaseService,
    PurchaseBalanceError,
    PurchaseOptionsContext,
    PurchasePricingResult,
    PurchaseSelection,
    PurchaseValidationError,
)
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service
from app.utils.pricing_utils import format_period_description
from app.utils.timezone import format_local_datetime


logger = logging.getLogger(__name__)


def _format_user_id(user: User) -> str:
    """Format user identifier for logging (supports email-only users)."""
    return str(user.telegram_id) if user.telegram_id else f'email:{user.id}'


@dataclass(slots=True)
class AutoPurchaseContext:
    """Aggregated data prepared for automatic checkout processing."""

    context: PurchaseOptionsContext
    pricing: PurchasePricingResult
    selection: PurchaseSelection
    service: MiniAppSubscriptionPurchaseService


@dataclass(slots=True)
class AutoExtendContext:
    """Data required to automatically extend an existing subscription."""

    subscription: Subscription
    period_days: int
    price_kopeks: int
    description: str
    device_limit: int | None = None
    traffic_limit_gb: int | None = None
    squad_uuid: str | None = None
    consume_promo_offer: bool = False
    tariff_id: int | None = None
    allowed_squads: list | None = None


async def _prepare_auto_purchase(
    db: AsyncSession,
    user: User,
    cart_data: dict,
) -> AutoPurchaseContext | None:
    """Builds purchase context and pricing for a saved cart."""

    period_days = int(cart_data.get('period_days') or 0)
    if period_days <= 0:
        logger.info(
            '🔁 Автопокупка: у пользователя %s нет корректного периода в сохранённой корзине',
            _format_user_id(user),
        )
        return None

    # Перезагружаем user с нужными связями (user_promo_groups),
    # т.к. после db.refresh() в payment-сервисах связи сбрасываются
    fresh_user = await get_user_by_id(db, user.id)
    if not fresh_user:
        logger.warning(
            '🔁 Автопокупка: не удалось перезагрузить пользователя %s',
            _format_user_id(user),
        )
        return None
    user = fresh_user

    miniapp_service = MiniAppSubscriptionPurchaseService()
    context = await miniapp_service.build_options(db, user)

    period_config = context.period_map.get(f'days:{period_days}')
    if not period_config:
        logger.warning(
            '🔁 Автопокупка: период %s дней недоступен для пользователя %s',
            period_days,
            _format_user_id(user),
        )
        return None

    traffic_value = cart_data.get('traffic_gb')
    if traffic_value is None:
        traffic_value = (
            period_config.traffic.current_value
            if period_config.traffic.current_value is not None
            else period_config.traffic.default_value or 0
        )
    else:
        traffic_value = int(traffic_value)

    devices = int(cart_data.get('devices') or period_config.devices.current or 1)
    servers = list(cart_data.get('countries') or [])
    if not servers:
        servers = list(period_config.servers.default_selection)

    selection = PurchaseSelection(
        period=period_config,
        traffic_value=traffic_value,
        servers=servers,
        devices=devices,
    )

    pricing = await miniapp_service.calculate_pricing(db, context, selection)
    return AutoPurchaseContext(
        context=context,
        pricing=pricing,
        selection=selection,
        service=miniapp_service,
    )


def _safe_int(value: object | None, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _apply_promo_discount_for_tariff(price: int, discount_percent: int) -> int:
    """Применяет скидку промогруппы к цене тарифа."""
    if discount_percent <= 0:
        return price
    discount = int(price * discount_percent / 100)
    return max(0, price - discount)


async def _get_tariff_price_for_period(
    db: AsyncSession,
    user: User,
    tariff_id: int,
    period_days: int,
) -> int | None:
    """Получает актуальную цену тарифа для заданного периода с учётом скидки пользователя."""
    from app.database.crud.tariff import get_tariff_by_id
    from app.utils.promo_offer import get_user_active_promo_discount_percent

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        logger.warning(
            '🔁 Автопокупка: тариф %s недоступен для пользователя %s',
            tariff_id,
            _format_user_id(user),
        )
        return None

    prices = tariff.period_prices or {}
    base_price = prices.get(str(period_days))
    if base_price is None:
        logger.warning(
            '🔁 Автопокупка: период %s дней недоступен для тарифа %s',
            period_days,
            tariff_id,
        )
        return None

    # Получаем скидку пользователя
    discount_percent = 0
    promo_group = getattr(user, 'promo_group', None)
    if promo_group:
        discount_percent = getattr(promo_group, 'server_discount_percent', 0)

    personal_discount = get_user_active_promo_discount_percent(user)
    discount_percent = max(discount_percent, personal_discount)

    final_price = _apply_promo_discount_for_tariff(base_price, discount_percent)
    return final_price


async def _prepare_auto_extend_context(
    db: AsyncSession,
    user: User,
    cart_data: dict,
) -> AutoExtendContext | None:
    from app.database.crud.subscription import get_subscription_by_user_id

    subscription = await get_subscription_by_user_id(db, user.id)
    if subscription is None:
        logger.info(
            '🔁 Автопокупка: у пользователя %s нет активной подписки для продления',
            _format_user_id(user),
        )
        return None

    saved_subscription_id = cart_data.get('subscription_id')
    if saved_subscription_id is not None:
        saved_subscription_id = _safe_int(saved_subscription_id, subscription.id)
        if saved_subscription_id != subscription.id:
            logger.warning(
                '🔁 Автопокупка: сохранённая подписка %s не совпадает с текущей %s у пользователя %s',
                saved_subscription_id,
                subscription.id,
                _format_user_id(user),
            )
            return None

    period_days = _safe_int(cart_data.get('period_days'))

    if period_days <= 0:
        logger.warning(
            '🔁 Автопокупка: некорректное количество дней продления (%s) у пользователя %s',
            period_days,
            _format_user_id(user),
        )
        return None

    # Если в корзине есть tariff_id - пересчитываем цену по актуальному тарифу
    tariff_id = cart_data.get('tariff_id')
    if tariff_id:
        tariff_id = _safe_int(tariff_id)
        price_kopeks = await _get_tariff_price_for_period(db, user, tariff_id, period_days)
        if price_kopeks is None:
            # Тариф недоступен или период отсутствует - используем сохранённую цену как fallback
            price_kopeks = _safe_int(
                cart_data.get('total_price') or cart_data.get('price') or cart_data.get('final_price'),
            )
            logger.warning(
                '🔁 Автопокупка: не удалось пересчитать цену тарифа %s, используем сохранённую: %s',
                tariff_id,
                price_kopeks,
            )
        # Добавляем стоимость докупленных устройств при продлении того же тарифа
        elif subscription.tariff_id == tariff_id:
            from app.database.crud.tariff import get_tariff_by_id as _get_tariff

            _tariff = await _get_tariff(db, tariff_id)
            if _tariff:
                extra_devices = max(0, (subscription.device_limit or 0) - (_tariff.device_limit or 0))
                if extra_devices > 0:
                    from app.utils.pricing_utils import calculate_months_from_days

                    device_price_per_month = _tariff.device_price_kopeks or settings.PRICE_PER_DEVICE
                    months = calculate_months_from_days(period_days)
                    price_kopeks += extra_devices * device_price_per_month * months
    else:
        price_kopeks = _safe_int(
            cart_data.get('total_price') or cart_data.get('price') or cart_data.get('final_price'),
        )

    if price_kopeks <= 0:
        logger.warning(
            '🔁 Автопокупка: некорректная цена продления (%s) у пользователя %s',
            price_kopeks,
            _format_user_id(user),
        )
        return None

    # Формируем описание с учётом тарифа
    if tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, tariff_id)
        tariff_name = tariff.name if tariff else 'тариф'
        description = cart_data.get('description') or f'Продление тарифа {tariff_name} на {period_days} дней'
    else:
        description = cart_data.get('description') or f'Продление подписки на {period_days} дней'

    device_limit = cart_data.get('device_limit')
    if device_limit is not None:
        device_limit = _safe_int(device_limit, subscription.device_limit)

    traffic_limit_gb = cart_data.get('traffic_limit_gb')
    if traffic_limit_gb is not None:
        traffic_limit_gb = _safe_int(traffic_limit_gb, subscription.traffic_limit_gb or 0)

    squad_uuid = cart_data.get('squad_uuid')
    consume_promo_offer = bool(cart_data.get('consume_promo_offer'))
    allowed_squads = cart_data.get('allowed_squads')

    return AutoExtendContext(
        subscription=subscription,
        period_days=period_days,
        price_kopeks=price_kopeks,
        description=description,
        device_limit=device_limit,
        traffic_limit_gb=traffic_limit_gb,
        squad_uuid=squad_uuid,
        consume_promo_offer=consume_promo_offer,
        tariff_id=tariff_id,
        allowed_squads=allowed_squads,
    )


def _apply_extension_updates(context: AutoExtendContext) -> None:
    """
    Применяет обновления лимитов подписки (трафик, устройства, серверы, тариф).
    НЕ изменяет is_trial - это делается позже после успешного коммита продления.
    """
    subscription = context.subscription

    # Обновляем tariff_id если указан в контексте
    if context.tariff_id is not None:
        subscription.tariff_id = context.tariff_id

    # Обновляем allowed_squads если указаны (заменяем полностью)
    if context.allowed_squads is not None:
        subscription.connected_squads = context.allowed_squads

    # Обновляем лимиты для триальной подписки
    if subscription.is_trial:
        # НЕ удаляем триал здесь! Это будет сделано после успешного extend_subscription()
        # subscription.is_trial = False  # УДАЛЕНО: преждевременное удаление триала
        if context.traffic_limit_gb is not None:
            subscription.traffic_limit_gb = context.traffic_limit_gb
        if context.device_limit is not None:
            subscription.device_limit = max(subscription.device_limit, context.device_limit)
        if context.squad_uuid and context.squad_uuid not in (subscription.connected_squads or []):
            subscription.connected_squads = (subscription.connected_squads or []) + [context.squad_uuid]
    else:
        # Обновляем лимиты для платной подписки
        if context.traffic_limit_gb not in (None, 0):
            subscription.traffic_limit_gb = context.traffic_limit_gb
        if context.device_limit is not None and context.device_limit > subscription.device_limit:
            subscription.device_limit = context.device_limit
        if context.squad_uuid and context.squad_uuid not in (subscription.connected_squads or []):
            subscription.connected_squads = (subscription.connected_squads or []) + [context.squad_uuid]


async def _auto_extend_subscription(
    db: AsyncSession,
    user: User,
    cart_data: dict,
    *,
    bot: Bot | None = None,
) -> bool:
    # Lazy import to avoid circular dependency
    from app.cabinet.routes.websocket import notify_user_subscription_renewed

    try:
        prepared = await _prepare_auto_extend_context(db, user, cart_data)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            '❌ Автопокупка: ошибка подготовки данных продления для пользователя %s: %s',
            _format_user_id(user),
            error,
            exc_info=True,
        )
        return False

    if prepared is None:
        return False

    if user.balance_kopeks < prepared.price_kopeks:
        logger.info(
            '🔁 Автопокупка: у пользователя %s недостаточно средств для продления (%s < %s)',
            _format_user_id(user),
            user.balance_kopeks,
            prepared.price_kopeks,
        )
        return False

    subscription = prepared.subscription
    old_end_date = subscription.end_date
    was_trial = subscription.is_trial  # Запоминаем, была ли подписка триальной
    old_tariff_id = subscription.tariff_id  # Запоминаем старый тариф для определения смены

    # Определяем, произошла ли смена тарифа
    is_tariff_change = prepared.tariff_id is not None and old_tariff_id != prepared.tariff_id

    # ВАЖНО: Все операции выполняем в единой транзакции без автокоммита,
    # чтобы при ошибке на любом шаге можно было откатить всё целиком
    try:
        # Шаг 1: Списываем баланс БЕЗ автокоммита
        deducted = await subtract_user_balance(
            db,
            user,
            prepared.price_kopeks,
            prepared.description,
            consume_promo_offer=prepared.consume_promo_offer,
            auto_commit=False,  # Не коммитим сразу!
        )

        if not deducted:
            logger.warning(
                '❌ Автопокупка: списание средств для продления подписки пользователя %s не выполнено',
                _format_user_id(user),
            )
            await db.rollback()
            return False

        _apply_extension_updates(prepared)

        # Шаг 2: Продлеваем подписку БЕЗ автокоммита
        # При смене тарифа передаём traffic_limit_gb для сброса трафика в БД
        updated_subscription = await extend_subscription(
            db,
            subscription,
            prepared.period_days,
            tariff_id=prepared.tariff_id if is_tariff_change else None,
            traffic_limit_gb=prepared.traffic_limit_gb if is_tariff_change else None,
            device_limit=prepared.device_limit if is_tariff_change else None,
            auto_commit=False,  # Не коммитим сразу, коммит будет ниже
        )

        # Шаг 3: Конвертируем триал в платную подписку если нужно
        if was_trial and subscription.is_trial:
            subscription.is_trial = False
            subscription.status = 'active'
            user.has_had_paid_subscription = True
            logger.info(
                '✅ Триал конвертирован в платную подписку %s для пользователя %s',
                subscription.id,
                _format_user_id(user),
            )

        # Шаг 4: Коммитим всю транзакцию целиком
        await db.commit()
        await db.refresh(user)

    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            '❌ Автопокупка: ошибка при продлении подписки пользователя %s: %s',
            _format_user_id(user),
            error,
            exc_info=True,
        )
        # Откатываем ВСЕ изменения включая списание баланса
        await db.rollback()
        return False

    transaction = None
    try:
        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=prepared.price_kopeks,
            description=prepared.description,
        )
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            '⚠️ Автопокупка: не удалось зафиксировать транзакцию продления для пользователя %s: %s',
            _format_user_id(user),
            error,
            exc_info=True,
        )

    subscription_service = SubscriptionService()
    # При смене тарифа ВСЕГДА сбрасываем трафик, иначе по настройке
    should_reset_traffic = is_tariff_change or settings.RESET_TRAFFIC_ON_PAYMENT
    try:
        await subscription_service.update_remnawave_user(
            db,
            updated_subscription,
            reset_traffic=should_reset_traffic,
            reset_reason='смена тарифа' if is_tariff_change else 'продление подписки',
        )
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            '⚠️ Автопокупка: не удалось обновить RemnaWave пользователя %s после продления: %s',
            _format_user_id(user),
            error,
        )

    await user_cart_service.delete_user_cart(user.id)
    await clear_subscription_checkout_draft(user.id)

    texts = get_texts(getattr(user, 'language', 'ru'))
    period_label = format_period_description(
        prepared.period_days,
        getattr(user, 'language', 'ru'),
    )
    new_end_date = updated_subscription.end_date
    end_date_label = format_local_datetime(new_end_date, '%d.%m.%Y %H:%M')

    if bot:
        try:
            notification_service = AdminNotificationService(bot)
            await notification_service.send_subscription_extension_notification(
                db,
                user,
                updated_subscription,
                transaction,
                prepared.period_days,
                old_end_date,
                new_end_date=new_end_date,
                balance_after=user.balance_kopeks,
            )
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                '⚠️ Автопокупка: не удалось уведомить администраторов о продлении пользователя %s: %s',
                _format_user_id(user),
                error,
            )

        # Send user notification only for Telegram users
        if user.telegram_id:
            try:
                auto_message = texts.t(
                    'AUTO_PURCHASE_SUBSCRIPTION_EXTENDED',
                    '✅ Subscription automatically extended for {period}.',
                ).format(period=period_label)
                details_message = texts.t(
                    'AUTO_PURCHASE_SUBSCRIPTION_EXTENDED_DETAILS',
                    'New expiration date: {date}.',
                ).format(date=end_date_label)
                hint_message = texts.t(
                    'AUTO_PURCHASE_SUBSCRIPTION_HINT',
                    "Open the 'My subscription' section to access your link.",
                )

                full_message = '\n\n'.join(
                    part.strip() for part in [auto_message, details_message, hint_message] if part and part.strip()
                )

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 My subscription'),
                                callback_data='menu_subscription',
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '🏠 Main menu'),
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )

                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=full_message,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
            except Exception as error:  # pragma: no cover - defensive logging
                logger.error(
                    '⚠️ Автопокупка: не удалось уведомить пользователя %s о продлении: %s',
                    user.telegram_id or user.id,
                    error,
                )

    logger.info(
        '✅ Автопокупка: подписка продлена на %s дней для пользователя %s',
        prepared.period_days,
        _format_user_id(user),
    )

    # Send WebSocket notification to cabinet frontend
    try:
        await notify_user_subscription_renewed(
            user_id=user.id,
            new_expires_at=new_end_date.isoformat() if new_end_date else '',
            amount_kopeks=prepared.price_kopeks,
        )
    except Exception as ws_error:
        logger.warning(
            '⚠️ Автопокупка: не удалось отправить WS уведомление о продлении для %s: %s',
            _format_user_id(user),
            ws_error,
        )

    return True


async def _auto_purchase_tariff(
    db: AsyncSession,
    user: User,
    cart_data: dict,
    *,
    bot: Bot | None = None,
) -> bool:
    """Автоматическая покупка периодного тарифа из сохранённой корзины."""
    # Lazy imports to avoid circular dependency
    from app.cabinet.routes.websocket import (
        notify_user_subscription_activated,
        notify_user_subscription_renewed,
    )
    from app.database.crud.server_squad import get_all_server_squads
    from app.database.crud.subscription import (
        create_paid_subscription,
        extend_subscription,
        get_subscription_by_user_id,
    )
    from app.database.crud.tariff import get_tariff_by_id
    from app.database.crud.transaction import create_transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import TransactionType

    tariff_id = _safe_int(cart_data.get('tariff_id'))
    period_days = _safe_int(cart_data.get('period_days'))
    discount_percent = _safe_int(cart_data.get('discount_percent'))

    if not tariff_id or period_days <= 0:
        logger.warning(
            '🔁 Автопокупка тарифа: некорректные данные корзины для пользователя %s (tariff_id=%s, period=%s)',
            _format_user_id(user),
            tariff_id,
            period_days,
        )
        return False

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        logger.warning(
            '🔁 Автопокупка тарифа: тариф %s недоступен для пользователя %s',
            tariff_id,
            _format_user_id(user),
        )
        return False

    # Получаем актуальную цену тарифа
    prices = tariff.period_prices or {}
    base_price = prices.get(str(period_days))
    if base_price is None:
        logger.warning(
            '🔁 Автопокупка тарифа: период %s дней недоступен для тарифа %s',
            period_days,
            tariff_id,
        )
        return False

    final_price = _apply_promo_discount_for_tariff(base_price, discount_percent)

    # Проверяем есть ли уже подписка (нужно до расчёта цены для учёта доп. устройств)
    existing_subscription = await get_subscription_by_user_id(db, user.id)

    # Добавляем стоимость докупленных устройств при продлении того же тарифа
    if existing_subscription and existing_subscription.tariff_id == tariff_id:
        extra_devices = max(0, (existing_subscription.device_limit or 0) - (tariff.device_limit or 0))
        if extra_devices > 0:
            from app.utils.pricing_utils import calculate_months_from_days

            device_price_per_month = tariff.device_price_kopeks or settings.PRICE_PER_DEVICE
            months = calculate_months_from_days(period_days)
            extra_devices_cost = extra_devices * device_price_per_month * months
            final_price += extra_devices_cost

    if user.balance_kopeks < final_price:
        logger.info(
            '🔁 Автопокупка тарифа: у пользователя %s недостаточно средств (%s < %s)',
            _format_user_id(user),
            user.balance_kopeks,
            final_price,
        )
        return False

    # Списываем баланс
    try:
        description = f'Покупка тарифа {tariff.name} на {period_days} дней'
        success = await subtract_user_balance(db, user, final_price, description)
        if not success:
            logger.warning(
                '❌ Автопокупка тарифа: не удалось списать баланс пользователя %s',
                _format_user_id(user),
            )
            return False
    except Exception as error:
        logger.error(
            '❌ Автопокупка тарифа: ошибка списания баланса пользователя %s: %s',
            _format_user_id(user),
            error,
            exc_info=True,
        )
        return False

    # Получаем список серверов из тарифа
    squads = tariff.allowed_squads or []
    if not squads:
        all_servers, _ = await get_all_server_squads(db, available_only=True)
        squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

    try:
        if existing_subscription:
            # Продлеваем существующую подписку
            # Сохраняем докупленные устройства при продлении того же тарифа
            if existing_subscription.tariff_id == tariff.id:
                effective_device_limit = max(tariff.device_limit or 0, existing_subscription.device_limit or 0)
            else:
                effective_device_limit = tariff.device_limit
            subscription = await extend_subscription(
                db,
                existing_subscription,
                days=period_days,
                tariff_id=tariff.id,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=effective_device_limit,
                connected_squads=squads,
            )
            was_trial_conversion = existing_subscription.is_trial
            if was_trial_conversion:
                subscription.is_trial = False
                subscription.status = 'active'
                user.has_had_paid_subscription = True
                await db.commit()
        else:
            # Создаём новую подписку
            subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=period_days,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=squads,
                tariff_id=tariff.id,
            )
            was_trial_conversion = False
    except Exception as error:
        logger.error(
            '❌ Автопокупка тарифа: ошибка создания подписки для пользователя %s: %s',
            _format_user_id(user),
            error,
            exc_info=True,
        )
        await db.rollback()
        return False

    # Создаём транзакцию
    try:
        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=final_price,
            description=description,
        )
    except Exception as error:
        logger.warning(
            '⚠️ Автопокупка тарифа: не удалось создать транзакцию для пользователя %s: %s',
            _format_user_id(user),
            error,
        )
        transaction = None

    # Обновляем Remnawave
    # При покупке тарифа ВСЕГДА сбрасываем трафик в панели
    remnawave_success = False
    try:
        subscription_service = SubscriptionService()
        await subscription_service.create_remnawave_user(
            db,
            subscription,
            reset_traffic=True,
            reset_reason='покупка тарифа',
        )
        remnawave_success = True
    except Exception as error:
        logger.error(
            '❌ КРИТИЧНО Автопокупка тарифа: не удалось обновить Remnawave для пользователя %s: %s',
            _format_user_id(user),
            error,
        )
        # КРИТИЧНО: Уведомляем админов об ошибке RemnaWave — деньги списаны, VPN не работает!
        if bot:
            try:
                from app.config import settings as app_settings

                admin_ids = app_settings.ADMIN_IDS or []
                for admin_id in admin_ids[:3]:  # Первые 3 админа
                    await bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f'🚨 <b>КРИТИЧНО: Ошибка RemnaWave при автопокупке тарифа</b>\n\n'
                            f'👤 User ID: {user.id}\n'
                            f'🆔 Telegram: {user.telegram_id or "N/A"}\n'
                            f'📧 Email: {user.email or "N/A"}\n'
                            f'📦 Подписка: {subscription.id}\n'
                            f'💰 Списано: {final_price / 100:.2f} ₽\n'
                            f'❌ Ошибка: {str(error)[:200]}\n\n'
                            f'⚠️ Деньги СПИСАНЫ, но VPN НЕ РАБОТАЕТ!\n'
                            f'Требуется ручное создание в RemnaWave.'
                        ),
                        parse_mode='HTML',
                    )
            except Exception as admin_notify_err:
                logger.error(f'Не удалось уведомить админов об ошибке RemnaWave: {admin_notify_err}')

    # Очищаем корзину
    await user_cart_service.delete_user_cart(user.id)
    await clear_subscription_checkout_draft(user.id)

    # Уведомления
    if bot:
        texts = get_texts(getattr(user, 'language', 'ru'))
        period_label = format_period_description(period_days, getattr(user, 'language', 'ru'))

        try:
            notification_service = AdminNotificationService(bot)
            await notification_service.send_subscription_purchase_notification(
                db, user, subscription, transaction, period_days, was_trial_conversion
            )
        except Exception as error:
            logger.warning(
                '⚠️ Автопокупка тарифа: не удалось уведомить админов о покупке пользователя %s: %s',
                _format_user_id(user),
                error,
            )

        # Send user notification only for Telegram users
        if user.telegram_id:
            try:
                message = texts.t(
                    'AUTO_PURCHASE_SUBSCRIPTION_SUCCESS',
                    '✅ Подписка на {period} автоматически оформлена после пополнения баланса.',
                ).format(period=period_label)

                hint = texts.t(
                    'AUTO_PURCHASE_SUBSCRIPTION_HINT',
                    'Перейдите в раздел «Моя подписка», чтобы получить ссылку.',
                )

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                                callback_data='menu_subscription',
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '🏠 Главное меню'),
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )

                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=f'{message}\n\n{hint}',
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
            except Exception as error:
                logger.warning(
                    '⚠️ Автопокупка тарифа: не удалось уведомить пользователя %s: %s',
                    user.telegram_id or user.id,
                    error,
                )

    logger.info(
        '✅ Автопокупка тарифа: подписка на тариф %s (%s дней) оформлена для пользователя %s',
        tariff.name,
        period_days,
        _format_user_id(user),
    )

    # Send WebSocket notification to cabinet frontend
    try:
        if existing_subscription:
            # Renewal of existing subscription
            await notify_user_subscription_renewed(
                user_id=user.id,
                new_expires_at=subscription.end_date.isoformat() if subscription.end_date else '',
                amount_kopeks=final_price,
            )
        else:
            # New subscription activation
            await notify_user_subscription_activated(
                user_id=user.id,
                expires_at=subscription.end_date.isoformat() if subscription.end_date else '',
                tariff_name=tariff.name,
            )
    except Exception as ws_error:
        logger.warning(
            '⚠️ Автопокупка тарифа: не удалось отправить WS уведомление для %s: %s',
            _format_user_id(user),
            ws_error,
        )

    return True


async def _auto_purchase_daily_tariff(
    db: AsyncSession,
    user: User,
    cart_data: dict,
    *,
    bot: Bot | None = None,
) -> bool:
    """Автоматическая покупка суточного тарифа из сохранённой корзины."""
    from datetime import datetime, timedelta

    # Lazy imports to avoid circular dependency
    from app.cabinet.routes.websocket import (
        notify_user_subscription_activated,
        notify_user_subscription_renewed,
    )
    from app.database.crud.server_squad import get_all_server_squads
    from app.database.crud.subscription import create_paid_subscription, get_subscription_by_user_id
    from app.database.crud.tariff import get_tariff_by_id
    from app.database.crud.transaction import create_transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import TransactionType

    tariff_id = _safe_int(cart_data.get('tariff_id'))
    if not tariff_id:
        logger.warning(
            '🔁 Автопокупка суточного тарифа: нет tariff_id в корзине пользователя %s',
            _format_user_id(user),
        )
        return False

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        logger.warning(
            '🔁 Автопокупка суточного тарифа: тариф %s недоступен для пользователя %s',
            tariff_id,
            _format_user_id(user),
        )
        return False

    if not getattr(tariff, 'is_daily', False):
        logger.warning(
            '🔁 Автопокупка суточного тарифа: тариф %s не является суточным для пользователя %s',
            tariff_id,
            _format_user_id(user),
        )
        return False

    daily_price = getattr(tariff, 'daily_price_kopeks', 0)
    if daily_price <= 0:
        logger.warning(
            '🔁 Автопокупка суточного тарифа: некорректная цена тарифа %s для пользователя %s',
            tariff_id,
            _format_user_id(user),
        )
        return False

    if user.balance_kopeks < daily_price:
        logger.info(
            '🔁 Автопокупка суточного тарифа: у пользователя %s недостаточно средств (%s < %s)',
            _format_user_id(user),
            user.balance_kopeks,
            daily_price,
        )
        return False

    # Списываем баланс за первый день
    try:
        description = f'Активация суточного тарифа {tariff.name}'
        success = await subtract_user_balance(db, user, daily_price, description)
        if not success:
            logger.warning(
                '❌ Автопокупка суточного тарифа: не удалось списать баланс пользователя %s',
                _format_user_id(user),
            )
            return False
    except Exception as error:
        logger.error(
            '❌ Автопокупка суточного тарифа: ошибка списания баланса пользователя %s: %s',
            _format_user_id(user),
            error,
            exc_info=True,
        )
        return False

    # Получаем список серверов из тарифа
    squads = tariff.allowed_squads or []
    if not squads:
        all_servers, _ = await get_all_server_squads(db, available_only=True)
        squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

    # Проверяем есть ли уже подписка
    existing_subscription = await get_subscription_by_user_id(db, user.id)

    try:
        if existing_subscription:
            # Обновляем существующую подписку на суточный тариф
            # Суточность определяется через tariff.is_daily, поэтому достаточно установить tariff_id
            was_trial_conversion = existing_subscription.is_trial  # Сохраняем до изменения
            existing_subscription.tariff_id = tariff.id
            existing_subscription.traffic_limit_gb = tariff.traffic_limit_gb
            existing_subscription.device_limit = tariff.device_limit
            existing_subscription.connected_squads = squads
            existing_subscription.status = 'active'
            existing_subscription.is_trial = False
            existing_subscription.last_daily_charge_at = datetime.utcnow()
            existing_subscription.is_daily_paused = False
            existing_subscription.end_date = datetime.utcnow() + timedelta(days=1)
            if was_trial_conversion:
                user.has_had_paid_subscription = True
            await db.commit()
            await db.refresh(existing_subscription)
            subscription = existing_subscription
        else:
            # Создаём новую суточную подписку
            # Суточность определяется через tariff.is_daily
            subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=1,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=squads,
                tariff_id=tariff.id,
            )
            # Устанавливаем параметры для суточного списания
            subscription.last_daily_charge_at = datetime.utcnow()
            subscription.is_daily_paused = False
            await db.commit()
            was_trial_conversion = False
    except Exception as error:
        logger.error(
            '❌ Автопокупка суточного тарифа: ошибка создания подписки для пользователя %s: %s',
            _format_user_id(user),
            error,
            exc_info=True,
        )
        await db.rollback()
        return False

    # Создаём транзакцию
    try:
        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=daily_price,
            description=description,
        )
    except Exception as error:
        logger.warning(
            '⚠️ Автопокупка суточного тарифа: не удалось создать транзакцию для пользователя %s: %s',
            _format_user_id(user),
            error,
        )
        transaction = None

    # Обновляем Remnawave
    # При покупке тарифа ВСЕГДА сбрасываем трафик в панели
    try:
        subscription_service = SubscriptionService()
        await subscription_service.create_remnawave_user(
            db,
            subscription,
            reset_traffic=True,
            reset_reason='активация суточного тарифа',
        )
    except Exception as error:
        logger.warning(
            '⚠️ Автопокупка суточного тарифа: не удалось обновить Remnawave для пользователя %s: %s',
            _format_user_id(user),
            error,
        )

    # Очищаем корзину
    await user_cart_service.delete_user_cart(user.id)
    await clear_subscription_checkout_draft(user.id)

    # Уведомления
    if bot:
        texts = get_texts(getattr(user, 'language', 'ru'))

        try:
            notification_service = AdminNotificationService(bot)
            await notification_service.send_subscription_purchase_notification(
                db, user, subscription, transaction, 1, was_trial_conversion
            )
        except Exception as error:
            logger.warning(
                '⚠️ Автопокупка суточного тарифа: не удалось уведомить админов о покупке пользователя %s: %s',
                _format_user_id(user),
                error,
            )

        # Send user notification only for Telegram users
        if user.telegram_id:
            try:
                message = (
                    f'✅ <b>Суточный тариф «{tariff.name}» активирован!</b>\n\n'
                    f'💰 Списано: {daily_price / 100:.0f} ₽ за первый день\n'
                    f'🔄 Средства будут списываться автоматически раз в сутки.\n\n'
                    f'ℹ️ Вы можете приостановить подписку в любой момент.'
                )

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                                callback_data='menu_subscription',
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '🏠 Главное меню'),
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )

                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=message,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
            except Exception as error:
                logger.warning(
                    '⚠️ Автопокупка суточного тарифа: не удалось уведомить пользователя %s: %s',
                    user.telegram_id or user.id,
                    error,
                )

    logger.info(
        '✅ Автопокупка суточного тарифа: тариф %s активирован для пользователя %s',
        tariff.name,
        _format_user_id(user),
    )

    # Send WebSocket notification to cabinet frontend
    try:
        if existing_subscription:
            # Renewal/upgrade of existing subscription
            await notify_user_subscription_renewed(
                user_id=user.id,
                new_expires_at=subscription.end_date.isoformat() if subscription.end_date else '',
                amount_kopeks=daily_price,
            )
        else:
            # New subscription activation
            await notify_user_subscription_activated(
                user_id=user.id,
                expires_at=subscription.end_date.isoformat() if subscription.end_date else '',
                tariff_name=tariff.name,
            )
    except Exception as ws_error:
        logger.warning(
            '⚠️ Автопокупка суточного тарифа: не удалось отправить WS уведомление для %s: %s',
            _format_user_id(user),
            ws_error,
        )

    return True


async def auto_purchase_saved_cart_after_topup(
    db: AsyncSession,
    user: User,
    *,
    bot: Bot | None = None,
) -> bool:
    """Attempts to automatically purchase a subscription from a saved cart."""
    from datetime import datetime, timedelta

    # Lazy imports to avoid circular dependency
    from app.cabinet.routes.websocket import (
        notify_user_subscription_activated,
        notify_user_subscription_renewed,
    )
    from app.database.crud.transaction import get_user_transactions

    if not settings.is_auto_purchase_after_topup_enabled():
        return False

    if not user or not getattr(user, 'id', None):
        return False

    cart_data = await user_cart_service.get_user_cart(user.id)
    if not cart_data:
        return False

    logger.info('🔁 Автопокупка: обнаружена сохранённая корзина у пользователя %s', _format_user_id(user))

    cart_mode = cart_data.get('cart_mode') or cart_data.get('mode')

    # Защита от race condition: если подписка была куплена/продлена в последние 60 секунд,
    # пропускаем автопокупку чтобы избежать двойного списания
    if cart_mode in ('extend', 'tariff_purchase', 'daily_tariff_purchase'):
        try:
            recent_transactions = await get_user_transactions(db, user.id, limit=1)
            if recent_transactions:
                last_tx = recent_transactions[0]
                if (
                    last_tx.type == TransactionType.SUBSCRIPTION_PAYMENT
                    and last_tx.created_at
                    and (datetime.utcnow() - last_tx.created_at) < timedelta(seconds=60)
                ):
                    logger.info(
                        '🔁 Автопокупка: пропускаем для пользователя %s - подписка уже куплена %s секунд назад',
                        _format_user_id(user),
                        (datetime.utcnow() - last_tx.created_at).total_seconds(),
                    )
                    # Очищаем корзину чтобы не срабатывало повторно
                    await user_cart_service.delete_user_cart(user.id)
                    return False
        except Exception as check_error:
            logger.warning(
                '🔁 Автопокупка: ошибка проверки последней транзакции для %s: %s',
                _format_user_id(user),
                check_error,
            )

    # Обработка продления подписки
    if cart_mode == 'extend':
        return await _auto_extend_subscription(db, user, cart_data, bot=bot)

    # Обработка покупки периодного тарифа
    if cart_mode == 'tariff_purchase':
        return await _auto_purchase_tariff(db, user, cart_data, bot=bot)

    # Обработка покупки суточного тарифа
    if cart_mode == 'daily_tariff_purchase':
        return await _auto_purchase_daily_tariff(db, user, cart_data, bot=bot)

    try:
        prepared = await _prepare_auto_purchase(db, user, cart_data)
    except PurchaseValidationError as error:
        logger.error(
            '❌ Автопокупка: ошибка валидации корзины пользователя %s: %s',
            _format_user_id(user),
            error,
        )
        return False
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            '❌ Автопокупка: непредвиденная ошибка при подготовке корзины %s: %s',
            _format_user_id(user),
            error,
            exc_info=True,
        )
        return False

    if prepared is None:
        return False

    pricing = prepared.pricing
    selection = prepared.selection

    if pricing.final_total <= 0:
        logger.warning(
            '❌ Автопокупка: итоговая сумма для пользователя %s некорректна (%s)',
            _format_user_id(user),
            pricing.final_total,
        )
        return False

    if user.balance_kopeks < pricing.final_total:
        logger.info(
            '🔁 Автопокупка: у пользователя %s недостаточно средств (%s < %s)',
            _format_user_id(user),
            user.balance_kopeks,
            pricing.final_total,
        )
        return False

    purchase_service = prepared.service

    try:
        purchase_result = await purchase_service.submit_purchase(
            db,
            prepared.context,
            pricing,
        )
    except PurchaseBalanceError:
        logger.info(
            '🔁 Автопокупка: баланс пользователя %s изменился и стал недостаточным',
            _format_user_id(user),
        )
        return False
    except PurchaseValidationError as error:
        logger.error(
            '❌ Автопокупка: не удалось подтвердить корзину пользователя %s: %s',
            _format_user_id(user),
            error,
        )
        return False
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            '❌ Автопокупка: ошибка оформления подписки для пользователя %s: %s',
            _format_user_id(user),
            error,
            exc_info=True,
        )
        return False

    await user_cart_service.delete_user_cart(user.id)
    await clear_subscription_checkout_draft(user.id)

    subscription = purchase_result.get('subscription')
    transaction = purchase_result.get('transaction')
    was_trial_conversion = purchase_result.get('was_trial_conversion', False)
    texts = get_texts(getattr(user, 'language', 'ru'))

    if bot:
        try:
            notification_service = AdminNotificationService(bot)
            await notification_service.send_subscription_purchase_notification(
                db,
                user,
                subscription,
                transaction,
                selection.period.days,
                was_trial_conversion,
            )
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                '⚠️ Автопокупка: не удалось отправить уведомление админам (%s): %s',
                _format_user_id(user),
                error,
            )

        # Send user notification only for Telegram users
        if user.telegram_id:
            try:
                period_label = format_period_description(
                    selection.period.days,
                    getattr(user, 'language', 'ru'),
                )
                auto_message = texts.t(
                    'AUTO_PURCHASE_SUBSCRIPTION_SUCCESS',
                    '✅ Subscription purchased automatically after balance top-up ({period}).',
                ).format(period=period_label)

                hint_message = texts.t(
                    'AUTO_PURCHASE_SUBSCRIPTION_HINT',
                    "Open the 'My subscription' section to access your link.",
                )

                purchase_message = purchase_result.get('message', '')
                full_message = '\n\n'.join(
                    part.strip() for part in [auto_message, purchase_message, hint_message] if part and part.strip()
                )

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 My subscription'),
                                callback_data='menu_subscription',
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '🏠 Main menu'),
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )

                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=full_message,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
            except Exception as error:  # pragma: no cover - defensive logging
                logger.error(
                    '⚠️ Автопокупка: не удалось уведомить пользователя %s: %s',
                    user.telegram_id or user.id,
                    error,
                )

    logger.info(
        '✅ Автопокупка: подписка на %s дней оформлена для пользователя %s',
        selection.period.days,
        _format_user_id(user),
    )

    # Send WebSocket notification to cabinet frontend
    try:
        if was_trial_conversion:
            # Trial conversion = activation
            await notify_user_subscription_activated(
                user_id=user.id,
                expires_at=subscription.end_date.isoformat() if subscription and subscription.end_date else '',
                tariff_name='',
            )
        else:
            # Regular purchase = renewal or new activation
            await notify_user_subscription_renewed(
                user_id=user.id,
                new_expires_at=subscription.end_date.isoformat() if subscription and subscription.end_date else '',
                amount_kopeks=pricing.final_total,
            )
    except Exception as ws_error:
        logger.warning(
            '⚠️ Автопокупка: не удалось отправить WS уведомление для %s: %s',
            _format_user_id(user),
            ws_error,
        )

    return True


async def auto_activate_subscription_after_topup(
    db: AsyncSession,
    user: User,
    *,
    bot: Bot | None = None,
    topup_amount: int | None = None,
) -> tuple[bool, bool]:
    """
    Умная автоактивация после пополнения баланса.

    Работает БЕЗ сохранённой корзины:
    - Если подписка активна — ничего не делает
    - Если подписка истекла — продлевает с теми же параметрами
    - Если подписки нет — создаёт новую с дефолтными параметрами

    Выбирает максимальный период, который можно оплатить из баланса.

    Args:
        topup_amount: Сумма пополнения в копейках (для отображения в уведомлении)

    Returns:
        tuple[bool, bool]: (success, notification_sent)
            - success: True если подписка активирована
            - notification_sent: True если уведомление отправлено пользователю
    """
    from datetime import datetime

    # Lazy imports to avoid circular dependency
    from app.cabinet.routes.websocket import (
        notify_user_subscription_activated,
        notify_user_subscription_renewed,
    )
    from app.database.crud.server_squad import get_available_server_squads, get_server_ids_by_uuids
    from app.database.crud.subscription import create_paid_subscription, get_subscription_by_user_id
    from app.database.crud.transaction import create_transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import PaymentMethod, TransactionType
    from app.services.admin_notification_service import AdminNotificationService
    from app.services.subscription_renewal_service import SubscriptionRenewalService
    from app.services.subscription_service import SubscriptionService

    if not user or not getattr(user, 'id', None):
        return (False, False)

    subscription = await get_subscription_by_user_id(db, user.id)

    # Если автоактивация отключена - уведомление отправится из _send_payment_success_notification
    if not settings.is_auto_activate_after_topup_enabled():
        logger.info(
            '⚠️ Автоактивация отключена для пользователя %s, уведомление будет отправлено из payment service',
            _format_user_id(user),
        )
        return (False, False)

    # Если подписка активна — ничего не делаем (автоактивация включена, но подписка уже есть)
    if subscription and subscription.status == 'ACTIVE' and subscription.end_date > datetime.utcnow():
        logger.info(
            '🔁 Автоактивация: у пользователя %s уже активная подписка, пропускаем',
            _format_user_id(user),
        )
        return (False, False)

    # Определяем параметры подписки
    if subscription:
        device_limit = subscription.device_limit or settings.DEFAULT_DEVICE_LIMIT
        # В режиме fixed_with_topup при автоактивации используем фиксированный лимит
        if settings.is_traffic_fixed():
            traffic_limit_gb = settings.get_fixed_traffic_limit()
        else:
            traffic_limit_gb = subscription.traffic_limit_gb or 0
        connected_squads = subscription.connected_squads or []
    else:
        device_limit = settings.DEFAULT_DEVICE_LIMIT
        # В режиме fixed_with_topup при автоактивации используем фиксированный лимит
        if settings.is_traffic_fixed():
            traffic_limit_gb = settings.get_fixed_traffic_limit()
        else:
            traffic_limit_gb = 0
        connected_squads = []

    # Если серверы не выбраны — берём бесплатные по умолчанию
    if not connected_squads:
        available_servers = await get_available_server_squads(db, promo_group_id=user.promo_group_id)
        connected_squads = [s.squad_uuid for s in available_servers if s.is_available and s.price_kopeks == 0]
        if not connected_squads and available_servers:
            connected_squads = [available_servers[0].squad_uuid]

    server_ids = await get_server_ids_by_uuids(db, connected_squads) if connected_squads else []

    balance = user.balance_kopeks
    available_periods = sorted(settings.get_available_subscription_periods(), reverse=True)

    if not available_periods:
        logger.warning('🔁 Автоактивация: нет доступных периодов подписки')
        return (False, False)

    subscription_service = SubscriptionService()

    # Найти максимальный период <= баланса
    best_period = None
    best_price = 0

    for period in available_periods:
        try:
            price, _ = await subscription_service.calculate_subscription_price_with_months(
                period, traffic_limit_gb, server_ids, device_limit, db, user=user
            )
            if price <= balance:
                best_period = period
                best_price = price
                break
        except Exception as calc_error:
            logger.warning(
                '🔁 Автоактивация: ошибка расчёта цены для периода %s: %s',
                period,
                calc_error,
            )
            continue

    if not best_period:
        logger.info(
            '🔁 Автоактивация: у пользователя %s недостаточно средств (%s) для любого периода',
            _format_user_id(user),
            balance,
        )
        # Уведомление отправится из _send_payment_success_notification
        logger.info(
            '⚠️ Недостаточно средств для автоактивации пользователя %s, уведомление будет отправлено из payment service',
            _format_user_id(user),
        )
        return (False, False)

    texts = get_texts(getattr(user, 'language', 'ru'))

    try:
        if subscription:
            # Продление существующей подписки
            renewal_service = SubscriptionRenewalService()
            pricing = await renewal_service.calculate_pricing(db, user, subscription, best_period)

            result = await renewal_service.finalize(
                db,
                user,
                subscription,
                pricing,
                description=f'Автоматическое продление на {best_period} дней',
                payment_method=PaymentMethod.BALANCE,
            )

            logger.info(
                '✅ Автоактивация: подписка пользователя %s продлена на %s дней за %s коп.',
                _format_user_id(user),
                best_period,
                best_price,
            )

            # Send WebSocket notification to cabinet frontend
            try:
                await notify_user_subscription_renewed(
                    user_id=user.id,
                    new_expires_at=result.subscription.end_date.isoformat() if result.subscription.end_date else '',
                    amount_kopeks=best_price,
                )
            except Exception as ws_error:
                logger.warning(
                    '⚠️ Автоактивация: не удалось отправить WS уведомление о продлении для %s: %s',
                    _format_user_id(user),
                    ws_error,
                )

            # Уведомление пользователю (только для Telegram-пользователей)
            if bot and user.telegram_id:
                try:
                    period_label = format_period_description(best_period, getattr(user, 'language', 'ru'))
                    new_end_date = result.subscription.end_date
                    end_date_str = new_end_date.strftime('%d.%m.%Y') if new_end_date else '—'

                    message = texts.t(
                        'AUTO_PURCHASE_SUBSCRIPTION_EXTENDED',
                        '✅ Подписка автоматически продлена на {period}.',
                    ).format(period=period_label)

                    details = texts.t(
                        'AUTO_PURCHASE_SUBSCRIPTION_EXTENDED_DETAILS',
                        '⏰ Новая дата окончания: {date}.',
                    ).format(date=end_date_str)

                    hint = texts.t(
                        'AUTO_PURCHASE_SUBSCRIPTION_HINT',
                        'Перейдите в раздел «Моя подписка», чтобы получить ссылку.',
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                                    callback_data='menu_subscription',
                                )
                            ],
                        ]
                    )

                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=f'{message}\n{details}\n\n{hint}',
                        reply_markup=keyboard,
                        parse_mode='HTML',
                    )
                except Exception as notify_error:
                    logger.warning(
                        '⚠️ Автоактивация: не удалось уведомить пользователя %s: %s',
                        user.telegram_id or user.id,
                        notify_error,
                    )

        else:
            # Создание новой подписки
            new_subscription = await create_paid_subscription(
                db,
                user.id,
                best_period,
                traffic_limit_gb=traffic_limit_gb,
                device_limit=device_limit,
                connected_squads=connected_squads,
                update_server_counters=True,
            )

            await subtract_user_balance(db, user, best_price, f'Активация подписки на {best_period} дней')

            await subscription_service.create_remnawave_user(db, new_subscription)

            await create_transaction(
                db=db,
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=best_price,
                description=f'Активация подписки на {best_period} дней',
                payment_method=PaymentMethod.BALANCE,
            )

            logger.info(
                '✅ Автоактивация: новая подписка на %s дней создана для пользователя %s за %s коп.',
                best_period,
                _format_user_id(user),
                best_price,
            )

            # Send WebSocket notification to cabinet frontend
            try:
                await notify_user_subscription_activated(
                    user_id=user.id,
                    expires_at=new_subscription.end_date.isoformat() if new_subscription.end_date else '',
                    tariff_name='',
                )
            except Exception as ws_error:
                logger.warning(
                    '⚠️ Автоактивация: не удалось отправить WS уведомление об активации для %s: %s',
                    _format_user_id(user),
                    ws_error,
                )

            # Уведомление пользователю (только для Telegram-пользователей)
            if bot and user.telegram_id:
                try:
                    period_label = format_period_description(best_period, getattr(user, 'language', 'ru'))

                    message = texts.t(
                        'AUTO_PURCHASE_SUBSCRIPTION_SUCCESS',
                        '✅ Подписка на {period} автоматически оформлена после пополнения баланса.',
                    ).format(period=period_label)

                    hint = texts.t(
                        'AUTO_PURCHASE_SUBSCRIPTION_HINT',
                        'Перейдите в раздел «Моя подписка», чтобы получить ссылку.',
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                                    callback_data='menu_subscription',
                                )
                            ],
                        ]
                    )

                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=f'{message}\n\n{hint}',
                        reply_markup=keyboard,
                        parse_mode='HTML',
                    )

                except Exception as notify_error:
                    logger.warning(
                        '⚠️ Автоактивация: не удалось уведомить пользователя %s: %s',
                        user.telegram_id or user.id,
                        notify_error,
                    )

            # Уведомление админам (независимо от telegram_id)
            if bot:
                try:
                    notification_service = AdminNotificationService(bot)
                    await notification_service.send_subscription_purchase_notification(
                        db,
                        user,
                        new_subscription,
                        None,  # transaction
                        best_period,
                        False,  # was_trial_conversion
                    )
                except Exception as admin_error:
                    logger.warning(
                        '⚠️ Автоактивация: не удалось уведомить админов: %s',
                        admin_error,
                    )

        return (True, True)  # success=True, notification_sent=True (об активации)

    except Exception as e:
        logger.error(
            '❌ Автоактивация: ошибка для пользователя %s: %s',
            _format_user_id(user),
            e,
            exc_info=True,
        )
        try:
            await db.rollback()
        except Exception:
            pass
        return (False, False)


__all__ = ['auto_activate_subscription_after_topup', 'auto_purchase_saved_cart_after_topup']
