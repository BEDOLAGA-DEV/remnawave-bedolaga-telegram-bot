"""Handler for subscription reissue (revoke + regenerate link)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_subscription_by_id_for_user
from app.database.models import Subscription, User
from app.localization.texts import get_texts
from app.services.subscription_service import SubscriptionService
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


def _check_revoke_cooldown(subscription: Subscription) -> int | None:
    """Returns remaining seconds if on cooldown, None if ready."""
    if not subscription.last_revoke_at:
        return None
    elapsed = (datetime.now(UTC) - subscription.last_revoke_at).total_seconds()
    cooldown = settings.SUBSCRIPTION_REVOKE_COOLDOWN_SECONDS
    if elapsed < cooldown:
        return int(cooldown - elapsed)
    return None


def _build_revoke_confirm_keyboard(
    language: str,
    multi_tariff: bool = False,
) -> InlineKeyboardMarkup:
    """Build confirmation keyboard for revoke action."""
    texts = get_texts(language)
    back_callback = 'my_subscriptions' if multi_tariff else 'subscription_settings'
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_REVOKE_CONFIRM_BTN', '✅ Подтвердить'),
                    callback_data='subscription_revoke_confirm',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=back_callback,
                ),
            ],
        ]
    )


def _build_revoke_success_keyboard(
    language: str,
    multi_tariff: bool = False,
) -> InlineKeyboardMarkup:
    """Build success keyboard with connect and back buttons."""
    texts = get_texts(language)
    back_callback = 'my_subscriptions' if multi_tariff else 'menu_subscription'
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_REVOKE_CONNECT_BTN', '🔗 Подключиться'),
                    callback_data='subscription_connect',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=back_callback,
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Classic mode (single subscription)
# ---------------------------------------------------------------------------


@error_handler
async def start_subscription_revoke(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext | None = None,
) -> None:
    """Show revoke confirmation for classic single-subscription mode."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)

    if not settings.is_subscription_revoke_enabled():
        await callback.answer(
            texts.t('SUBSCRIPTION_REVOKE_DISABLED', 'Перевыпуск подписки недоступен'),
            show_alert=True,
        )
        return

    subscription = db_user.subscription
    if not subscription or not subscription.is_active:
        await callback.answer(
            texts.t('SUBSCRIPTION_NOT_FOUND', 'Подписка не найдена'),
            show_alert=True,
        )
        return

    # Check cooldown
    remaining = _check_revoke_cooldown(subscription)
    if remaining is not None:
        minutes = remaining // 60
        seconds = remaining % 60
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_REVOKE_COOLDOWN',
                '⏱ Перевыпуск будет доступен через {minutes} мин. {seconds} сек.',
            ).format(minutes=minutes, seconds=seconds),
            show_alert=True,
        )
        return

    await callback.answer()

    await callback.message.edit_text(
        texts.t(
            'SUBSCRIPTION_REVOKE_WARNING',
            (
                '⚠️ <b>Перевыпуск подписки</b>\n\n'
                'Это действие:\n'
                '• Сгенерирует новую ссылку подключения\n'
                '• Сбросит все подключённые устройства\n'
                '• Старая ссылка перестанет работать\n\n'
                'Продолжить?'
            ),
        ),
        reply_markup=_build_revoke_confirm_keyboard(db_user.language, multi_tariff=False),
        parse_mode='HTML',
    )


@error_handler
async def confirm_subscription_revoke(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext | None = None,
) -> None:
    """Execute revoke for classic or multi-tariff mode (uses FSM state for multi)."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)

    if not settings.is_subscription_revoke_enabled():
        await callback.answer(
            texts.t('SUBSCRIPTION_REVOKE_DISABLED', 'Перевыпуск подписки недоступен'),
            show_alert=True,
        )
        return

    # Determine subscription: multi-tariff via FSM state or classic via db_user
    is_multi = False
    subscription: Subscription | None = None

    if state:
        data = await state.get_data()
        revoke_sub_id = data.get('revoke_sub_id')
        if revoke_sub_id is not None:
            is_multi = True
            subscription = await get_subscription_by_id_for_user(db, revoke_sub_id, db_user.id)
            if not subscription:
                await callback.answer(
                    texts.t('SUBSCRIPTION_NOT_FOUND', 'Подписка не найдена'),
                    show_alert=True,
                )
                return

    if subscription is None:
        subscription = db_user.subscription

    if not subscription or not subscription.is_active:
        await callback.answer(
            texts.t('SUBSCRIPTION_NOT_FOUND', 'Подписка не найдена'),
            show_alert=True,
        )
        return

    # TOCTOU protection: re-check cooldown
    remaining = _check_revoke_cooldown(subscription)
    if remaining is not None:
        minutes = remaining // 60
        seconds = remaining % 60
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_REVOKE_COOLDOWN',
                '⏱ Перевыпуск будет доступен через {minutes} мин. {seconds} сек.',
            ).format(minutes=minutes, seconds=seconds),
            show_alert=True,
        )
        return

    # Answer callback BEFORE heavy operation
    await callback.answer()

    # Execute revoke
    sub_service = SubscriptionService()
    new_url = await sub_service.revoke_subscription(db, subscription)

    if not new_url:
        await callback.message.edit_text(
            texts.t('SUBSCRIPTION_REVOKE_ERROR', '❌ Ошибка при перевыпуске подписки. Попробуйте позже.'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')],
                ]
            ),
            parse_mode='HTML',
        )
        return

    # Update cooldown timestamp
    subscription.last_revoke_at = datetime.now(UTC)
    await db.commit()

    logger.info(
        'Subscription revoked successfully',
        user_id=db_user.id,
        subscription_id=subscription.id,
        is_multi=is_multi,
    )

    # Clean up FSM state
    if state and is_multi:
        await state.update_data(revoke_sub_id=None)

    await callback.message.edit_text(
        texts.t(
            'SUBSCRIPTION_REVOKE_SUCCESS',
            (
                '✅ <b>Подписка перевыпущена!</b>\n\n'
                'Новая ссылка подключения готова. '
                'Старая ссылка больше не действительна.\n\n'
                'Все устройства были отключены.'
            ),
        ),
        reply_markup=_build_revoke_success_keyboard(db_user.language, multi_tariff=is_multi),
        parse_mode='HTML',
    )


# ---------------------------------------------------------------------------
# Multi-tariff mode
# ---------------------------------------------------------------------------


@error_handler
async def start_multi_revoke(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Show revoke confirmation for multi-tariff mode (callback_data = 'sr:{sub_id}')."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)

    if not settings.is_subscription_revoke_enabled():
        await callback.answer(
            texts.t('SUBSCRIPTION_REVOKE_DISABLED', 'Перевыпуск подписки недоступен'),
            show_alert=True,
        )
        return

    # Extract sub_id from callback_data
    parts = (callback.data or '').split(':')
    if len(parts) < 2:
        await callback.answer('Неверный формат', show_alert=True)
        return

    try:
        sub_id = int(parts[1])
    except (ValueError, TypeError):
        await callback.answer('Неверный формат', show_alert=True)
        return

    # Validate ownership (IDOR protection)
    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer(
            texts.t('SUBSCRIPTION_NOT_FOUND', 'Подписка не найдена'),
            show_alert=True,
        )
        return

    if not subscription.is_active:
        await callback.answer(
            texts.t('SUBSCRIPTION_NOT_FOUND', 'Подписка не найдена'),
            show_alert=True,
        )
        return

    # Check cooldown
    remaining = _check_revoke_cooldown(subscription)
    if remaining is not None:
        minutes = remaining // 60
        seconds = remaining % 60
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_REVOKE_COOLDOWN',
                '⏱ Перевыпуск будет доступен через {minutes} мин. {seconds} сек.',
            ).format(minutes=minutes, seconds=seconds),
            show_alert=True,
        )
        return

    # Store sub_id in FSM state for the confirmation handler
    await state.update_data(revoke_sub_id=sub_id)

    await callback.answer()

    await callback.message.edit_text(
        texts.t(
            'SUBSCRIPTION_REVOKE_WARNING',
            (
                '⚠️ <b>Перевыпуск подписки</b>\n\n'
                'Это действие:\n'
                '• Сгенерирует новую ссылку подключения\n'
                '• Сбросит все подключённые устройства\n'
                '• Старая ссылка перестанет работать\n\n'
                'Продолжить?'
            ),
        ),
        reply_markup=_build_revoke_confirm_keyboard(db_user.language, multi_tariff=True),
        parse_mode='HTML',
    )
