"""После действия над заявкой на вывод в группе карточка остаётся групповой.

«Одобрить»/«Отклонить» перерисовывали сообщение экраном личной админки:
«Профиль пользователя» и «⬅️ К списку» в общем чате открывали бы админку
прямо в группе. В группе после действия остаются только действия по новому
статусу (одобрена → «Деньги переведены»), в личке админа — как раньше.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.enums import ChatType

import app.handlers.admin.referrals as admin_referrals
from app.config import Settings
from app.database.models import WithdrawalRequestStatus


def _request() -> MagicMock:
    request = MagicMock()
    request.id = 5
    request.user_id = 42
    request.status = WithdrawalRequestStatus.PENDING.value
    request.amount_kopeks = 50_000
    request.payment_details = 'карта 1234'
    request.risk_analysis = None
    request.created_at = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    return request


def _user() -> MagicMock:
    user = MagicMock()
    user.id = 42
    user.telegram_id = 777
    user.full_name = 'Автор'
    user.email = None
    user.language = 'ru'
    return user


def _callback(chat_type: str) -> MagicMock:
    # @admin_required узнаёт нажатие по isinstance(CallbackQuery); spec тут не годится —
    # поля pydantic-модели не атрибуты класса, поэтому подменяем __class__.
    callback = MagicMock()
    callback.__class__ = types.CallbackQuery
    callback.data = 'admin_withdrawal_approve_5'
    callback.from_user.id = 1
    callback.from_user.username = 'admin'
    callback.answer = AsyncMock()
    callback.bot.send_message = AsyncMock()
    callback.message.chat.type = chat_type
    callback.message.edit_text = AsyncMock()
    return callback


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


@pytest.fixture
def approved(monkeypatch):
    monkeypatch.setattr(Settings, 'is_admin', lambda self, user_id: True)
    request = _request()

    async def approve(db, request_id, admin_id):
        request.status = WithdrawalRequestStatus.APPROVED.value
        return True, None

    monkeypatch.setattr(admin_referrals.referral_withdrawal_service, 'approve_request', approve)
    monkeypatch.setattr(admin_referrals.referral_withdrawal_service, 'format_analysis_for_admin', lambda analysis: '')
    monkeypatch.setattr(admin_referrals, 'get_user_by_id', AsyncMock(return_value=_user()))
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: request))
    return db


@pytest.mark.asyncio
async def test_approve_in_group_leaves_only_the_next_action(approved):
    callback = _callback(ChatType.SUPERGROUP)

    await admin_referrals.approve_withdrawal_request(callback, _user(), approved)

    callback.message.edit_text.assert_awaited_once()
    callbacks = _callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert callbacks == ['admin_withdrawal_complete_5']


@pytest.mark.asyncio
async def test_approve_in_private_keeps_profile_and_navigation(approved):
    callback = _callback(ChatType.PRIVATE)

    await admin_referrals.approve_withdrawal_request(callback, _user(), approved)

    callbacks = _callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'admin_withdrawal_complete_5' in callbacks
    assert 'admin_user_manage_42' in callbacks
    assert 'admin_withdrawal_requests' in callbacks
