"""Automatic subscription purchase from a saved cart after balance top-up."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import Subscription, SubscriptionStatus, TransactionType, User
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.pricing_engine import PricingEngine, pricing_engine
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


logger = structlog.get_logger(__name__)


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
    tariff_name: str | None = None
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
            '🔁 Автопокупка: у пользователя нет корректного периода в сохранённой корзине',
            format_user_id=_format_user_id(user),
        )
        return None

    # Блокируем user с нужными связями (user_promo_groups) для защиты от TOCTOU,
    # т.к. после db.refresh() в payment-сервисах связи сбрасываются
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    miniapp_service = MiniAppSubscriptionPurchaseService()
    context = await miniapp_service.build_options(db, user)

    period_config = context.period_map.get(f'days:{period_days}')
    if not period_config:
        logger.warning(
            '🔁 Автопокупка: период дней недоступен для пользователя',
            period_days=period_days,
            format_user_id=_format_user_id(user),
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


async def _delete_cart_for_subscription(user_id: int, cart_data: dict) -> None:
    """Delete the correct cart key(s) for a processed cart entry.

    When ``subscription_id`` is present:
      - deletes the per-subscription key (``user_cart:{uid}:sub:{sid}``)
      - deletes the global key ONLY if it still references the same
        subscription_id (avoids nuking another subscription's global cart)

    When ``subscription_id`` is absent:
      - deletes the global key via ``delete_user_cart`` (which also cascades
        to any associated per-subscription key).
    """
    sub_id = _safe_int(cart_data.get('subscription_id'))
    if sub_id:
        await user_cart_service.delete_subscription_cart(user_id, sub_id)
        # Clean up the global key only when it still holds THIS subscription's data.
        # We read the global cart to compare, avoiding deletion of a newer cart
        # that belongs to a different subscription.
        global_cart = await user_cart_service.get_user_cart(user_id)
        if global_cart and _safe_int(global_cart.get('subscription_id')) == sub_id:
            await user_cart_service.delete_global_cart_only(user_id)
    else:
        await user_cart_service.delete_user_cart(user_id)


async def _prepare_auto_extend_context(
    db: AsyncSession,
    user: User,
    cart_data: dict,
) -> AutoExtendContext | None:
    from app.database.crud.subscription import get_subscription_by_user_id

    saved_subscription_id = cart_data.get('subscription_id')

    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import (
            get_active_subscriptions_by_user_id,
            get_subscription_by_id_for_user,
        )

        if saved_subscription_id is not None:
            parsed_sub_id = _safe_int(saved_subscription_id)
            subscription = await get_subscription_by_id_for_user(db, parsed_sub_id, user.id) if parsed_sub_id else None
            if subscription is None and parsed_sub_id:
                logger.warning(
                    'Автопокупка: subscription_id из корзины не найден у пользователя, '
                    'НЕ используем эвристику (cart привязан к конкретной подписке)',
                    saved_subscription_id=parsed_sub_id,
                    user_id=user.id,
                )
                return None
        else:
            active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            if len(active_subs) == 1:
                subscription = active_subs[0]
            elif len(active_subs) > 1:
                # Multi-tariff: process each subscription with autopay independently
                # The calling code iterates subscriptions_needing_topup which already
                # selects the specific subscription. This fallback means we're in a
                # context without explicit subscription — pick the one with autopay enabled.
                autopay_subs = [s for s in active_subs if getattr(s, 'autopay_enabled', False)]
                if len(autopay_subs) == 1:
                    subscription = autopay_subs[0]
                elif autopay_subs:
                    # Multiple with autopay — log and pick most urgent (fewest days left)
                    subscription = min(autopay_subs, key=lambda s: s.days_left)
                    logger.info(
                        'Multi-tariff: multiple autopay subscriptions, processing most urgent',
                        user_id=user.id,
                        selected_sub_id=subscription.id,
                        days_left=subscription.days_left,
                    )
                else:
                    logger.warning(
                        'Multi-tariff: multiple active subscriptions but none with autopay enabled',
                        user_id=user.id,
                        count=len(active_subs),
                    )
                    return None
            else:
                subscription = None
    else:
        subscription = await get_subscription_by_user_id(db, user.id)
        if subscription is not None and saved_subscription_id is not None:
            parsed_sub_id = _safe_int(saved_subscription_id, subscription.id)
            if parsed_sub_id != subscription.id:
                logger.warning(
                    '🔁 Автопокупка: сохранённая подписка не совпадает с текущей у пользователя',
                    saved_subscription_id=parsed_sub_id,
                    subscription_id=subscription.id,
                    format_user_id=_format_user_id(user),
                )
                return None

    if subscription is None:
        logger.info(
            '🔁 Автопокупка: у пользователя нет активной подписки для продления', format_user_id=_format_user_id(user)
        )
        return None

    # Block auto-renewal of classic subscriptions when tariff mode is enabled
    if settings.is_tariffs_mode() and not subscription.tariff_id:
        logger.info(
            '🔁 Автопокупка: пропускаем классическую подписку без тарифа (режим тарифов включён)',
            format_user_id=_format_user_id(user),
            subscription_id=subscription.id,
        )
        return None

    period_days = _safe_int(cart_data.get('period_days'))

    if period_days <= 0:
        logger.warning(
            '🔁 Автопокупка: некорректное количество дней продления у пользователя',
            period_days=period_days,
            format_user_id=_format_user_id(user),
        )
        return None

    # Fresh pricing via unified PricingEngine (no stale cart prices)
    tariff_id = cart_data.get('tariff_id')
    if tariff_id:
        tariff_id = _safe_int(tariff_id)

    # Validate period_days against tariff or global renewal periods
    if tariff_id:
        from app.database.crud.tariff import get_tariff_by_id as _get_tariff

        _tariff = await _get_tariff(db, tariff_id)
        if _tariff and _tariff.period_prices and not getattr(_tariff, 'is_daily', False):
            available_periods = [int(p) for p in _tariff.period_prices.keys()]
            if period_days not in available_periods:
                logger.warning(
                    '🔁 Автопокупка: period_days из корзины не входит в доступные периоды тарифа',
                    period_days=period_days,
                    available_periods=available_periods,
                    tariff_id=tariff_id,
                    format_user_id=_format_user_id(user),
                )
                return None
    else:
        from app.config import settings as _settings

        available_periods = _settings.get_available_renewal_periods()
        if period_days not in available_periods:
            logger.warning(
                '🔁 Автопокупка: period_days из корзины не входит в доступные периоды продления',
                period_days=period_days,
                available_periods=available_periods,
                format_user_id=_format_user_id(user),
            )
            return None

    from app.database.crud.user import lock_user_for_pricing
    from app.services.pricing_engine import pricing_engine as _pricing_engine
    from app.utils.promo_offer import get_user_active_promo_discount_percent

    user = await lock_user_for_pricing(db, user.id)

    try:
        pricing = await _pricing_engine.calculate_renewal_price(
            db,
            subscription,
            period_days,
            user=user,
        )
        price_kopeks = pricing.final_total
    except Exception as e:
        logger.error(
            'Автопокупка: ошибка PricingEngine, пропускаем автопродление',
            format_user_id=_format_user_id(user),
            error=str(e),
        )
        return None

    if price_kopeks <= 0:
        logger.warning(
            '🔁 Автопокупка: некорректная цена продления у пользователя',
            price_kopeks=price_kopeks,
            format_user_id=_format_user_id(user),
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
    consume_promo_offer = get_user_active_promo_discount_percent(user) > 0
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
        tariff_name=tariff_name if tariff_id else None,
        allowed_squads=allowed_squads,
    )


def _apply_extension_updates(context: AutoExtendContext) -> None:
    """
    Применяет обновления лимитов подписки (трафик, устройства, серверы, тариф).
    НЕ изменяет is_trial - это делается позже после успешного коммита продления.
    """
    subscription = context.subscription

    # НЕ обновляем tariff_id здесь — это делает extend_subscription(),
    # чтобы корректно определить is_tariff_change внутри CRUD

    # Обновляем allowed_squads если указаны (заменяем полностью)
    if context.allowed_squads is not None:
        subscription.connected_squads = context.allowed_squads

    # Обновляем лимиты для триальной подписки
    if subscription.is_trial:
        # НЕ удаляем триал здесь! Это будет сделано после успешного extend_subscription()
        # subscription.is_trial = False  # УДАЛЕНО: преждевременное удаление триала
        if context.traffic_limit_gb is not None:
            subscription.traffic_limit_gb = context.traffic_limit_gb
        # При конвертации триала device_limit должен быть не ниже DEFAULT_DEVICE_LIMIT
        if context.device_limit is not None:
            subscription.device_limit = max(
                subscription.device_limit or 0, context.device_limit, settings.DEFAULT_DEVICE_LIMIT
            )
        else:
            subscription.device_limit = max(subscription.device_limit or 0, settings.DEFAULT_DEVICE_LIMIT)
        if context.squad_uuid and context.squad_uuid not in (subscription.connected_squads or []):
            subscription.connected_squads = (subscription.connected_squads or []) + [context.squad_uuid]
    else:
        # Обновляем лимиты для платной подписки
        if context.traffic_limit_gb not in (None, 0):
            subscription.traffic_limit_gb = context.traffic_limit_gb
        if context.device_limit is not None and context.device_limit > (subscription.device_limit or 0):
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
            '❌ Автопокупка: ошибка подготовки данных продления для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        return False

    if prepared is None:
        return False

    if user.balance_kopeks < prepared.price_kopeks:
        logger.info(
            '🔁 Автопокупка: у пользователя недостаточно средств для продления (<)',
            format_user_id=_format_user_id(user),
            balance_kopeks=user.balance_kopeks,
            price_kopeks=prepared.price_kopeks,
        )
        return False

    # Save promo offer state before charge so we can restore on failure
    saved_promo_percent = (
        int(getattr(user, 'promo_offer_discount_percent', 0) or 0) if prepared.consume_promo_offer else 0
    )
    saved_promo_source = getattr(user, 'promo_offer_discount_source', None) if prepared.consume_promo_offer else None
    saved_promo_expires = (
        getattr(user, 'promo_offer_discount_expires_at', None) if prepared.consume_promo_offer else None
    )

    try:
        deducted = await subtract_user_balance(
            db,
            user,
            prepared.price_kopeks,
            prepared.description,
            consume_promo_offer=prepared.consume_promo_offer,
            mark_as_paid_subscription=True,
        )
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            '❌ Автопокупка: ошибка списания средств при продлении пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        return False

    if not deducted:
        logger.warning(
            '❌ Автопокупка: списание средств для продления подписки пользователя не выполнено',
            format_user_id=_format_user_id(user),
        )
        return False

    subscription = prepared.subscription
    old_end_date = subscription.end_date
    was_trial = subscription.is_trial  # Запоминаем, была ли подписка триальной
    old_tariff_id = subscription.tariff_id  # Запоминаем старый тариф для определения смены

    _apply_extension_updates(prepared)

    # Определяем, произошла ли смена тарифа
    is_tariff_change = prepared.tariff_id is not None and old_tariff_id != prepared.tariff_id

    try:
        # При смене тарифа передаём traffic_limit_gb для сброса трафика в БД
        updated_subscription = await extend_subscription(
            db,
            subscription,
            prepared.period_days,
            tariff_id=prepared.tariff_id if is_tariff_change else None,
            traffic_limit_gb=prepared.traffic_limit_gb if is_tariff_change else None,
            device_limit=prepared.device_limit if is_tariff_change else None,
        )

        # Конвертируем триал в платную подписку ТОЛЬКО после успешного продления
        if was_trial and subscription.is_trial:
            subscription.is_trial = False
            subscription.status = 'active'
            await db.commit()
            logger.info(
                '✅ Триал конвертирован в платную подписку для пользователя',
                subscription_id=subscription.id,
                format_user_id=_format_user_id(user),
            )

    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            '❌ Автопокупка: не удалось продлить подписку пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        await db.rollback()
        # Compensating refund: balance was already committed by subtract_user_balance
        try:
            from app.database.crud.user import add_user_balance

            await add_user_balance(
                db,
                user,
                prepared.price_kopeks,
                'Возврат: ошибка автопродления подписки',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
            )

            # Restore consumed promo offer fields
            if prepared.consume_promo_offer and saved_promo_percent > 0:
                user.promo_offer_discount_percent = saved_promo_percent
                user.promo_offer_discount_source = saved_promo_source
                user.promo_offer_discount_expires_at = saved_promo_expires
                await db.commit()
                logger.info(
                    '💰 Автопокупка: восстановлен промо-оффер после ошибки продления',
                    format_user_id=_format_user_id(user),
                    restored_percent=saved_promo_percent,
                )

            logger.info(
                '💰 Автопокупка: возврат средств после ошибки продления',
                format_user_id=_format_user_id(user),
                refund_kopeks=prepared.price_kopeks,
            )
        except Exception as refund_error:
            logger.critical(
                'CRITICAL: Автопокупка: не удалось вернуть средства после ошибки продления',
                format_user_id=_format_user_id(user),
                price_kopeks=prepared.price_kopeks,
                refund_error=refund_error,
            )
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
            '⚠️ Автопокупка: не удалось зафиксировать транзакцию продления для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )

    subscription_service = SubscriptionService()
    # Сброс трафика: при смене тарифа — по RESET_TRAFFIC_ON_TARIFF_SWITCH, при оплате — по RESET_TRAFFIC_ON_PAYMENT
    if is_tariff_change:
        should_reset_traffic = settings.RESET_TRAFFIC_ON_TARIFF_SWITCH
    else:
        should_reset_traffic = settings.RESET_TRAFFIC_ON_PAYMENT
    try:
        await subscription_service.update_remnawave_user(
            db,
            updated_subscription,
            reset_traffic=should_reset_traffic,
            reset_reason='смена тарифа' if is_tariff_change else 'продление подписки',
            sync_squads=is_tariff_change,
        )
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            '⚠️ Автопокупка: не удалось обновить RemnaWave пользователя после продления',
            format_user_id=_format_user_id(user),
            error=error,
        )

    await _delete_cart_for_subscription(user.id, cart_data)
    await clear_subscription_checkout_draft(user.id)

    texts = get_texts(getattr(user, 'language', 'ru'))
    period_label = format_period_description(
        prepared.period_days,
        getattr(user, 'language', 'ru'),
    )
    new_end_date = updated_subscription.end_date
    end_date_label = format_local_datetime(new_end_date, '%d.%m.%Y %H:%M')

    # Уведомление администраторам (не зависит от наличия bot)
    try:
        from app.services.subscription_renewal_service import with_admin_notification_service

        await with_admin_notification_service(
            lambda svc: svc.send_subscription_extension_notification(
                db,
                user,
                updated_subscription,
                transaction,
                prepared.period_days,
                old_end_date,
                new_end_date=new_end_date,
                balance_after=user.balance_kopeks,
            )
        )
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            '⚠️ Автопокупка: не удалось уведомить администраторов о продлении пользователя',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Send user notification only for Telegram users
    if bot and user.telegram_id:
        try:
            auto_message = texts.t(
                'AUTO_PURCHASE_SUBSCRIPTION_EXTENDED',
                '✅ Subscription automatically extended for {period}.',
            ).format(period=period_label)
            if settings.is_multi_tariff_enabled() and prepared.tariff_name:
                auto_message += f'\n📦 Тариф: «{prepared.tariff_name}»'
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
                '⚠️ Автопокупка: не удалось уведомить пользователя о продлении',
                telegram_id=user.telegram_id or user.id,
                error=error,
            )

    logger.info(
        '✅ Автопокупка: подписка продлена на дней для пользователя',
        period_days=prepared.period_days,
        format_user_id=_format_user_id(user),
    )

    # Send WebSocket notification to cabinet frontend
    try:
        await notify_user_subscription_renewed(
            user_id=user.id,
            subscription_id=subscription.id if subscription else None,
            new_expires_at=new_end_date.isoformat() if new_end_date else '',
            amount_kopeks=prepared.price_kopeks,
        )
    except Exception as ws_error:
        logger.warning(
            '⚠️ Автопокупка: не удалось отправить WS уведомление о продлении для',
            format_user_id=_format_user_id(user),
            ws_error=ws_error,
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

    if not tariff_id or period_days <= 0:
        logger.warning(
            '🔁 Автопокупка тарифа: некорректные данные корзины для пользователя (tariff_id period=)',
            format_user_id=_format_user_id(user),
            tariff_id=tariff_id,
            period_days=period_days,
        )
        return False

    tariff = await get_tariff_by_id(db, tariff_id)
    # Capture name before any db.commit() can expire the ORM object
    tariff_name_for_label = tariff.name if tariff else None
    if not tariff or not tariff.is_active:
        logger.warning(
            '🔁 Автопокупка тарифа: тариф недоступен для пользователя',
            tariff_id=tariff_id,
            format_user_id=_format_user_id(user),
        )
        return False

    # Validate period_days against tariff's configured periods (prevent arbitrary periods from saved cart)
    is_daily_tariff = getattr(tariff, 'is_daily', False)
    if not is_daily_tariff:
        if tariff.period_prices:
            available_periods = [int(p) for p in tariff.period_prices.keys()]
        else:
            available_periods = []
        custom_days_allowed = (
            hasattr(tariff, 'can_purchase_custom_days')
            and tariff.can_purchase_custom_days()
            and hasattr(tariff, 'get_price_for_custom_days')
            and tariff.get_price_for_custom_days(period_days) is not None
        )
        if period_days not in available_periods and not custom_days_allowed:
            logger.warning(
                '🔁 Автопокупка тарифа: period_days не входит в доступные периоды тарифа',
                tariff_id=tariff_id,
                period_days=period_days,
                available_periods=available_periods,
                format_user_id=_format_user_id(user),
            )
            return False

    # Lock user BEFORE price computation to prevent TOCTOU on promo offer
    from app.database.crud.user import lock_user_for_pricing

    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        active_subs = await get_active_subscriptions_by_user_id(db, user.id)
        _cart_sub_id = cart_data.get('subscription_id')
        if _cart_sub_id:
            existing_subscription = next(
                (s for s in active_subs if s.id == int(_cart_sub_id)),
                None,
            )
        else:
            existing_subscription = next(
                (s for s in active_subs if s.tariff_id == tariff_id),
                None,
            )
    else:
        existing_subscription = await get_subscription_by_user_id(db, user.id)

    user = await lock_user_for_pricing(db, user.id)

    # Calculate price via PricingEngine (single source of truth)
    device_limit = None
    if existing_subscription and existing_subscription.tariff_id == tariff_id:
        device_limit = existing_subscription.device_limit

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period_days,
        device_limit=device_limit,
        user=user,
    )
    final_price = result.final_total
    consume_promo = result.promo_offer_discount > 0

    if user.balance_kopeks < final_price:
        logger.info(
            '🔁 Автопокупка тарифа: у пользователя недостаточно средств (<)',
            format_user_id=_format_user_id(user),
            balance_kopeks=user.balance_kopeks,
            final_price=final_price,
        )
        return False

    # Save promo offer state before deduction (for restore on failure)
    saved_promo_percent = int(getattr(user, 'promo_offer_discount_percent', 0) or 0) if consume_promo else 0
    saved_promo_source = getattr(user, 'promo_offer_discount_source', None) if consume_promo else None
    saved_promo_expires = getattr(user, 'promo_offer_discount_expires_at', None) if consume_promo else None

    # Списываем баланс
    try:
        description = f'Покупка тарифа {tariff.name} на {period_days} дней'
        success = await subtract_user_balance(
            db,
            user,
            final_price,
            description,
            consume_promo_offer=consume_promo,
            mark_as_paid_subscription=True,
        )
        if not success:
            logger.warning(
                '❌ Автопокупка тарифа: не удалось списать баланс пользователя', format_user_id=_format_user_id(user)
            )
            return False
    except Exception as error:
        logger.error(
            '❌ Автопокупка тарифа: ошибка списания баланса пользователя',
            format_user_id=_format_user_id(user),
            error=error,
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
            '❌ Автопокупка тарифа: ошибка создания подписки для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        await db.rollback()
        # Compensating refund: balance was already committed by subtract_user_balance
        try:
            from app.database.crud.user import add_user_balance

            await add_user_balance(
                db,
                user,
                final_price,
                'Возврат: ошибка автопокупки тарифа',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
            )
            # Restore promo offer if consumed
            if consume_promo and saved_promo_percent > 0:
                user.promo_offer_discount_percent = saved_promo_percent
                user.promo_offer_discount_source = saved_promo_source
                user.promo_offer_discount_expires_at = saved_promo_expires
                await db.commit()
            logger.info(
                '💰 Автопокупка тарифа: возврат средств после ошибки создания подписки',
                format_user_id=_format_user_id(user),
                refund_kopeks=final_price,
            )
        except Exception as refund_error:
            logger.critical(
                'CRITICAL: Автопокупка тарифа: не удалось вернуть средства после ошибки создания подписки',
                format_user_id=_format_user_id(user),
                price_kopeks=final_price,
                refund_error=refund_error,
            )
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
            '⚠️ Автопокупка тарифа: не удалось создать транзакцию для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
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
            reset_reason='покупка тарифа',
        )
    except Exception as error:
        logger.warning(
            '⚠️ Автопокупка тарифа: не удалось обновить Remnawave для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Очищаем корзину (per-subscription if subscription_id is in cart)
    await _delete_cart_for_subscription(user.id, cart_data)
    await clear_subscription_checkout_draft(user.id)

    # Уведомление администраторам (не зависит от наличия bot)
    try:
        from app.services.subscription_renewal_service import with_admin_notification_service

        await with_admin_notification_service(
            lambda svc: svc.send_subscription_purchase_notification(
                db,
                user,
                subscription,
                transaction,
                period_days,
                was_trial_conversion,
                purchase_type='renewal',
            )
        )
    except Exception as error:
        logger.warning(
            '⚠️ Автопокупка тарифа: не удалось уведомить админов о покупке пользователя',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Send user notification only for Telegram users
    if bot and user.telegram_id:
        try:
            texts = get_texts(getattr(user, 'language', 'ru'))
            period_label = format_period_description(period_days, getattr(user, 'language', 'ru'))

            message = texts.t(
                'AUTO_PURCHASE_SUBSCRIPTION_SUCCESS',
                '✅ Подписка на {period} автоматически оформлена после пополнения баланса.',
            ).format(period=period_label)
            if settings.is_multi_tariff_enabled() and tariff_name_for_label:
                message += f'\n📦 Тариф: «{tariff_name_for_label}»'

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
                '⚠️ Автопокупка тарифа: не удалось уведомить пользователя',
                telegram_id=user.telegram_id or user.id,
                error=error,
            )

    logger.info(
        '✅ Автопокупка тарифа: подписка на тариф (дней) оформлена для пользователя',
        tariff_name=tariff.name,
        period_days=period_days,
        format_user_id=_format_user_id(user),
    )

    # Yandex offline conversions: fire purchase event
    try:
        from app.services.yandex_offline_conv_service import fire_purchase_bg, spawn_bg

        spawn_bg(fire_purchase_bg(user.id, final_price))
    except Exception:
        pass

    # Send WebSocket notification to cabinet frontend
    try:
        if existing_subscription:
            # Renewal of existing subscription
            await notify_user_subscription_renewed(
                user_id=user.id,
                subscription_id=subscription.id if subscription else None,
                new_expires_at=subscription.end_date.isoformat() if subscription.end_date else '',
                amount_kopeks=final_price,
            )
        else:
            # New subscription activation
            await notify_user_subscription_activated(
                user_id=user.id,
                subscription_id=subscription.id if subscription else None,
                expires_at=subscription.end_date.isoformat() if subscription.end_date else '',
                tariff_name=tariff.name,
            )
    except Exception as ws_error:
        logger.warning(
            '⚠️ Автопокупка тарифа: не удалось отправить WS уведомление для',
            format_user_id=_format_user_id(user),
            ws_error=ws_error,
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
            '🔁 Автопокупка суточного тарифа: нет tariff_id в корзине пользователя',
            format_user_id=_format_user_id(user),
        )
        return False

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        logger.warning(
            '🔁 Автопокупка суточного тарифа: тариф недоступен для пользователя',
            tariff_id=tariff_id,
            format_user_id=_format_user_id(user),
        )
        return False

    if not getattr(tariff, 'is_daily', False):
        logger.warning(
            '🔁 Автопокупка суточного тарифа: тариф не является суточным для пользователя',
            tariff_id=tariff_id,
            format_user_id=_format_user_id(user),
        )
        return False

    daily_price = getattr(tariff, 'daily_price_kopeks', 0)
    if daily_price <= 0:
        logger.warning(
            '🔁 Автопокупка суточного тарифа: некорректная цена тарифа для пользователя',
            tariff_id=tariff_id,
            format_user_id=_format_user_id(user),
        )
        return False

    # Блокируем пользователя и применяем скидки (group + promo-offer)
    from app.database.crud.user import lock_user_for_pricing
    from app.utils.promo_offer import get_user_active_promo_discount_percent

    user = await lock_user_for_pricing(db, user.id)

    promo_group = user.get_primary_promo_group()
    group_pct = promo_group.get_discount_percent('period', 1) if promo_group else 0
    offer_pct = get_user_active_promo_discount_percent(user)

    final_price, _, _ = PricingEngine.apply_stacked_discounts(daily_price, group_pct, offer_pct)
    consume_promo = offer_pct > 0

    if user.balance_kopeks < final_price:
        logger.info(
            '🔁 Автопокупка суточного тарифа: у пользователя недостаточно средств (<)',
            format_user_id=_format_user_id(user),
            balance_kopeks=user.balance_kopeks,
            final_price=final_price,
        )
        return False

    # Списываем баланс за первый день
    try:
        description = f'Активация суточного тарифа {tariff.name}'
        success = await subtract_user_balance(
            db,
            user,
            final_price,
            description,
            consume_promo_offer=consume_promo,
            mark_as_paid_subscription=True,
        )
        if not success:
            logger.warning(
                '❌ Автопокупка суточного тарифа: не удалось списать баланс пользователя',
                format_user_id=_format_user_id(user),
            )
            return False
    except Exception as error:
        logger.error(
            '❌ Автопокупка суточного тарифа: ошибка списания баланса пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        return False

    # Получаем список серверов из тарифа
    squads = tariff.allowed_squads or []
    if not squads:
        all_servers, _ = await get_all_server_squads(db, available_only=True)
        squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

    # Проверяем есть ли уже подписка
    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        active_subs = await get_active_subscriptions_by_user_id(db, user.id)
        _cart_sub_id = cart_data.get('subscription_id')
        if _cart_sub_id:
            existing_subscription = next(
                (s for s in active_subs if s.id == int(_cart_sub_id)),
                None,
            )
        else:
            existing_subscription = next(
                (s for s in active_subs if s.tariff_id == tariff_id),
                None,
            )
    else:
        existing_subscription = await get_subscription_by_user_id(db, user.id)

    try:
        if existing_subscription:
            # Обновляем существующую подписку на суточный тариф
            # Суточность определяется через tariff.is_daily, поэтому достаточно установить tariff_id
            was_trial_conversion = existing_subscription.is_trial  # Сохраняем до изменения
            from app.database.crud.subscription import calc_device_limit_on_tariff_switch
            from app.database.crud.tariff import get_tariff_by_id as _get_old_tariff

            old_tariff = (
                await _get_old_tariff(db, existing_subscription.tariff_id) if existing_subscription.tariff_id else None
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
            existing_subscription.is_trial = False
            existing_subscription.last_daily_charge_at = datetime.now(UTC)
            existing_subscription.is_daily_paused = False
            existing_subscription.end_date = datetime.now(UTC) + timedelta(days=1)
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
            subscription.last_daily_charge_at = datetime.now(UTC)
            subscription.is_daily_paused = False
            await db.commit()
            was_trial_conversion = False
    except Exception as error:
        logger.error(
            '❌ Автопокупка суточного тарифа: ошибка создания подписки для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        await db.rollback()
        # Compensating refund: balance was already committed by subtract_user_balance
        try:
            from app.database.crud.user import add_user_balance

            await add_user_balance(
                db,
                user,
                final_price,
                'Возврат: ошибка автопокупки суточного тарифа',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
            )
            logger.info(
                '💰 Автопокупка суточного тарифа: возврат средств после ошибки создания подписки',
                format_user_id=_format_user_id(user),
                refund_kopeks=final_price,
            )
        except Exception as refund_error:
            logger.critical(
                'CRITICAL: Автопокупка суточного тарифа: не удалось вернуть средства',
                format_user_id=_format_user_id(user),
                price_kopeks=final_price,
                refund_error=refund_error,
            )
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
            '⚠️ Автопокупка суточного тарифа: не удалось создать транзакцию для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
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
            '⚠️ Автопокупка суточного тарифа: не удалось обновить Remnawave для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Очищаем корзину (per-subscription if subscription_id is in cart)
    await _delete_cart_for_subscription(user.id, cart_data)
    await clear_subscription_checkout_draft(user.id)

    # Уведомление администраторам (не зависит от наличия bot)
    try:
        from app.services.subscription_renewal_service import with_admin_notification_service

        await with_admin_notification_service(
            lambda svc: svc.send_subscription_purchase_notification(
                db,
                user,
                subscription,
                transaction,
                1,
                was_trial_conversion,
                purchase_type='renewal',
            )
        )
    except Exception as error:
        logger.warning(
            '⚠️ Автопокупка суточного тарифа: не удалось уведомить админов о покупке пользователя',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Send user notification only for Telegram users
    if bot and user.telegram_id:
        try:
            texts = get_texts(getattr(user, 'language', 'ru'))

            message = (
                f'✅ <b>Суточный тариф «{html.escape(tariff.name)}» активирован!</b>\n\n'
                f'💰 Списано: {final_price / 100:.0f} ₽ за первый день\n'
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
                '⚠️ Автопокупка суточного тарифа: не удалось уведомить пользователя',
                telegram_id=user.telegram_id or user.id,
                error=error,
            )

    logger.info(
        '✅ Автопокупка суточного тарифа: тариф активирован для пользователя',
        tariff_name=tariff.name,
        format_user_id=_format_user_id(user),
    )

    # Send WebSocket notification to cabinet frontend
    try:
        if existing_subscription:
            # Renewal/upgrade of existing subscription
            await notify_user_subscription_renewed(
                user_id=user.id,
                subscription_id=subscription.id if subscription else None,
                new_expires_at=subscription.end_date.isoformat() if subscription.end_date else '',
                amount_kopeks=final_price,
            )
        else:
            # New subscription activation
            await notify_user_subscription_activated(
                user_id=user.id,
                subscription_id=subscription.id if subscription else None,
                expires_at=subscription.end_date.isoformat() if subscription.end_date else '',
                tariff_name=tariff.name,
            )
    except Exception as ws_error:
        logger.warning(
            '⚠️ Автопокупка суточного тарифа: не удалось отправить WS уведомление для',
            format_user_id=_format_user_id(user),
            ws_error=ws_error,
        )

    return True


async def _auto_add_devices(
    db: AsyncSession,
    user: User,
    cart_data: dict,
    *,
    bot: Bot | None = None,
) -> bool:
    """Auto-purchase devices from saved cart after balance topup."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.database.crud.user import lock_user_for_pricing, subtract_user_balance
    from app.database.models import PaymentMethod
    from app.utils.pricing_utils import apply_percentage_discount

    devices_to_add = _safe_int(cart_data.get('devices_to_add'))
    cart_price_kopeks = _safe_int(cart_data.get('price_kopeks'))

    if devices_to_add <= 0 or cart_price_kopeks <= 0:
        logger.warning(
            '🔁 Автопокупка устройств: некорректные данные корзины для пользователя (devices price=)',
            format_user_id=_format_user_id(user),
            devices_to_add=devices_to_add,
            cart_price_kopeks=cart_price_kopeks,
        )
        return False

    # Проверяем подписку (with lock to prevent concurrent device modifications)
    _cart_sub_id_devices = _safe_int(cart_data.get('subscription_id'))
    if settings.is_multi_tariff_enabled() and _cart_sub_id_devices:
        locked_result = await db.execute(
            select(Subscription)
            .where(Subscription.id == _cart_sub_id_devices, Subscription.user_id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    else:
        locked_result = await db.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    subscription = locked_result.scalar_one_or_none()
    if not subscription:
        logger.warning('🔁 Автопокупка устройств: у пользователя нет подписки', format_user_id=_format_user_id(user))
        await _delete_cart_for_subscription(user.id, cart_data)
        return False

    if subscription.status not in ('active', 'trial', 'disabled', 'limited', 'ACTIVE', 'TRIAL', 'DISABLED', 'LIMITED'):
        logger.warning(
            '🔁 Автопокупка устройств: подписка пользователя не активна (status=)',
            format_user_id=_format_user_id(user),
            subscription_status=subscription.status,
        )
        await _delete_cart_for_subscription(user.id, cart_data)
        return False

    # Load tariff for device price and max limit
    tariff = None
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    if tariff and tariff.device_price_kopeks is not None:
        tariff_device_price = tariff.device_price_kopeks
        tariff_max_device_limit = tariff.max_device_limit
    else:
        tariff_device_price = settings.PRICE_PER_DEVICE
        tariff_max_device_limit = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None

    # Block purchase if device price is 0 or negative (purchase unavailable for this tariff)
    if not tariff_device_price or tariff_device_price <= 0:
        logger.warning(
            '🔁 Автопокупка устройств: докупка устройств недоступна для тарифа, корзина удалена',
            format_user_id=_format_user_id(user),
            tariff_id=subscription.tariff_id,
            tariff_device_price=tariff_device_price,
        )
        await _delete_cart_for_subscription(user.id, cart_data)
        return False

    # Check max device limit before charging
    old_device_limit = subscription.device_limit or 1
    new_device_limit = old_device_limit + devices_to_add
    if tariff_max_device_limit and new_device_limit > tariff_max_device_limit:
        logger.warning(
            '🔁 Автопокупка устройств: превышен лимит устройств',
            format_user_id=_format_user_id(user),
            current=old_device_limit,
            requested=new_device_limit,
            tariff_max_device_limit=tariff_max_device_limit,
        )
        await _delete_cart_for_subscription(user.id, cart_data)
        return False

    # Lock user BEFORE price computation to prevent TOCTOU on promo-offer/group discount
    user = await lock_user_for_pricing(db, user.id)

    # Recompute price fresh under lock (pricing config may have changed since cart was saved)
    devices_price_per_month = devices_to_add * tariff_device_price
    days_left = max(1, (subscription.end_date - datetime.now(UTC)).days)
    devices_discount_percent = PricingEngine.get_addon_discount_percent(
        user,
        'devices',
        days_left,
    )
    discounted_per_month, _ = apply_percentage_discount(
        devices_price_per_month,
        devices_discount_percent,
    )
    price_kopeks = int(discounted_per_month * days_left / 30)
    price_kopeks = max(100, price_kopeks)

    if price_kopeks != cart_price_kopeks:
        logger.warning(
            '🔁 Автопокупка устройств: пересчитанная цена отличается от корзины',
            format_user_id=_format_user_id(user),
            cart_price_kopeks=cart_price_kopeks,
            recomputed_price_kopeks=price_kopeks,
            devices_discount_percent=devices_discount_percent,
            days_left=days_left,
        )

    # Проверяем баланс (с актуальной ценой)
    if user.balance_kopeks < price_kopeks:
        logger.info(
            '🔁 Автопокупка устройств: у пользователя недостаточно средств (<)',
            format_user_id=_format_user_id(user),
            balance_kopeks=user.balance_kopeks,
            price_kopeks=price_kopeks,
        )
        return False

    # Списываем баланс
    description = f'Покупка {devices_to_add} доп. устройств'
    try:
        success = await subtract_user_balance(
            db,
            user,
            price_kopeks,
            description,
            create_transaction=True,
            payment_method=PaymentMethod.BALANCE,
            transaction_type=TransactionType.SUBSCRIPTION_PAYMENT,
        )
        if not success:
            logger.warning(
                '❌ Автопокупка устройств: не удалось списать баланс пользователя', format_user_id=_format_user_id(user)
            )
            return False
    except Exception as error:
        logger.error(
            '❌ Автопокупка устройств: ошибка списания баланса пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        return False

    # Re-lock subscription after subtract_user_balance committed (released locks)
    relock_result = await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    subscription = relock_result.scalar_one()

    old_device_limit = subscription.device_limit or 1
    new_device_limit = old_device_limit + devices_to_add

    if tariff_max_device_limit and new_device_limit > tariff_max_device_limit:
        # Concurrent modification exceeded limit — refund
        user_refund = await db.execute(
            select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
        )
        refund_user = user_refund.scalar_one()
        refund_user.balance_kopeks += price_kopeks
        await db.commit()
        logger.warning(
            '🔁 Автопокупка устройств: лимит превышен после оплаты, баланс возвращён',
            format_user_id=_format_user_id(user),
        )
        await _delete_cart_for_subscription(user.id, cart_data)
        return False

    # Добавляем устройства (under lock)
    subscription.device_limit = new_device_limit

    try:
        await db.commit()
        await db.refresh(subscription)
    except Exception as error:
        logger.error(
            '❌ Автопокупка устройств: ошибка сохранения подписки пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        await db.rollback()
        return False

    # Реактивируем подписку если она была DISABLED/EXPIRED (например, после LIMITED/EXPIRED в RemnaWave)
    from app.database.crud.subscription import reactivate_subscription

    await reactivate_subscription(db, subscription)

    # Синхронизация с RemnaWave
    try:
        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)
        # Явно включаем пользователя на панели (PATCH может не снять LIMITED-статус)
        _panel_uuid = (
            subscription.remnawave_uuid
            if settings.is_multi_tariff_enabled() and subscription.remnawave_uuid
            else getattr(user, 'remnawave_uuid', None)
        )
        if _panel_uuid and subscription.status == 'active':
            await subscription_service.enable_remnawave_user(_panel_uuid)
    except Exception as error:
        logger.warning(
            '⚠️ Автопокупка устройств: не удалось обновить Remnawave для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Очищаем корзину (транзакция уже создана в subtract_user_balance)
    await _delete_cart_for_subscription(user.id, cart_data)

    logger.info(
        '✅ Автопокупка устройств: пользователь добавил устройств (было , стало) за коп.',
        format_user_id=_format_user_id(user),
        devices_to_add=devices_to_add,
        old_device_limit=old_device_limit,
        device_limit=subscription.device_limit,
        price_kopeks=price_kopeks,
    )

    # WebSocket уведомление для кабинета
    try:
        from app.cabinet.routes.websocket import notify_user_devices_purchased

        await notify_user_devices_purchased(
            user_id=user.id,
            devices_added=devices_to_add,
            new_device_limit=subscription.device_limit,
            amount_kopeks=price_kopeks,
        )
    except Exception as ws_error:
        logger.warning('⚠️ Автопокупка устройств: не удалось отправить WebSocket уведомление', ws_error=ws_error)

    # Уведомление пользователю
    if bot and user.telegram_id:
        texts = get_texts(getattr(user, 'language', 'ru'))
        try:
            message = texts.t(
                'AUTO_PURCHASE_DEVICES_SUCCESS',
                (
                    '✅ <b>Устройства добавлены автоматически!</b>\n\n'
                    '📱 Добавлено: {devices_to_add} устройств\n'
                    '📊 Новый лимит: {new_limit} устройств\n'
                    '💰 Списано: {price}'
                ),
            ).format(
                devices_to_add=devices_to_add,
                new_limit=subscription.device_limit,
                price=texts.format_price(price_kopeks),
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
                '⚠️ Автопокупка устройств: не удалось уведомить пользователя', telegram_id=user.telegram_id, error=error
            )

    # Уведомление админам
    if bot:
        try:
            notification_service = AdminNotificationService(bot)
            await notification_service.send_subscription_update_notification(
                db,
                user,
                subscription,
                'devices',
                old_device_limit,
                subscription.device_limit,
                price_kopeks,
            )
        except Exception as error:
            logger.warning('⚠️ Автопокупка устройств: не удалось уведомить админов', error=error)

    return True


async def _auto_add_traffic(
    db: AsyncSession,
    user: User,
    cart_data: dict,
    *,
    bot: Bot | None = None,
) -> bool:
    """Auto-purchase traffic from saved cart after balance topup."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.database.crud.subscription import add_subscription_traffic, get_subscription_by_user_id
    from app.database.crud.user import lock_user_for_pricing, subtract_user_balance
    from app.database.models import PaymentMethod
    from app.utils.pricing_utils import calculate_prorated_price

    traffic_gb = _safe_int(cart_data.get('traffic_gb'))
    cart_price_kopeks = _safe_int(cart_data.get('price_kopeks'))

    if traffic_gb <= 0 or cart_price_kopeks <= 0:
        logger.warning(
            '🔁 Автопокупка трафика: некорректные данные корзины для пользователя (traffic_gb price=)',
            format_user_id=_format_user_id(user),
            traffic_gb=traffic_gb,
            cart_price_kopeks=cart_price_kopeks,
        )
        return False

    # Verify subscription
    saved_subscription_id = cart_data.get('subscription_id')

    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        if saved_subscription_id is not None:
            from app.database.crud.subscription import get_subscription_by_id_for_user

            parsed_sub_id = _safe_int(saved_subscription_id)
            subscription = await get_subscription_by_id_for_user(db, parsed_sub_id, user.id) if parsed_sub_id else None
            if subscription is None and parsed_sub_id:
                logger.warning(
                    'Автопокупка трафика: subscription_id из корзины не найден у пользователя, НЕ используем эвристику',
                    saved_subscription_id=parsed_sub_id,
                    user_id=user.id,
                )
                return False
        else:
            active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            if len(active_subs) == 1:
                subscription = active_subs[0]
            elif len(active_subs) > 1:
                # Multi-tariff: pick the subscription with autopay enabled for add-traffic.
                # The calling code iterates per-subscription, so this fallback handles
                # contexts where no explicit subscription_id was provided.
                autopay_subs = [s for s in active_subs if getattr(s, 'autopay_enabled', False)]
                if len(autopay_subs) == 1:
                    subscription = autopay_subs[0]
                elif autopay_subs:
                    # Multiple with autopay — pick most urgent (fewest days left)
                    subscription = min(autopay_subs, key=lambda s: s.days_left)
                    logger.info(
                        'Multi-tariff: multiple autopay subscriptions for add-traffic, processing most urgent',
                        user_id=user.id,
                        selected_sub_id=subscription.id,
                        days_left=subscription.days_left,
                    )
                else:
                    logger.warning(
                        'Multi-tariff: multiple active subscriptions but none with autopay enabled for add-traffic',
                        user_id=user.id,
                        count=len(active_subs),
                    )
                    return False
            else:
                subscription = None
    else:
        subscription = await get_subscription_by_user_id(db, user.id)
    if not subscription:
        logger.warning('🔁 Автопокупка трафика: у пользователя нет подписки', format_user_id=_format_user_id(user))
        await _delete_cart_for_subscription(user.id, cart_data)
        return False

    if subscription.status not in ('active', 'trial', 'disabled', 'limited', 'ACTIVE', 'TRIAL', 'DISABLED', 'LIMITED'):
        logger.warning(
            '🔁 Автопокупка трафика: подписка пользователя не активна (status=)',
            format_user_id=_format_user_id(user),
            subscription_status=subscription.status,
        )
        await _delete_cart_for_subscription(user.id, cart_data)
        return False

    if subscription.is_trial:
        logger.warning('🔁 Автопокупка трафика: у пользователя пробная подписка', format_user_id=_format_user_id(user))
        await _delete_cart_for_subscription(user.id, cart_data)
        return False

    if subscription.traffic_limit_gb == 0:
        logger.warning(
            '🔁 Автопокупка трафика: у пользователя уже безлимитный трафик', format_user_id=_format_user_id(user)
        )
        await _delete_cart_for_subscription(user.id, cart_data)
        return False

    # Lock user BEFORE price computation to prevent TOCTOU on promo-offer/group discount
    user = await lock_user_for_pricing(db, user.id)

    # Recompute base price from tariff/settings (config may have changed since cart was saved)
    tariff = None
    if settings.is_tariffs_mode() and subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    if tariff and tariff.can_topup_traffic():
        base_price = tariff.get_traffic_topup_price(traffic_gb) or 0
    else:
        base_price = settings.get_traffic_topup_price(traffic_gb)

    if base_price <= 0 and traffic_gb != 0:
        logger.warning(
            '🔁 Автопокупка трафика: цена пакета не настроена, корзина удалена',
            format_user_id=_format_user_id(user),
            traffic_gb=traffic_gb,
        )
        await _delete_cart_for_subscription(user.id, cart_data)
        return False

    # Apply traffic discount from promo group
    period_hint_days: int | None = None
    if subscription.end_date:
        days_remaining = (subscription.end_date - datetime.now(UTC)).days
        period_hint_days = days_remaining if days_remaining > 0 else None

    discounted_per_month, _, _ = PricingEngine.calculate_traffic_discount(
        base_price,
        user,
        period_hint_days,
    )

    # Prorate for classic mode (tariff mode uses monthly price as-is)
    is_tariff_mode = settings.is_tariffs_mode() and subscription.tariff_id
    if is_tariff_mode:
        price_kopeks = discounted_per_month
    elif subscription and subscription.end_date:
        price_kopeks, _ = calculate_prorated_price(discounted_per_month, subscription.end_date)
    else:
        price_kopeks = discounted_per_month

    if cart_price_kopeks != price_kopeks:
        logger.warning(
            '🔁 Автопокупка трафика: пересчитанная цена отличается от корзины',
            format_user_id=_format_user_id(user),
            cart_price_kopeks=cart_price_kopeks,
            recomputed_price_kopeks=price_kopeks,
            base_price=base_price,
            discounted_per_month=discounted_per_month,
            period_hint_days=period_hint_days,
        )

    # Verify balance (with fresh price)
    if user.balance_kopeks < price_kopeks:
        logger.info(
            '🔁 Автопокупка трафика: у пользователя недостаточно средств (<)',
            format_user_id=_format_user_id(user),
            balance_kopeks=user.balance_kopeks,
            price_kopeks=price_kopeks,
        )
        return False

    # Deduct balance
    description = f'Докупка {traffic_gb} ГБ трафика'
    try:
        success = await subtract_user_balance(
            db,
            user,
            price_kopeks,
            description,
            create_transaction=True,
            payment_method=PaymentMethod.BALANCE,
            transaction_type=TransactionType.SUBSCRIPTION_PAYMENT,
        )
        if not success:
            logger.warning(
                '❌ Автопокупка трафика: не удалось списать баланс пользователя', format_user_id=_format_user_id(user)
            )
            return False
    except Exception as error:
        logger.error(
            '❌ Автопокупка трафика: ошибка списания баланса пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        return False

    # Add traffic
    old_traffic_limit = subscription.traffic_limit_gb or 0
    try:
        await add_subscription_traffic(db, subscription, traffic_gb)
        await db.commit()
        await db.refresh(subscription)
    except Exception as error:
        logger.error(
            '❌ Автопокупка трафика: ошибка добавления трафика пользователю',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        await db.rollback()
        # Compensating refund: balance was already committed by subtract_user_balance
        try:
            from app.database.crud.user import add_user_balance

            await add_user_balance(
                db,
                user,
                price_kopeks,
                'Возврат: ошибка автопокупки трафика',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
            )
            logger.info(
                '💰 Автопокупка трафика: возврат средств после ошибки добавления трафика',
                format_user_id=_format_user_id(user),
                refund_kopeks=price_kopeks,
            )
        except Exception as refund_error:
            logger.critical(
                'CRITICAL: Автопокупка трафика: не удалось вернуть средства',
                format_user_id=_format_user_id(user),
                price_kopeks=price_kopeks,
                refund_error=refund_error,
            )
        return False

    # Реактивируем подписку если она была DISABLED/EXPIRED (например, после LIMITED/EXPIRED в RemnaWave)
    from app.database.crud.subscription import reactivate_subscription

    await reactivate_subscription(db, subscription)

    # Sync with RemnaWave
    try:
        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)
        # Явно включаем пользователя на панели (PATCH может не снять LIMITED-статус)
        _panel_uuid = (
            subscription.remnawave_uuid
            if settings.is_multi_tariff_enabled() and subscription.remnawave_uuid
            else getattr(user, 'remnawave_uuid', None)
        )
        if _panel_uuid and subscription.status == 'active':
            await subscription_service.enable_remnawave_user(_panel_uuid)
    except Exception as error:
        logger.warning(
            '⚠️ Автопокупка трафика: не удалось обновить Remnawave для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Clear cart (transaction already created in subtract_user_balance)
    await _delete_cart_for_subscription(user.id, cart_data)

    logger.info(
        '✅ Автопокупка трафика: пользователь добавил ГБ (было , стало) за коп.',
        format_user_id=_format_user_id(user),
        traffic_gb=traffic_gb,
        old_traffic_limit=old_traffic_limit,
        traffic_limit_gb=subscription.traffic_limit_gb,
        price_kopeks=price_kopeks,
    )

    # WebSocket notification for cabinet
    try:
        from app.cabinet.routes.websocket import notify_user_traffic_purchased

        await notify_user_traffic_purchased(
            user_id=user.id,
            traffic_gb_added=traffic_gb,
            new_traffic_limit_gb=subscription.traffic_limit_gb or 0,
            amount_kopeks=price_kopeks,
        )
    except Exception as ws_error:
        logger.warning('⚠️ Автопокупка трафика: не удалось отправить WebSocket уведомление', ws_error=ws_error)

    # User notification
    if bot and user.telegram_id:
        texts = get_texts(getattr(user, 'language', 'ru'))
        try:
            message = texts.t(
                'AUTO_PURCHASE_TRAFFIC_SUCCESS',
                (
                    '✅ <b>Трафик добавлен автоматически!</b>\n\n'
                    '📈 Добавлено: {traffic_gb} ГБ\n'
                    '📊 Новый лимит: {new_limit} ГБ\n'
                    '💰 Списано: {price}'
                ),
            ).format(
                traffic_gb=traffic_gb,
                new_limit=subscription.traffic_limit_gb,
                price=texts.format_price(price_kopeks),
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
                '⚠️ Автопокупка трафика: не удалось уведомить пользователя', telegram_id=user.telegram_id, error=error
            )

    # Admin notification
    if bot:
        try:
            notification_service = AdminNotificationService(bot)
            await notification_service.send_subscription_update_notification(
                db,
                user,
                subscription,
                'traffic',
                old_traffic_limit,
                subscription.traffic_limit_gb,
                price_kopeks,
            )
        except Exception as error:
            logger.warning('⚠️ Автопокупка трафика: не удалось уведомить админов', error=error)

    return True


async def try_auto_extend_expired_after_topup(
    db: AsyncSession,
    user: User,
    *,
    bot: Bot | None = None,
) -> bool:
    """Try to auto-extend an expired subscription after balance top-up.

    Unlike cart-based auto-purchase, this works without a saved cart.
    It finds the user's expired subscription and attempts to extend it
    with the shortest available period if the balance is sufficient.

    Returns True if the subscription was successfully extended.
    """
    from app.cabinet.routes.websocket import notify_user_subscription_renewed
    from app.database.crud.subscription import get_subscription_by_user_id

    if not user or not getattr(user, 'id', None):
        return False

    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_all_subscriptions_by_user_id

        all_subs = await get_all_subscriptions_by_user_id(db, user.id)
        expired_subs = [s for s in all_subs if s.status == SubscriptionStatus.EXPIRED.value and not s.is_trial]
        if not expired_subs:
            subscription = None
        else:
            # Pick the most recently expired -- most likely what user wants to renew
            subscription = max(expired_subs, key=lambda s: s.end_date or datetime.min.replace(tzinfo=UTC))
    else:
        subscription = await get_subscription_by_user_id(db, user.id)
    if subscription is None:
        logger.debug(
            '🔄 Автопродление expired: у пользователя нет подписки',
            format_user_id=_format_user_id(user),
        )
        return False

    # Only process expired subscriptions (not trial, not disabled)
    if subscription.status != SubscriptionStatus.EXPIRED.value:
        return False
    if subscription.is_trial:
        return False

    # Only process subscriptions expired within the last 30 days
    if subscription.end_date is None:
        return False
    expired_delta = datetime.now(UTC) - subscription.end_date
    if expired_delta.days > 30:
        logger.info(
            '🔄 Автопродление expired: подписка истекла более 30 дней назад',
            format_user_id=_format_user_id(user),
            expired_days=expired_delta.days,
        )
        return False

    # Determine renewal period from tariff or default to 30 days
    tariff = getattr(subscription, 'tariff', None)
    # Capture name before any db.commit() can expire the ORM object
    tariff_name_for_label = tariff.name if tariff else None
    if tariff:
        period_days = tariff.get_shortest_period() or 30
    else:
        period_days = 30

    # Lock user BEFORE price computation to prevent TOCTOU on promo offer
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    # Calculate renewal price via PricingEngine
    subscription_service = SubscriptionService()
    try:
        pricing = await pricing_engine.calculate_renewal_price(
            db,
            subscription,
            period_days,
            user=user,
        )
        renewal_cost = pricing.final_total
    except Exception as error:
        logger.error(
            '❌ Автопродление expired: ошибка расчёта стоимости',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        return False

    logger.info(
        'Расчёт цены автопродления (PricingEngine)',
        user_id=getattr(user, 'id', None),
        period_days=period_days,
        final_total=pricing.final_total,
        is_tariff_mode=pricing.is_tariff_mode,
        breakdown=pricing.breakdown,
    )

    if renewal_cost <= 0:
        logger.warning(
            '❌ Автопродление expired: некорректная стоимость',
            format_user_id=_format_user_id(user),
            renewal_cost=renewal_cost,
        )
        return False

    # Check balance
    if user.balance_kopeks < renewal_cost:
        logger.info(
            '🔄 Автопродление expired: недостаточно средств',
            format_user_id=_format_user_id(user),
            balance_kopeks=user.balance_kopeks,
            renewal_cost=renewal_cost,
        )
        return False

    # Race condition guard (per-subscription): skip if THIS subscription was
    # modified in the last 60 seconds (indicates a concurrent renewal just landed).
    try:
        await db.refresh(subscription, attribute_names=['updated_at'])
        if subscription.updated_at and (datetime.now(UTC) - subscription.updated_at) < timedelta(seconds=60):
            logger.info(
                '🔄 Автопродление expired: пропуск — подписка обновлена секунд назад',
                format_user_id=_format_user_id(user),
                subscription_id=subscription.id,
                total_seconds=(datetime.now(UTC) - subscription.updated_at).total_seconds(),
            )
            return False
    except Exception as check_error:
        logger.warning(
            '🔄 Автопродление expired: ошибка проверки updated_at подписки',
            format_user_id=_format_user_id(user),
            check_error=check_error,
        )

    # Derive consume_promo_offer from PricingEngine result (user already locked above)
    consume_promo_offer = pricing.promo_offer_discount > 0

    # Save promo offer state before deduction (for restore on failure)
    saved_promo_percent = int(getattr(user, 'promo_offer_discount_percent', 0) or 0) if consume_promo_offer else 0
    saved_promo_source = getattr(user, 'promo_offer_discount_source', None) if consume_promo_offer else None
    saved_promo_expires = getattr(user, 'promo_offer_discount_expires_at', None) if consume_promo_offer else None

    # Deduct balance
    description = f'Автопродление истёкшей подписки на {period_days} дней'
    try:
        deducted = await subtract_user_balance(
            db,
            user,
            renewal_cost,
            description,
            consume_promo_offer=consume_promo_offer,
            mark_as_paid_subscription=True,
        )
    except Exception as error:
        logger.error(
            '❌ Автопродление expired: ошибка списания средств',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        return False

    if not deducted:
        logger.warning(
            '❌ Автопродление expired: списание средств не выполнено',
            format_user_id=_format_user_id(user),
        )
        return False

    old_end_date = subscription.end_date
    was_trial = subscription.is_trial

    # Extend subscription
    try:
        updated_subscription = await extend_subscription(db, subscription, period_days)

        # Convert trial to paid if needed
        if was_trial and subscription.is_trial:
            subscription.is_trial = False
            subscription.status = 'active'
            await db.commit()
            logger.info(
                '✅ Триал конвертирован в платную подписку (автопродление expired)',
                subscription_id=subscription.id,
                format_user_id=_format_user_id(user),
            )
    except Exception as error:
        logger.error(
            '❌ Автопродление expired: не удалось продлить подписку',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        await db.rollback()
        # Compensating refund: balance was already committed by subtract_user_balance
        try:
            from app.database.crud.user import add_user_balance

            await add_user_balance(
                db,
                user,
                renewal_cost,
                'Возврат: ошибка автопродления истёкшей подписки',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
            )
            # Restore promo offer if consumed
            if consume_promo_offer and saved_promo_percent > 0:
                user.promo_offer_discount_percent = saved_promo_percent
                user.promo_offer_discount_source = saved_promo_source
                user.promo_offer_discount_expires_at = saved_promo_expires
                await db.commit()
            logger.info(
                '💰 Автопродление expired: возврат средств после ошибки продления',
                format_user_id=_format_user_id(user),
                refund_kopeks=renewal_cost,
            )
        except Exception as refund_error:
            logger.critical(
                'CRITICAL: Автопродление expired: не удалось вернуть средства',
                format_user_id=_format_user_id(user),
                price_kopeks=renewal_cost,
                refund_error=refund_error,
            )
        return False

    # Create transaction record
    transaction = None
    try:
        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=renewal_cost,
            description=description,
        )
    except Exception as error:
        logger.error(
            '⚠️ Автопродление expired: не удалось зафиксировать транзакцию',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )

    # Update RemnaWave
    try:
        await subscription_service.update_remnawave_user(
            db,
            updated_subscription,
            reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
            reset_reason='автопродление истёкшей подписки',
        )
    except Exception as error:
        logger.error(
            '⚠️ Автопродление expired: не удалось обновить RemnaWave',
            format_user_id=_format_user_id(user),
            error=error,
        )

    texts = get_texts(getattr(user, 'language', 'ru'))
    period_label = format_period_description(period_days, getattr(user, 'language', 'ru'))
    new_end_date = updated_subscription.end_date
    end_date_label = format_local_datetime(new_end_date, '%d.%m.%Y %H:%M')

    # Admin notification
    try:
        from app.services.subscription_renewal_service import with_admin_notification_service

        await with_admin_notification_service(
            lambda svc: svc.send_subscription_extension_notification(
                db,
                user,
                updated_subscription,
                transaction,
                period_days,
                old_end_date,
                new_end_date=new_end_date,
                balance_after=user.balance_kopeks,
            )
        )
    except Exception as error:
        logger.error(
            '⚠️ Автопродление expired: не удалось уведомить администраторов',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Send user notification (only for Telegram users)
    if bot and user.telegram_id:
        try:
            auto_message = texts.t(
                'AUTO_PURCHASE_SUBSCRIPTION_EXTENDED',
                '✅ Subscription automatically extended for {period}.',
            ).format(period=period_label)
            if settings.is_multi_tariff_enabled() and tariff_name_for_label:
                auto_message += f'\n📦 Тариф: «{tariff_name_for_label}»'
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
        except Exception as error:
            logger.error(
                '⚠️ Автопродление expired: не удалось уведомить пользователя',
                telegram_id=user.telegram_id or user.id,
                error=error,
            )

    logger.info(
        '✅ Автопродление expired: подписка продлена для пользователя',
        period_days=period_days,
        renewal_cost=renewal_cost,
        format_user_id=_format_user_id(user),
    )

    # Send WebSocket notification
    try:
        await notify_user_subscription_renewed(
            user_id=user.id,
            subscription_id=subscription.id if subscription else None,
            new_expires_at=new_end_date.isoformat() if new_end_date else '',
            amount_kopeks=renewal_cost,
        )
    except Exception as ws_error:
        logger.warning(
            '⚠️ Автопродление expired: не удалось отправить WS уведомление',
            format_user_id=_format_user_id(user),
            ws_error=ws_error,
        )

    return True


async def try_resume_disabled_daily_after_topup(
    db: AsyncSession,
    user: User,
    *,
    bot: Bot | None = None,
) -> bool:
    """Resume a DISABLED daily subscription immediately after balance top-up.

    Daily subscriptions get DISABLED when balance is insufficient.
    The DailySubscriptionService loop picks them up every 30 minutes,
    but this function provides instant resumption right when the user tops up.

    Returns True if the subscription was successfully resumed and charged.
    """
    from app.cabinet.routes.websocket import notify_user_subscription_renewed
    from app.database.crud.subscription import get_subscription_by_user_id, update_daily_charge_time

    if not user or not getattr(user, 'id', None):
        return False

    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_all_subscriptions_by_user_id

        _target_statuses = (
            SubscriptionStatus.DISABLED.value,
            SubscriptionStatus.EXPIRED.value,
            SubscriptionStatus.LIMITED.value,
        )
        all_subs = await get_all_subscriptions_by_user_id(db, user.id)
        disabled_daily = [
            s
            for s in all_subs
            if s.status in _target_statuses
            and getattr(s, 'is_daily_tariff', False)
            and not s.is_trial
            and not getattr(s, 'is_daily_paused', False)
        ]
        if not disabled_daily:
            subscription = None
        else:
            # get_all orders active first then newest; pick first matching candidate
            subscription = disabled_daily[0]
    else:
        subscription = await get_subscription_by_user_id(db, user.id)
    if subscription is None:
        return False

    # Only handle DISABLED/LIMITED (or EXPIRED) daily tariff subscriptions
    if subscription.status not in (
        SubscriptionStatus.DISABLED.value,
        SubscriptionStatus.EXPIRED.value,
        SubscriptionStatus.LIMITED.value,
    ):
        return False
    if not getattr(subscription, 'is_daily_tariff', False):
        return False
    if subscription.is_trial:
        return False
    # Skip user-paused subscriptions — they chose to pause, don't auto-resume
    if getattr(subscription, 'is_daily_paused', False):
        return False

    tariff = getattr(subscription, 'tariff', None)
    if not tariff:
        return False

    raw_daily_price = getattr(tariff, 'daily_price_kopeks', 0)
    if raw_daily_price <= 0:
        return False

    # Lock user row to prevent TOCTOU between discount read and balance charge
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    # Apply group discount to daily price (consistent with PricingEngine._calculate_switch_to_daily)
    from app.services.pricing_engine import PricingEngine

    promo_group = PricingEngine.resolve_promo_group(user)
    daily_group_pct = promo_group.get_discount_percent('period', 1) if promo_group else 0
    daily_price = (
        PricingEngine.apply_discount(raw_daily_price, daily_group_pct) if daily_group_pct > 0 else raw_daily_price
    )

    # Check balance (uses locked user's balance_kopeks — safe from concurrent reads)
    if user.balance_kopeks < daily_price:
        logger.info(
            '🔄 Авто-возобновление daily: недостаточно средств',
            format_user_id=_format_user_id(user),
            balance_kopeks=user.balance_kopeks,
            daily_price=daily_price,
        )
        return False

    # Race condition guard (per-subscription): skip if THIS daily subscription
    # was modified in the last 60 seconds (indicates a concurrent charge just landed).
    try:
        await db.refresh(subscription, attribute_names=['updated_at'])
        if subscription.updated_at and (datetime.now(UTC) - subscription.updated_at) < timedelta(seconds=60):
            logger.info(
                '🔄 Авто-возобновление daily: пропуск — подписка обновлена секунд назад',
                format_user_id=_format_user_id(user),
                subscription_id=subscription.id,
                total_seconds=(datetime.now(UTC) - subscription.updated_at).total_seconds(),
            )
            return False
    except Exception as check_error:
        logger.warning(
            '🔄 Авто-возобновление daily: ошибка проверки updated_at подписки',
            format_user_id=_format_user_id(user),
            check_error=check_error,
        )

    # Deduct daily price FIRST (before changing status to avoid free-access window)
    previous_status = subscription.status
    description = f'Суточная оплата тарифа «{tariff.name}» (авто-возобновление)'
    try:
        deducted = await subtract_user_balance(
            db,
            user,
            daily_price,
            description,
            mark_as_paid_subscription=True,
        )
    except Exception as error:
        logger.error(
            '❌ Авто-возобновление daily: ошибка списания средств',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        return False

    if not deducted:
        logger.warning(
            '❌ Авто-возобновление daily: списание не выполнено',
            format_user_id=_format_user_id(user),
        )
        return False

    # Activate the subscription (balance already deducted)
    subscription.status = SubscriptionStatus.ACTIVE.value
    try:
        await db.commit()
        await db.refresh(subscription)
    except Exception as error:
        logger.error(
            '❌ Авто-возобновление daily: ошибка активации подписки',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        await db.rollback()
        # Compensating refund: balance was already committed by subtract_user_balance
        try:
            from app.database.crud.user import add_user_balance

            await add_user_balance(
                db,
                user,
                daily_price,
                'Возврат: ошибка авто-возобновления суточной подписки',
                create_transaction=True,
                transaction_type=TransactionType.REFUND,
            )
            logger.info(
                '💰 Авто-возобновление daily: возврат средств после ошибки активации',
                format_user_id=_format_user_id(user),
                refund_kopeks=daily_price,
            )
        except Exception as refund_error:
            logger.critical(
                'CRITICAL: Авто-возобновление daily: не удалось вернуть средства',
                format_user_id=_format_user_id(user),
                price_kopeks=daily_price,
                refund_error=refund_error,
            )
        return False

    logger.info(
        '✅ Авто-возобновление daily: подписка → ACTIVE после пополнения',
        format_user_id=_format_user_id(user),
        previous_status=previous_status,
        subscription_id=subscription.id,
    )

    # Create transaction
    transaction = None
    try:
        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=daily_price,
            description=description,
        )
    except Exception as error:
        logger.error(
            '⚠️ Авто-возобновление daily: не удалось создать транзакцию',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Update charge time and end_date (+24h)
    old_end_date = subscription.end_date
    try:
        subscription = await update_daily_charge_time(db, subscription)
    except Exception as error:
        logger.error(
            '⚠️ Авто-возобновление daily: не удалось обновить время списания',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Restore connected_squads from tariff if cleared by deactivation sync
    try:
        if not subscription.connected_squads:
            squads = tariff.allowed_squads or []
            if not squads:
                from app.database.crud.server_squad import get_all_server_squads

                all_servers, _ = await get_all_server_squads(db, available_only=True, limit=10000)
                squads = [s.squad_uuid for s in all_servers if s.squad_uuid]
            if squads:
                subscription.connected_squads = squads
                await db.commit()
                await db.refresh(subscription)
    except Exception as error:
        logger.warning(
            '⚠️ Авто-возобновление daily: не удалось восстановить connected_squads',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Sync with RemnaWave
    try:
        subscription_service = SubscriptionService()
        if getattr(user, 'remnawave_uuid', None):
            await subscription_service.update_remnawave_user(
                db,
                subscription,
                reset_traffic=False,
                reset_reason=None,
                sync_squads=True,
            )
        else:
            await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=False,
                reset_reason=None,
            )
            # POST may ignore activeInternalSquads — follow up with PATCH
            await db.refresh(user)
            if getattr(user, 'remnawave_uuid', None) and subscription.connected_squads:
                try:
                    await subscription_service.update_remnawave_user(
                        db,
                        subscription,
                        reset_traffic=False,
                        sync_squads=True,
                    )
                except Exception as patch_err:
                    logger.warning(
                        '⚠️ Авто-возобновление daily: не удалось синхронизировать сквады',
                        format_user_id=_format_user_id(user),
                        error=patch_err,
                    )
    except Exception as error:
        logger.error(
            '⚠️ Авто-возобновление daily: не удалось обновить RemnaWave',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # Admin notification
    try:
        from app.services.subscription_renewal_service import with_admin_notification_service

        await with_admin_notification_service(
            lambda svc: svc.send_subscription_extension_notification(
                db,
                user,
                subscription,
                transaction,
                1,
                old_end_date,
                new_end_date=subscription.end_date,
                balance_after=user.balance_kopeks,
            )
        )
    except Exception as error:
        logger.error(
            '⚠️ Авто-возобновление daily: не удалось уведомить администраторов',
            format_user_id=_format_user_id(user),
            error=error,
        )

    # User notification
    if bot and user.telegram_id:
        try:
            texts = get_texts(getattr(user, 'language', 'ru'))

            message = texts.t(
                'DAILY_SUBSCRIPTION_RESUMED_AFTER_TOPUP',
                '✅ <b>Подписка возобновлена!</b>\n\n'
                'Ваш суточный тариф «{tariff_name}» возобновлён после пополнения баланса.\n\n'
                '💳 Списано: {amount}\n'
                '💰 Остаток: {balance}',
            ).format(
                tariff_name=html.escape(tariff.name),
                amount=settings.format_price(daily_price),
                balance=settings.format_price(user.balance_kopeks),
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
                text=message,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        except Exception as error:
            logger.error(
                '⚠️ Авто-возобновление daily: не удалось уведомить пользователя',
                telegram_id=user.telegram_id or user.id,
                error=error,
            )

    logger.info(
        '✅ Авто-возобновление daily: подписка возобновлена для пользователя',
        format_user_id=_format_user_id(user),
        daily_price=daily_price,
        tariff_name=tariff.name,
    )

    # WebSocket notification
    try:
        await notify_user_subscription_renewed(
            user_id=user.id,
            subscription_id=subscription.id if subscription else None,
            new_expires_at=subscription.end_date.isoformat() if subscription.end_date else '',
            amount_kopeks=daily_price,
        )
    except Exception as ws_error:
        logger.warning(
            '⚠️ Авто-возобновление daily: не удалось отправить WS уведомление',
            format_user_id=_format_user_id(user),
            ws_error=ws_error,
        )

    return True


async def _is_subscription_disabled(
    db: AsyncSession,
    user: User,
    subscription_id: int | None,
) -> bool:
    """Check whether the target subscription is DISABLED.

    When *subscription_id* is given, only that subscription is checked.
    Otherwise falls back to heuristic selection (single-tariff or best
    active subscription).
    """
    from app.database.crud.subscription import get_subscription_by_user_id as _get_sub

    if subscription_id:
        from app.database.crud.subscription import get_subscription_by_id_for_user

        sub = await get_subscription_by_id_for_user(db, subscription_id, user.id)
        return sub is not None and sub.status == SubscriptionStatus.DISABLED.value

    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        _active_subs = await get_active_subscriptions_by_user_id(db, user.id)
        if len(_active_subs) == 1:
            _existing_sub = _active_subs[0]
        elif _active_subs:
            _non_daily = [s for s in _active_subs if not getattr(s, 'is_daily_tariff', False)]
            _pool = _non_daily or _active_subs
            _existing_sub = max(_pool, key=lambda s: s.days_left)
        else:
            _existing_sub = None
    else:
        _existing_sub = await _get_sub(db, user.id)

    return _existing_sub is not None and _existing_sub.status == SubscriptionStatus.DISABLED.value


async def _process_single_cart(
    db: AsyncSession,
    user: User,
    cart_data: dict,
    *,
    bot: Bot | None = None,
) -> bool:
    """Process a single cart entry.  Returns True if purchase succeeded."""
    from app.database.crud.transaction import get_user_transactions

    cart_mode = cart_data.get('cart_mode') or cart_data.get('mode')
    cart_sub_id = _safe_int(cart_data.get('subscription_id'))

    # Guard: DISABLED subscription -- stale cart
    if await _is_subscription_disabled(db, user, cart_sub_id or None):
        logger.warning(
            'Автопокупка: пропускаем -- подписка DISABLED, корзина устарела',
            format_user_id=_format_user_id(user),
            subscription_id=cart_sub_id,
        )
        await _delete_cart_for_subscription(user.id, cart_data)
        await clear_subscription_checkout_draft(user.id)
        return False

    # Race condition guard (per-subscription): skip if THIS subscription was
    # modified in the last 60 seconds (indicates a concurrent purchase just landed).
    # When cart_sub_id is available we check the specific subscription's updated_at;
    # otherwise fall back to the user-global last transaction check.
    if cart_mode in ('extend', 'tariff_purchase', 'daily_tariff_purchase'):
        try:
            if cart_sub_id:
                from app.database.crud.subscription import get_subscription_by_id_for_user

                target_sub = await get_subscription_by_id_for_user(db, cart_sub_id, user.id)
                if (
                    target_sub
                    and target_sub.updated_at
                    and (datetime.now(UTC) - target_sub.updated_at) < timedelta(seconds=60)
                ):
                    logger.info(
                        'Автопокупка: пропускаем -- подписка обновлена секунд назад',
                        format_user_id=_format_user_id(user),
                        subscription_id=cart_sub_id,
                        total_seconds=(datetime.now(UTC) - target_sub.updated_at).total_seconds(),
                    )
                    return False
            else:
                recent_transactions = await get_user_transactions(db, user.id, limit=1)
                if recent_transactions:
                    last_tx = recent_transactions[0]
                    if (
                        last_tx.type == TransactionType.SUBSCRIPTION_PAYMENT
                        and last_tx.created_at
                        and (datetime.now(UTC) - last_tx.created_at) < timedelta(seconds=60)
                    ):
                        logger.info(
                            'Автопокупка: пропускаем -- подписка уже куплена секунд назад',
                            format_user_id=_format_user_id(user),
                            total_seconds=(datetime.now(UTC) - last_tx.created_at).total_seconds(),
                        )
                        return False
        except Exception as check_error:
            logger.warning(
                'Автопокупка: ошибка проверки последней транзакции',
                format_user_id=_format_user_id(user),
                check_error=check_error,
            )

    if cart_mode == 'extend':
        return await _auto_extend_subscription(db, user, cart_data, bot=bot)
    if cart_mode == 'tariff_purchase':
        return await _auto_purchase_tariff(db, user, cart_data, bot=bot)
    if cart_mode == 'daily_tariff_purchase':
        return await _auto_purchase_daily_tariff(db, user, cart_data, bot=bot)
    if cart_mode == 'add_devices':
        return await _auto_add_devices(db, user, cart_data, bot=bot)
    if cart_mode == 'add_traffic':
        return await _auto_add_traffic(db, user, cart_data, bot=bot)

    logger.warning(
        'Автопокупка: неизвестный cart_mode, пропускаем',
        format_user_id=_format_user_id(user),
        cart_mode=cart_mode,
    )
    return False


async def auto_purchase_saved_cart_after_topup(
    db: AsyncSession,
    user: User,
    *,
    bot: Bot | None = None,
) -> bool:
    """Attempts to automatically purchase subscriptions from saved carts.

    Supports both per-subscription carts (``user_cart:{user_id}:sub:{sub_id}``)
    and the legacy global cart (``user_cart:{user_id}``).  When multiple
    per-subscription carts exist (multi-tariff mode), each is processed
    independently so that one subscription's cart cannot shadow another's.
    """

    if not settings.is_auto_purchase_after_topup_enabled():
        return False

    if not user or not getattr(user, 'id', None):
        return False

    # Collect all carts: per-subscription + global (deduplicated)
    carts_to_process: list[dict] = []
    seen_subscription_ids: set[int] = set()

    # 1. Per-subscription carts (multi-tariff safe)
    per_sub_carts = await user_cart_service.get_all_subscription_carts(user.id)
    for cart in per_sub_carts:
        sub_id = _safe_int(cart.get('subscription_id'))
        if sub_id:
            seen_subscription_ids.add(sub_id)
        carts_to_process.append(cart)

    # 2. Global cart (backward compat): only add if its subscription_id
    #    is not already covered by a per-subscription cart.
    global_cart = await user_cart_service.get_user_cart(user.id)
    if global_cart:
        global_sub_id = _safe_int(global_cart.get('subscription_id'))
        if global_sub_id and global_sub_id in seen_subscription_ids:
            pass  # Already covered by per-subscription cart
        else:
            carts_to_process.append(global_cart)

    if not carts_to_process:
        return False

    logger.info(
        'Автопокупка: обнаружено корзин у пользователя',
        format_user_id=_format_user_id(user),
        cart_count=len(carts_to_process),
    )

    any_succeeded = False
    for cart_data in carts_to_process:
        cart_mode = cart_data.get('cart_mode') or cart_data.get('mode')

        # For non-mode carts (legacy generic purchase), handle separately below
        if cart_mode:
            result = await _process_single_cart(db, user, cart_data, bot=bot)
            if result:
                any_succeeded = True
            continue

        # Legacy generic purchase flow (no cart_mode -- old-style cart from FSM state)
        result = await _process_legacy_generic_cart(db, user, cart_data, bot=bot)
        if result:
            any_succeeded = True

    return any_succeeded


async def _process_legacy_generic_cart(
    db: AsyncSession,
    user: User,
    cart_data: dict,
    *,
    bot: Bot | None = None,
) -> bool:
    """Handle old-style carts without an explicit cart_mode (generic FSM carts)."""
    # Lazy imports to avoid circular dependency
    from app.cabinet.routes.websocket import (
        notify_user_subscription_activated,
        notify_user_subscription_renewed,
    )

    try:
        prepared = await _prepare_auto_purchase(db, user, cart_data)
    except PurchaseValidationError as error:
        logger.error(
            'Автопокупка: ошибка валидации корзины пользователя', format_user_id=_format_user_id(user), error=error
        )
        return False
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            'Автопокупка: непредвиденная ошибка при подготовке корзины',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        return False

    if prepared is None:
        return False

    pricing = prepared.pricing
    selection = prepared.selection

    if pricing.final_total <= 0:
        logger.warning(
            'Автопокупка: итоговая сумма для пользователя некорректна',
            format_user_id=_format_user_id(user),
            final_total=pricing.final_total,
        )
        return False

    if user.balance_kopeks < pricing.final_total:
        logger.info(
            'Автопокупка: у пользователя недостаточно средств',
            format_user_id=_format_user_id(user),
            balance_kopeks=user.balance_kopeks,
            final_total=pricing.final_total,
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
            'Автопокупка: баланс пользователя изменился и стал недостаточным', format_user_id=_format_user_id(user)
        )
        return False
    except PurchaseValidationError as error:
        logger.error(
            'Автопокупка: не удалось подтвердить корзину пользователя',
            format_user_id=_format_user_id(user),
            error=error,
        )
        return False
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            'Автопокупка: ошибка оформления подписки для пользователя',
            format_user_id=_format_user_id(user),
            error=error,
            exc_info=True,
        )
        return False

    await _delete_cart_for_subscription(user.id, cart_data)
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
                purchase_type='renewal',
            )
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                'Автопокупка: не удалось отправить уведомление админам',
                format_user_id=_format_user_id(user),
                error=error,
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
                if settings.is_multi_tariff_enabled() and subscription and getattr(subscription, 'tariff_id', None):
                    try:
                        from app.database.crud.tariff import get_tariff_by_id as _get_tariff_label

                        _t = await _get_tariff_label(db, subscription.tariff_id)
                        if _t:
                            auto_message += f'\n📦 Тариф: «{_t.name}»'
                    except Exception:
                        pass

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
                    'Автопокупка: не удалось уведомить пользователя',
                    telegram_id=user.telegram_id or user.id,
                    error=error,
                )

    logger.info(
        'Автопокупка: подписка оформлена для пользователя',
        days=selection.period.days,
        format_user_id=_format_user_id(user),
    )

    # Send WebSocket notification to cabinet frontend
    try:
        if was_trial_conversion:
            # Trial conversion = activation
            await notify_user_subscription_activated(
                user_id=user.id,
                subscription_id=subscription.id if subscription else None,
                expires_at=subscription.end_date.isoformat() if subscription and subscription.end_date else '',
                tariff_name='',
            )
        else:
            # Regular purchase = renewal or new activation
            await notify_user_subscription_renewed(
                user_id=user.id,
                subscription_id=subscription.id if subscription else None,
                new_expires_at=subscription.end_date.isoformat() if subscription and subscription.end_date else '',
                amount_kopeks=pricing.final_total,
            )
    except Exception as ws_error:
        logger.warning(
            'Автопокупка: не удалось отправить WS уведомление',
            format_user_id=_format_user_id(user),
            ws_error=ws_error,
        )

    return True


__all__ = ['auto_purchase_saved_cart_after_topup']
