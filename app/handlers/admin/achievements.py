from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.achievement import (
    create_template,
    delete_template,
    get_all_templates,
    get_template_by_id,
    update_template,
)
from app.database.models import AchievementTemplate, User, UserAchievement
from app.localization.texts import get_texts
from app.states import AchievementAdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

CONDITION_TYPES = {
    # Emojis MUST use \U-style 8-hex codepoint escapes, not UTF-16 surrogate
    # pairs (\udXXX\udYYY). Surrogate pairs survive Python parsing but blow up
    # in `urllib.parse.quote(..., encoding='utf-8')` with
    # `UnicodeEncodeError: surrogates not allowed`, which crashes
    # `callback.message.edit_text` when these labels reach Telegram's HTTP
    # form-urlencoded body (aiohttp.formdata._gen_form_urlencoded).
    'total_spent_kopeks': '\U0001f4b0 \u0421\u0443\u043c\u043c\u0430 \u043f\u043e\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u0439 (\u043a\u043e\u043f.)',
    'days_active': '\U0001f4c5 \u0414\u043d\u0435\u0439 \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0441\u0442\u0438',
    'registered': '\U0001f44b \u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f',
    'referral_count': '\U0001f465 \u041a\u043e\u043b-\u0432\u043e \u0440\u0435\u0444\u0435\u0440\u0430\u043b\u043e\u0432 (\u043f\u043b\u0430\u0442\u044f\u0449\u0438\u0445)',
    'traffic_gb': '\U0001f4c8 \u0422\u0440\u0430\u0444\u0438\u043a (\u0413\u0411)',
    'topup_count': '\U0001f4b3 \u041a\u043e\u043b-\u0432\u043e \u043f\u043e\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u0439',
    'review_left': '\u2b50 \u041e\u0441\u0442\u0430\u0432\u0438\u043b \u043e\u0442\u0437\u044b\u0432 (\u043e\u0434\u043e\u0431\u0440\u0435\u043d)',
    'first_paid_subscription': '\U0001f39f\ufe0f \u041f\u0435\u0440\u0432\u0430\u044f \u043f\u043b\u0430\u0442\u043d\u0430\u044f \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0430',
    'autopay_enabled': '\U0001f504 \u0412\u043a\u043b\u044e\u0447\u0438\u043b \u0430\u0432\u0442\u043e\u043f\u043b\u0430\u0442\u0451\u0436',
    'single_topup_max_kopeks': '\U0001f48e \u041c\u0430\u043a\u0441. \u043e\u0434\u0438\u043d\u043e\u0447\u043d\u044b\u0439 \u043f\u043b\u0430\u0442\u0451\u0436 (\u043a\u043e\u043f.)',
    'promocode_used_count': '\U0001f381 \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043b \u043f\u0440\u043e\u043c\u043e\u043a\u043e\u0434\u043e\u0432',
    'poll_completed_count': '\U0001f4dd \u041f\u0440\u043e\u0448\u0451\u043b \u043e\u043f\u0440\u043e\u0441\u043e\u0432',
    'referral_revenue_kopeks': '\U0001f31f \u0414\u043e\u0445\u043e\u0434 \u0441 \u0440\u0435\u0444\u0435\u0440\u0430\u043b\u043e\u0432 (\u043a\u043e\u043f.)',
    'tickets_resolved_count': '\U0001f3ab \u0420\u0435\u0448\u0451\u043d\u043d\u044b\u0445 \u0442\u0438\u043a\u0435\u0442\u043e\u0432',
    'subscription_period_days': '\U0001f4c6 \u041c\u0430\u043a\u0441. \u043f\u0435\u0440\u0438\u043e\u0434 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0438 (\u0434\u043d\u0438)',
}

REWARD_TYPES = {
    'balance_kopeks': '\U0001f4b0 \u0411\u0430\u043b\u0430\u043d\u0441 (\u043a\u043e\u043f.)',
    'traffic_gb': '\U0001f4c8 \u0422\u0440\u0430\u0444\u0438\u043a (\u0413\u0411)',
    'wl_traffic_gb': '\U0001f4ca WL-\u0442\u0440\u0430\u0444\u0438\u043a (\u0413\u0411)',
    'subscription_days': '\U0001f4c5 \u0414\u043d\u0438 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0438',
    'topup_window_sum_percent': '\U0001f4b3 % \u043e\u0442 \u043f\u043e\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u0439 \u0441 \u043f\u0440\u043e\u0448\u043b\u043e\u0433\u043e \u0443\u0440\u043e\u0432\u043d\u044f',
    'none': '\u2796 \u0411\u0435\u0437 \u043d\u0430\u0433\u0440\u0430\u0434\u044b',
}


def _templates_list_keyboard(templates: list[AchievementTemplate], language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = []
    for t in templates:
        status = '\U0001f7e2' if t.is_active else '\U0001f534'
        rows.append([
            InlineKeyboardButton(
                text=f'{status} {t.emoji} {t.name}',
                callback_data=f'admin_ach_view:{t.id}',
            )
        ])
    rows.append([InlineKeyboardButton(text='\u2795 Создать', callback_data='admin_ach_create')])

    # Bulk toggle: show opposite action of majority state
    active_count = sum(1 for t in templates if t.is_active)
    if templates:
        if active_count > 0:
            rows.append([InlineKeyboardButton(text='\u23f8 Выключить все', callback_data='admin_ach_toggle_all_off')])
        if active_count < len(templates):
            rows.append([InlineKeyboardButton(text='\u25b6\ufe0f Включить все', callback_data='admin_ach_toggle_all_on')])

    rows.append([InlineKeyboardButton(text='\U0001f4ca Статистика', callback_data='admin_ach_stats')])
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_promo')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_required
@error_handler
async def admin_achievements(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    templates = await get_all_templates(db)
    text = '\U0001f3c6 <b>\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f</b>\n\n\u0423\u043f\u0440\u0430\u0432\u043b\u044f\u0439\u0442\u0435 \u0448\u0430\u0431\u043b\u043e\u043d\u0430\u043c\u0438 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0439.'
    if not templates:
        text += '\n\n\u041f\u043e\u043a\u0430 \u0448\u0430\u0431\u043b\u043e\u043d\u043e\u0432 \u043d\u0435\u0442.'
    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=_templates_list_keyboard(templates, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def admin_ach_view(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    template_id = int(callback.data.split(':')[1])
    result = await db.execute(
        select(AchievementTemplate).where(AchievementTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    if not template:
        await callback.answer('\u0428\u0430\u0431\u043b\u043e\u043d \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d', show_alert=True)
        return

    # Count users who unlocked
    count_result = await db.execute(
        select(func.count(UserAchievement.id)).where(UserAchievement.template_id == template_id)
    )
    unlock_count = count_result.scalar() or 0

    condition_label = CONDITION_TYPES.get(template.condition_type, template.condition_type)
    reward_label = REWARD_TYPES.get(template.reward_type, template.reward_type)

    text = (
        f'{template.emoji} <b>{template.name}</b>\n\n'
        f'\U0001f3af \u0423\u0441\u043b\u043e\u0432\u0438\u0435: {condition_label}\n'
        f'\U0001f522 \u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435: {template.condition_value}\n'
        f'\U0001f381 \u041d\u0430\u0433\u0440\u0430\u0434\u0430: {reward_label}\n'
        f'\U0001f4b5 \u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u043d\u0430\u0433\u0440\u0430\u0434\u044b: {template.reward_value}\n'
        f'\U0001f4ca \u0420\u0430\u0437\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043b\u0438: {unlock_count} \u043f\u043e\u043b\u044c\u0437.\n'
        f'\u2699\ufe0f \u0410\u043a\u0442\u0438\u0432\u043d\u043e: {"Да" if template.is_active else "Нет"}'
    )

    texts = get_texts(db_user.language)
    toggle_text = '\U0001f534 Выключить' if template.is_active else '\U0001f7e2 Включить'
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='✏️ Название', callback_data=f'admin_ach_edit_name:{template.id}'),
                InlineKeyboardButton(text='🎨 Эмодзи', callback_data=f'admin_ach_edit_emoji:{template.id}'),
            ],
            [
                InlineKeyboardButton(text='🎯 Тип условия', callback_data=f'admin_ach_edit_ctype:{template.id}'),
                InlineKeyboardButton(text='🔢 Значение условия', callback_data=f'admin_ach_edit_cvalue:{template.id}'),
            ],
            [
                InlineKeyboardButton(text='🎁 Тип награды', callback_data=f'admin_ach_edit_rtype:{template.id}'),
                InlineKeyboardButton(text='💵 Значение награды', callback_data=f'admin_ach_edit_rvalue:{template.id}'),
            ],
            [
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f'admin_ach_toggle:{template.id}',
                ),
                InlineKeyboardButton(
                    text='\U0001f5d1 Удалить',
                    callback_data=f'admin_ach_delete:{template.id}',
                ),
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_achievements')],
        ]
    )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def admin_ach_delete(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    template_id = int(callback.data.split(':')[1])
    texts = get_texts(db_user.language)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='\u2705 \u0414\u0430, \u0443\u0434\u0430\u043b\u0438\u0442\u044c',
                    callback_data=f'admin_ach_delete_confirm:{template_id}',
                ),
                InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_ach_view:{template_id}'),
            ]
        ]
    )
    await callback.message.edit_text(
        f'\U0001f5d1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435 #{template_id}?',
        reply_markup=keyboard,
    )
    await callback.answer()


@admin_required
@error_handler
async def admin_ach_delete_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    template_id = int(callback.data.split(':')[1])
    deleted = await delete_template(db, template_id)
    if deleted:
        await db.commit()
        await callback.answer('\u0423\u0434\u0430\u043b\u0435\u043d\u043e', show_alert=False)
    else:
        await callback.answer('\u041d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e', show_alert=True)

    templates = await get_all_templates(db)
    await callback.message.edit_text(
        '\U0001f3c6 <b>\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f</b>\n\n\u0423\u043f\u0440\u0430\u0432\u043b\u044f\u0439\u0442\u0435 \u0448\u0430\u0431\u043b\u043e\u043d\u0430\u043c\u0438 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0439.',
        parse_mode='HTML',
        reply_markup=_templates_list_keyboard(templates, db_user.language),
    )


# ---- FSM: create achievement ----

@admin_required
@error_handler
async def admin_ach_create(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(AchievementAdminStates.waiting_for_name)
    await callback.message.edit_text('\u2709\ufe0f \u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f:')
    await callback.answer()


@admin_required
@error_handler
async def ach_name_received(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    await state.update_data(ach_name=message.text.strip())
    await state.set_state(AchievementAdminStates.waiting_for_emoji)
    await message.answer('\U0001f3a8 \u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u044d\u043c\u043e\u0434\u0437\u0438 (\u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440 \U0001f3c6):')


@admin_required
@error_handler
async def ach_emoji_received(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    await state.update_data(ach_emoji=message.text.strip()[:10])
    await state.set_state(AchievementAdminStates.waiting_for_condition_type)

    rows = []
    for ct, label in CONDITION_TYPES.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f'ach_ctype:{ct}')])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

    await message.answer('\U0001f3af \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0442\u0438\u043f \u0443\u0441\u043b\u043e\u0432\u0438\u044f:', reply_markup=keyboard)


@admin_required
@error_handler
async def ach_condition_type_selected(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    ctype = callback.data.split(':')[1]
    await state.update_data(ach_condition_type=ctype)
    await state.set_state(AchievementAdminStates.waiting_for_condition_value)
    await callback.message.edit_text(
        f'\U0001f522 \u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u0443\u0441\u043b\u043e\u0432\u0438\u044f (\u0447\u0438\u0441\u043b\u043e):\n'
        f'\u0422\u0438\u043f: {CONDITION_TYPES.get(ctype, ctype)}'
    )
    await callback.answer()


@admin_required
@error_handler
async def ach_condition_value_received(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer('\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0446\u0435\u043b\u043e\u0435 \u0447\u0438\u0441\u043b\u043e.')
        return

    await state.update_data(ach_condition_value=value)
    await state.set_state(AchievementAdminStates.waiting_for_reward_type)

    rows = []
    for rt, label in REWARD_TYPES.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f'ach_rtype:{rt}')])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

    await message.answer('\U0001f381 \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0442\u0438\u043f \u043d\u0430\u0433\u0440\u0430\u0434\u044b:', reply_markup=keyboard)


@admin_required
@error_handler
async def ach_reward_type_selected(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    rtype = callback.data.split(':')[1]
    await state.update_data(ach_reward_type=rtype)

    if rtype == 'none':
        # Skip reward value, create immediately
        data = await state.get_data()
        await state.clear()

        template = await create_template(
            db,
            name=data['ach_name'],
            emoji=data['ach_emoji'],
            condition_type=data['ach_condition_type'],
            condition_value=data['ach_condition_value'],
            reward_type='none',
            reward_value=0,
        )
        await db.commit()

        await callback.message.edit_text(
            f'\u2705 \u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435 <b>{template.name}</b> \u0441\u043e\u0437\u0434\u0430\u043d\u043e!',
            parse_mode='HTML',
        )
        await callback.answer()
        return

    await state.set_state(AchievementAdminStates.waiting_for_reward_value)
    await callback.message.edit_text(
        f'\U0001f4b5 \u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u043d\u0430\u0433\u0440\u0430\u0434\u044b (\u0447\u0438\u0441\u043b\u043e):\n'
        f'\u0422\u0438\u043f: {REWARD_TYPES.get(rtype, rtype)}'
    )
    await callback.answer()


@admin_required
@error_handler
async def ach_reward_value_received(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer('\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0446\u0435\u043b\u043e\u0435 \u0447\u0438\u0441\u043b\u043e.')
        return

    data = await state.get_data()
    await state.clear()

    template = await create_template(
        db,
        name=data['ach_name'],
        emoji=data['ach_emoji'],
        condition_type=data['ach_condition_type'],
        condition_value=data['ach_condition_value'],
        reward_type=data['ach_reward_type'],
        reward_value=value,
    )
    await db.commit()

    await message.answer(
        f'\u2705 \u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435 <b>{template.name}</b> \u0441\u043e\u0437\u0434\u0430\u043d\u043e!\n'
        f'\u041d\u0430\u0433\u0440\u0430\u0434\u0430: {REWARD_TYPES.get(data["ach_reward_type"], "")} = {value}',
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def admin_ach_toggle(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Toggle is_active for a single achievement template."""
    template_id = int(callback.data.split(':')[1])
    result = await db.execute(
        select(AchievementTemplate).where(AchievementTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    if not template:
        await callback.answer('Шаблон не найден', show_alert=True)
        return
    template.is_active = not template.is_active
    await db.commit()
    status = 'включено' if template.is_active else 'выключено'
    await callback.answer(f'{template.emoji} {template.name} — {status}')
    # Re-render the view
    await admin_ach_view(callback, db_user, db)


@admin_required
@error_handler
async def admin_ach_toggle_all(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Bulk enable or disable all achievement templates."""
    new_state = callback.data == 'admin_ach_toggle_all_on'
    from sqlalchemy import update

    await db.execute(
        update(AchievementTemplate).values(is_active=new_state)
    )
    await db.commit()
    label = 'Все включены' if new_state else 'Все выключены'
    await callback.answer(label)
    await admin_achievements(callback, db_user, db)


@admin_required
@error_handler
async def admin_ach_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Show achievement statistics."""
    texts = get_texts(db_user.language)

    # Total templates / active
    all_templates = await get_all_templates(db)
    total = len(all_templates)
    active = sum(1 for t in all_templates if t.is_active)

    # Total unlocked
    total_unlocked_result = await db.execute(select(func.count(UserAchievement.id)))
    total_unlocked = total_unlocked_result.scalar() or 0

    # Top 5 popular
    top_query = (
        select(AchievementTemplate.name, AchievementTemplate.emoji, func.count(UserAchievement.id).label('cnt'))
        .outerjoin(UserAchievement, UserAchievement.template_id == AchievementTemplate.id)
        .group_by(AchievementTemplate.id)
        .order_by(func.count(UserAchievement.id).desc())
        .limit(5)
    )
    top_result = await db.execute(top_query)
    top_rows = top_result.all()

    lines = [
        '\U0001f3c6 <b>Статистика достижений</b>\n',
        f'Шаблонов: {total} (активных: {active})',
        f'Всего выдано: {total_unlocked}\n',
    ]
    if top_rows:
        lines.append('<b>Топ-5 популярных:</b>')
        for i, (name, emoji, cnt) in enumerate(top_rows, 1):
            lines.append(f'  {i}. {emoji} {name} — {cnt} польз.')

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_achievements')],
        ]
    )
    await callback.message.edit_text(
        '\n'.join(lines),
        parse_mode='HTML',
        reply_markup=keyboard,
    )
    await callback.answer()


# ---- FSM: edit achievement ----

@admin_required
@error_handler
async def admin_ach_edit_name(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    template_id = int(callback.data.split(':')[1])
    await state.update_data(edit_template_id=template_id)
    await state.set_state(AchievementAdminStates.edit_waiting_for_name)
    await callback.message.edit_text('✏️ Введите новое название достижения:')
    await callback.answer()


@admin_required
@error_handler
async def ach_edit_name_received(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    template_id = data.get('edit_template_id')
    new_name = message.text.strip()
    if not new_name:
        await message.answer('Название не может быть пустым.')
        return
    await update_template(db, template_id, name=new_name)
    await db.commit()
    await state.clear()
    await message.answer(f'✅ Название обновлено: <b>{new_name}</b>', parse_mode='HTML')


@admin_required
@error_handler
async def admin_ach_edit_emoji(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    template_id = int(callback.data.split(':')[1])
    await state.update_data(edit_template_id=template_id)
    await state.set_state(AchievementAdminStates.edit_waiting_for_emoji)
    await callback.message.edit_text('🎨 Введите новый эмодзи (например 🏆):')
    await callback.answer()


@admin_required
@error_handler
async def ach_edit_emoji_received(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    template_id = data.get('edit_template_id')
    new_emoji = message.text.strip()[:10]
    if not new_emoji:
        await message.answer('Эмодзи не может быть пустым.')
        return
    await update_template(db, template_id, emoji=new_emoji)
    await db.commit()
    await state.clear()
    await message.answer(f'✅ Эмодзи обновлён: {new_emoji}')


@admin_required
@error_handler
async def admin_ach_edit_ctype(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    template_id = int(callback.data.split(':')[1])
    rows = []
    for ct, label in CONDITION_TYPES.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f'admin_ach_set_ctype:{template_id}:{ct}')])
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=f'admin_ach_view:{template_id}')])
    await callback.message.edit_text(
        '🎯 Выберите новый тип условия:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@admin_required
@error_handler
async def admin_ach_set_ctype(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    _, template_id_str, ctype = callback.data.split(':')
    template_id = int(template_id_str)
    await update_template(db, template_id, condition_type=ctype)
    await db.commit()
    await callback.answer(f'Тип условия обновлён: {CONDITION_TYPES.get(ctype, ctype)}')
    callback.data = f'admin_ach_view:{template_id}'
    await admin_ach_view(callback, db_user, db)


@admin_required
@error_handler
async def admin_ach_edit_cvalue(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    template_id = int(callback.data.split(':')[1])
    template = await get_template_by_id(db, template_id)
    if not template:
        await callback.answer('Шаблон не найден', show_alert=True)
        return
    await state.update_data(edit_template_id=template_id)
    await state.set_state(AchievementAdminStates.edit_waiting_for_condition_value)
    label = CONDITION_TYPES.get(template.condition_type, template.condition_type)
    await callback.message.edit_text(
        f'🔢 Введите новое значение условия (целое число).\n'
        f'Тип: {label}\nТекущее: {template.condition_value}'
    )
    await callback.answer()


@admin_required
@error_handler
async def ach_edit_cvalue_received(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    template_id = data.get('edit_template_id')
    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer('Введите целое число.')
        return
    await update_template(db, template_id, condition_value=value)
    await db.commit()
    await state.clear()
    await message.answer(f'✅ Значение условия обновлено: {value}')


@admin_required
@error_handler
async def admin_ach_edit_rtype(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    template_id = int(callback.data.split(':')[1])
    rows = []
    for rt, label in REWARD_TYPES.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f'admin_ach_set_rtype:{template_id}:{rt}')])
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=f'admin_ach_view:{template_id}')])
    await callback.message.edit_text(
        '🎁 Выберите новый тип награды:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@admin_required
@error_handler
async def admin_ach_set_rtype(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    _, template_id_str, rtype = callback.data.split(':')
    template_id = int(template_id_str)
    if rtype == 'none':
        await update_template(db, template_id, reward_type='none', reward_value=0)
    else:
        await update_template(db, template_id, reward_type=rtype)
    await db.commit()
    await callback.answer(f'Тип награды обновлён: {REWARD_TYPES.get(rtype, rtype)}')
    callback.data = f'admin_ach_view:{template_id}'
    await admin_ach_view(callback, db_user, db)


@admin_required
@error_handler
async def admin_ach_edit_rvalue(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    template_id = int(callback.data.split(':')[1])
    template = await get_template_by_id(db, template_id)
    if not template:
        await callback.answer('Шаблон не найден', show_alert=True)
        return
    if template.reward_type == 'none':
        await callback.answer('У этой ачивки нет награды. Сначала выберите тип награды.', show_alert=True)
        return
    await state.update_data(edit_template_id=template_id)
    await state.set_state(AchievementAdminStates.edit_waiting_for_reward_value)
    label = REWARD_TYPES.get(template.reward_type, template.reward_type)
    await callback.message.edit_text(
        f'💵 Введите новое значение награды (целое число).\n'
        f'Тип: {label}\nТекущее: {template.reward_value}'
    )
    await callback.answer()


@admin_required
@error_handler
async def ach_edit_rvalue_received(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    template_id = data.get('edit_template_id')
    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer('Введите целое число.')
        return
    await update_template(db, template_id, reward_value=value)
    await db.commit()
    await state.clear()
    await message.answer(f'✅ Значение награды обновлено: {value}')


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(admin_achievements, F.data == 'admin_achievements')
    dp.callback_query.register(admin_ach_stats, F.data == 'admin_ach_stats')
    dp.callback_query.register(admin_ach_toggle, F.data.startswith('admin_ach_toggle:'))
    dp.callback_query.register(admin_ach_toggle_all, F.data.startswith('admin_ach_toggle_all_'))
    dp.callback_query.register(admin_ach_view, F.data.startswith('admin_ach_view:'))
    dp.callback_query.register(admin_ach_create, F.data == 'admin_ach_create')
    dp.callback_query.register(
        admin_ach_delete,
        F.data.startswith('admin_ach_delete:') & ~F.data.startswith('admin_ach_delete_confirm:'),
    )
    dp.callback_query.register(admin_ach_delete_confirm, F.data.startswith('admin_ach_delete_confirm:'))
    dp.callback_query.register(ach_condition_type_selected, F.data.startswith('ach_ctype:'))
    dp.callback_query.register(ach_reward_type_selected, F.data.startswith('ach_rtype:'))
    dp.message.register(ach_name_received, AchievementAdminStates.waiting_for_name)
    dp.message.register(ach_emoji_received, AchievementAdminStates.waiting_for_emoji)
    dp.message.register(ach_condition_value_received, AchievementAdminStates.waiting_for_condition_value)
    dp.message.register(ach_reward_value_received, AchievementAdminStates.waiting_for_reward_value)

    # Edit handlers
    dp.callback_query.register(admin_ach_edit_name, F.data.startswith('admin_ach_edit_name:'))
    dp.callback_query.register(admin_ach_edit_emoji, F.data.startswith('admin_ach_edit_emoji:'))
    dp.callback_query.register(admin_ach_edit_ctype, F.data.startswith('admin_ach_edit_ctype:'))
    dp.callback_query.register(admin_ach_set_ctype, F.data.startswith('admin_ach_set_ctype:'))
    dp.callback_query.register(admin_ach_edit_cvalue, F.data.startswith('admin_ach_edit_cvalue:'))
    dp.callback_query.register(admin_ach_edit_rtype, F.data.startswith('admin_ach_edit_rtype:'))
    dp.callback_query.register(admin_ach_set_rtype, F.data.startswith('admin_ach_set_rtype:'))
    dp.callback_query.register(admin_ach_edit_rvalue, F.data.startswith('admin_ach_edit_rvalue:'))
    dp.message.register(ach_edit_name_received, AchievementAdminStates.edit_waiting_for_name)
    dp.message.register(ach_edit_emoji_received, AchievementAdminStates.edit_waiting_for_emoji)
    dp.message.register(ach_edit_cvalue_received, AchievementAdminStates.edit_waiting_for_condition_value)
    dp.message.register(ach_edit_rvalue_received, AchievementAdminStates.edit_waiting_for_reward_value)
