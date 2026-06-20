"""Regression tests: the bot must hide/refuse the trial when it is disabled
(TRIAL_DURATION_DAYS <= 0 or TRIAL_DISABLED_FOR == 'all'), matching the
mini-app/cabinet behaviour.

Covers all three render/grant surfaces:
  1. get_main_menu_keyboard (default sync keyboard)
  2. MenuLayoutService._evaluate_conditions (custom-menu constructor path)
  3. show_trial_offer / activate_trial handlers
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User


def _menu_has_trial(markup) -> bool:
    return any(getattr(btn, 'callback_data', None) == 'menu_trial' for row in markup.inline_keyboard for btn in row)


# --- Surface 1: default sync keyboard -------------------------------------


def test_keyboard_hides_trial_when_duration_zero():
    from app.keyboards.inline import get_main_menu_keyboard

    orig = settings.TRIAL_DURATION_DAYS
    try:
        settings.TRIAL_DURATION_DAYS = 0
        kb = get_main_menu_keyboard(has_had_paid_subscription=False, has_active_subscription=False)
        assert not _menu_has_trial(kb)

        settings.TRIAL_DURATION_DAYS = 3
        kb = get_main_menu_keyboard(has_had_paid_subscription=False, has_active_subscription=False)
        assert _menu_has_trial(kb)
    finally:
        settings.TRIAL_DURATION_DAYS = orig


def test_keyboard_hides_trial_when_disabled_for_all():
    from app.keyboards.inline import get_main_menu_keyboard

    orig_days, orig_disabled = settings.TRIAL_DURATION_DAYS, settings.TRIAL_DISABLED_FOR
    try:
        settings.TRIAL_DURATION_DAYS = 3
        settings.TRIAL_DISABLED_FOR = 'all'
        kb = get_main_menu_keyboard(has_had_paid_subscription=False, has_active_subscription=False)
        assert not _menu_has_trial(kb)
    finally:
        settings.TRIAL_DURATION_DAYS, settings.TRIAL_DISABLED_FOR = orig_days, orig_disabled


# --- Surface 2: custom-menu constructor path ------------------------------


def test_menu_layout_hides_trial_when_disabled():
    from app.services.menu_layout import MenuContext
    from app.services.menu_layout.service import MenuLayoutService

    ctx = MenuContext(has_had_paid_subscription=False, has_active_subscription=False)
    cond = {'show_trial': True}

    orig_days, orig_disabled = settings.TRIAL_DURATION_DAYS, settings.TRIAL_DISABLED_FOR
    try:
        settings.TRIAL_DURATION_DAYS = 3
        settings.TRIAL_DISABLED_FOR = 'none'
        assert MenuLayoutService._evaluate_conditions(cond, ctx) is True

        settings.TRIAL_DURATION_DAYS = 0
        assert MenuLayoutService._evaluate_conditions(cond, ctx) is False

        settings.TRIAL_DURATION_DAYS = 3
        settings.TRIAL_DISABLED_FOR = 'all'
        assert MenuLayoutService._evaluate_conditions(cond, ctx) is False
    finally:
        settings.TRIAL_DURATION_DAYS, settings.TRIAL_DISABLED_FOR = orig_days, orig_disabled


# --- Surface 3: handlers --------------------------------------------------


def _make_cb_user_db():
    cb = AsyncMock(spec=CallbackQuery)
    cb.message = AsyncMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    user = MagicMock(spec=User)
    user.language = 'ru'
    user.auth_type = 'telegram'
    user.restriction_subscription = False
    user.is_trial_already_used = MagicMock(return_value=False)
    db = AsyncMock(spec=AsyncSession)
    return cb, user, db


@pytest.mark.asyncio
async def test_show_trial_offer_blocks_when_duration_zero():
    from app.handlers.subscription import purchase

    cb, user, db = _make_cb_user_db()
    orig = settings.TRIAL_DURATION_DAYS
    try:
        settings.TRIAL_DURATION_DAYS = 0
        await purchase.show_trial_offer(cb, user, db)
        cb.message.edit_text.assert_awaited_once()
        # Returned before the eligibility/used check — proves the guard fired.
        user.is_trial_already_used.assert_not_called()
    finally:
        settings.TRIAL_DURATION_DAYS = orig


@pytest.mark.asyncio
async def test_activate_trial_blocks_when_duration_zero():
    from app.handlers.subscription import purchase

    cb, user, db = _make_cb_user_db()
    orig = settings.TRIAL_DURATION_DAYS
    try:
        settings.TRIAL_DURATION_DAYS = 0
        await purchase.activate_trial(cb, user, db)
        cb.message.edit_text.assert_awaited_once()
        user.is_trial_already_used.assert_not_called()
    finally:
        settings.TRIAL_DURATION_DAYS = orig
