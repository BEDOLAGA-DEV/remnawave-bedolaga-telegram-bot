import html
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.poll import (
    create_poll,
    delete_poll,
    get_poll_by_id,
    get_poll_statistics,
    list_polls,
)
from app.database.models import Poll, User
from app.handlers.admin.messages import (
    get_custom_users,
    get_custom_users_count,
    get_target_display_name,
    get_target_users,
    get_target_users_count,
)
from app.keyboards.admin import get_admin_communications_submenu_keyboard
from app.localization.texts import get_texts
from app.services.poll_service import send_poll_to_users
from app.utils.decorators import admin_required, error_handler
from app.utils.validators import get_html_help_text, validate_html_tags


logger = structlog.get_logger(__name__)


def _safe_format_price(amount_kopeks: int) -> str:
    try:
        return settings.format_price(amount_kopeks)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error('Не удалось отформатировать сумму', amount_kopeks=amount_kopeks, error=error)
        return f'{amount_kopeks / 100:.2f} ₽'


async def _safe_delete_message(message: types.Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest as error:
        if 'message to delete not found' in str(error).lower():
            logger.debug('Сообщение уже удалено', error=error)
        else:
            logger.warning('Не удалось удалить сообщение', message_id=message.message_id, error=error)


async def _edit_creation_message(
    bot: Bot,
    state_data: dict,
    text: str,
    *,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    parse_mode: str | None = 'HTML',
) -> bool:
    chat_id = state_data.get('form_chat_id')
    message_id = state_data.get('form_message_id')

    if not chat_id or not message_id:
        return False

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return True
    except TelegramBadRequest as error:
        error_text = str(error).lower()
        if 'message is not modified' in error_text:
            return True
        log_method = logger.debug if 'there is no text in the message to edit' in error_text else logger.warning
        log_method(
            'Не удалось обновить сообщение создания опроса %s: %s',
            message_id,
            error,
        )
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            'Непредвиденная ошибка при обновлении сообщения создания опроса', message_id=message_id, error=error
        )
    return False


async def _send_creation_message(
    message: types.Message,
    state: FSMContext,
    text: str,
    *,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    parse_mode: str | None = 'HTML',
) -> types.Message:
    state_data = await state.get_data()
    chat_id = state_data.get('form_chat_id')
    message_id = state_data.get('form_message_id')

    if chat_id and message_id:
        try:
            await message.bot.delete_message(chat_id, message_id)
        except TelegramBadRequest as error:
            error_text = str(error).lower()
            if 'message to delete not found' in error_text:
                logger.debug('Сообщение уже удалено', error=error)
            else:
                logger.warning('Не удалось удалить сообщение создания опроса', message_id=message_id, error=error)
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                'Непредвиденная ошибка при удалении сообщения создания опроса', message_id=message_id, error=error
            )

    sent_message = await message.bot.send_message(
        chat_id=message.chat.id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )

    await state.update_data(
        form_chat_id=sent_message.chat.id,
        form_message_id=sent_message.message_id,
    )

    return sent_message


def _render_creation_progress(
    texts,
    data: dict,
    next_step: str,
    *,
    status_message: str | None = None,
    error_message: str | None = None,
) -> str:
    lines: list[str] = ['🗳️ <b>Создание опроса</b>']

    title_prompt = texts.t(
        'ADMIN_POLLS_CREATION_TITLE_PROMPT',
        'Введите заголовок опроса:',
    )
    lines.append('')
    lines.append(title_prompt)

    title = data.get('title')
    if title:
        lines.append(f'• {html.escape(title)}')

    if next_step == 'title':
        if error_message:
            lines.append('')
            lines.append(error_message)
        return '\n'.join(lines)

    description_prompt = texts.t(
        'ADMIN_POLLS_CREATION_DESCRIPTION_PROMPT',
        'Введите описание опроса. HTML разрешён.\nОтправьте /skip, чтобы пропустить.',
    )

    lines.append('')
    lines.append(description_prompt)

    if 'description' in data:
        description = data.get('description')
        if description:
            lines.append(f'• {description}')
        else:
            lines.append(
                '• '
                + texts.t(
                    'ADMIN_POLLS_CREATION_DESCRIPTION_SKIPPED',
                    'Описание пропущено.',
                )
            )
    else:
        lines.append('')
        lines.append(get_html_help_text())

    if next_step == 'description':
        if error_message:
            lines.append('')
            lines.append(error_message)
        return '\n'.join(lines)

    reward_prompt = texts.t(
        'ADMIN_POLLS_CREATION_REWARD_PROMPT',
        'Введите сумму награды в рублях. Отправьте 0 чтобы отключить награду.',
    )

    lines.append('')
    lines.append(reward_prompt)

    if 'reward_enabled' in data:
        if data.get('reward_enabled'):
            amount = data.get('reward_amount_kopeks', 0)
            lines.append(f'• {_safe_format_price(amount)}')
        else:
            lines.append(texts.t('ADMIN_POLLS_REWARD_DISABLED', 'Награда отключена'))

    if next_step == 'reward':
        if error_message:
            lines.append('')
            lines.append(error_message)
        return '\n'.join(lines)

    question_prompt = texts.t(
        'ADMIN_POLLS_CREATION_QUESTION_PROMPT',
        (
            'Введите вопрос и варианты ответов.\n'
            'Каждая строка — отдельный вариант.\n'
            'Первая строка — текст вопроса.\n'
            'Отправьте /done, когда вопросы будут добавлены.'
        ),
    )

    lines.append('')
    lines.append(question_prompt)

    questions = data.get('questions', [])
    if questions:
        lines.append('')
        for idx, question in enumerate(questions, start=1):
            lines.append(f'{idx}. {html.escape(question["text"])}')
            for option in question['options']:
                lines.append(f'   • {html.escape(option)}')

    if status_message:
        lines.append('')
        lines.append(status_message)

    if error_message:
        lines.append('')
        lines.append(error_message)

    return '\n'.join(lines)


class PollCreationStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_reward = State()
    waiting_for_questions = State()


def _build_polls_keyboard(polls: list[Poll], language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard: list[list[types.InlineKeyboardButton]] = []

    for poll in polls[:10]:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'🗳️ {poll.title[:40]}',
                    callback_data=f'poll_view:{poll.id}',
                )
            ]
        )

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_POLLS_CREATE', '➕ Создать опрос'),
                callback_data='poll_create',
            )
        ]
    )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.BACK,
                callback_data='admin_submenu_communications',
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _format_reward_text(poll: Poll, language: str) -> str:
    texts = get_texts(language)
    if poll.reward_enabled and poll.reward_amount_kopeks > 0:
        return texts.t(
            'ADMIN_POLLS_REWARD_ENABLED',
            'Награда: {amount}',
        ).format(amount=settings.format_price(poll.reward_amount_kopeks))
    return texts.t('ADMIN_POLLS_REWARD_DISABLED', 'Награда отключена')


def _build_poll_details_keyboard(poll_id: int, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_POLLS_SEND', '📤 Отправить'),
                    callback_data=f'poll_send:{poll_id}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_POLLS_STATS', '📊 Статистика'),
                    callback_data=f'poll_stats:{poll_id}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_POLLS_DELETE', '🗑️ Удалить'),
                    callback_data=f'poll_delete:{poll_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_POLLS_BACK', '⬅️ К списку'), callback_data='admin_polls')],
        ]
    )


def _build_target_keyboard(poll_id: int, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_BROADCAST_TARGET_ALL', '👥 Всем'),
                    callback_data=f'poll_target:{poll_id}:all',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_BROADCAST_TARGET_ACTIVE', '📱 С подпиской'),
                    callback_data=f'poll_target:{poll_id}:active',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_BROADCAST_TARGET_TRIAL', '🎁 Триал'),
                    callback_data=f'poll_target:{poll_id}:trial',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_BROADCAST_TARGET_NO_SUB', '❌ Без подписки'),
                    callback_data=f'poll_target:{poll_id}:no',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_BROADCAST_TARGET_EXPIRING', '⏰ Истекающие'),
                    callback_data=f'poll_target:{poll_id}:expiring',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_BROADCAST_TARGET_EXPIRED', '🔚 Истекшие'),
                    callback_data=f'poll_target:{poll_id}:expired',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_BROADCAST_TARGET_ACTIVE_ZERO', '🧊 Активна 0 ГБ'),
                    callback_data=f'poll_target:{poll_id}:active_zero',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_BROADCAST_TARGET_TRIAL_ZERO', '🥶 Триал 0 ГБ'),
                    callback_data=f'poll_target:{poll_id}:trial_zero',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_POLLS_CUSTOM_TARGET', '⚙️ По критериям'),
                    callback_data=f'poll_custom_menu:{poll_id}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=f'poll_view:{poll_id}',
                )
            ],
        ]
    )


def _build_custom_target_keyboard(poll_id: int, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_CRITERIA_TODAY', '📅 Сегодня'),
                    callback_data=f'poll_custom_target:{poll_id}:today',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_CRITERIA_WEEK', '📅 За неделю'),
                    callback_data=f'poll_custom_target:{poll_id}:week',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_CRITERIA_MONTH', '📅 За месяц'),
                    callback_data=f'poll_custom_target:{poll_id}:month',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_CRITERIA_ACTIVE_TODAY', '⚡ Активные сегодня'),
                    callback_data=f'poll_custom_target:{poll_id}:active_today',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_CRITERIA_INACTIVE_WEEK', '💤 Неактивные 7+ дней'),
                    callback_data=f'poll_custom_target:{poll_id}:inactive_week',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_CRITERIA_INACTIVE_MONTH', '💤 Неактивные 30+ дней'),
                    callback_data=f'poll_custom_target:{poll_id}:inactive_month',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_CRITERIA_REFERRALS', '🤝 Через рефералов'),
                    callback_data=f'poll_custom_target:{poll_id}:referrals',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_CRITERIA_DIRECT', '🎯 Прямая регистрация'),
                    callback_data=f'poll_custom_target:{poll_id}:direct',
                ),
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=f'poll_send:{poll_id}')],
        ]
    )


def _build_send_confirmation_keyboard(poll_id: int, target: str, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_POLLS_SEND_CONFIRM_BUTTON', '✅ Отправить'),
                    callback_data=f'poll_send_confirm:{poll_id}:{target}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=f'poll_send:{poll_id}',
                )
            ],
        ]
    )


@admin_required
@error_handler
async def show_polls_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    polls = await list_polls(db)
    texts = get_texts(db_user.language)

    lines = [texts.t('ADMIN_POLLS_LIST_TITLE', '🗳️ <b>Опросы</b>'), '']
    if not polls:
        lines.append(texts.t('ADMIN_POLLS_LIST_EMPTY', 'Опросов пока нет.'))
    else:
        for poll in polls[:10]:
            reward = _format_reward_text(poll, db_user.language)
            lines.append(
                f'• <b>{html.escape(poll.title)}</b> — '
                f'{texts.t("ADMIN_POLLS_QUESTIONS_COUNT", "Вопросов: {count}").format(count=len(poll.questions))}\n'
                f'  {reward}'
            )

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=_build_polls_keyboard(polls, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def start_poll_creation(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    await state.clear()
    await state.set_state(PollCreationStates.waiting_for_title)
    await _safe_delete_message(callback.message)
    await state.update_data(questions=[])

    form_text = _render_creation_progress(texts, await state.get_data(), 'title')
    await _send_creation_message(
        callback.message,
        state,
        form_text,
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_poll_title(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    state_data = await state.get_data()

    if message.text == '/cancel':
        await _safe_delete_message(message)
        cancel_text = texts.t(
            'ADMIN_POLLS_CREATION_CANCELLED',
            '❌ Создание опроса отменено.',
        )
        keyboard = get_admin_communications_submenu_keyboard(db_user.language)
        updated = await _edit_creation_message(
            message.bot,
            state_data,
            cancel_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
        if not updated:
            await _send_creation_message(
                message,
                state,
                cancel_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        await state.clear()
        return

    title = (message.text or '').strip()
    await _safe_delete_message(message)

    if not title:
        error_text = texts.t(
            'ADMIN_POLLS_CREATION_TITLE_EMPTY',
            '❌ Заголовок не может быть пустым. Попробуйте снова.',
        )
        form_text = _render_creation_progress(texts, state_data, 'title', error_message=error_text)
        updated = await _edit_creation_message(message.bot, state_data, form_text)
        if not updated:
            await _send_creation_message(
                message,
                state,
                form_text,
                parse_mode='HTML',
            )
        return

    await state.update_data(title=title)
    await state.set_state(PollCreationStates.waiting_for_description)

    new_data = await state.get_data()
    form_text = _render_creation_progress(texts, new_data, 'description')
    updated = await _edit_creation_message(message.bot, new_data, form_text)
    if not updated:
        await _send_creation_message(
            message,
            state,
            form_text,
            parse_mode='HTML',
        )


@admin_required
@error_handler
async def process_poll_description(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    state_data = await state.get_data()

    if message.text == '/cancel':
        await _safe_delete_message(message)
        cancel_text = texts.t(
            'ADMIN_POLLS_CREATION_CANCELLED',
            '❌ Создание опроса отменено.',
        )
        keyboard = get_admin_communications_submenu_keyboard(db_user.language)
        updated = await _edit_creation_message(
            message.bot,
            state_data,
            cancel_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
        if not updated:
            await _send_creation_message(
                message,
                state,
                cancel_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        await state.clear()
        return

    description: str | None
    message_text = message.text or ''
    await _safe_delete_message(message)

    if message_text == '/skip':
        description = None
    else:
        description = message_text.strip()
        is_valid, error_message = validate_html_tags(description)
        if not is_valid:
            error_text = texts.t(
                'ADMIN_POLLS_CREATION_INVALID_HTML',
                '❌ Ошибка в HTML: {error}',
            ).format(error=error_message)
            form_text = _render_creation_progress(texts, state_data, 'description', error_message=error_text)
            updated = await _edit_creation_message(message.bot, state_data, form_text)
            if not updated:
                await _send_creation_message(
                    message,
                    state,
                    form_text,
                    parse_mode='HTML',
                )
            return

    await state.update_data(description=description)
    await state.set_state(PollCreationStates.waiting_for_reward)

    new_data = await state.get_data()
    form_text = _render_creation_progress(texts, new_data, 'reward')
    updated = await _edit_creation_message(message.bot, new_data, form_text)
    if not updated:
        await _send_creation_message(
            message,
            state,
            form_text,
            parse_mode='HTML',
        )


def _parse_reward_amount(message_text: str) -> int | None:
    normalized = message_text.replace(' ', '').replace(',', '.')
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None

    if value < 0:
        value = Decimal(0)

    kopeks = int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return max(0, kopeks)


@admin_required
@error_handler
async def process_poll_reward(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    state_data = await state.get_data()

    if message.text == '/cancel':
        await _safe_delete_message(message)
        cancel_text = texts.t(
            'ADMIN_POLLS_CREATION_CANCELLED',
            '❌ Создание опроса отменено.',
        )
        keyboard = get_admin_communications_submenu_keyboard(db_user.language)
        updated = await _edit_creation_message(
            message.bot,
            state_data,
            cancel_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
        if not updated:
            await _send_creation_message(
                message,
                state,
                cancel_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        await state.clear()
        return

    reward_kopeks = _parse_reward_amount(message.text or '')
    await _safe_delete_message(message)
    if reward_kopeks is None:
        error_text = texts.t(
            'ADMIN_POLLS_CREATION_REWARD_INVALID',
            '❌ Некорректная сумма. Попробуйте ещё раз.',
        )
        form_text = _render_creation_progress(texts, state_data, 'reward', error_message=error_text)
        updated = await _edit_creation_message(message.bot, state_data, form_text)
        if not updated:
            await _send_creation_message(
                message,
                state,
                form_text,
                parse_mode='HTML',
            )
        return

    reward_enabled = reward_kopeks > 0
    await state.update_data(
        reward_enabled=reward_enabled,
        reward_amount_kopeks=reward_kopeks,
    )
    await state.set_state(PollCreationStates.waiting_for_questions)

    new_data = await state.get_data()
    form_text = _render_creation_progress(texts, new_data, 'questions')
    updated = await _edit_creation_message(message.bot, new_data, form_text)
    if not updated:
        await _send_creation_message(
            message,
            state,
            form_text,
            parse_mode='HTML',
        )


@admin_required
@error_handler
async def process_poll_question(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    state_data = await state.get_data()

    if message.text == '/cancel':
        await _safe_delete_message(message)
        cancel_text = texts.t(
            'ADMIN_POLLS_CREATION_CANCELLED',
            '❌ Создание опроса отменено.',
        )
        keyboard = get_admin_communications_submenu_keyboard(db_user.language)
        updated = await _edit_creation_message(
            message.bot,
            state_data,
            cancel_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
        if not updated:
            await _send_creation_message(
                message,
                state,
                cancel_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        await state.clear()
        return

    if message.text == '/done':
        await _safe_delete_message(message)
        data = await state.get_data()
        questions = data.get('questions', [])
        if not questions:
            error_text = texts.t(
                'ADMIN_POLLS_CREATION_NEEDS_QUESTION',
                '❌ Добавьте хотя бы один вопрос.',
            )
            form_text = _render_creation_progress(texts, data, 'questions', error_message=error_text)
            updated = await _edit_creation_message(message.bot, data, form_text)
            if not updated:
                await _send_creation_message(
                    message,
                    state,
                    form_text,
                    parse_mode='HTML',
                )
            return

        title = data.get('title')
        description = data.get('description')
        reward_enabled = data.get('reward_enabled', False)
        reward_amount = data.get('reward_amount_kopeks', 0)

        form_data = data.copy()

        poll = await create_poll(
            db,
            title=title,
            description=description,
            reward_enabled=reward_enabled,
            reward_amount_kopeks=reward_amount,
            created_by=db_user.id,
            questions=questions,
        )

        reward_text = _format_reward_text(poll, db_user.language)
        result_text = texts.t(
            'ADMIN_POLLS_CREATION_FINISHED',
            ('✅ Опрос «{title}» создан!\nВопросов: {count}\n{reward}'),
        ).format(
            title=html.escape(poll.title),
            count=len(poll.questions),
            reward=reward_text,
        )

        keyboard = _build_polls_keyboard(await list_polls(db), db_user.language)
        updated = await _edit_creation_message(
            message.bot,
            form_data,
            result_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
        if not updated:
            await _send_creation_message(
                message,
                state,
                result_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        await state.clear()
        return

    lines = [line.strip() for line in (message.text or '').splitlines() if line.strip()]
    await _safe_delete_message(message)
    if len(lines) < 3:
        error_text = texts.t(
            'ADMIN_POLLS_CREATION_MIN_OPTIONS',
            '❌ Нужен вопрос и минимум два варианта ответа.',
        )
        form_text = _render_creation_progress(texts, state_data, 'questions', error_message=error_text)
        updated = await _edit_creation_message(message.bot, state_data, form_text)
        if not updated:
            await _send_creation_message(
                message,
                state,
                form_text,
                parse_mode='HTML',
            )
        return

    question_text = lines[0]
    options = lines[1:]
    data = await state.get_data()
    questions = data.get('questions', [])
    questions.append({'text': question_text, 'options': options})
    await state.update_data(questions=questions)

    new_data = await state.get_data()
    status_message = texts.t(
        'ADMIN_POLLS_CREATION_ADDED_QUESTION',
        'Вопрос добавлен: «{question}». Добавьте следующий вопрос или отправьте /done.',
    ).format(question=html.escape(question_text))

    form_text = _render_creation_progress(
        texts,
        new_data,
        'questions',
        status_message=status_message,
    )
    updated = await _edit_creation_message(message.bot, new_data, form_text)
    if not updated:
        await _send_creation_message(
            message,
            state,
            form_text,
            parse_mode='HTML',
        )


async def _render_poll_details(poll: Poll, language: str) -> str:
    texts = get_texts(language)
    lines = [f'🗳️ <b>{html.escape(poll.title)}</b>']
    if poll.description:
        lines.append(html.escape(poll.description))

    lines.append(_format_reward_text(poll, language))
    lines.append(texts.t('ADMIN_POLLS_QUESTIONS_COUNT', 'Вопросов: {count}').format(count=len(poll.questions)))

    if poll.questions:
        lines.append('')
        lines.append(texts.t('ADMIN_POLLS_QUESTION_LIST_HEADER', '<b>Вопросы:</b>'))
        for idx, question in enumerate(sorted(poll.questions, key=lambda q: q.order), start=1):
            lines.append(f'{idx}. {html.escape(question.text)}')
            for option in sorted(question.options, key=lambda o: o.order):
                lines.append(
                    texts.t('ADMIN_POLLS_OPTION_BULLET', '   • {option}').format(option=html.escape(option.text))
                )

    return '\n'.join(lines)


@admin_required
@error_handler
async def show_poll_details(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    poll_id = int(callback.data.split(':')[1])
    poll = await get_poll_by_id(db, poll_id)
    if not poll:
        await callback.answer('❌ Опрос не найден', show_alert=True)
        return

    text = await _render_poll_details(poll, db_user.language)
    await callback.message.edit_text(
        text,
        reply_markup=_build_poll_details_keyboard(poll.id, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def start_poll_send(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    poll_id = int(callback.data.split(':')[1])
    poll = await get_poll_by_id(db, poll_id)
    if not poll:
        await callback.answer('❌ Опрос не найден', show_alert=True)
        return

    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_POLLS_SEND_CHOOSE_TARGET', '🎯 Выберите аудиторию для отправки опроса:'),
        reply_markup=_build_target_keyboard(poll.id, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_custom_target_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    poll_id = int(callback.data.split(':')[1])
    await callback.message.edit_text(
        get_texts(db_user.language).t(
            'ADMIN_POLLS_CUSTOM_PROMPT',
            'Выберите дополнительный критерий аудитории:',
        ),
        reply_markup=_build_custom_target_keyboard(poll_id, db_user.language),
    )
    await callback.answer()


async def _show_send_confirmation(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    poll_id: int,
    target: str,
    user_count: int,
):
    poll = await get_poll_by_id(db, poll_id)
    if not poll:
        await callback.answer('❌ Опрос не найден', show_alert=True)
        return

    audience_name = get_target_display_name(target)
    texts = get_texts(db_user.language)
    confirmation_text = texts.t(
        'ADMIN_POLLS_SEND_CONFIRM',
        '📤 Отправить опрос «{title}» аудитории «{audience}»? Пользователей: {count}',
    ).format(title=poll.title, audience=audience_name, count=user_count)

    await callback.message.edit_text(
        confirmation_text,
        reply_markup=_build_send_confirmation_keyboard(poll_id, target, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def select_poll_target(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    _, payload = callback.data.split(':', 1)
    poll_id_str, target = payload.split(':', 1)
    poll_id = int(poll_id_str)

    user_count = await get_target_users_count(db, target)
    await _show_send_confirmation(callback, db_user, db, poll_id, target, user_count)


@admin_required
@error_handler
async def select_custom_poll_target(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    _, payload = callback.data.split(':', 1)
    poll_id_str, criteria = payload.split(':', 1)
    poll_id = int(poll_id_str)

    user_count = await get_custom_users_count(db, criteria)
    await _show_send_confirmation(callback, db_user, db, poll_id, f'custom_{criteria}', user_count)


@admin_required
@error_handler
async def confirm_poll_send(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    _, payload = callback.data.split(':', 1)
    poll_id_str, target = payload.split(':', 1)
    poll_id = int(poll_id_str)

    poll = await get_poll_by_id(db, poll_id)
    if not poll:
        await callback.answer('❌ Опрос не найден', show_alert=True)
        return

    poll_id_value = poll.id

    if target.startswith('custom_'):
        users = await get_custom_users(db, target.replace('custom_', ''))
    else:
        users = await get_target_users(db, target)

    user_language = db_user.language
    texts = get_texts(user_language)
    await callback.message.edit_text(
        texts.t('ADMIN_POLLS_SENDING', '📤 Запускаю отправку опроса...'),
        parse_mode='HTML',
    )

    result = await send_poll_to_users(callback.bot, db, poll, users)

    result_text = texts.t(
        'ADMIN_POLLS_SEND_RESULT',
        '📤 Отправка завершена\nУспешно: {sent}\nОшибок: {failed}\nПропущено: {skipped}\nВсего: {total}',
    ).format(**result)

    await callback.message.edit_text(
        result_text,
        reply_markup=_build_poll_details_keyboard(poll_id_value, user_language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_poll_stats(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    poll_id = int(callback.data.split(':')[1])
    poll = await get_poll_by_id(db, poll_id)
    if not poll:
        await callback.answer('❌ Опрос не найден', show_alert=True)
        return

    stats = await get_poll_statistics(db, poll_id)
    texts = get_texts(db_user.language)

    reward_sum = settings.format_price(stats['reward_sum_kopeks'])
    lines = [texts.t('ADMIN_POLLS_STATS_HEADER', '📊 <b>Статистика опроса</b>'), '']
    lines.append(
        texts.t(
            'ADMIN_POLLS_STATS_OVERVIEW',
            'Всего приглашено: {total}\nЗавершили: {completed}\nВыплачено наград: {reward}',
        ).format(
            total=stats['total_responses'],
            completed=stats['completed_responses'],
            reward=reward_sum,
        )
    )

    for question in stats['questions']:
        lines.append('')
        lines.append(f'<b>{html.escape(question["text"])}</b>')
        for option in question['options']:
            lines.append(
                texts.t(
                    'ADMIN_POLLS_STATS_OPTION_LINE',
                    '• {option}: {count}',
                ).format(option=html.escape(option['text']), count=option['count'])
            )

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=_build_poll_details_keyboard(poll.id, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_poll_delete(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    poll_id = int(callback.data.split(':')[1])
    poll = await get_poll_by_id(db, poll_id)
    if not poll:
        await callback.answer('❌ Опрос не найден', show_alert=True)
        return

    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_POLLS_CONFIRM_DELETE',
            'Вы уверены, что хотите удалить опрос «{title}»?',
        ).format(title=poll.title),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_POLLS_DELETE', '🗑️ Удалить'),
                        callback_data=f'poll_delete_confirm:{poll_id}',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.BACK,
                        callback_data=f'poll_view:{poll_id}',
                    )
                ],
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_poll_handler(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    poll_id = int(callback.data.split(':')[1])
    success = await delete_poll(db, poll_id)
    texts = get_texts(db_user.language)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_POLLS_DELETED', '🗑️ Опрос удалён.'),
            reply_markup=_build_polls_keyboard(await list_polls(db), db_user.language),
        )
    else:
        await callback.answer('❌ Опрос не найден', show_alert=True)
        return

    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_polls_panel, F.data == 'admin_polls')
    dp.callback_query.register(start_poll_creation, F.data == 'poll_create')
    dp.callback_query.register(show_poll_details, F.data.startswith('poll_view:'))
    dp.callback_query.register(start_poll_send, F.data.startswith('poll_send:'))
    dp.callback_query.register(show_custom_target_menu, F.data.startswith('poll_custom_menu:'))
    dp.callback_query.register(select_poll_target, F.data.startswith('poll_target:'))
    dp.callback_query.register(select_custom_poll_target, F.data.startswith('poll_custom_target:'))
    dp.callback_query.register(confirm_poll_send, F.data.startswith('poll_send_confirm:'))
    dp.callback_query.register(show_poll_stats, F.data.startswith('poll_stats:'))
    dp.callback_query.register(confirm_poll_delete, F.data.startswith('poll_delete:'))
    dp.callback_query.register(delete_poll_handler, F.data.startswith('poll_delete_confirm:'))

    dp.message.register(process_poll_title, PollCreationStates.waiting_for_title)
    dp.message.register(process_poll_description, PollCreationStates.waiting_for_description)
    dp.message.register(process_poll_reward, PollCreationStates.waiting_for_reward)
    dp.message.register(process_poll_question, PollCreationStates.waiting_for_questions)
