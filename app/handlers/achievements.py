import html as html_module

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.achievement import (
    _get_user_stat,
    check_and_unlock_all,
    get_active_templates,
    get_user_achievements,
)
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

CONDITION_LABELS = {
    'total_spent_kopeks': 'Потратить на подписки',
    'days_active': 'Дней с подпиской',
    'referral_count': 'Пригласить друзей',
    'traffic_gb': 'Использовать трафика (ГБ)',
    'topup_count': 'Пополнений баланса',
    'review_left': 'Оставить отзыв',
}

REWARD_LABELS = {
    'balance_kopeks': 'Бонус на баланс',
    'traffic_gb': 'Бонус трафика',
    'wl_traffic_gb': 'Бонус WL-трафика',
    'subscription_days': 'Дни подписки',
    'none': 'Без награды',
}


def _filter_visible_templates(templates, unlocked_ids: set) -> list:
    """For multi-level groups, show only unlocked levels + the next one.

    Non-grouped achievements always show.
    Grouped: show all unlocked + the first non-unlocked (current goal).
    """
    # Build group → sorted templates
    groups: dict[str, list] = {}
    standalone = []
    for t in templates:
        group = getattr(t, 'group_name', None)
        if group:
            groups.setdefault(group, []).append(t)
        else:
            standalone.append(t)

    visible = list(standalone)

    for group_name, group_templates in groups.items():
        sorted_group = sorted(group_templates, key=lambda x: getattr(x, 'level', 1))
        for t in sorted_group:
            visible.append(t)
            if t.id not in unlocked_ids:
                break  # Show up to the first non-unlocked (current goal)

    return visible


@error_handler
async def show_achievements_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Show list of achievements as buttons."""
    if not settings.ACHIEVEMENTS_ENABLED:
        await callback.answer('Достижения отключены', show_alert=True)
        return

    texts = get_texts(db_user.language)

    # Auto-check and unlock any earned achievements
    try:
        await check_and_unlock_all(db, db_user.id, bot=callback.bot)
        await db.commit()
    except Exception as e:
        logger.error('Failed to check achievements', error=e)

    templates = await get_active_templates(db)
    user_achievements = await get_user_achievements(db, db_user.id)
    unlocked_ids = {ua.template_id for ua in user_achievements}

    # For multi-level: only show the CURRENT level per group
    # (highest unlocked + next one, hide future levels)
    visible_templates = _filter_visible_templates(templates, unlocked_ids)

    total = len(visible_templates)
    unlocked_count = sum(1 for t in visible_templates if t.id in unlocked_ids)

    if total == 0:
        msg = '\U0001f3c6 <b>Достижения</b>\n\nПока достижений нет.'
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='nz!_back_to_menu')]]
        )
    else:
        msg = f'\U0001f3c6 <b>Достижения ({unlocked_count}/{total})</b>\n\nВыберите достижение:'

        buttons: list[list[InlineKeyboardButton]] = []
        for t in visible_templates:
            is_hidden = getattr(t, 'is_hidden', False)
            is_unlocked = t.id in unlocked_ids
            group = getattr(t, 'group_name', None)
            level = getattr(t, 'level', 1)

            level_tag = f' Ур.{level}' if group and level > 1 else ''

            if is_hidden and not is_unlocked:
                label = '\U0001f512 ??? Скрытое'
            elif is_unlocked:
                label = f'\u2705 {t.emoji} {t.name}{level_tag}'
            else:
                label = f'\U0001f512 {t.emoji} {t.name}{level_tag}'

            buttons.append([InlineKeyboardButton(
                text=label,
                callback_data=f'nz!_ach_view_{t.id}',
            )])

        buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='nz!_back_to_menu')])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(msg, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@error_handler
async def show_achievement_detail(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Show detailed card for a single achievement."""
    texts = get_texts(db_user.language)
    template_id = int(callback.data.split('_')[-1])

    templates = await get_active_templates(db)
    template = next((t for t in templates if t.id == template_id), None)
    if not template:
        await callback.answer('Достижение не найдено', show_alert=True)
        return

    user_achievements = await get_user_achievements(db, db_user.id)
    unlocked_ids = {ua.template_id: ua for ua in user_achievements}
    ua = unlocked_ids.get(template.id)
    is_unlocked = ua is not None
    is_hidden = getattr(template, 'is_hidden', False)

    if is_hidden and not is_unlocked:
        msg = (
            '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n'
            '\U0001f512 <b>??? Скрытое достижение</b>\n\n'
            '\U0001f4a1 <i>Продолжайте пользоваться сервисом, чтобы открыть!</i>\n'
            '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500'
        )
    else:
        current = await _get_user_stat(db, db_user, template.condition_type)
        msg = _format_detail_card(template, ua, current, is_unlocked)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='\u2b05 К списку', callback_data='nz!_achievements')]]
    )
    await callback.message.edit_text(msg, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


def _format_detail_card(template, user_achievement, current_value: int, is_unlocked: bool) -> str:
    """Format detailed achievement card."""
    status = '\u2705 Получено' if is_unlocked else '\U0001f512 Не получено'
    emoji = template.emoji or '\U0001f3c6'
    name = html_module.escape(template.name)
    group = getattr(template, 'group_name', None)
    level = getattr(template, 'level', 1)
    if group:
        name += f' (Ур. {level})'
    description = html_module.escape(template.description or '')

    # Condition
    condition_label = CONDITION_LABELS.get(template.condition_type, template.condition_type)
    target = template.condition_value

    if template.condition_type == 'total_spent_kopeks':
        target_display = settings.format_price(target)
        current_display = settings.format_price(current_value)
    else:
        target_display = str(target)
        current_display = str(current_value)

    # Progress bar
    pct = min(100, int(current_value * 100 / target)) if target > 0 else 0
    filled = pct // 10
    bar = '\u2588' * filled + '\u2591' * (10 - filled)

    # Reward
    reward_text = ''
    if template.reward_type == 'balance_kopeks' and template.reward_value:
        reward_text = f'\U0001f381 <b>Награда:</b> {settings.format_price(template.reward_value)} на баланс'
    elif template.reward_type == 'traffic_gb' and template.reward_value:
        reward_text = f'\U0001f381 <b>Награда:</b> +{template.reward_value} ГБ трафика'
    elif template.reward_type == 'wl_traffic_gb' and template.reward_value:
        reward_text = f'\U0001f381 <b>Награда:</b> +{template.reward_value} ГБ WL-трафика'
    elif template.reward_type == 'subscription_days' and template.reward_value:
        reward_text = f'\U0001f381 <b>Награда:</b> +{template.reward_value} дн. подписки'

    # Hint
    hint = getattr(template, 'hint', '') or ''
    hint_text = f'\n\U0001f4a1 <b>Как получить:</b> <i>{html_module.escape(hint)}</i>' if hint else ''

    # Unlocked date
    date_text = ''
    if is_unlocked and user_achievement and user_achievement.unlocked_at:
        date_text = f'\n\U0001f4c5 Получено: {user_achievement.unlocked_at.strftime("%d.%m.%Y")}'

    lines = [
        '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500',
        f'{emoji} <b>{name}</b>',
        f'{status}',
    ]
    if description:
        lines.append(f'\n{description}')
    lines.append('')
    lines.append(f'\U0001f3af <b>Условие:</b> {condition_label}')
    lines.append(f'\U0001f4ca <b>Прогресс:</b> {current_display} / {target_display}')
    lines.append(f'[{bar}] {pct}%')
    if reward_text:
        lines.append(f'\n{reward_text}')
    if hint_text:
        lines.append(hint_text)
    if date_text:
        lines.append(date_text)
    lines.append('\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500')

    return '\n'.join(lines)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_achievements_list, F.data == 'nz!_achievements')
    dp.callback_query.register(show_achievement_detail, F.data.startswith('nz!_ach_view_'))
