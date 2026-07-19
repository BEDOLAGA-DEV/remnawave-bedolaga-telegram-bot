from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.handlers.subscription.purchase import activate_trial
from app.services.trial_activation_service import TrialPaymentInsufficientFunds


@pytest.fixture
def trial_callback_query():
    callback = AsyncMock(spec=CallbackQuery)
    callback.message = AsyncMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


@pytest.fixture
def trial_user():
    user = MagicMock(spec=User)
    user.subscription = None
    user.has_had_paid_subscription = False
    user.language = 'ru'
    return user


@pytest.fixture
def trial_db():
    return AsyncMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_activate_trial_paid_shows_payment_screen_with_trial_price(
    trial_callback_query,
    trial_user,
    trial_db,
):
    # Paid-trial entrypoint: when the activation charge is positive and the
    # balance cannot cover it, activate_trial must render the paid-trial payment
    # screen (price + balance lines + payment keyboard), NOT silently activate.
    trial_price_kopeks = 15900
    balance_kopeks = 100

    trial_user.balance_kopeks = balance_kopeks
    trial_user.restriction_subscription = False
    trial_user.auth_type = 'telegram'
    trial_user.is_trial_already_used.return_value = False

    mock_keyboard = InlineKeyboardMarkup(inline_keyboard=[])

    with (
        # get_trial_activation_charge_amount is imported locally inside
        # activate_trial via `from app.services.trial_activation_service import
        # ...`, so the name resolves from the source module at call time ->
        # patch it there.
        patch(
            'app.services.trial_activation_service.get_trial_activation_charge_amount',
            return_value=trial_price_kopeks,
        ),
        patch(
            'app.handlers.subscription.purchase.get_texts',
            return_value=MagicMock(
                t=lambda key, default, **kwargs: default,
            ),
        ),
        patch('app.config.Settings.is_trial_disabled_for_user', return_value=False),
        patch('app.config.Settings.is_tariffs_mode', return_value=False),
        patch(
            'app.handlers.subscription.purchase._get_trial_payment_keyboard',
            return_value=mock_keyboard,
        ) as payment_keyboard,
    ):
        await activate_trial(trial_callback_query, trial_user, trial_db)

    # The paid-trial keyboard is shown for the can_pay_from_balance=False case.
    payment_keyboard.assert_called_once_with(trial_user.language, False)

    trial_callback_query.message.edit_text.assert_called_once()
    _args, kwargs = trial_callback_query.message.edit_text.call_args
    body = trial_callback_query.message.edit_text.call_args[0][0]

    # The payment screen must surface the exact trial price and balance, and use
    # the paid-trial keyboard sentinel.
    assert kwargs['reply_markup'] is mock_keyboard
    assert settings.format_price(trial_price_kopeks) in body
    assert settings.format_price(balance_kopeks) in body

    trial_callback_query.answer.assert_called_once()


@pytest.mark.asyncio
async def test_activate_free_trial_insufficient_funds_redirects_to_topup(
    trial_callback_query,
    trial_user,
    trial_db,
):
    # Free-trial path (activation charge == 0): the subscription is created
    # first, then charge_trial_activation_if_required raises
    # TrialPaymentInsufficientFunds. activate_trial must roll back and redirect
    # the user to the top-up keyboard for the EXACT required amount.
    error = TrialPaymentInsufficientFunds(required_amount=15900, balance_amount=100)

    mock_keyboard = InlineKeyboardMarkup(inline_keyboard=[])

    trial_user.restriction_subscription = False
    trial_user.auth_type = 'telegram'
    trial_user.is_trial_already_used.return_value = False
    trial_user.id = 42

    with (
        # Paid branch skipped (price 0) -> free-trial activation flow runs.
        # Locally imported from the service module -> patch at the source.
        patch(
            'app.services.trial_activation_service.get_trial_activation_charge_amount',
            return_value=0,
        ),
        patch(
            'app.handlers.subscription.purchase.get_texts',
            return_value=MagicMock(
                t=lambda key, default, **kwargs: default,
            ),
        ),
        patch('app.config.Settings.is_trial_disabled_for_user', return_value=False),
        patch('app.config.Settings.is_tariffs_mode', return_value=False),
        patch('app.config.Settings.is_devices_selection_enabled', return_value=True),
        # Imported locally inside activate_trial -> patch at the source module.
        patch(
            'app.database.crud.server_squad.get_random_trial_squad_uuid',
            new=AsyncMock(return_value='squad-uuid'),
        ),
        patch(
            'app.handlers.subscription.purchase.create_trial_subscription',
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            'app.handlers.subscription.purchase.charge_trial_activation_if_required',
            new=AsyncMock(side_effect=error),
        ),
        patch(
            'app.handlers.subscription.purchase.rollback_trial_subscription_activation',
            new=AsyncMock(return_value=True),
        ) as rollback_mock,
        patch(
            'app.handlers.subscription.purchase.get_insufficient_balance_keyboard',
            return_value=mock_keyboard,
        ) as insufficient_keyboard,
    ):
        await activate_trial(trial_callback_query, trial_user, trial_db)

    # Rollback must run before redirecting (no orphaned trial subscription).
    rollback_mock.assert_awaited_once()

    # Top-up redirect must target the EXACT required amount, not the balance.
    insufficient_keyboard.assert_called_once_with(
        trial_user.language,
        amount_kopeks=error.required_amount,
    )
    trial_callback_query.message.edit_text.assert_called_once()
    trial_callback_query.answer.assert_called_once()
