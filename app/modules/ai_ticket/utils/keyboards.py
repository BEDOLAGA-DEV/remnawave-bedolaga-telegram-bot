from aiogram import types
from app.localization.texts import get_texts


def get_manager_kb(ticket_id: int, lang: str = 'ru', ai_enabled: bool = True) -> types.InlineKeyboardMarkup:
    """Клавиатура управления тикетом в Forum-топике (для менеджера)."""
    texts = get_texts(lang)
    ai_btn_text = '🔇 Выключить AI' if ai_enabled else '🤖 Включить AI'
    ai_btn_data = f'ai_ticket_toggle_ai:{ticket_id}'

    close_btn_text = texts.get('CLOSE_TICKET_BUTTON', '✅ Закрыть тикет')

    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text=ai_btn_text, callback_data=ai_btn_data),
                types.InlineKeyboardButton(text=close_btn_text, callback_data=f'ai_ticket_close:{ticket_id}')
            ]
        ]
    )


def get_user_navigation_kb(ticket_id: int | None = None, lang: str = 'ru', show_call_manager: bool = True) -> types.InlineKeyboardMarkup:
    """Навигационные кнопки для пользователя: Вызвать менеджера (опционально), Мои обращения, Главное меню."""
    texts = get_texts(lang)
    kb = []

    if ticket_id and show_call_manager:
        call_manager_text = texts.get('AI_TICKET_CALL_MANAGER', '🆘 Вызвать менеджера')
        kb.append([types.InlineKeyboardButton(text=call_manager_text, callback_data=f'ai_ticket_call_manager:{ticket_id}')])

    my_tickets_text = texts.get('MY_TICKETS_BUTTON', '🎫 Мои обращения')
    main_menu_text = texts.get('MAIN_MENU_BUTTON', '🏠 Главное меню')

    kb.append([
        types.InlineKeyboardButton(text=my_tickets_text, callback_data='my_tickets'),
        types.InlineKeyboardButton(text=main_menu_text, callback_data='menu_support')
    ])

    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_user_reply_kb(ticket_id: int, lang: str = 'ru', show_call_manager: bool = True) -> types.InlineKeyboardMarkup:
    """Клавиатура с кнопкой «Ответить» для пользователя (как в оригинале get_ticket_view_keyboard)."""
    texts = get_texts(lang)
    kb = []

    # Кнопка «Ответить» — как в оригинале
    kb.append([
        types.InlineKeyboardButton(
            text=texts.t('REPLY_TO_TICKET', '💬 Ответить'),
            callback_data=f'reply_forum_ticket_{ticket_id}'
        )
    ])

    # Вызвать менеджера (если AI включён)
    if show_call_manager:
        kb.append([
            types.InlineKeyboardButton(
                text=texts.get('AI_TICKET_CALL_MANAGER', '🆘 Вызвать менеджера'),
                callback_data=f'ai_ticket_call_manager:{ticket_id}'
            )
        ])

    # Посмотреть тикет
    kb.append([
        types.InlineKeyboardButton(
            text=texts.t('VIEW_TICKET', '👁️ Посмотреть тикет'),
            callback_data=f'view_forum_ticket_{ticket_id}'
        )
    ])

    kb.append([
        types.InlineKeyboardButton(text=texts.get('MAIN_MENU_BUTTON', '🏠 Главное меню'), callback_data='menu_support')
    ])

    return types.InlineKeyboardMarkup(inline_keyboard=kb)
