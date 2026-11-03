import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import Command

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_support_keyboard
from app.localization.texts import get_texts
from app.services.support_settings_service import SupportSettingsService
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)


async def show_support_info(callback: types.CallbackQuery, db_user: User):
    get_texts(db_user.language)
    support_info = SupportSettingsService.get_support_info_text(db_user.language)
    await edit_or_answer_photo(
        callback=callback,
        caption=support_info,
        keyboard=get_support_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_support_info, F.data == 'menu_support', flags={'support_access': True})


async def payment_support_command(message: types.Message, db_user: User | None = None):
    language = db_user.language if db_user else (message.from_user.language_code or settings.DEFAULT_LANGUAGE)
    language = 'ua' if language == 'uk' else language
    texts = get_texts(language)
    rows = []
    if db_user and db_user.status == 'active' and SupportSettingsService.is_tickets_enabled():
        rows.extend(
            [
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CREATE_TICKET_BUTTON'), callback_data='payment_support_create'
                    )
                ],
                [types.InlineKeyboardButton(text=texts.t('MY_TICKETS_BUTTON'), callback_data='my_tickets')],
            ]
        )
    # Keep a contact route even when tickets/account access is blocked or the menu is hidden.
    contact = settings.get_support_contact_url()
    if contact:
        rows.append([types.InlineKeyboardButton(text=texts.t('CONTACT_SUPPORT_BUTTON'), url=contact)])
    text = texts.t('PAYMENT_SUPPORT_INFO')
    if not rows:
        text += '\n\n' + SupportSettingsService.get_support_info_text(language)
    await message.answer(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows) if rows else None, parse_mode='HTML'
    )


def register_command_handlers(dp: Dispatcher):
    # Register before purchase/registration FSM handlers. Old payments still need help
    # after new Stars payments have been disabled; only menu visibility is conditional.
    dp.message.register(
        payment_support_command, Command('paysupport'), F.chat.type == 'private', flags={'public_payment_support': True}
    )
