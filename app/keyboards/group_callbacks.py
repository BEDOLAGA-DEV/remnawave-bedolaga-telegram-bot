"""Кнопки, которым разрешено работать в групповом админ-чате.

Бот молчит в чужих группах (``ChatTypeFilterMiddleware``), но свою карточку
тикета в групповой админ-чат он кладёт с кнопками действий. В группе работают
только «надёжные» кнопки — обычный callback без ввода текста: FSM-ввод там
невозможен из-за privacy mode бота, а меню админки в общем чате не место.

Единственный источник для фильтра: сторожа в тестах сверяют с ним каждую кнопку
групповых карточек (тикет — ``get_ticket_notification_keyboard(fsm_enabled=False)``,
заявка на вывод — ``get_withdrawal_request_keyboard(role='group')``), чтобы новая
кнопка не оказалась нарисованной, но мёртвой.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


GROUP_SAFE_CALLBACK_PREFIXES: tuple[str, ...] = (
    # карточка тикета
    'admin_close_ticket_',
    'admin_block_user_perm_ticket_',
    'admin_unblock_user_ticket_',
    # заявка на вывод реферального баланса
    'admin_withdrawal_approve_',
    'admin_withdrawal_reject_',
    'admin_withdrawal_complete_',
)

GROUP_SAFE_CALLBACKS: frozenset[str] = frozenset({'admin_support_delete_msg'})


def is_group_safe_callback(data: str | None) -> bool:
    """Можно ли обрабатывать это нажатие вне лички."""
    if not data:
        return False
    return data in GROUP_SAFE_CALLBACKS or data.startswith(GROUP_SAFE_CALLBACK_PREFIXES)


def strip_group_unsafe_buttons(
    markup: InlineKeyboardMarkup | None,
) -> tuple[InlineKeyboardMarkup | None, list[str]]:
    """Оставляет для группы только URL-кнопки и разрешённые callback'и.

    Возвращает новую клавиатуру (``None``, если ничего не осталось) и список
    выброшенных callback'ов — чтобы точка отправки записала их в лог: кнопка,
    нарисованная в группе и не доходящая до обработчика, хуже отсутствия кнопки.
    """
    if markup is None:
        return None, []

    dropped: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        kept = []
        for button in row:
            if button.callback_data is None or is_group_safe_callback(button.callback_data):
                kept.append(button)
            else:
                dropped.append(button.callback_data)
        if kept:
            rows.append(kept)

    return (InlineKeyboardMarkup(inline_keyboard=rows) if rows else None), dropped
