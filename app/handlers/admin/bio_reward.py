"""Admin in-bot panel for the bio-reward feature.

Buttons-driven editor for BioRewardConfig (the singleton DB row that controls
enable/discount/grace/cooldown/free-sub knobs and accepted bio strings),
plus participant browser with force-revoke / restore / bypass actions.

Callback prefix: ``br_admin_``.
"""

from __future__ import annotations

import html as html_module
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud import bio_reward as bio_crud
from app.database.models import BioRewardParticipant, BioRewardStatus, User
from app.services import bio_reward_analytics
from app.services.bio_reward_service import bio_reward_service
from app.states import BioRewardAdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


PAGE_SIZE = 8


# ---------------- Keyboards ----------------


def _config_keyboard(cfg) -> InlineKeyboardMarkup:
    on = '🟢' if cfg.enabled else '🔴'
    rows = [
        [InlineKeyboardButton(text=f'{on} Включено: {"Да" if cfg.enabled else "Нет"}', callback_data='br_admin_toggle_enabled')],
        [
            InlineKeyboardButton(text=f'💸 Скидка: {cfg.discount_percent}%', callback_data='br_admin_edit_discount'),
            InlineKeyboardButton(text=f'⏳ Grace: {cfg.grace_period_hours}ч', callback_data='br_admin_edit_grace'),
        ],
        [
            InlineKeyboardButton(text=f'🧊 Cooldown: {cfg.cooldown_hours}ч', callback_data='br_admin_edit_cooldown'),
            InlineKeyboardButton(text=f'🔁 Интервал: {cfg.check_interval_minutes}м', callback_data='br_admin_edit_interval'),
        ],
        [
            InlineKeyboardButton(text=f'📅 Окно: {cfg.free_sub_window_days}д', callback_data='br_admin_edit_window'),
            InlineKeyboardButton(text=f'📊 Трафик/день: {cfg.free_sub_traffic_gb_per_day}ГБ', callback_data='br_admin_edit_traffic'),
        ],
        [
            InlineKeyboardButton(text=f'📱 Устройств: {cfg.free_sub_device_limit}', callback_data='br_admin_edit_devices'),
            InlineKeyboardButton(text='🛰 Squad UUID', callback_data='br_admin_edit_squad'),
        ],
        [InlineKeyboardButton(text='🔗 Тексты для описания', callback_data='br_admin_strings')],
        [
            InlineKeyboardButton(
                text=f'{"🟢" if cfg.match_personal_referral_link else "🔴"} Реф-ссылка как валидная',
                callback_data='br_admin_toggle_personal_link',
            ),
        ],
        [InlineKeyboardButton(text='🔔 Уведомления', callback_data='br_admin_notify_panel')],
        [InlineKeyboardButton(text='📝 Инструкция', callback_data='br_admin_edit_instruction')],
        [
            InlineKeyboardButton(text='👥 Участники', callback_data='br_admin_participants:0'),
            InlineKeyboardButton(text='📊 Статистика', callback_data='br_admin_stats'),
        ],
        [InlineKeyboardButton(text='📈 Аналитика', callback_data='br_admin_analytics')],
        [InlineKeyboardButton(text='⬅ В админ-меню', callback_data='admin_panel')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _strings_keyboard(strings: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, s in enumerate(strings):
        preview = (s[:40] + '…') if len(s) > 40 else s
        rows.append([InlineKeyboardButton(text=f'❌ {preview}', callback_data=f'br_admin_string_del:{i}')])
    rows.append([InlineKeyboardButton(text='➕ Добавить строку', callback_data='br_admin_string_add')])
    rows.append([InlineKeyboardButton(text='⬅ Назад', callback_data='br_admin_open')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _notify_keyboard(cfg) -> InlineKeyboardMarkup:
    flag = lambda v: '🟢' if v else '🔴'
    rows = [
        [InlineKeyboardButton(text=f'{flag(cfg.notify_on_opt_in)} Opt-in', callback_data='br_admin_notify_toggle:opt_in')],
        [InlineKeyboardButton(text=f'{flag(cfg.notify_on_activate)} Активация', callback_data='br_admin_notify_toggle:activate')],
        [InlineKeyboardButton(text=f'{flag(cfg.notify_on_grace)} Grace', callback_data='br_admin_notify_toggle:grace')],
        [InlineKeyboardButton(text=f'{flag(cfg.notify_on_revoke)} Revoke', callback_data='br_admin_notify_toggle:revoke')],
        [InlineKeyboardButton(text='⬅ Назад', callback_data='br_admin_open')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _participants_keyboard(rows: list[BioRewardParticipant], page: int, total: int) -> InlineKeyboardMarkup:
    kb_rows: list[list[InlineKeyboardButton]] = []
    for p in rows:
        u = p.user
        ident = (u.username if u else None) or (u.full_name if u else None) or str(p.user_id)
        label = f'{p.status[:4]} | {ident}'
        kb_rows.append([InlineKeyboardButton(text=label, callback_data=f'br_admin_p_view:{p.id}')])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text='⬅', callback_data=f'br_admin_participants:{page - 1}'))
    nav.append(InlineKeyboardButton(text=f'{page + 1}', callback_data='br_admin_noop'))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text='➡', callback_data=f'br_admin_participants:{page + 1}'))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(text='⬅ Конфиг', callback_data='br_admin_open')])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


def _participant_actions_keyboard(p: BioRewardParticipant) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='🔴 Force revoke', callback_data=f'br_admin_p_revoke:{p.id}')],
        [InlineKeyboardButton(text='♻ Restore (PENDING)', callback_data=f'br_admin_p_restore:{p.id}')],
        [
            InlineKeyboardButton(
                text=('🟢 Bypass: Вкл' if p.bypass_check else '🔴 Bypass: Выкл'),
                callback_data=f'br_admin_p_bypass:{p.id}',
            )
        ],
        [InlineKeyboardButton(text='⬅ Список', callback_data='br_admin_participants:0')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------- Render helpers ----------------


def _format_config(cfg) -> str:
    accepted = (
        '\n'.join(f'  • <code>{html_module.escape(s)}</code>' for s in (cfg.accepted_bio_strings or []))
        or '  <i>пусто</i>'
    )
    return (
        '🎁 <b>Акция «Бесплатно за описание профиля» — настройки</b>\n\n'
        f'Включено: <b>{"Да" if cfg.enabled else "Нет"}</b>\n'
        f'Скидка: <b>{cfg.discount_percent}%</b>\n'
        f'Grace-период (на возврат текста): <b>{cfg.grace_period_hours} ч.</b>\n'
        f'Cooldown (блокировка после отключения): <b>{cfg.cooldown_hours} ч.</b>\n'
        f'Интервал проверки описания: <b>{cfg.check_interval_minutes} мин.</b>\n'
        f'Окно free-sub: <b>{cfg.free_sub_window_days} дн.</b>\n'
        f'Трафик free-sub в день: <b>{cfg.free_sub_traffic_gb_per_day} ГБ</b>\n'
        f'Устройств в free-sub: <b>{cfg.free_sub_device_limit}</b>\n'
        f'Squad UUID: <code>{html_module.escape(cfg.free_sub_squad_uuid or "не задан")}</code>\n'
        f'Реф-ссылка пользователя считается валидной: <b>{"Да" if cfg.match_personal_referral_link else "Нет"}</b>\n\n'
        f'<b>Принимаемые тексты для описания ({len(cfg.accepted_bio_strings or [])}):</b>\n{accepted}\n\n'
        f'<i>Инструкция для пользователей:</i> {html_module.escape((cfg.instruction_text or "не задана")[:200])}'
    )


# ---------------- Top-level entry ----------------


@admin_required
@error_handler
async def open_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    cfg = await bio_crud.get_config(db)
    await callback.message.edit_text(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
    await callback.answer()


# ---------------- Toggles ----------------


@admin_required
@error_handler
async def toggle_enabled(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    cfg = await bio_crud.get_config(db)
    cfg = await bio_crud.update_config(db, enabled=not cfg.enabled)
    await callback.message.edit_text(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
    await callback.answer('✅ Переключено')


@admin_required
@error_handler
async def toggle_personal_link(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    cfg = await bio_crud.get_config(db)
    cfg = await bio_crud.update_config(db, match_personal_referral_link=not cfg.match_personal_referral_link)
    await callback.message.edit_text(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
    await callback.answer('✅ Переключено')


@admin_required
@error_handler
async def open_notify(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    cfg = await bio_crud.get_config(db)
    await callback.message.edit_text(
        '🔔 <b>Уведомления для участников акции</b>\n\n'
        'Управляйте, какие сообщения бот будет отправлять пользователю '
        'на разных этапах участия в акции.',
        parse_mode='HTML',
        reply_markup=_notify_keyboard(cfg),
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_notify(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    kind = callback.data.split(':')[1]
    field_map = {
        'opt_in': 'notify_on_opt_in',
        'activate': 'notify_on_activate',
        'grace': 'notify_on_grace',
        'revoke': 'notify_on_revoke',
    }
    field = field_map.get(kind)
    if not field:
        await callback.answer('Unknown', show_alert=True)
        return
    cfg = await bio_crud.get_config(db)
    cfg = await bio_crud.update_config(db, **{field: not getattr(cfg, field)})
    await callback.message.edit_reply_markup(reply_markup=_notify_keyboard(cfg))
    await callback.answer('✅')


# ---------------- Numeric edits via FSM ----------------


_NUMERIC_PROMPTS: dict[str, tuple[str, str, BioRewardAdminStates, int, int]] = {
    'discount': ('discount_percent', '💸 Введите % скидки (0–100):', BioRewardAdminStates.waiting_for_discount_percent, 0, 100),
    'grace': ('grace_period_hours', '⏳ Введите grace-период в часах (0–720):', BioRewardAdminStates.waiting_for_grace_hours, 0, 720),
    'cooldown': ('cooldown_hours', '🧊 Введите cooldown в часах (0–8760):', BioRewardAdminStates.waiting_for_cooldown_hours, 0, 8760),
    'interval': ('check_interval_minutes', '🔁 Введите интервал проверки в минутах (1–1440):', BioRewardAdminStates.waiting_for_check_interval, 1, 1440),
    'window': ('free_sub_window_days', '📅 Введите окно free-sub в днях (1–30):', BioRewardAdminStates.waiting_for_window_days, 1, 30),
    'traffic': ('free_sub_traffic_gb_per_day', '📊 Введите дневной лимит трафика в ГБ (0–10000):', BioRewardAdminStates.waiting_for_traffic_gb, 0, 10000),
    'devices': ('free_sub_device_limit', '📱 Введите лимит устройств (1–100):', BioRewardAdminStates.waiting_for_device_limit, 1, 100),
}


async def _start_numeric_edit(callback: types.CallbackQuery, state: FSMContext, key: str):
    if key not in _NUMERIC_PROMPTS:
        await callback.answer('Unknown', show_alert=True)
        return
    field, prompt, st, lo, hi = _NUMERIC_PROMPTS[key]
    await state.set_state(st)
    await state.update_data(field=field, lo=lo, hi=hi)
    await callback.message.answer(prompt + '\n\nОтправьте число сообщением или /cancel.')
    await callback.answer()


async def _handle_numeric_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    raw = (message.text or '').strip()
    if raw.lower() in ('/cancel', 'cancel', 'отмена'):
        await state.clear()
        await message.answer('Отменено.')
        return
    data = await state.get_data()
    field = data.get('field')
    lo = data.get('lo', 0)
    hi = data.get('hi', 100)
    try:
        value = int(raw)
    except ValueError:
        await message.answer('❌ Не число. Повторите или /cancel.')
        return
    if value < lo or value > hi:
        await message.answer(f'❌ Вне диапазона [{lo}; {hi}]. Повторите или /cancel.')
        return
    await bio_crud.update_config(db, **{field: value})
    await state.clear()
    cfg = await bio_crud.get_config(db)
    await message.answer(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))


@admin_required
@error_handler
async def edit_discount(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await _start_numeric_edit(callback, state, 'discount')


@admin_required
@error_handler
async def edit_grace(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await _start_numeric_edit(callback, state, 'grace')


@admin_required
@error_handler
async def edit_cooldown(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await _start_numeric_edit(callback, state, 'cooldown')


@admin_required
@error_handler
async def edit_interval(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await _start_numeric_edit(callback, state, 'interval')


@admin_required
@error_handler
async def edit_window(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await _start_numeric_edit(callback, state, 'window')


@admin_required
@error_handler
async def edit_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await _start_numeric_edit(callback, state, 'traffic')


@admin_required
@error_handler
async def edit_devices(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await _start_numeric_edit(callback, state, 'devices')


# ---------------- Squad UUID + Instruction (free-text) ----------------


@admin_required
@error_handler
async def edit_squad(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(BioRewardAdminStates.waiting_for_squad_uuid)
    await callback.message.answer('🛰 Отправьте UUID сквада для бесплатной подписки. /cancel — отмена. /clear — очистить.')
    await callback.answer()


async def _handle_squad_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    raw = (message.text or '').strip()
    if raw.lower() in ('/cancel', 'cancel', 'отмена'):
        await state.clear()
        await message.answer('Отменено.')
        return
    if raw.lower() in ('/clear', 'clear', 'очистить'):
        await bio_crud.update_config(db, free_sub_squad_uuid=None)
        await state.clear()
        cfg = await bio_crud.get_config(db)
        await message.answer(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
        return
    await bio_crud.update_config(db, free_sub_squad_uuid=raw)
    await state.clear()
    cfg = await bio_crud.get_config(db)
    await message.answer(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))


@admin_required
@error_handler
async def edit_instruction(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(BioRewardAdminStates.waiting_for_instruction_text)
    await callback.message.answer('📝 Отправьте текст инструкции для пользователей. /cancel — отмена. /clear — очистить.')
    await callback.answer()


async def _handle_instruction_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    raw = (message.text or '').strip()
    if raw.lower() in ('/cancel', 'cancel', 'отмена'):
        await state.clear()
        await message.answer('Отменено.')
        return
    if raw.lower() in ('/clear', 'clear', 'очистить'):
        await bio_crud.update_config(db, instruction_text=None)
        await state.clear()
        cfg = await bio_crud.get_config(db)
        await message.answer(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))
        return
    await bio_crud.update_config(db, instruction_text=raw)
    await state.clear()
    cfg = await bio_crud.get_config(db)
    await message.answer(_format_config(cfg), parse_mode='HTML', reply_markup=_config_keyboard(cfg))


# ---------------- Bio strings list management ----------------


@admin_required
@error_handler
async def show_strings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    cfg = await bio_crud.get_config(db)
    strings = list(cfg.accepted_bio_strings or [])
    head = (
        '🔗 <b>Принимаемые тексты для описания профиля</b>\n\n'
        f'Сейчас в списке: <b>{len(strings)}</b>\n'
        'Бот ищет любой из этих текстов в описании профиля пользователя '
        '(совпадение по подстроке, регистр не важен).\n\n'
        'Нажмите на строку, чтобы удалить.\n\n'
        '<b>Доступные плейсхолдеры (можно использовать в шаблонах):</b>\n'
        '• <code>{{bot_username}}</code> — имя бота без @\n'
        '• <code>{{bot_mention}}</code> — @имя_бота\n'
        '• <code>{{user_ref}}</code> — реферальный код пользователя\n'
        '• <code>{{user_ref_link}}</code> — полная реферальная ссылка пользователя\n\n'
        '<i>Пример шаблона:</i> <code>Я пользуюсь VPN от {{bot_mention}}</code>'
    )
    await callback.message.edit_text(head, parse_mode='HTML', reply_markup=_strings_keyboard(strings))
    await callback.answer()


@admin_required
@error_handler
async def add_string(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(BioRewardAdminStates.waiting_for_bio_string_add)
    await callback.message.answer(
        '➕ Отправьте текст-шаблон для добавления.\n\n'
        'Поддерживаемые плейсхолдеры:\n'
        '• <code>{{bot_username}}</code> — имя бота без @\n'
        '• <code>{{bot_mention}}</code> — @имя_бота\n'
        '• <code>{{user_ref}}</code> — реф-код юзера\n'
        '• <code>{{user_ref_link}}</code> — полная реф-ссылка\n\n'
        'Пример: <code>Я пользуюсь VPN от {{bot_mention}}</code>\n\n'
        '/cancel — отмена.',
        parse_mode='HTML',
    )
    await callback.answer()


async def _handle_string_add_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    raw = (message.text or '').strip()
    if raw.lower() in ('/cancel', 'cancel', 'отмена'):
        await state.clear()
        await message.answer('Отменено.')
        return
    if not raw:
        await message.answer('❌ Пустая строка. Повторите или /cancel.')
        return
    cfg = await bio_crud.get_config(db)
    items = list(cfg.accepted_bio_strings or [])
    if raw.lower() in {s.lower() for s in items}:
        await state.clear()
        await message.answer('ℹ️ Уже есть в списке.')
        return
    items.append(raw)
    await bio_crud.update_config(db, accepted_bio_strings=items)
    await state.clear()
    cfg = await bio_crud.get_config(db)
    await message.answer(
        '🔗 <b>Принимаемые тексты для описания профиля</b>',
        parse_mode='HTML',
        reply_markup=_strings_keyboard(list(cfg.accepted_bio_strings or [])),
    )


@admin_required
@error_handler
async def del_string(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        idx = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer('Bad index', show_alert=True)
        return
    cfg = await bio_crud.get_config(db)
    items = list(cfg.accepted_bio_strings or [])
    if 0 <= idx < len(items):
        removed = items.pop(idx)
        await bio_crud.update_config(db, accepted_bio_strings=items)
        await callback.answer(f'❌ Удалено: {removed[:30]}')
    else:
        await callback.answer('Out of range', show_alert=True)
    cfg = await bio_crud.get_config(db)
    await callback.message.edit_reply_markup(reply_markup=_strings_keyboard(list(cfg.accepted_bio_strings or [])))


# ---------------- Stats ----------------


@admin_required
@error_handler
async def show_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    counts_result = await db.execute(
        select(BioRewardParticipant.status, func.count(BioRewardParticipant.id)).group_by(
            BioRewardParticipant.status
        )
    )
    counts: dict[str, int] = {row[0]: int(row[1]) for row in counts_result.all()}

    cutoff = datetime.now(UTC) - timedelta(hours=24)
    recent_revokes_result = await db.execute(
        select(func.count(BioRewardParticipant.id)).where(
            BioRewardParticipant.revoked_at.isnot(None),
            BioRewardParticipant.revoked_at >= cutoff,
        )
    )
    recent_revokes = int(recent_revokes_result.scalar() or 0)

    text = (
        '📊 <b>Статистика акции «За описание профиля»</b>\n\n'
        f'Всего участников: <b>{sum(counts.values())}</b>\n\n'
        f'🟢 ACTIVE (всё работает): <b>{counts.get(BioRewardStatus.ACTIVE.value, 0)}</b>\n'
        f'⚠️ GRACE (текст убран, идёт таймер): <b>{counts.get(BioRewardStatus.GRACE.value, 0)}</b>\n'
        f'🧊 COOLDOWN (блокировка после отключения): <b>{counts.get(BioRewardStatus.COOLDOWN.value, 0)}</b>\n'
        f'❌ REVOKED (отключено): <b>{counts.get(BioRewardStatus.REVOKED.value, 0)}</b>\n'
        f'⏳ PENDING (ждём добавления текста): <b>{counts.get(BioRewardStatus.PENDING.value, 0)}</b>\n\n'
        f'Отключений за последние 24 часа: <b>{recent_revokes}</b>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='⬅ Конфиг', callback_data='br_admin_open')]]
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    await callback.answer()


# ---------------- Participants browser ----------------


@admin_required
@error_handler
async def show_participants(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        page = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        page = 0
    page = max(0, page)

    total_result = await db.execute(select(func.count(BioRewardParticipant.id)))
    total = int(total_result.scalar() or 0)

    rows_result = await db.execute(
        select(BioRewardParticipant)
        .options(selectinload(BioRewardParticipant.user))
        .order_by(BioRewardParticipant.opted_in_at.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    rows = list(rows_result.scalars().all())

    text = (
        f'👥 <b>Участники акции «За описание профиля»</b>\n\n'
        f'Всего: <b>{total}</b>\n'
        f'Страница: <b>{page + 1}</b>'
    )
    await callback.message.edit_text(
        text, parse_mode='HTML', reply_markup=_participants_keyboard(rows, page, total)
    )
    await callback.answer()


@admin_required
@error_handler
async def view_participant(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        pid = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer('Bad id', show_alert=True)
        return
    result = await db.execute(
        select(BioRewardParticipant)
        .where(BioRewardParticipant.id == pid)
        .options(selectinload(BioRewardParticipant.user))
    )
    p = result.scalar_one_or_none()
    if p is None:
        await callback.answer('Не найден', show_alert=True)
        return
    u = p.user
    last_check = p.last_check_at.strftime('%Y-%m-%d %H:%M') if p.last_check_at else '—'
    last_seen = p.last_bio_seen_at.strftime('%Y-%m-%d %H:%M') if p.last_bio_seen_at else '—'
    cooldown = p.cooldown_until.strftime('%Y-%m-%d %H:%M') if p.cooldown_until else '—'
    bio = (p.bio_snapshot or '—')[:300]
    text = (
        f'👤 <b>Участник #{p.id}</b>\n\n'
        f'Telegram: <code>{u.telegram_id if u else p.user_id}</code> '
        f'@{html_module.escape(u.username) if u and u.username else "—"}\n'
        f'Статус: <b>{p.status}</b>\n'
        f'Обход проверки (bypass): <b>{"Да" if p.bypass_check else "Нет"}</b>\n'
        f'Последняя проверка: {last_check}\n'
        f'Последний раз нашли нужный текст: {last_seen}\n'
        f'Блокировка до: {cooldown}\n'
        f'ID бесплатной подписки: <code>{p.free_subscription_id or "—"}</code>\n\n'
        f'<i>Текст из описания (последний снимок):</i>\n<code>{html_module.escape(bio)}</code>'
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=_participant_actions_keyboard(p))
    await callback.answer()


@admin_required
@error_handler
async def participant_revoke(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        pid = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer('Bad id', show_alert=True)
        return
    result = await db.execute(
        select(BioRewardParticipant)
        .where(BioRewardParticipant.id == pid)
        .options(selectinload(BioRewardParticipant.user))
    )
    p = result.scalar_one_or_none()
    if p is None or p.user is None:
        await callback.answer('Нет участника / пользователя', show_alert=True)
        return
    cfg = await bio_crud.get_config(db)
    await bio_reward_service._revoke(db, p, p.user, cfg)
    await bio_crud.log_event(db, p.id, 'admin_force_revoke', {'admin_id': db_user.id})
    await callback.answer('🔴 Revoked')
    await db.refresh(p)
    callback.data = f'br_admin_p_view:{p.id}'
    await view_participant(callback, db_user=db_user, db=db)


@admin_required
@error_handler
async def participant_restore(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        pid = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer('Bad id', show_alert=True)
        return
    result = await db.execute(
        select(BioRewardParticipant)
        .where(BioRewardParticipant.id == pid)
        .options(selectinload(BioRewardParticipant.user))
    )
    p = result.scalar_one_or_none()
    if p is None:
        await callback.answer('Не найден', show_alert=True)
        return
    await bio_crud.set_status(
        db, p, BioRewardStatus.PENDING, cooldown_until=None, grace_started_at=None, revoked_at=None
    )
    await bio_crud.log_event(db, p.id, 'admin_restore', {'admin_id': db_user.id})
    await callback.answer('♻ Restored')
    await db.refresh(p)
    callback.data = f'br_admin_p_view:{p.id}'
    await view_participant(callback, db_user=db_user, db=db)


@admin_required
@error_handler
async def participant_bypass(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        pid = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer('Bad id', show_alert=True)
        return
    result = await db.execute(
        select(BioRewardParticipant)
        .where(BioRewardParticipant.id == pid)
        .options(selectinload(BioRewardParticipant.user))
    )
    p = result.scalar_one_or_none()
    if p is None:
        await callback.answer('Не найден', show_alert=True)
        return
    p.bypass_check = not p.bypass_check
    await db.commit()
    await db.refresh(p)
    await bio_crud.log_event(db, p.id, 'admin_bypass_toggle', {'admin_id': db_user.id, 'enabled': p.bypass_check})
    await callback.answer('✅ Bypass переключен')
    callback.data = f'br_admin_p_view:{p.id}'
    await view_participant(callback, db_user=db_user, db=db)


@admin_required
@error_handler
async def noop(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.answer()


# ---------------- Analytics ----------------


def _analytics_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='📅 Cohorts (monthly)', callback_data='br_admin_analytics_cohorts:monthly')],
            [InlineKeyboardButton(text='📅 Cohorts (weekly)', callback_data='br_admin_analytics_cohorts:weekly')],
            [InlineKeyboardButton(text='🚀 Viral coefficient', callback_data='br_admin_analytics_viral')],
            [InlineKeyboardButton(text='🔄 Пересчитать сейчас', callback_data='br_admin_analytics_recompute')],
            [InlineKeyboardButton(text='⬅ Конфиг', callback_data='br_admin_open')],
        ]
    )


def _analytics_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='🔄 Пересчитать сейчас', callback_data='br_admin_analytics_recompute')],
            [InlineKeyboardButton(text='⬅ Аналитика', callback_data='br_admin_analytics')],
        ]
    )


def _format_cohort_table(rows: list, granularity: str) -> str:
    if not rows:
        return f'📈 <b>Cohorts ({granularity})</b>\n\n<i>Нет данных. Запустите пересчёт.</i>'
    lines = [f'📈 <b>Cohorts ({granularity})</b>\n', '<pre>', f'{"bucket":<10} {"opt":>4} {"conv":>4} {"%":>4} {"rev₽":>8} {"d→":>4}']
    for snap in rows:
        p = snap.payload or {}
        revenue_rub = (p.get('total_paid_revenue_kopeks', 0) or 0) // 100
        avg_days = p.get('avg_days_to_convert')
        avg_str = '—' if avg_days is None else str(avg_days)
        lines.append(
            f'{snap.bucket_key:<10} {p.get("total_opted_in", 0):>4} '
            f'{p.get("converted_paid", 0):>4} {p.get("conversion_pct", 0):>3}% '
            f'{revenue_rub:>8} {avg_str:>4}'
        )
    lines.append('</pre>')
    return '\n'.join(lines)


def _format_viral(rows: list) -> str:
    if not rows:
        return '🚀 <b>Viral coefficient</b>\n\n<i>Нет данных. Запустите пересчёт.</i>'
    by_window = {snap.bucket_key: (snap.payload or {}) for snap in rows}
    lines = ['🚀 <b>Viral coefficient</b>\n']
    for window in ('7d', '30d', '90d'):
        p = by_window.get(window, {})
        lines.append(
            f'<b>{window}</b>: K = <b>{p.get("k_factor", 0)}</b> | '
            f'bio-active: {p.get("bio_active_users", 0)} | '
            f'referrals: {p.get("attributed_referrals", 0)} | '
            f'paid: {p.get("paid_attributed_referrals", 0)}'
        )
    return '\n'.join(lines)


@admin_required
@error_handler
async def open_analytics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    last = await bio_reward_analytics.last_computed_at(db)
    last_str = last.strftime('%Y-%m-%d %H:%M UTC') if last else '<i>никогда</i>'
    text = (
        f'📈 <b>Аналитика акции «За описание профиля»</b>\n\n'
        f'Последний пересчёт: <i>{last_str}</i>'
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=_analytics_menu_kb())
    await callback.answer()


@admin_required
@error_handler
async def show_cohorts(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    granularity = callback.data.split(':')[1] if ':' in callback.data else 'monthly'
    snap_type = (
        bio_reward_analytics.CONVERSION_MONTHLY
        if granularity == 'monthly'
        else bio_reward_analytics.CONVERSION_WEEKLY
    )
    rows = await bio_reward_analytics.read_snapshots(db, snap_type, limit=12)
    text = _format_cohort_table(rows, granularity)
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=_analytics_back_kb())
    await callback.answer()


@admin_required
@error_handler
async def show_viral(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    rows = await bio_reward_analytics.read_snapshots(db, bio_reward_analytics.VIRAL, limit=10)
    text = _format_viral(rows)
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=_analytics_back_kb())
    await callback.answer()


@admin_required
@error_handler
async def recompute_analytics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.answer('⏳ Пересчитываю…')
    try:
        stats = await bio_reward_analytics.recompute_all(db)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning('bio_reward.analytics.manual_recompute_failed', err=str(exc))
        await callback.message.answer(f'❌ Ошибка: {exc}')
        return
    last = await bio_reward_analytics.last_computed_at(db)
    last_str = last.strftime('%Y-%m-%d %H:%M UTC') if last else '—'
    text = (
        '✅ <b>Аналитика пересчитана</b>\n\n'
        f'monthly: {stats.get("conversion_monthly", 0)}\n'
        f'weekly: {stats.get("conversion_weekly", 0)}\n'
        f'viral: {stats.get("viral_windows", 0)}\n\n'
        f'computed_at: {last_str}'
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=_analytics_menu_kb())


# ---------------- Registration ----------------


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(open_panel, F.data == 'br_admin_open')
    dp.callback_query.register(toggle_enabled, F.data == 'br_admin_toggle_enabled')
    dp.callback_query.register(toggle_personal_link, F.data == 'br_admin_toggle_personal_link')
    dp.callback_query.register(open_notify, F.data == 'br_admin_notify_panel')
    dp.callback_query.register(toggle_notify, F.data.startswith('br_admin_notify_toggle:'))

    dp.callback_query.register(edit_discount, F.data == 'br_admin_edit_discount')
    dp.callback_query.register(edit_grace, F.data == 'br_admin_edit_grace')
    dp.callback_query.register(edit_cooldown, F.data == 'br_admin_edit_cooldown')
    dp.callback_query.register(edit_interval, F.data == 'br_admin_edit_interval')
    dp.callback_query.register(edit_window, F.data == 'br_admin_edit_window')
    dp.callback_query.register(edit_traffic, F.data == 'br_admin_edit_traffic')
    dp.callback_query.register(edit_devices, F.data == 'br_admin_edit_devices')
    dp.callback_query.register(edit_squad, F.data == 'br_admin_edit_squad')
    dp.callback_query.register(edit_instruction, F.data == 'br_admin_edit_instruction')

    dp.callback_query.register(show_strings, F.data == 'br_admin_strings')
    dp.callback_query.register(add_string, F.data == 'br_admin_string_add')
    dp.callback_query.register(del_string, F.data.startswith('br_admin_string_del:'))

    dp.callback_query.register(show_stats, F.data == 'br_admin_stats')
    dp.callback_query.register(show_participants, F.data.startswith('br_admin_participants:'))
    dp.callback_query.register(view_participant, F.data.startswith('br_admin_p_view:'))
    dp.callback_query.register(participant_revoke, F.data.startswith('br_admin_p_revoke:'))
    dp.callback_query.register(participant_restore, F.data.startswith('br_admin_p_restore:'))
    dp.callback_query.register(participant_bypass, F.data.startswith('br_admin_p_bypass:'))
    dp.callback_query.register(noop, F.data == 'br_admin_noop')

    dp.callback_query.register(open_analytics, F.data == 'br_admin_analytics')
    dp.callback_query.register(show_cohorts, F.data.startswith('br_admin_analytics_cohorts:'))
    dp.callback_query.register(show_viral, F.data == 'br_admin_analytics_viral')
    dp.callback_query.register(recompute_analytics, F.data == 'br_admin_analytics_recompute')

    # FSM message handlers — gated on state
    dp.message.register(_handle_numeric_input, BioRewardAdminStates.waiting_for_discount_percent)
    dp.message.register(_handle_numeric_input, BioRewardAdminStates.waiting_for_grace_hours)
    dp.message.register(_handle_numeric_input, BioRewardAdminStates.waiting_for_cooldown_hours)
    dp.message.register(_handle_numeric_input, BioRewardAdminStates.waiting_for_check_interval)
    dp.message.register(_handle_numeric_input, BioRewardAdminStates.waiting_for_window_days)
    dp.message.register(_handle_numeric_input, BioRewardAdminStates.waiting_for_traffic_gb)
    dp.message.register(_handle_numeric_input, BioRewardAdminStates.waiting_for_device_limit)
    dp.message.register(_handle_squad_input, BioRewardAdminStates.waiting_for_squad_uuid)
    dp.message.register(_handle_instruction_input, BioRewardAdminStates.waiting_for_instruction_text)
    dp.message.register(_handle_string_add_input, BioRewardAdminStates.waiting_for_bio_string_add)
