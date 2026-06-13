from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.trial_activation_service as trial_activation_service
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
    user.id = 1
    user.subscription = None
    user.has_had_paid_subscription = False
    user.language = 'ru'
    # Reach the activation/charge path: no admin restriction and trial enabled.
    user.restriction_subscription = False
    user.auth_type = 'telegram'
    user.balance_kopeks = 100
    return user


@pytest.fixture
def trial_db():
    return AsyncMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_activate_trial_uses_trial_price_for_topup_redirect(
    trial_callback_query,
    trial_user,
    trial_db,
):
    error = TrialPaymentInsufficientFunds(required_amount=15900, balance_amount=100)

    mock_keyboard = InlineKeyboardMarkup(inline_keyboard=[])

    fake_subscription = MagicMock()

    with (
        # Free-trial branch: the activation charge amount is resolved to 0 so we
        # skip the paid-trial selection screen and go straight to creating the
        # trial subscription + charging. ``get_trial_activation_charge_amount`` is
        # imported locally inside ``activate_trial`` so it must be patched on the
        # REAL service module.
        patch.object(
            trial_activation_service,
            'get_trial_activation_charge_amount',
            return_value=0,
        ),
        # Avoid the tariffs-mode branch (DB tariff/squad lookups) and keep device
        # selection enabled so no disabled-mode device limit is fetched. ``settings``
        # is a pydantic ``BaseSettings`` instance which blocks attribute deletion on
        # patch teardown, so patch the methods on the ``Settings`` class instead.
        patch(
            'app.config.Settings.is_tariffs_mode',
            return_value=False,
        ),
        patch(
            'app.config.Settings.is_devices_selection_enabled',
            return_value=True,
        ),
        patch(
            'app.config.Settings.is_trial_disabled_for_user',
            return_value=False,
        ),
        # Legacy random-squad fallback used when no trial tariff is configured.
        patch(
            'app.database.crud.server_squad.get_random_trial_squad_uuid',
            new=AsyncMock(return_value=None),
        ),
        # Trial subscription is created successfully...
        patch(
            'app.handlers.subscription.purchase.create_trial_subscription',
            new=AsyncMock(return_value=fake_subscription),
        ),
        # ...but the balance charge fails with insufficient funds.
        patch(
            'app.handlers.subscription.purchase.charge_trial_activation_if_required',
            new=AsyncMock(side_effect=error),
        ),
        # Rollback of the just-created subscription succeeds so the handler
        # proceeds to render the insufficient-balance screen.
        patch(
            'app.handlers.subscription.purchase.rollback_trial_subscription_activation',
            new=AsyncMock(return_value=True),
        ),
        patch(
            'app.handlers.subscription.purchase.get_texts',
            return_value=MagicMock(
                t=lambda key, default, **kwargs: default,
            ),
        ),
        patch(
            'app.handlers.subscription.purchase.get_insufficient_balance_keyboard',
            return_value=mock_keyboard,
        ) as insufficient_keyboard,
    ):
        await activate_trial(trial_callback_query, trial_user, trial_db)

    # The insufficient-balance keyboard must be built for the full required
    # amount (the trial price), not the user's partial balance.
    insufficient_keyboard.assert_called_once_with(
        trial_user.language,
        amount_kopeks=error.required_amount,
    )
    trial_callback_query.message.edit_text.assert_called_once()
    trial_callback_query.answer.assert_called_once()
