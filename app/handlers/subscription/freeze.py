from __future__ import annotations

from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User


def _freeze_available() -> bool:
    # Single source of truth: the admin freeze panel toggle
    # (FreezeSettingsService, stored in data/freeze_settings.json) so the
    # feature can be turned on/off entirely from the bot admin panel without
    # the legacy env flag SUBSCRIPTION_FREEZE_ENABLED.
    from app.services.freeze_settings_service import FreezeSettingsService

    return FreezeSettingsService.is_enabled()


def _parse_sub_id(callback: types.CallbackQuery) -> int | None:
    """Extract the subscription id from ``nz!_freeze_sub:<id>`` callbacks.

    Legacy buttons from stale inline keyboards send a bare ``nz!_freeze_sub``
    with no id — those return None and fall back to the user's first active
    subscription.
    """
    data = callback.data or ''
    if ':' in data:
        try:
            return int(data.split(':', 1)[1])
        except (ValueError, IndexError):
            return None
    return None


async def _resolve_target_subscription(db: AsyncSession, db_user: User, sub_id: int | None):
    """Resolve which subscription to (un)freeze.

    Prefer the explicit ``sub_id`` from the per-subscription detail screen
    (IDOR-protected via ``get_subscription_by_id_for_user``). Fall back to the
    user's first active subscription for legacy id-less callbacks.
    """
    if sub_id is not None:
        from app.database.crud.subscription import get_subscription_by_id_for_user

        sub = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
        if sub is not None:
            return sub

    from app.database.crud.subscription import get_active_subscriptions_by_user_id

    subs = await get_active_subscriptions_by_user_id(db, db_user.id)
    return subs[0] if subs else None


async def _refresh_screen(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext | None,
    sub_id: int | None,
    subscription,
) -> None:
    """Re-render the originating screen so the freeze/resume button flips.

    Take the per-subscription detail view only when the button id matched the
    subscription actually acted upon (the normal detail-screen tap) — then
    show_subscription_detail re-parses the same valid id from callback.data and
    repaints in place. For id-less callbacks (classic view) or a stale/tampered
    id that resolved to a different sub, fall back to the classic
    single-subscription view, which repaints reliably.
    """
    on_detail = (
        sub_id is not None
        and state is not None
        and subscription is not None
        and getattr(subscription, 'id', None) == sub_id
    )
    try:
        if on_detail:
            from app.handlers.subscription.my_subscriptions import show_subscription_detail

            await show_subscription_detail(callback, db_user, db, state)
        else:
            from app.handlers.subscription.purchase import show_subscription_info

            await show_subscription_info(callback, db_user, db)
    except TelegramBadRequest as exc:
        if 'message is not modified' not in str(exc):
            raise


async def handle_freeze_subscription(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
) -> None:
    from app.services.freeze_service import FreezeError, freeze_service

    # Guard: stale inline keyboards can deliver this callback even with the
    # feature disabled — the button is hidden but the callback stays registered.
    if not _freeze_available():
        await callback.answer('Функция недоступна', show_alert=True)
        return

    sub_id = _parse_sub_id(callback)
    subscription = await _resolve_target_subscription(db, db_user, sub_id)
    if subscription is None:
        await callback.answer('Нет активной подписки', show_alert=True)
        return

    try:
        await freeze_service.freeze_subscription(db, subscription, db_user)
    except FreezeError as e:
        await callback.answer(e.message, show_alert=True)
        return

    await callback.answer('❄️ Подписка заморожена')
    await _refresh_screen(callback, db_user, db, state, sub_id, subscription)


async def handle_resume_subscription(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
) -> None:
    from app.services.freeze_service import FreezeError, freeze_service

    if not _freeze_available():
        await callback.answer('Функция недоступна', show_alert=True)
        return

    sub_id = _parse_sub_id(callback)
    subscription = await _resolve_target_subscription(db, db_user, sub_id)
    if subscription is None:
        await callback.answer('Нет подписки', show_alert=True)
        return

    try:
        await freeze_service.resume_subscription(db, subscription, db_user, reason='manual')
    except FreezeError as e:
        await callback.answer(e.message, show_alert=True)
        return

    await callback.answer('▶️ Подписка разморожена')
    await _refresh_screen(callback, db_user, db, state, sub_id, subscription)
