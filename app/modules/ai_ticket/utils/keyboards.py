from aiogram import types
from app.localization.texts import get_texts


def get_manager_kb(ticket_id: int, lang: str = 'ru', ai_enabled: bool = True) -> types.InlineKeyboardMarkup:
    """Keyboard for ticket management in Forum topic."""
    texts = get_texts(lang)
    # Using existing or new keys for manager buttons
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
    """Navigation buttons for user: Call Manager (if ticket_id and show_call_manager), My Tickets, Main Menu."""
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
