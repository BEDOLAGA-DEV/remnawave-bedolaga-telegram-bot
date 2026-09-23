"""Кнопки заявки на вывод реферального баланса — одни на уведомление и карточку.

Уведомление уходит в личку админа или в групповой админ-чат, карточка
открывается из списка заявок в админке. Набор зависит от роли получателя:

- ``admin`` — действия по статусу, «Профиль пользователя» (по id из базы, как
  в карточке пользователя) и, если попросили, «⬅️ К списку»;
- ``group`` — только действия: в общем чате админку не открывают, а получателя
  не определить (privacy mode);
- ``moderator`` / ``none`` — ничего: одобрение только для админа
  (``@admin_required``), модератору кнопки отвечали бы «нет доступа».

Кнопки для группы обязаны быть в ``group_callbacks`` — иначе фильтр чатов их
заглушит; сторож в тестах это проверяет.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import WithdrawalRequestStatus


def _actions(request_id: int, status: str) -> list[InlineKeyboardButton]:
    if status == WithdrawalRequestStatus.PENDING.value:
        return [
            InlineKeyboardButton(text='✅ Одобрить', callback_data=f'admin_withdrawal_approve_{request_id}'),
            InlineKeyboardButton(text='❌ Отклонить', callback_data=f'admin_withdrawal_reject_{request_id}'),
        ]
    if status == WithdrawalRequestStatus.APPROVED.value:
        return [
            InlineKeyboardButton(text='✅ Деньги переведены', callback_data=f'admin_withdrawal_complete_{request_id}')
        ]
    return []


def get_withdrawal_request_keyboard(
    request_id: int,
    status: str,
    *,
    user_db_id: int | None = None,
    role: str = 'admin',
    navigation: bool = False,
) -> InlineKeyboardMarkup | None:
    """Клавиатура заявки по её статусу и роли получателя; ``None`` — кнопок нет."""
    if role not in ('admin', 'group'):
        return None

    rows: list[list[InlineKeyboardButton]] = []
    actions = _actions(request_id, status)
    if actions:
        rows.append(actions)

    if role == 'admin':
        if user_db_id:
            rows.append(
                [InlineKeyboardButton(text='👤 Профиль пользователя', callback_data=f'admin_user_manage_{user_db_id}')]
            )
        if navigation:
            rows.append([InlineKeyboardButton(text='⬅️ К списку', callback_data='admin_withdrawal_requests')])

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
