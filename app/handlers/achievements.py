import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.achievement import (
    _get_user_stat,
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
    'subscription_days': 'Дни подписки',
    'none': 'Без награды',
}


def _format_achievement_card(template, user_achievement, current_value: int, texts) -> str:
    """Format a single achievement as an HTML card."""
    is_unlocked = user_achievement is not None
    is_hidden = getattr(template, 'is_hidden', False)

    if is_hidden and not is_unlocked:
        return (
            '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n'
            '\U0001f512 <b>??? \u0421\u043a\u0440\u044b\u0442\u043e\u0435 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435</b>\n\n'
            '\U0001f4a1 <i>\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0430\u0439\u0442\u0435 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u044c\u0441\u044f \u0441\u0435\u0440\u0432\u0438\u0441\u043e\u043c, \u0447\u0442\u043e\u0431\u044b \u043e\u0442\u043a\u0440\u044b\u0442\u044c!</i>\n'
            '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500'
        )

    status = '\u2705' if is_unlocked else '\U0001f512'
    emoji = template.emoji or '\U0001f3c6'
    name = template.name
    description = template.description or ''

    # Condition info
    condition_label = CONDITION_LABELS.get(template.condition_type, template.condition_type)
    target = template.condition_value

    # Format condition value for kopeks
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

    # Reward info
    reward_label = REWARD_LABELS.get(template.reward_type, template.reward_type)
    reward_text = ''
    if template.reward_type == 'balance_kopeks' and template.reward_value:
        reward_text = f'\U0001f381 {reward_label}: {settings.format_price(template.reward_value)}'
    elif template.reward_type == 'traffic_gb' and template.reward_value:
        reward_text = f'\U0001f381 {reward_label}: {template.reward_value} \u0413\u0411'
    elif template.reward_type == 'subscription_days' and template.reward_value:
        reward_text = f'\U0001f381 {reward_label}: {template.reward_value} \u0434\u043d.'
    elif template.reward_type != 'none':
        reward_text = f'\U0001f381 {reward_label}'

    # How to get (hint)
    hint = getattr(template, 'hint', '') or ''
    hint_text = f'\n\U0001f4a1 <i>{hint}</i>' if hint else ''

    # Unlocked date
    date_text = ''
    if is_unlocked and user_achievement.unlocked_at:
        date_text = f'\n\U0001f4c5 \u041f\u043e\u043b\u0443\u0447\u0435\u043d\u043e: {user_achievement.unlocked_at.strftime("%d.%m.%Y")}'

    lines = [
        '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500',
        f'{status} {emoji} <b>{name}</b>',
    ]
    if description:
        lines.append(f'{description}')
    lines.append('')
    lines.append(f'\U0001f3af {condition_label}: {current_display}/{target_display}')
    lines.append(f'[{bar}] {pct}%')
    if reward_text:
        lines.append(reward_text)
    if hint_text:
        lines.append(hint_text)
    if date_text:
        lines.append(date_text)
    lines.append('\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500')

    return '\n'.join(lines)


@error_handler
async def show_achievements(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    if not settings.ACHIEVEMENTS_ENABLED:
        await callback.answer('\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f \u043e\u0442\u043a\u043b\u044e\u0447\u0435\u043d\u044b', show_alert=True)
        return

    texts = get_texts(db_user.language)

    # Page from callback
    data = callback.data
    if data.startswith('nz!_ach_page_'):
        page = int(data.split('_')[-1])
    else:
        page = 1

    templates = await get_active_templates(db)
    user_achievements = await get_user_achievements(db, db_user.id)
    unlocked_ids = {ua.template_id: ua for ua in user_achievements}

    total = len(templates)

    if total == 0:
        msg = '\U0001f3c6 <b>\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f</b>\n\n\u041f\u043e\u043a\u0430 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0439 \u043d\u0435\u0442.'
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='nz!_back_to_menu')]]
        )
    else:
        page = max(1, min(page, total))
        template = templates[page - 1]
        ua = unlocked_ids.get(template.id)
        current = await _get_user_stat(db, db_user, template.condition_type)

        # Stats header
        unlocked_count = sum(1 for t in templates if t.id in unlocked_ids)
        header = f'\U0001f3c6 <b>\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f ({unlocked_count}/{total})</b>\n'

        msg = header + '\n' + _format_achievement_card(template, ua, current, texts)

        # Navigation
        buttons: list[list[InlineKeyboardButton]] = []
        nav_row: list[InlineKeyboardButton] = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(text='\u2b05', callback_data=f'nz!_ach_page_{page - 1}'))
        nav_row.append(InlineKeyboardButton(text=f'{page}/{total}', callback_data='nz!_noop'))
        if page < total:
            nav_row.append(InlineKeyboardButton(text='\u27a1', callback_data=f'nz!_ach_page_{page + 1}'))
        buttons.append(nav_row)
        buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='nz!_back_to_menu')])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(msg, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_achievements, F.data == 'nz!_achievements')
    dp.callback_query.register(show_achievements, F.data.startswith('nz!_ach_page_'))
