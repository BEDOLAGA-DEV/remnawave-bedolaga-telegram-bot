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
)
from app.database.models import AchievementTemplate, User, UserAchievement
from app.localization.texts import get_texts
from app.states import AchievementAdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

CONDITION_TYPES = {
    'total_spent_kopeks': '\U0001f4b0 \u0421\u0443\u043c\u043c\u0430 \u043f\u043e\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u0439 (\u043a\u043e\u043f.)',
    'days_active': '\U0001f4c5 \u0414\u043d\u0435\u0439 \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0441\u0442\u0438',
    'referral_count': '\U0001f465 \u041a\u043e\u043b-\u0432\u043e \u0440\u0435\u0444\u0435\u0440\u0430\u043b\u043e\u0432',
    'traffic_gb': '\U0001f4c8 \u0422\u0440\u0430\u0444\u0438\u043a (\u0413\u0411)',
    'topup_count': '\U0001f4b3 \u041a\u043e\u043b-\u0432\u043e \u043f\u043e\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u0439',
    'review_left': '\u2b50 \u041e\u0441\u0442\u0430\u0432\u0438\u043b \u043e\u0442\u0437\u044b\u0432',
}

REWARD_TYPES = {
    'balance_kopeks': '\U0001f4b0 \u0411\u0430\u043b\u0430\u043d\u0441 (\u043a\u043e\u043f.)',
    'traffic_gb': '\U0001f4c8 \u0422\u0440\u0430\u0444\u0438\u043a (\u0413\u0411)',
    'subscription_days': '\U0001f4c5 \u0414\u043d\u0438 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0438',
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
    rows.append([InlineKeyboardButton(text='\u2795 \u0421\u043e\u0437\u0434\u0430\u0442\u044c', callback_data='admin_ach_create')])
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')])
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
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='\U0001f5d1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c',
                    callback_data=f'admin_ach_delete:{template.id}',
                )
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


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(admin_achievements, F.data == 'admin_achievements')
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
