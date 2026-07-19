"""Regression tests for the ticket-notification action buttons (issue #2988).

Covers the two security-critical units:
- ``get_ticket_notification_keyboard`` — the role/data → button matrix.
- ``AdminNotificationService.resolve_recipient_role`` — recipient role from chat_id.

The core risks guarded here:
- a moderator must NOT be offered «👤 К пользователю» (its handler is
  ``@admin_required`` → ACCESS_DENIED);
- a group/channel (``chat_id <= 0``), an outsider's private chat, or a string
  ``@username`` chat must resolve to role ``none`` so NO buttons are attached
  (no contact leak via the ``tg://`` URL buttons, no broken FSM buttons in a
  shared chat);
- «👤 К пользователю» must use the DB id, not the telegram id;
- the notification keyboard must never carry the «⬅️ Назад» button.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.types import InlineKeyboardButton

from app.keyboards.inline import get_ticket_notification_keyboard
from app.services.admin_notification_service import AdminNotificationService
from app.services.support_settings_service import SupportSettingsService
from app.utils.miniapp_buttons import build_admin_ticket_cabinet_button


# --- helpers ---------------------------------------------------------------


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _callbacks(kb):
    return [b.callback_data for b in _buttons(kb) if b.callback_data]


def _urls(kb):
    return [b.url for b in _buttons(kb) if b.url]


def _has_user_manage(kb):
    return any((cb or '').startswith('admin_user_manage_') for cb in _callbacks(kb))


# --- get_ticket_notification_keyboard: role matrix -------------------------


def test_admin_gets_full_set_including_user_manage():
    kb = get_ticket_notification_keyboard(7, user_id=42, telegram_id=123, username='john', is_admin=True)
    callbacks = _callbacks(kb)
    assert 'admin_user_manage_42_from_ticket_7' in callbacks
    assert 'admin_reply_ticket_7' in callbacks
    assert 'admin_close_ticket_7' in callbacks
    assert 'admin_block_user_perm_ticket_7' in callbacks
    assert 'admin_block_user_ticket_7' in callbacks


def test_moderator_omits_user_manage_but_keeps_actions():
    kb = get_ticket_notification_keyboard(7, user_id=42, telegram_id=123, username='john', is_admin=False)
    callbacks = _callbacks(kb)
    # «👤 К пользователю» is @admin_required — a moderator must not see it.
    assert not _has_user_manage(kb)
    # …but the shared ticket actions remain available.
    assert 'admin_reply_ticket_7' in callbacks
    assert 'admin_close_ticket_7' in callbacks
    assert 'admin_block_user_perm_ticket_7' in callbacks
    assert 'admin_block_user_ticket_7' in callbacks


def test_user_manage_uses_db_id_not_telegram_id():
    # DB id 42, telegram id 999 — the callback must carry the DB id.
    kb = get_ticket_notification_keyboard(7, user_id=42, telegram_id=999, is_admin=True)
    assert 'admin_user_manage_42_from_ticket_7' in _callbacks(kb)
    assert 'admin_user_manage_999_from_ticket_7' not in _callbacks(kb)


def test_user_manage_hidden_when_no_db_id_even_for_admin():
    kb = get_ticket_notification_keyboard(7, user_id=None, telegram_id=123, is_admin=True)
    assert not _has_user_manage(kb)


def test_url_buttons_present_for_username_and_telegram_id():
    kb = get_ticket_notification_keyboard(7, telegram_id=123, username='john', is_admin=True)
    urls = _urls(kb)
    assert 'tg://resolve?domain=john' in urls
    assert 'tg://user?id=123' in urls


def test_username_with_at_prefix_is_stripped():
    kb = get_ticket_notification_keyboard(7, username='@john', is_admin=True)
    assert 'tg://resolve?domain=john' in _urls(kb)


def test_no_username_hides_dm_keeps_profile():
    kb = get_ticket_notification_keyboard(7, telegram_id=123, username=None, is_admin=True)
    urls = _urls(kb)
    assert not any(u.startswith('tg://resolve') for u in urls)
    assert 'tg://user?id=123' in urls


def test_email_user_without_telegram_id_has_no_url_buttons_but_keeps_callbacks():
    # Email-only author: no username, no numeric telegram id.
    kb = get_ticket_notification_keyboard(7, user_id=42, telegram_id=None, username=None, is_admin=True)
    assert _urls(kb) == []
    callbacks = _callbacks(kb)
    # Callback actions still work, «К пользователю» uses the DB id.
    assert 'admin_user_manage_42_from_ticket_7' in callbacks
    assert 'admin_reply_ticket_7' in callbacks


def test_non_numeric_telegram_id_dropped_from_profile_url():
    kb = get_ticket_notification_keyboard(7, telegram_id='not-a-number', is_admin=True)
    assert not any(u.startswith('tg://user') for u in _urls(kb))


def test_blocked_user_shows_unblock_not_block_controls():
    kb = get_ticket_notification_keyboard(7, user_id=42, is_admin=True, is_user_blocked=True)
    callbacks = _callbacks(kb)
    assert 'admin_unblock_user_ticket_7' in callbacks
    assert 'admin_block_user_perm_ticket_7' not in callbacks
    assert 'admin_block_user_ticket_7' not in callbacks


def test_closed_ticket_hides_reply_and_close():
    kb = get_ticket_notification_keyboard(7, user_id=42, is_admin=True, is_closed=True)
    callbacks = _callbacks(kb)
    assert 'admin_reply_ticket_7' not in callbacks
    assert 'admin_close_ticket_7' not in callbacks
    # Block controls remain available on a closed ticket.
    assert 'admin_block_user_perm_ticket_7' in callbacks


def test_notification_keyboard_never_has_back_button():
    kb = get_ticket_notification_keyboard(7, user_id=42, telegram_id=123, username='john', is_admin=True)
    assert 'admin_tickets' not in _callbacks(kb)


# --- group keyboard (fsm_enabled=False): reliable buttons only --------------


def test_group_keyboard_omits_fsm_buttons_keeps_reliable():
    # Group/channel recipient: no FSM buttons (reply, block-by-time), but the
    # reliable callbacks + URL buttons remain.
    kb = get_ticket_notification_keyboard(
        7, user_id=42, telegram_id=123, username='john', is_admin=False, fsm_enabled=False
    )
    callbacks = _callbacks(kb)
    # FSM buttons are gone:
    assert 'admin_reply_ticket_7' not in callbacks
    assert 'admin_block_user_ticket_7' not in callbacks
    # Reliable (non-FSM) actions stay:
    assert 'admin_close_ticket_7' in callbacks
    assert 'admin_block_user_perm_ticket_7' in callbacks
    # URL buttons stay (admin chat → trusted, no contact-leak concern):
    urls = _urls(kb)
    assert 'tg://resolve?domain=john' in urls
    assert 'tg://user?id=123' in urls


def test_group_keyboard_omits_user_manage():
    # "К пользователю" is @admin_required and we can't gate per-tapper in a group.
    kb = get_ticket_notification_keyboard(7, user_id=42, is_admin=False, fsm_enabled=False)
    assert not _has_user_manage(kb)


def test_group_keyboard_blocked_shows_unblock():
    kb = get_ticket_notification_keyboard(7, user_id=42, is_user_blocked=True, fsm_enabled=False)
    callbacks = _callbacks(kb)
    assert 'admin_unblock_user_ticket_7' in callbacks
    assert 'admin_block_user_perm_ticket_7' not in callbacks
    assert 'admin_block_user_ticket_7' not in callbacks


def test_group_keyboard_closed_ticket_leaves_only_block_forever():
    kb = get_ticket_notification_keyboard(7, user_id=42, is_closed=True, fsm_enabled=False)
    callbacks = _callbacks(kb)
    assert 'admin_reply_ticket_7' not in callbacks
    assert 'admin_close_ticket_7' not in callbacks
    assert 'admin_block_user_ticket_7' not in callbacks
    assert 'admin_block_user_perm_ticket_7' in callbacks


# --- resolve_recipient_role -------------------------------------------------


@pytest.fixture
def service_factory(monkeypatch):
    """Build an AdminNotificationService with patched permission helpers.

    ``settings.is_admin`` is a pydantic Settings *method* → patch on the class.
    ``SupportSettingsService.is_moderator`` is a classmethod → patch on the class.
    """

    def _make(chat_id, *, admins=(), moderators=()):
        from app.config import settings

        monkeypatch.setattr(
            type(settings),
            'is_admin',
            lambda self, telegram_id=None, email=None: telegram_id in admins,
            raising=False,
        )
        monkeypatch.setattr(
            SupportSettingsService,
            'is_moderator',
            staticmethod(lambda telegram_id: telegram_id in moderators),
            raising=False,
        )
        service = AdminNotificationService(bot=SimpleNamespace())
        service.chat_id = chat_id
        return service

    return _make


def test_role_admin(service_factory):
    service = service_factory(100, admins={100})
    assert service.resolve_recipient_role() == 'admin'


def test_role_moderator(service_factory):
    service = service_factory(200, admins={100}, moderators={200})
    assert service.resolve_recipient_role() == 'moderator'


def test_role_admin_takes_precedence_over_moderator(service_factory):
    service = service_factory(100, admins={100}, moderators={100})
    assert service.resolve_recipient_role() == 'admin'


def test_role_outsider_private_chat_is_none(service_factory):
    service = service_factory(300, admins={100}, moderators={200})
    assert service.resolve_recipient_role() == 'none'


@pytest.mark.parametrize('chat_id', [-1001234567890, -123])
def test_role_group_or_channel_is_group(service_factory, chat_id):
    service = service_factory(chat_id, admins={100, chat_id}, moderators={chat_id})
    # A negative chat is a group/channel (trusted admin chat): role 'group' →
    # reliable, non-FSM buttons only. Returns before the admin/moderator check.
    assert service.resolve_recipient_role() == 'group'


def test_role_zero_chat_id_is_none(service_factory):
    service = service_factory(0, admins={100})
    assert service.resolve_recipient_role() == 'none'


def test_role_none_chat_id_is_none(service_factory):
    service = service_factory(None, admins={100})
    assert service.resolve_recipient_role() == 'none'


def test_role_string_username_chat_is_none(service_factory):
    service = service_factory('@some_channel', admins={100})
    assert service.resolve_recipient_role() == 'none'


def test_role_numeric_string_chat_id_is_resolved(service_factory):
    # ADMIN_NOTIFICATIONS_CHAT_ID may arrive as a numeric string from env.
    service = service_factory('100', admins={100})
    assert service.resolve_recipient_role() == 'admin'


# --- cabinet deep-link button -----------------------------------------------


@pytest.fixture
def cabinet_settings(monkeypatch):
    """Configure settings for the cabinet deep-link button.

    is_cabinet_mode / get_bot_username are Settings methods → patch on the class;
    MINIAPP_* are fields → patch on the instance.
    """

    def _configure(*, cabinet_mode=True, custom_url='https://cab.example.com', short_name='cabinet', bot='mybot'):
        from app.config import settings

        monkeypatch.setattr(type(settings), 'is_cabinet_mode', lambda self: cabinet_mode, raising=False)
        monkeypatch.setattr(type(settings), 'get_bot_username', lambda self: bot, raising=False)
        monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', custom_url, raising=False)
        monkeypatch.setattr(settings, 'MINIAPP_APP_SHORT_NAME', short_name, raising=False)

    return _configure


def test_cabinet_button_none_when_not_cabinet_mode(cabinet_settings):
    cabinet_settings(cabinet_mode=False)
    assert build_admin_ticket_cabinet_button(42, text='x', in_group=False) is None
    assert build_admin_ticket_cabinet_button(42, text='x', in_group=True) is None


def test_cabinet_button_private_is_webapp_to_admin_ticket_path(cabinet_settings):
    cabinet_settings()
    btn = build_admin_ticket_cabinet_button(42, text='🗂 Кабинет', in_group=False)
    assert btn is not None
    assert btn.web_app is not None
    assert btn.web_app.url == 'https://cab.example.com/admin/tickets/42'
    assert btn.url is None


def test_cabinet_button_private_none_without_custom_url(cabinet_settings):
    cabinet_settings(custom_url='')
    assert build_admin_ticket_cabinet_button(42, text='x', in_group=False) is None


def test_cabinet_button_group_is_startapp_deeplink(cabinet_settings):
    cabinet_settings()
    btn = build_admin_ticket_cabinet_button(42, text='🗂 Кабинет', in_group=True)
    assert btn is not None
    assert btn.url == 'https://t.me/mybot/cabinet?startapp=admin_ticket_42'
    assert btn.web_app is None


def test_cabinet_button_group_none_without_short_name(cabinet_settings):
    # No registered Mini App → can't build a group-safe deep link.
    cabinet_settings(short_name='')
    assert build_admin_ticket_cabinet_button(42, text='x', in_group=True) is None


def test_cabinet_button_group_none_without_bot_username(cabinet_settings):
    cabinet_settings(bot=None)
    assert build_admin_ticket_cabinet_button(42, text='x', in_group=True) is None


def test_keyboard_places_cabinet_button_on_top():
    cab = InlineKeyboardButton(text='🗂 Кабинет', url='https://t.me/mybot/cabinet?startapp=admin_ticket_7')
    kb = get_ticket_notification_keyboard(7, user_id=42, is_admin=True, cabinet_button=cab)
    # First row is the cabinet button.
    assert kb.inline_keyboard[0][0].url == 'https://t.me/mybot/cabinet?startapp=admin_ticket_7'
