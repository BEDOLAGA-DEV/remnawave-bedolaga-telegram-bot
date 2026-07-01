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
    ('admin_mass_delete', 'users'),
    ('admin_cleanup_inactive', 'users'),
    ('admin_referral_diagnostics', 'users'),
    ('admin_ref_', 'users'),
    ('admin_top_ref', 'users'),
    ('admin_test_referral_earning', 'users'),
    # trials (was payments)
    ('admin_trials', 'trials'),
    ('admin_trials_reset', 'trials'),
    # pricing (was payments; admin_subs_pricing was subscriptions)
    ('admin_subs_pricing', 'pricing'),   # MUST precede admin_subs_
    ('admin_pricing', 'pricing'),
    # reviews (was promos)
    ('admin_reviews', 'reviews'),
    ('admin_review_', 'reviews'),
    # offers (was promos; spromo_* was ungated)
    ('admin_scheduled_promos', 'offers'),
    ('spromo_', 'offers'),
    # payments
    ('admin_payments', 'payments'),
    ('admin_payment_', 'payments'),
    ('admin_txn_', 'payments'),
    ('admin_stxn_', 'payments'),
    ('admin_withdrawal_', 'payments'),
    ('admin_nalogo_', 'payments'),
    ('admin_test_payment', 'payments'),
    ('admin_cryptobot_test_', 'payments'),
    ('admin_stars_test_', 'payments'),
    # tariffs
    ('admin_tariffs', 'tariffs'),
    ('admin_tariff_', 'tariffs'),
    # subscriptions
    ('admin_subscriptions', 'subscriptions'),
    ('admin_subscription_', 'subscriptions'),
    ('admin_subs_', 'subscriptions'),
    # admin_sub_* are subscription-mutating actions (grant/delete/extend/...)
    # registered in users.py. Broad prefix — placed after the more-specific
    # admin_subs_/admin_subscription_ so first-match-wins is a no-op here (same
    # section). Does NOT match admin_submenu_ (that is 'admin_subm...').
    ('admin_sub_', 'subscriptions'),
    ('admin_buy_sub_', 'subscriptions'),
    ('admin_send_expiry_reminders', 'subscriptions'),
    # promos / promotions / engagement
    ('admin_promo_groups', 'promos'),
    ('admin_promo_offers', 'promos'),
    ('admin_promocodes', 'promos'),
    ('admin_promo_', 'promos'),
    ('admin_promo', 'promos'),
    ('admin_campaigns', 'promos'),
    ('admin_campaign_', 'promos'),
    ('admin_contests', 'promos'),
    ('admin_contest_', 'promos'),
    ('admin_daily_contests', 'promos'),
    ('admin_daily_', 'promos'),
    ('admin_polls', 'promos'),
    ('admin_partner_promos', 'promos'),
    ('admin_achievements', 'promos'),
    ('admin_ach_', 'promos'),
    ('admin_ach', 'promos'),
    # broadcasts
    ('admin_messages', 'broadcasts'),
    ('admin_message_', 'broadcasts'),
    ('admin_msg_', 'broadcasts'),
    ('admin_pinned', 'broadcasts'),
    ('admin_confirm_broadcast', 'broadcasts'),
    # servers
    ('admin_servers', 'servers'),
    ('admin_server_', 'servers'),
    ('admin_remnawave', 'servers'),
    ('admin_remna', 'servers'),
    ('admin_rw_', 'servers'),
    ('admin_squad_', 'servers'),
    ('admin_node_', 'servers'),
    ('admin_restart_all_nodes', 'servers'),
    ('admin_migration_', 'servers'),
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
    # ticket actions (tokens where 'ticket' is not the leading word) + housekeeping
    ('admin_close_ticket_', 'support'),
    ('admin_reply_ticket_', 'support'),
    ('admin_view_ticket_', 'support'),
    ('admin_block_user_ticket_', 'support'),
    ('admin_block_user_perm_ticket_', 'support'),
    ('admin_unblock_user_ticket_', 'support'),
    ('admin_mark_answered_', 'support'),
    ('admin_delete_message_', 'support'),
    ('admin_close_report', 'support'),
    # settings (configuration / system)
    ('admin_bot_config', 'settings'),
    ('admin_bot_roles', 'settings'),
    ('admin_required_channels', 'settings'),
    ('admin_maintenance', 'settings'),
    ('admin_backup', 'settings'),
    ('admin_system_logs', 'settings'),
    ('admin_updates', 'settings'),
    ('admin_mon_settings', 'settings'),
    # config toggles / editors living under the settings submenu
    ('admin_freeze_', 'settings'),
    ('admin_birthday_', 'settings'),
    ('admin_traffic_', 'settings'),
    ('admin_edit_rules', 'settings'),
    ('admin_save_rules', 'settings'),
    ('admin_clear_rules', 'settings'),
    ('admin_confirm_clear_rules', 'settings'),
    ('admin_view_rules', 'settings'),
    # analytics
    ('admin_monitoring', 'analytics'),
    ('admin_statistics', 'analytics'),
    ('admin_reports', 'analytics'),
    ('admin_stats_', 'analytics'),
    ('admin_successful_topups', 'analytics'),
    ('admin_stopups_', 'analytics'),
    ('admin_revenue_period', 'analytics'),
    ('admin_mon_', 'analytics'),
    ('admin_wl_analytics', 'analytics'),
    # --- nz!_ ADMIN actions -------------------------------------------------
    # The nz!_ namespace is shared: ~120 ADMIN actions and ~312 USER actions
    # live under it. Only SPECIFIC admin prefixes are mapped here; every prefix
    # below was proven (against the full nz! classification + actual handler
    # registration) not to swallow any USER callback. The bare ambiguous
    # prefixes (nz!_period_, nz!_sync_) are deliberately NOT mapped.
    # nz! broadcasts
    ('nz!_broadcast_', 'broadcasts'),
    ('nz!_criteria_', 'broadcasts'),
    ('nz!_bcast_', 'broadcasts'),
    ('nz!_btn_', 'broadcasts'),
    ('nz!_edit_buttons', 'broadcasts'),
    ('nz!_buttons_confirm', 'broadcasts'),
    ('nz!_add_media_', 'broadcasts'),
    ('nz!_change_media', 'broadcasts'),
    ('nz!_confirm_media', 'broadcasts'),
    ('nz!_replace_media', 'broadcasts'),
    ('nz!_skip_media', 'broadcasts'),
    ('nz!_user_messages_panel', 'broadcasts'),
    ('nz!_add_user_message', 'broadcasts'),
    ('nz!_edit_user_message', 'broadcasts'),
    ('nz!_delete_user_message', 'broadcasts'),
    ('nz!_toggle_user_message', 'broadcasts'),
    ('nz!_view_user_message', 'broadcasts'),
    ('nz!_list_user_messages', 'broadcasts'),
    ('nz!_user_messages_stats', 'broadcasts'),
    # nz! settings
    ('nz!_welcome_text_panel', 'settings'),
    ('nz!_edit_welcome_text', 'settings'),
    ('nz!_preview_welcome_text', 'settings'),
    ('nz!_reset_welcome_text', 'settings'),
    ('nz!_toggle_welcome_text', 'settings'),
    ('nz!_show_welcome_text', 'settings'),
    ('nz!_show_formatting_help', 'settings'),
    ('nz!_show_placeholders_help', 'settings'),
    ('nz!_maintenance_', 'settings'),
    ('nz!_manual_notify_', 'settings'),
    ('nz!_reqch', 'settings'),
    # nz! servers
    ('nz!_node_', 'servers'),
    ('nz!_squad_', 'servers'),
    ('nz!_sqd_', 'servers'),
    ('nz!_create_squad_finish', 'servers'),
    ('nz!_create_tgl_', 'servers'),
    ('nz!_cancel_squad_create', 'servers'),
    ('nz!_cancel_rename_', 'servers'),
    # nz!_sync_ is AMBIGUOUS; only the two exact admin sync actions are mapped.
    ('nz!_sync_all_users', 'servers'),
    ('nz!_sync_to_panel', 'servers'),
    ('nz!_remnawave_auto_sync', 'servers'),
    ('nz!_force_cleanup_orphaned', 'servers'),
    # nz! promos
    ('nz!_promo_manage_', 'promos'),
    ('nz!_promo_toggle_', 'promos'),
    ('nz!_promo_stats_', 'promos'),
    ('nz!_promo_delete_', 'promos'),
    ('nz!_promo_edit_', 'promos'),
    ('nz!_promo_type_', 'promos'),
    ('nz!_promo_select_group_', 'promos'),
    ('nz!_promo_group_', 'promos'),
    # nz!_promo_offer_ is admin (promo_offers.py); nz!_promo_offer_close is the
    # one USER token under it and is excluded via NZ_NEVER_GATE below.
    ('nz!_promo_offer_', 'promos'),
    ('nz!_poll_create', 'promos'),
    ('nz!_poll_view', 'promos'),
    ('nz!_poll_stats', 'promos'),
    ('nz!_poll_send', 'promos'),
    ('nz!_poll_delete', 'promos'),
    ('nz!_poll_target', 'promos'),
    ('nz!_poll_custom_target', 'promos'),
    ('nz!_poll_custom_menu', 'promos'),
    # nz! tariffs
    ('nz!_tariff_type_daily', 'tariffs'),
    ('nz!_tariff_type_periodic', 'tariffs'),
]


# USER callbacks that would otherwise be swallowed by a broad admin prefix
# above. These share their prefix with an admin family but are handled by a
# USER handler, so they must NEVER gate. Checked before the prefix map.
NZ_NEVER_GATE: frozenset[str] = frozenset({
    # Registered by claim_discount_offer / promo_offer close in the user
    # subscription flow, but the leading prefix overlaps admin promo actions.
    'nz!_promo_offer_close',
})


# Navigation-only callbacks: any admin (super or role) can open them.
# The keyboards rendered by these handlers are filtered separately based on
# the user's permissions.
ALWAYS_ALLOWED_PREFIXES: tuple[str, ...] = (
    'admin_panel',
    'admin_submenu_',
)


def resolve_admin_section(callback_data: str) -> str | None:
    """Return the section a given admin_*/nz!_*/spromo_* callback belongs to, or None.

    The nz!_ namespace is shared between admin and user actions; only the
    specific admin prefixes in ADMIN_CALLBACK_SECTION_MAP resolve to a section,
    and NZ_NEVER_GATE forces the handful of user tokens that overlap an admin
    prefix back to None. Everything else (all user callbacks) returns None.
    """
    if not callback_data or not callback_data.startswith(('admin_', 'nz!_', 'spromo_')):
        return None
    if callback_data in NZ_NEVER_GATE:
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

        cb = event.data or ''
        # Resolution-driven gate: only callbacks that resolve to a section are
        # gated. Everything else — including every user nz!_ callback and any
        # unmapped admin_ callback — passes straight through. Do NOT log here:
        # this branch fires for hundreds of user callbacks per second.
        required = resolve_admin_section(cb)
        if required is None:
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
