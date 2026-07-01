"""Middleware enforcing per-section permissions for admin callbacks.

Superadmins (ADMIN_IDS) bypass all checks. Role-based admins (BotAdminRole)
must have the matching section in their permissions list, otherwise the
callback is denied with ACCESS_DENIED.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, TelegramObject

from app.config import settings
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)


# Callback prefix → required section. Order matters: first match wins.
# Always-allowed prefixes (navigation, no destructive action) live in ALWAYS_ALLOWED.
ADMIN_CALLBACK_SECTION_MAP: list[tuple[str, str]] = [
    # users
    ('admin_users', 'users'),
    ('admin_user_', 'users'),
    ('admin_referrals', 'users'),
    ('admin_referral_stats', 'users'),
    ('admin_blocked', 'users'),
    ('admin_blacklist', 'users'),
    ('admin_bulk_ban', 'users'),
    # payments
    ('admin_payments', 'payments'),
    ('admin_payment_', 'payments'),
    ('admin_pricing', 'payments'),
    ('admin_trials', 'payments'),
    # tariffs
    ('admin_tariffs', 'tariffs'),
    ('admin_tariff_', 'tariffs'),
    # subscriptions
    ('admin_subscriptions', 'subscriptions'),
    ('admin_subscription_', 'subscriptions'),
    # promos / promotions / engagement
    ('admin_promo_groups', 'promos'),
    ('admin_promo_offers', 'promos'),
    ('admin_promocodes', 'promos'),
    ('admin_promo_', 'promos'),
    ('admin_promo', 'promos'),
    ('admin_campaigns', 'promos'),
    ('admin_contests', 'promos'),
    ('admin_daily_contests', 'promos'),
    ('admin_polls', 'promos'),
    ('admin_scheduled_promos', 'promos'),
    ('admin_partner_promos', 'promos'),
    ('admin_reviews', 'promos'),
    ('admin_review_', 'promos'),
    ('admin_achievements', 'promos'),
    ('admin_ach_', 'promos'),
    ('admin_ach', 'promos'),
    # broadcasts
    ('admin_messages', 'broadcasts'),
    ('admin_message_', 'broadcasts'),
    # servers
    ('admin_servers', 'servers'),
    ('admin_server_', 'servers'),
    ('admin_remnawave', 'servers'),
    ('admin_remna', 'servers'),
    # support
    ('admin_tickets', 'support'),
    ('admin_ticket_', 'support'),
    ('admin_support', 'support'),
    ('admin_quick_replies', 'support'),
    ('admin_quick_reply_', 'support'),
    ('admin_faq', 'support'),
    ('admin_rules', 'support'),
    ('admin_privacy_policy', 'support'),
    ('admin_public_offer', 'support'),
    # settings (configuration / system)
    ('admin_bot_config', 'settings'),
    ('admin_bot_roles', 'settings'),
    ('admin_required_channels', 'settings'),
    ('admin_maintenance', 'settings'),
    ('admin_backup', 'settings'),
    ('admin_system_logs', 'settings'),
    ('admin_updates', 'settings'),
    ('admin_mon_settings', 'settings'),
    # analytics
    ('admin_monitoring', 'analytics'),
    ('admin_statistics', 'analytics'),
    ('admin_reports', 'analytics'),
]


# Navigation-only callbacks: any admin (super or role) can open them.
# The keyboards rendered by these handlers are filtered separately based on
# the user's permissions.
ALWAYS_ALLOWED_PREFIXES: tuple[str, ...] = (
    'admin_panel',
    'admin_submenu_',
)


def resolve_admin_section(callback_data: str) -> str | None:
    """Return the section a given admin_* callback belongs to, or None if unknown."""
    if not callback_data or not callback_data.startswith('admin_'):
        return None
    for prefix, section in ADMIN_CALLBACK_SECTION_MAP:
        if prefix.endswith('_'):
            # Prefix already ends on a word boundary (e.g. 'admin_user_'):
            # a plain startswith is unambiguous and correctly matches
            # children like 'admin_user_balance_5'.
            if callback_data.startswith(prefix):
                return section
        elif callback_data == prefix or callback_data.startswith(prefix + ':') or callback_data.startswith(prefix + '_'):
            return section
    return None


class AdminPermissionMiddleware(BaseMiddleware):
    """Gate admin_* callbacks by BotAdminRole permissions."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery) or not event.data:
            return await handler(event, data)

        cb = event.data
        if not cb.startswith('admin_'):
            return await handler(event, data)

        # Always-allowed navigation
        if any(cb == p or cb.startswith(p) for p in ALWAYS_ALLOWED_PREFIXES):
            return await handler(event, data)

        user = event.from_user
        if not user:
            return await handler(event, data)

        # Superadmins bypass
        if settings.is_admin(user.id):
            return await handler(event, data)

        db = data.get('db')
        db_user = data.get('db_user')
        if db is None or db_user is None:
            return await handler(event, data)

        # Resolve required section
        required = resolve_admin_section(cb)
        if required is None:
            # Unknown admin callback — fall through to admin_required decorator,
            # which still gates by "any role". Better than a hard deny while we
            # haven't mapped every callback.
            return await handler(event, data)

        try:
            from app.database.crud.bot_role import BotRoleCRUD

            role = await BotRoleCRUD.get_bot_role(db, db_user.id)
        except Exception as e:
            logger.warning('AdminPermissionMiddleware: не удалось получить роль', error=e)
            role = None

        permissions = list(role.permissions or []) if role else []
        if required in permissions:
            return await handler(event, data)

        texts = get_texts(getattr(db_user, 'language', 'ru'))
        try:
            await event.answer(texts.ACCESS_DENIED, show_alert=True)
        except TelegramBadRequest:
            pass
        logger.info(
            'admin permission denied',
            user_id=user.id,
            callback_data=cb,
            required_section=required,
            user_permissions=permissions,
        )
        return None
