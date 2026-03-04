"""
Admin handlers for DonMatteo-AI-Tiket provider management.

Provides Telegram admin UI for:
- Multi-provider list with status
- Add/remove API keys per provider
- Test connection & fetch models
- Select model from dynamic list
- Set provider priority and enable/disable
- Edit/reset system prompt
"""

import html
import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.modules.ai_ticket.services import ai_manager
from app.modules.ai_ticket.services import prompt_service
from app.utils.decorators import admin_required, error_handler

logger = structlog.get_logger(__name__)

PROVIDER_EMOJI = {
    'groq': '🟢',
    'openai': '🔵',
    'anthropic': '🟠',
    'google': '🔴',
    'openrouter': '🟣',
}


class AIProviderStates(StatesGroup):
    waiting_for_api_key = State()
    waiting_for_priority = State()
    waiting_for_custom_prompt = State()


# ───────────────── Provider List ─────────────────

@admin_required
@error_handler
async def show_ai_providers(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Show the list of all AI providers with their status."""
    await ai_manager.ensure_providers_exist(db)
    providers = await ai_manager.get_providers(db)

    text_lines = ['🤖 <b>AI Провайдеры</b>\n']
    rows: list[list[types.InlineKeyboardButton]] = []

    for p in providers:
        emoji = PROVIDER_EMOJI.get(p.name, '⚪')
        keys_count = len(p.api_keys or [])
        model = p.selected_model or '—'
        status = '✅' if p.enabled and keys_count > 0 else '❌'

        text_lines.append(
            f'{emoji} <b>{p.name.upper()}</b> [{status}]  '
            f'Приоритет: {p.priority}  '
            f'Ключей: {keys_count}  '
            f'Модель: <code>{model}</code>'
        )

        rows.append([
            types.InlineKeyboardButton(
                text=f'{emoji} {p.name.upper()} ({keys_count} 🔑)',
                callback_data=f'aip_detail:{p.name}',
            )
        ])

    rows.append([
        types.InlineKeyboardButton(
            text='📝 Системный промпт',
            callback_data='aip_prompt',
        )
    ])
    rows.append([
        types.InlineKeyboardButton(
            text='📚 База знаний (FAQ)',
            callback_data='admin_support_ai_faq',
        )
    ])
    rows.append([
        types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_support_settings')
    ])

    await callback.message.edit_text(
        '\n'.join(text_lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )
    await callback.answer()


# ───────────────── Provider Detail ─────────────────

@admin_required
@error_handler
async def show_provider_detail(callback: types.CallbackQuery, db_user: User, db: AsyncSession, provider_name: str | None = None):
    """Detail view for a single provider: keys, model, priority, enable/disable."""
    provider_name = provider_name or callback.data.split(':')[1]
    provider = await ai_manager.get_provider(db, provider_name)
    if not provider:
        await callback.answer('Провайдер не найден', show_alert=True)
        return

    emoji = PROVIDER_EMOJI.get(provider_name, '⚪')
    keys = provider.api_keys or []
    model = provider.selected_model or '—'

    text_lines = [
        f'{emoji} <b>{provider_name.upper()}</b>\n',
        f'Статус: {"✅ Включён" if provider.enabled else "❌ Выключен"}',
        f'Приоритет: {provider.priority} <i>(меньше = первый)</i>',
        f'Модель: <code>{model}</code>',
        f'Ключей: {len(keys)}',
    ]

    if keys:
        text_lines.append('\n<b>API ключи:</b>')
        for i, k in enumerate(keys):
            active = ' ← активный' if i == (provider.active_key_index or 0) else ''
            masked = k[:8] + '...' + k[-4:] if len(k) > 12 else '***'
            text_lines.append(f'  {i + 1}. <code>{masked}</code>{active}')

    rows: list[list[types.InlineKeyboardButton]] = []

    # Toggle enabled
    rows.append([
        types.InlineKeyboardButton(
            text=f'{"🔴 Выключить" if provider.enabled else "🟢 Включить"}',
            callback_data=f'aip_toggle:{provider_name}',
        )
    ])

    # Add / remove key
    rows.append([
        types.InlineKeyboardButton(
            text='➕ Добавить ключ',
            callback_data=f'aip_addkey:{provider_name}',
        ),
    ])

    if keys:
        rows.append([
            types.InlineKeyboardButton(
                text='🗑 Удалить последний ключ',
                callback_data=f'aip_rmkey:{provider_name}',
            )
        ])

    # Test connection
    rows.append([
        types.InlineKeyboardButton(
            text='🔍 Тест и загрузка моделей',
            callback_data=f'aip_test:{provider_name}',
        )
    ])

    # Model selection (if models cached)
    if provider.available_models:
        rows.append([
            types.InlineKeyboardButton(
                text=f'🧠 Выбрать модель ({len(provider.available_models)} доступно)',
                callback_data=f'aip_models:{provider_name}:0',
            )
        ])

    # Priority
    rows.append([
        types.InlineKeyboardButton(
            text='⬆️',
            callback_data=f'aip_prio:{provider_name}:up',
        ),
        types.InlineKeyboardButton(
            text=f'Приоритет: {provider.priority}',
            callback_data='noop',
        ),
        types.InlineKeyboardButton(
            text='⬇️',
            callback_data=f'aip_prio:{provider_name}:down',
        ),
    ])

    rows.append([
        types.InlineKeyboardButton(text='⬅️ Назад', callback_data='aip_list')
    ])

    await callback.message.edit_text(
        '\n'.join(text_lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )
    await callback.answer()


# ───────────────── Toggle Enable ─────────────────

@admin_required
@error_handler
async def toggle_provider(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    name = callback.data.split(':')[1]
    provider = await ai_manager.get_provider(db, name)
    if provider:
        await ai_manager.set_enabled(db, name, not provider.enabled)
    # Re-show detail
    await show_provider_detail(callback, db_user, db, provider_name=name)


# ───────────────── Add Key ─────────────────

@admin_required
@error_handler
async def start_add_key(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    name = callback.data.split(':')[1]
    await state.set_state(AIProviderStates.waiting_for_api_key)
    await state.update_data(provider_name=name)

    await callback.message.edit_text(
        f'🔑 Отправьте API ключ для <b>{name.upper()}</b>:',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'aip_detail:{name}')]
        ]),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_api_key_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    name = data.get('provider_name', '')
    key = (message.text or '').strip()

    if not key:
        await message.answer('❌ Пустой ключ. Попробуйте ещё раз.')
        return

    await ai_manager.add_key(db, name, key)
    await state.clear()

    # Delete the message with the key for security
    try:
        await message.delete()
    except Exception:
        pass

    await message.answer(
        f'✅ Ключ добавлен для <b>{name.upper()}</b>.',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=f'⬅️ К {name.upper()}', callback_data=f'aip_detail:{name}')]
        ]),
    )


# ───────────────── Remove Last Key ─────────────────

@admin_required
@error_handler
async def remove_last_key(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    name = callback.data.split(':')[1]
    provider = await ai_manager.get_provider(db, name)
    if provider:
        keys = provider.api_keys or []
        if keys:
            await ai_manager.remove_key(db, name, len(keys) - 1)

    await show_provider_detail(callback, db_user, db, provider_name=name)


# ───────────────── Test Connection ─────────────────

@admin_required
@error_handler
async def test_provider_connection(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    name = callback.data.split(':')[1]
    await callback.answer('⏳ Тестирую подключение...', show_alert=False)

    result = await ai_manager.test_connection(db, name)

    if result['ok']:
        models = result.get('models', [])
        count = len(models)
        await callback.message.answer(
            f'✅ <b>{name.upper()}</b> — подключение успешно!\n'
            f'Найдено моделей: {count}\n\n'
            f'Первые 10:\n' + '\n'.join(f'• <code>{m}</code>' for m in models[:10]),
            parse_mode='HTML',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(
                    text=f'🧠 Выбрать модель ({count})',
                    callback_data=f'aip_models:{name}:0',
                )],
                [types.InlineKeyboardButton(text=f'⬅️ К {name.upper()}', callback_data=f'aip_detail:{name}')],
            ]),
        )
    else:
        await callback.message.answer(
            f'❌ <b>{name.upper()}</b> — ошибка:\n<code>{html.escape(result.get("error", "unknown"))}</code>',
            parse_mode='HTML',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text=f'⬅️ К {name.upper()}', callback_data=f'aip_detail:{name}')],
            ]),
        )


# ───────────────── Model Selection ─────────────────

@admin_required
@error_handler
async def show_model_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Show paginated model list (10 per page)."""
    parts = callback.data.split(':')
    name = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0

    provider = await ai_manager.get_provider(db, name)
    if not provider or not provider.available_models:
        await callback.answer('Сначала запустите "Тест и загрузка моделей"', show_alert=True)
        return

    models = provider.available_models
    per_page = 8
    total_pages = (len(models) + per_page - 1) // per_page
    page_models = models[page * per_page:(page + 1) * per_page]

    rows: list[list[types.InlineKeyboardButton]] = []
    for m in page_models:
        selected = ' ✅' if m == provider.selected_model else ''
        # Truncate long model names for button text
        display = m[:40] + ('…' if len(m) > 40 else '')
        rows.append([
            types.InlineKeyboardButton(
                text=f'{display}{selected}',
                callback_data=f'aip_setmodel:{name}:{m}',
            )
        ])

    # Pagination
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(text='⬅️', callback_data=f'aip_models:{name}:{page - 1}'))
    nav.append(types.InlineKeyboardButton(text=f'{page + 1}/{total_pages}', callback_data='noop'))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton(text='➡️', callback_data=f'aip_models:{name}:{page + 1}'))
    rows.append(nav)

    rows.append([
        types.InlineKeyboardButton(text=f'⬅️ К {name.upper()}', callback_data=f'aip_detail:{name}')
    ])

    await callback.message.edit_text(
        f'🧠 <b>Модели {name.upper()}</b>\nВыберите модель:',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def set_model(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split(':')
    name = parts[1]
    model = ':'.join(parts[2:])  # model name may contain ':'

    await ai_manager.set_model(db, name, model)
    await callback.answer(f'✅ Модель: {model}', show_alert=True)

    await show_provider_detail(callback, db_user, db, provider_name=name)


# ───────────────── Priority ─────────────────

@admin_required
@error_handler
async def change_priority(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split(':')
    name = parts[1]
    direction = parts[2]  # 'up' or 'down'

    provider = await ai_manager.get_provider(db, name)
    if provider:
        current = provider.priority
        new_priority = max(0, current - 1) if direction == 'up' else current + 1
        await ai_manager.set_priority(db, name, new_priority)

    await show_provider_detail(callback, db_user, db, provider_name=name)


# ───────────────── System Prompt ─────────────────

@admin_required
@error_handler
async def show_prompt_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Show current prompt and options to edit/reset."""
    current = await prompt_service.get_system_prompt(db)
    stock = prompt_service.get_stock_prompt()
    is_custom = current != stock

    preview = current[:500] + ('…' if len(current) > 500 else '')

    text = (
        f'📝 <b>Системный промпт AI</b>\n\n'
        f'Тип: {"✏️ Кастомный" if is_custom else "📋 Стандартный"}\n\n'
        f'<code>{html.escape(preview)}</code>'
    )

    rows = [
        [types.InlineKeyboardButton(text='✏️ Редактировать промпт', callback_data='aip_prompt_edit')],
    ]
    if is_custom:
        rows.append([
            types.InlineKeyboardButton(text='🔄 Сбросить на стандартный', callback_data='aip_prompt_reset')
        ])
    rows.append([
        types.InlineKeyboardButton(text='👁 Показать полностью', callback_data='aip_prompt_full')
    ])
    rows.append([
        types.InlineKeyboardButton(text='⬅️ Назад', callback_data='aip_list')
    ])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_full_prompt(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Send full prompt as a separate message."""
    current = await prompt_service.get_system_prompt(db)

    # Split into chunks if too long
    chunks = [current[i:i + 4000] for i in range(0, len(current), 4000)]
    for chunk in chunks:
        await callback.message.answer(
            f'<code>{html.escape(chunk)}</code>',
            parse_mode='HTML',
        )
    await callback.answer('Промпт отправлен ниже')


@admin_required
@error_handler
async def start_prompt_edit(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(AIProviderStates.waiting_for_custom_prompt)
    await callback.message.edit_text(
        '📝 <b>Отправьте новый системный промпт.</b>\n\n'
        'Можно использовать переменную <code>{service_name}</code> — '
        'она будет заменена на имя вашего сервиса.',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='❌ Отмена', callback_data='aip_prompt')]
        ]),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_prompt_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    text = message.text or ''
    if not text.strip():
        await message.answer('❌ Пустой промпт. Попробуйте ещё раз.')
        return

    await prompt_service.set_custom_prompt(db, text)
    await state.clear()
    await message.answer(
        '✅ Системный промпт обновлён.',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='📝 К промпту', callback_data='aip_prompt')]
        ]),
    )


@admin_required
@error_handler
async def reset_prompt(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await prompt_service.reset_to_stock(db)
    await callback.answer('✅ Промпт сброшен на стандартный', show_alert=True)

    await show_prompt_settings(callback, db_user, db)


# ───────────────── Noop (for static buttons) ─────────────────

async def noop_callback(callback: types.CallbackQuery):
    await callback.answer()


# ───────────────── Registration ─────────────────

def register_ai_provider_handlers(dp: Dispatcher) -> None:
    # Provider list
    dp.callback_query.register(show_ai_providers, F.data == 'aip_list')
    # Also trigger from old callbacks for backward compat
    dp.callback_query.register(show_ai_providers, F.data == 'admin_support_ai_provider')

    # Provider detail
    dp.callback_query.register(show_provider_detail, F.data.startswith('aip_detail:'))

    # Toggle enable
    dp.callback_query.register(toggle_provider, F.data.startswith('aip_toggle:'))

    # Add/remove keys
    dp.callback_query.register(start_add_key, F.data.startswith('aip_addkey:'))
    dp.callback_query.register(remove_last_key, F.data.startswith('aip_rmkey:'))
    dp.message.register(handle_api_key_input, AIProviderStates.waiting_for_api_key)

    # Test connection
    dp.callback_query.register(test_provider_connection, F.data.startswith('aip_test:'))

    # Model selection
    dp.callback_query.register(show_model_list, F.data.startswith('aip_models:'))
    dp.callback_query.register(set_model, F.data.startswith('aip_setmodel:'))

    # Priority
    dp.callback_query.register(change_priority, F.data.startswith('aip_prio:'))

    # System prompt
    dp.callback_query.register(show_prompt_settings, F.data == 'aip_prompt')
    dp.callback_query.register(show_full_prompt, F.data == 'aip_prompt_full')
    dp.callback_query.register(start_prompt_edit, F.data == 'aip_prompt_edit')
    dp.callback_query.register(reset_prompt, F.data == 'aip_prompt_reset')
    dp.message.register(handle_prompt_input, AIProviderStates.waiting_for_custom_prompt)

    # Noop
    dp.callback_query.register(noop_callback, F.data == 'noop')
