"""Public payment-help entry and ticket access through non-security UI gates.

Flags come from the selected server-side handler, never from message text.
Object ownership, ticket bans, authentication and rate limiting remain separate.
"""

from aiogram.types import CallbackQuery, Message


def is_support_handler(event, data: dict, *, payment_only: bool = False) -> bool:
    flags = getattr(data.get('handler'), 'flags', {})
    if not isinstance(flags, dict) or not (
        flags.get('public_payment_support') is True or (not payment_only and flags.get('support_access') is True)
    ):
        return False
    if isinstance(event, Message):
        chat = event.chat
    elif isinstance(event, CallbackQuery) and event.message:
        chat = event.message.chat
    else:
        return False
    if chat.type != 'private' or not event.from_user or event.from_user.is_bot:
        return False
    return True
