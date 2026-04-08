import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.ticket_quick_reply import TicketQuickReplyCRUD
from app.database.models import User
from app.localization.texts import get_texts
from app.states import QuickReplyStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

TICKET_CATEGORIES = ['billing', 'technical', 'account', 'other']
CATEGORY_LABELS = {
    'billing': '💰 Оплата',
    'technical': '🔧 Техническая',
    'account': '👤 Аккаунт',
    'other': '📝 Другое',
}


def _quick_replies_list_keyboard(
    replies, language: str = 'ru'
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = []
    for r in replies:
        cat = CATEGORY_LABELS.get(r.category, '') if r.category else ''
        rows.append(
            [
                InlineKeyboardButton(
                    text=f'{cat} {r.title}'[:50],
                    callback_data=f'qr_view:{r.id}',
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text='➕ Добавить шаблон', callback_data='qr_add')]
    )
    rows.append(
        [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_support')]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_required
@error_handler
async def admin_quick_replies(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession
):
    replies = await TicketQuickReplyCRUD.get_quick_replies(db)
    text = '📋 <b>Быстрые ответы</b>\n\nШаблоны для быстрых ответов на тикеты.'
    if not replies:
        text += '\n\nПока шаблонов нет.'
    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=_quick_replies_list_keyboard(replies, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def qr_view(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession
):
    reply_id = int(callback.data.split(':')[1])
    replies = await TicketQuickReplyCRUD.get_quick_replies(db)
    reply = next((r for r in replies if r.id == reply_id), None)
    if not reply:
        await callback.answer('Шаблон не найден', show_alert=True)
        return

    texts = get_texts(db_user.language)
    cat = CATEGORY_LABELS.get(reply.category, reply.category or 'Без категории')
    text = (
        f'📋 <b>{reply.title}</b>\n\n'
        f'<b>Категория:</b> {cat}\n\n'
        f'<b>Текст:</b>\n{reply.text}'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='🗑️ Удалить',
                    callback_data=f'qr_delete:{reply.id}',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_quick_replies')],
        ]
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    await callback.answer()


@admin_required
@error_handler
async def qr_add(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession
):
    await state.set_state(QuickReplyStates.waiting_for_title)
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        '📋 <b>Новый шаблон</b>\n\nВведите заголовок шаблона:',
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.BACK, callback_data='admin_quick_replies')]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def qr_title_received(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession
):
    title = message.text.strip()[:255]
    await state.update_data(qr_title=title)
    await state.set_state(QuickReplyStates.waiting_for_text)
    await message.answer('Введите текст шаблона ответа:')


@admin_required
@error_handler
async def qr_text_received(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession
):
    text = message.text.strip()
    await state.update_data(qr_text=text)
    await state.set_state(QuickReplyStates.waiting_for_category)

    rows = []
    for cat in TICKET_CATEGORIES:
        rows.append(
            [
                InlineKeyboardButton(
                    text=CATEGORY_LABELS.get(cat, cat),
                    callback_data=f'qr_cat:{cat}',
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text='⏭️ Без категории', callback_data='qr_cat:none')]
    )
    await message.answer(
        'Выберите категорию для шаблона:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@admin_required
@error_handler
async def qr_category_selected(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession
):
    cat_raw = callback.data.split(':')[1]
    category = None if cat_raw == 'none' else cat_raw

    data = await state.get_data()
    await state.clear()

    await TicketQuickReplyCRUD.create_quick_reply(
        db,
        title=data['qr_title'],
        text=data['qr_text'],
        category=category,
        created_by=db_user.id,
    )
    await db.commit()

    await callback.answer('Шаблон создан!', show_alert=True)

    replies = await TicketQuickReplyCRUD.get_quick_replies(db)
    await callback.message.edit_text(
        '📋 <b>Быстрые ответы</b>\n\nШаблон успешно создан.',
        parse_mode='HTML',
        reply_markup=_quick_replies_list_keyboard(replies, db_user.language),
    )


@admin_required
@error_handler
async def qr_delete(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession
):
    reply_id = int(callback.data.split(':')[1])
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        f'🗑️ Удалить шаблон #{reply_id}?',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='✅ Да, удалить',
                        callback_data=f'qr_delete_confirm:{reply_id}',
                    ),
                    InlineKeyboardButton(text=texts.BACK, callback_data=f'qr_view:{reply_id}'),
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def qr_delete_confirm(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession
):
    reply_id = int(callback.data.split(':')[1])
    deleted = await TicketQuickReplyCRUD.delete_quick_reply(db, reply_id)
    await db.commit()

    if deleted:
        await callback.answer('Шаблон удален!', show_alert=True)
    else:
        await callback.answer('Шаблон не найден.', show_alert=True)

    replies = await TicketQuickReplyCRUD.get_quick_replies(db)
    await callback.message.edit_text(
        '📋 <b>Быстрые ответы</b>\n\nШаблоны для быстрых ответов на тикеты.',
        parse_mode='HTML',
        reply_markup=_quick_replies_list_keyboard(replies, db_user.language),
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(admin_quick_replies, F.data == 'admin_quick_replies')
    dp.callback_query.register(qr_view, F.data.startswith('qr_view:'))
    dp.callback_query.register(qr_add, F.data == 'qr_add')
    dp.callback_query.register(
        qr_category_selected, F.data.startswith('qr_cat:')
    )
    dp.callback_query.register(
        qr_delete,
        F.data.startswith('qr_delete:') & ~F.data.startswith('qr_delete_confirm:'),
    )
    dp.callback_query.register(
        qr_delete_confirm, F.data.startswith('qr_delete_confirm:')
    )
    dp.message.register(qr_title_received, QuickReplyStates.waiting_for_title)
    dp.message.register(qr_text_received, QuickReplyStates.waiting_for_text)
