from aiogram.types import CallbackQuery, TelegramObject

from app.states import RegistrationStates


def is_registration_process(event: TelegramObject, current_state: str | None) -> bool:
    registration_states = [
        RegistrationStates.waiting_for_language.state,
        RegistrationStates.waiting_for_rules_accept.state,
        RegistrationStates.waiting_for_privacy_policy_accept.state,
        RegistrationStates.waiting_for_referral_code.state,
    ]

    registration_callbacks = [
        'nz!_rules_accept',
        'nz!_rules_decline',
        'nz!_privacy_policy_accept',
        'nz!_privacy_policy_decline',
        'nz!_referral_skip',
    ]

    language_select_prefix = 'nz!_language_select:'

    if current_state in registration_states:
        return True

    if (
        isinstance(event, CallbackQuery)
        and event.data
        and (event.data in registration_callbacks or event.data.startswith(language_select_prefix))
    ):
        return True

    return False
