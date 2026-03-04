"""
FAQ Admin Handler — CRUD for AI FAQ articles in the admin panel.

Accessible from the support settings menu when DonMatteo-AI-Tiket mode is active.
Поддержка медиа-вложений (фото, видео, анимации) для FAQ-статей.
Возможность редактирования содержимого статей.
"""

import re

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.database.models_ai_ticket import AIFaqArticle, AIFaqMedia
from app.localization.texts import get_texts
from app.utils.decorators import admin_required, error_handler

logger = structlog.get_logger(__name__)


# ─── FSM States ───

class FAQStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_content = State()
    waiting_for_keywords = State()
    # Редактирование полей статьи
    editing_title = State()
    editing_content = State()
    editing_keywords = State()


class FAQMediaStates(StatesGroup):
    """FSM для добавления медиа-вложений к FAQ-статье."""
    waiting_for_media = State()
    waiting_for_tag = State()
    waiting_for_caption = State()


# ─── Внутренние хелперы рендера (без декораторов) ───

async def _render_faq_list(callback: types.CallbackQuery, db: AsyncSession):
    """Рендер списка FAQ-статей."""
    stmt = select(AIFaqArticle).options(joinedload(AIFaqArticle.media)).order_by(AIFaqArticle.id)
    result = await db.execute(stmt)
    articles = result.unique().scalars().all()

    rows: list[list[types.InlineKeyboardButton]] = []

    if not articles:
        text = '📚 <b>База знаний (FAQ)</b>\n\nСтатьи отсутствуют. Добавьте первую!'
    else:
        text_parts = ['📚 <b>База знаний (FAQ)</b>\n']
        for article in articles:
            st = '✅' if article.is_active else '❌'
            media_count = len(article.media) if article.media else 0
            media_badge = f' 📎{media_count}' if media_count > 0 else ''
            text_parts.append(f'{st} <b>{article.title}</b> (ID: {article.id}){media_badge}')
            rows.append([
                types.InlineKeyboardButton(
                    text=f'📝 {article.title[:30]}',
                    callback_data=f'ai_faq_view:{article.id}',
                ),
            ])
        text = '\n'.join(text_parts)

    rows.append([types.InlineKeyboardButton(text='➕ Добавить статью', callback_data='ai_faq_add')])
    rows.append([types.InlineKeyboardButton(text='🔙 Назад', callback_data='admin_support_settings')])

    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _render_article_view(callback: types.CallbackQuery, db: AsyncSession, article_id: int):
    """Рендер просмотра одной FAQ-статьи с медиа и кнопками управления."""
    stmt = select(AIFaqArticle).options(joinedload(AIFaqArticle.media)).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.unique().scalars().first()

    if not article:
        await callback.answer('Статья не найдена', show_alert=True)
        return

    st = '✅ Активна' if article.is_active else '❌ Неактивна'
    text = (
        f'📄 <b>{article.title}</b>\n'
        f'Статус: {st}\n'
        f'Ключевые слова: {article.keywords or "—"}\n\n'
        f'{article.content}'
    )

    # Медиа-вложения
    media_items = article.media or []
    if media_items:
        text += '\n\n📎 <b>Медиа-вложения:</b>'
        for m in media_items:
            type_emoji = {'photo': '📷', 'video': '🎬', 'animation': '🎞'}.get(m.media_type, '📁')
            caption_text = f' — {m.caption}' if m.caption else ''
            text += f'\n{type_emoji} <code>{m.tag}</code>{caption_text}'

    toggle_text = '❌ Деактивировать' if article.is_active else '✅ Активировать'
    rows = [
        [types.InlineKeyboardButton(text=toggle_text, callback_data=f'ai_faq_toggle:{article.id}')],
        [
            types.InlineKeyboardButton(text='✏️ Заголовок', callback_data=f'ai_faq_edit_title:{article.id}'),
            types.InlineKeyboardButton(text='✏️ Текст', callback_data=f'ai_faq_edit_content:{article.id}'),
            types.InlineKeyboardButton(text='✏️ Ключевые', callback_data=f'ai_faq_edit_kw:{article.id}'),
        ],
        [types.InlineKeyboardButton(text='📎 Добавить медиа', callback_data=f'ai_faq_add_media:{article.id}')],
        [types.InlineKeyboardButton(text='🗑 Удалить статью', callback_data=f'ai_faq_delete:{article.id}')],
    ]

    # Кнопки удаления медиа
    for m in media_items:
        type_emoji = {'photo': '📷', 'video': '🎬', 'animation': '🎞'}.get(m.media_type, '📁')
        rows.append([
            types.InlineKeyboardButton(
                text=f'🗑 {type_emoji} {m.tag}',
                callback_data=f'ai_faq_del_media:{m.id}:{article.id}',
            )
        ])

    rows.append([types.InlineKeyboardButton(text='🔙 Назад', callback_data='admin_support_ai_faq')])

    # Обрезаем если слишком длинный
    if len(text) > 4000:
        text = text[:3997] + '…'
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows))


# ─── Список FAQ ───

@admin_required
@error_handler
async def show_faq_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Show list of FAQ articles."""
    await _render_faq_list(callback, db)
    await callback.answer()


# ─── Создание статьи ───

@admin_required
@error_handler
async def start_add_article(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Start adding a new FAQ article — ask for title."""
    await callback.message.edit_text(
        '📝 <b>Новая статья FAQ</b>\n\nВведите заголовок статьи:',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='🔙 Отмена', callback_data='admin_support_ai_faq')]
        ]),
    )
    await state.set_state(FAQStates.waiting_for_title)
    await callback.answer()


@admin_required
@error_handler
async def handle_faq_title(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Received the title — now ask for content."""
    title = (message.text or '').strip()
    if not title:
        await message.answer('❌ Заголовок не может быть пустым.')
        return
    await state.update_data(faq_title=title)
    await message.answer(
        f'📝 Заголовок: <b>{title}</b>\n\nТеперь введите текст статьи:',
        parse_mode='HTML',
    )
    await state.set_state(FAQStates.waiting_for_content)


@admin_required
@error_handler
async def handle_faq_content(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Received content — now ask for keywords (optional)."""
    content = message.html_text or message.text or ''
    if not content.strip():
        await message.answer('❌ Содержание не может быть пустым.')
        return
    await state.update_data(faq_content=content)
    await message.answer(
        '🏷 Введите ключевые слова через запятую (или отправьте «-» для пропуска):',
    )
    await state.set_state(FAQStates.waiting_for_keywords)


@admin_required
@error_handler
async def handle_faq_keywords(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Received keywords — save the article."""
    data = await state.get_data()
    keywords_text = (message.text or '').strip()
    keywords = '' if keywords_text == '-' else keywords_text

    article = AIFaqArticle(
        title=data['faq_title'],
        content=data['faq_content'],
        keywords=keywords,
        is_active=True,
    )
    db.add(article)
    await db.commit()
    await state.clear()

    import html
    title_escaped = html.escape(article.title)
    await message.answer(
        f'✅ Статья «{title_escaped}» добавлена в базу знаний!',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='📚 К списку FAQ', callback_data='admin_support_ai_faq')]
        ]),
    )


# ─── Просмотр / Переключение статьи ───

@admin_required
@error_handler
async def view_article(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """View a single FAQ article."""
    article_id = int(callback.data.split(':')[1])
    await _render_article_view(callback, db, article_id)
    await callback.answer()


@admin_required
@error_handler
async def toggle_article(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Toggle active status of a FAQ article."""
    article_id = int(callback.data.split(':')[1])
    stmt = select(AIFaqArticle).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.scalars().first()

    if not article:
        await callback.answer('Статья не найдена', show_alert=True)
        return

    article.is_active = not article.is_active
    await db.commit()

    await callback.answer(f'{"Активирована" if article.is_active else "Деактивирована"}')
    await _render_article_view(callback, db, article_id)


# ─── Удаление статьи ───

@admin_required
@error_handler
async def delete_article(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Delete a FAQ article."""
    article_id = int(callback.data.split(':')[1])
    stmt = delete(AIFaqArticle).where(AIFaqArticle.id == article_id)
    await db.execute(stmt)
    await db.commit()
    await callback.answer('🗑 Статья удалена')
    await _render_faq_list(callback, db)


# ─── Редактирование полей статьи ───

@admin_required
@error_handler
async def start_edit_title(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Начать редактирование заголовка."""
    article_id = int(callback.data.split(':')[1])
    await state.update_data(edit_article_id=article_id)
    await callback.message.edit_text(
        '✏️ <b>Редактирование заголовка</b>\n\nВведите новый заголовок:',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='🔙 Отмена', callback_data=f'ai_faq_view:{article_id}')]
        ]),
    )
    await state.set_state(FAQStates.editing_title)
    await callback.answer()


@admin_required
@error_handler
async def handle_edit_title(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Сохранить новый заголовок."""
    data = await state.get_data()
    article_id = data['edit_article_id']
    new_title = (message.text or '').strip()

    if not new_title:
        await message.answer('❌ Заголовок не может быть пустым.')
        return

    stmt = select(AIFaqArticle).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.scalars().first()

    if not article:
        await message.answer('❌ Статья не найдена.')
        await state.clear()
        return

    article.title = new_title
    await db.commit()
    await state.clear()

    import html
    new_title_escaped = html.escape(new_title)
    
    await message.answer(
        f'✅ Заголовок обновлён: <b>{new_title_escaped}</b>',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='📄 К статье', callback_data=f'ai_faq_view:{article_id}')],
            [types.InlineKeyboardButton(text='📚 К списку FAQ', callback_data='admin_support_ai_faq')],
        ]),
    )


@admin_required
@error_handler
async def start_edit_content(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Начать редактирование содержимого статьи."""
    article_id = int(callback.data.split(':')[1])
    await state.update_data(edit_article_id=article_id)

    # Показываем текущее содержимое
    stmt = select(AIFaqArticle).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.scalars().first()

    current_content = article.content if article else '—'
    
    import html
    preview = current_content[:500] + '…' if len(current_content) > 500 else current_content
    preview_escaped = html.escape(preview)

    await callback.message.edit_text(
        f'✏️ <b>Редактирование текста статьи</b>\n\n'
        f'Текущий текст:\n<i>{preview_escaped}</i>\n\n'
        f'Введите новый текст статьи:',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='🔙 Отмена', callback_data=f'ai_faq_view:{article_id}')]
        ]),
    )
    await state.set_state(FAQStates.editing_content)
    await callback.answer()


@admin_required
@error_handler
async def handle_edit_content(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Сохранить новое содержимое."""
    data = await state.get_data()
    article_id = data['edit_article_id']
    new_content = message.html_text or message.text or ''

    if not new_content.strip():
        await message.answer('❌ Содержание не может быть пустым.')
        return

    stmt = select(AIFaqArticle).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.scalars().first()

    if not article:
        await message.answer('❌ Статья не найдена.')
        await state.clear()
        return

    article.content = new_content
    await db.commit()
    await state.clear()

    await message.answer(
        '✅ Текст статьи обновлён!',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='📄 К статье', callback_data=f'ai_faq_view:{article_id}')],
            [types.InlineKeyboardButton(text='📚 К списку FAQ', callback_data='admin_support_ai_faq')],
        ]),
    )


@admin_required
@error_handler
async def start_edit_keywords(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Начать редактирование ключевых слов."""
    article_id = int(callback.data.split(':')[1])
    await state.update_data(edit_article_id=article_id)

    stmt = select(AIFaqArticle).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.scalars().first()

    import html
    current_kw = article.keywords if article and article.keywords else '—'
    kw_escaped = html.escape(current_kw)
    
    await callback.message.edit_text(
        f'✏️ <b>Редактирование ключевых слов</b>\n\n'
        f'Текущие: <i>{kw_escaped}</i>\n\n'
        f'Введите новые ключевые слова через запятую (или «-» для очистки):',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='🔙 Отмена', callback_data=f'ai_faq_view:{article_id}')]
        ]),
    )
    await state.set_state(FAQStates.editing_keywords)
    await callback.answer()


@admin_required
@error_handler
async def handle_edit_keywords(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Сохранить новые ключевые слова."""
    data = await state.get_data()
    article_id = data['edit_article_id']
    kw_text = (message.text or '').strip()
    new_keywords = '' if kw_text == '-' else kw_text

    stmt = select(AIFaqArticle).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.scalars().first()

    if not article:
        await message.answer('❌ Статья не найдена.')
        await state.clear()
        return

    article.keywords = new_keywords
    await db.commit()
    await state.clear()

    import html
    new_kw_escaped = html.escape(new_keywords) if new_keywords else "—"
    
    await message.answer(
        f'✅ Ключевые слова обновлены: <i>{new_kw_escaped}</i>',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='📄 К статье', callback_data=f'ai_faq_view:{article_id}')],
            [types.InlineKeyboardButton(text='📚 К списку FAQ', callback_data='admin_support_ai_faq')],
        ]),
    )


# ─── Медиа: добавление ───

@admin_required
@error_handler
async def start_add_media(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Начать добавление медиа-вложения к статье."""
    article_id = int(callback.data.split(':')[1])
    await state.update_data(media_article_id=article_id)

    await callback.message.edit_text(
        '📎 <b>Добавление медиа</b>\n\n'
        'Отправьте фото, видео или GIF-анимацию для прикрепления к статье:',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='🔙 Отмена', callback_data=f'ai_faq_view:{article_id}')]
        ]),
    )
    await state.set_state(FAQMediaStates.waiting_for_media)
    await callback.answer()


@admin_required
@error_handler
async def handle_media_file(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Получили медиа-файл — сохраняем file_id и тип, запрашиваем тег."""
    file_id = None
    media_type = None

    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = 'photo'
    elif message.video:
        file_id = message.video.file_id
        media_type = 'video'
    elif message.animation:
        file_id = message.animation.file_id
        media_type = 'animation'
    elif message.video_note:
        file_id = message.video_note.file_id
        media_type = 'video'  # Трактуем как видео для отправки
    elif message.document:
        file_id = message.document.file_id
        mime = message.document.mime_type or ''
        if mime.startswith('video/'):
            media_type = 'video'
        elif mime.startswith('image/'):
            media_type = 'photo'
        else:
            media_type = 'document'
    else:
        await message.answer(
            '❌ Нужно отправить <b>фото</b>, <b>видео</b>, <b>GIF</b> или файл с медиа.',
            parse_mode='HTML',
        )
        return

    await state.update_data(media_file_id=file_id, media_type=media_type)

    type_emoji = {'photo': '📷', 'video': '🎬', 'animation': '🎞'}.get(media_type, '📁')
    await message.answer(
        f'{type_emoji} Файл получен ({media_type}).\n\n'
        '🏷 Введите уникальный <b>тег</b> для этого медиа (латиница, цифры, подчёркивание).\n'
        'Например: <code>setup_android</code>, <code>payment_screen</code>\n\n'
        'ИИ будет использовать этот тег: <code>[MEDIA:ваш_тег]</code>',
        parse_mode='HTML',
    )
    await state.set_state(FAQMediaStates.waiting_for_tag)


@admin_required
@error_handler
async def handle_media_tag(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Получили тег — проверяем уникальность, запрашиваем описание."""
    tag = (message.text or '').strip()

    if not tag or not re.match(r'^[a-zA-Z0-9_]+$', tag):
        await message.answer(
            '❌ Тег должен содержать только латинские буквы, цифры и подчёркивание.\n'
            'Попробуйте ещё раз:',
        )
        return

    # Проверяем уникальность
    existing = await db.execute(select(AIFaqMedia.id).where(AIFaqMedia.tag == tag))
    if existing.scalar():
        await message.answer(f'❌ Тег <code>{tag}</code> уже занят. Введите другой:', parse_mode='HTML')
        return

    await state.update_data(media_tag=tag)
    await message.answer(
        f'🏷 Тег: <code>{tag}</code>\n\n'
        '📝 Введите описание медиа для ИИ (что изображено/показано).\n'
        'Или отправьте «-» для пропуска:',
        parse_mode='HTML',
    )
    await state.set_state(FAQMediaStates.waiting_for_caption)


@admin_required
@error_handler
async def handle_media_caption(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Получили описание — сохраняем медиа."""
    data = await state.get_data()
    caption_text = (message.text or '').strip()
    caption = None if caption_text == '-' else caption_text

    media = AIFaqMedia(
        article_id=data['media_article_id'],
        media_type=data['media_type'],
        file_id=data['media_file_id'],
        tag=data['media_tag'],
        caption=caption,
    )
    db.add(media)
    await db.commit()
    await state.clear()

    type_emoji = {'photo': '📷', 'video': '🎬', 'animation': '🎞'}.get(data['media_type'], '📁')
    await message.answer(
        f'✅ {type_emoji} Медиа <code>{data["media_tag"]}</code> добавлено!\n'
        f'ИИ сможет отправить его по тегу <code>[MEDIA:{data["media_tag"]}]</code>',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='📄 К статье', callback_data=f'ai_faq_view:{data["media_article_id"]}')],
            [types.InlineKeyboardButton(text='📚 К списку FAQ', callback_data='admin_support_ai_faq')],
        ]),
    )


# ─── Медиа: удаление ───

@admin_required
@error_handler
async def delete_media(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Удалить медиа-вложение."""
    parts = callback.data.split(':')
    media_id = int(parts[1])
    article_id = int(parts[2])

    stmt = select(AIFaqMedia).where(AIFaqMedia.id == media_id)
    result = await db.execute(stmt)
    media = result.scalars().first()

    if media:
        await db.delete(media)
        await db.commit()
        await callback.answer(f'🗑 Медиа {media.tag} удалено')
    else:
        await callback.answer('Медиа не найдено', show_alert=True)

    # Обновляем просмотр статьи (используем хелпер без декораторов)
    await _render_article_view(callback, db, article_id)


# ─── Registration ───

def register_faq_handlers(dp: Dispatcher) -> None:
    # Список статей
    dp.callback_query.register(show_faq_list, F.data == 'admin_support_ai_faq')
    # Создание статьи
    dp.callback_query.register(start_add_article, F.data == 'ai_faq_add')
    dp.message.register(handle_faq_title, FAQStates.waiting_for_title)
    dp.message.register(handle_faq_content, FAQStates.waiting_for_content)
    dp.message.register(handle_faq_keywords, FAQStates.waiting_for_keywords)
    # Просмотр / переключение / удаление статей
    dp.callback_query.register(view_article, F.data.startswith('ai_faq_view:'))
    dp.callback_query.register(toggle_article, F.data.startswith('ai_faq_toggle:'))
    dp.callback_query.register(delete_article, F.data.startswith('ai_faq_delete:'))
    # Редактирование полей статьи
    dp.callback_query.register(start_edit_title, F.data.startswith('ai_faq_edit_title:'))
    dp.callback_query.register(start_edit_content, F.data.startswith('ai_faq_edit_content:'))
    dp.callback_query.register(start_edit_keywords, F.data.startswith('ai_faq_edit_kw:'))
    dp.message.register(handle_edit_title, FAQStates.editing_title)
    dp.message.register(handle_edit_content, FAQStates.editing_content)
    dp.message.register(handle_edit_keywords, FAQStates.editing_keywords)
    # Медиа: добавление
    dp.callback_query.register(start_add_media, F.data.startswith('ai_faq_add_media:'))
    dp.message.register(handle_media_file, FAQMediaStates.waiting_for_media)
    dp.message.register(handle_media_tag, FAQMediaStates.waiting_for_tag)
    dp.message.register(handle_media_caption, FAQMediaStates.waiting_for_caption)
    # Медиа: удаление
    dp.callback_query.register(delete_media, F.data.startswith('ai_faq_del_media:'))
