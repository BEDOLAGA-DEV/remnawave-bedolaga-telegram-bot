"""Telegram handlers for the bio-reward feature: opt-in flow, status panel."""

from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import bio_reward as bio_crud
from app.database.models import BioRewardStatus, User
from app.services.bio_reward_service import (
    _placeholder_resolutions,
    bio_reward_service,
    expand_bio_template,
)
from app.utils.decorators import error_handler


def _btn(
    text: str,
    *,
    callback_data: str | None = None,
    emoji_id: str | None = None,
    style: str | None = None,
) -> InlineKeyboardButton:
    """Build an InlineKeyboardButton with optional premium custom emoji icon.

    Empty/None ``emoji_id`` falls back to plain unicode in ``text``.
    """
    kwargs: dict = {'text': text}
    if callback_data is not None:
        kwargs['callback_data'] = callback_data
    if style:
        kwargs['style'] = style
    if emoji_id:
        kwargs['icon_custom_emoji_id'] = emoji_id
    return InlineKeyboardButton(**kwargs)


logger = structlog.get_logger(__name__)

CB_OPEN = 'nz!_bio_reward_open'
CB_OPT_IN = 'nz!_bio_reward_opt_in'
CB_RECHECK = 'nz!_bio_reward_recheck'


def _personal_link(user: User, bot_username: str | None) -> str:
    code = (user.referral_code or '').strip()
    if not code or not bot_username:
        return ''
    return f'https://t.me/{bot_username}?start={code}'


async def _build_status_text(
    db: AsyncSession, user: User, bot_username: str | None
) -> tuple[str, InlineKeyboardMarkup]:
    cfg = await bio_crud.get_config(db)
    participant = await bio_crud.get_participant_by_user_id(db, user.id, with_subscription=True)

    rendered_strings: list[str] = []
    for tpl in cfg.accepted_bio_strings or []:
        resolutions = _placeholder_resolutions(str(tpl), bot_username=bot_username, user=user)
        if any(v == '' for v in resolutions.values()):
            continue  # skip templates that can't fully resolve for this user
        rendered = expand_bio_template(str(tpl), bot_username=bot_username, user=user)
        if rendered and rendered.strip():
            rendered_strings.append(rendered)
    accepted_lines = (
        '\n'.join(f'• <code>{s}</code>' for s in rendered_strings) or '<i>(не настроено)</i>'
    )

    personal = _personal_link(user, bot_username)
    personal_block = (
        '\n\n🔗 <b>Можно поставить и вашу личную реферальную ссылку</b>\n'
        '   (она тоже считается подходящим текстом):\n'
        f'<code>{personal}</code>'
        if personal else ''
    )

    if not (cfg.enabled and bio_reward_service.is_enabled()):
        text = (
            '🚫 <b>Бесплатная подписка за описание профиля</b>\n\n'
            'Сейчас эта акция временно недоступна. Загляните позже — мы её обязательно включим снова.'
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[_btn('⬅ Назад в меню', callback_data='nz!_back_to_menu')]]
        )
        return text, kb

    status_label = {
        BioRewardStatus.PENDING.value: (
            '⏳ Ждём, пока вы добавите нужный текст в описание профиля.\n'
            '   Установите текст и нажмите «🔄 Проверить сейчас».'
        ),
        BioRewardStatus.ACTIVE.value: (
            '✅ Всё работает! Бесплатная подписка и скидка активны.\n'
            '   Спасибо, что помогаете нам расти 🙌'
        ),
        BioRewardStatus.GRACE.value: (
            '⚠️ Мы не нашли нужный текст в вашем описании.\n'
            f'   У вас ещё {cfg.grace_period_hours} ч., чтобы вернуть его — иначе подписка отключится.'
        ),
        BioRewardStatus.COOLDOWN.value: (
            '⏸ Временная блокировка после отключения подписки.\n'
            f'   Попробовать снова можно через {cfg.cooldown_hours} ч. с момента отключения.'
        ),
        BioRewardStatus.REVOKED.value: (
            '❌ Подписка по акции отключена.\n'
            '   Вы можете попробовать снова после окончания блокировки.'
        ),
    }
    state_line = '<i>Вы ещё не подавали заявку на участие.</i>'
    if participant is not None:
        state_line = status_label.get(participant.status, participant.status)

    head = (
        '🎁 <b>Бесплатная подписка за описание профиля</b>\n\n'
        '✨ <b>Что это?</b>\n'
        'Это акция нашего сервиса. Если вы добавите специальный текст '
        'в описание (поле «О себе») вашего Telegram-профиля, вы получите:\n'
        '• 🆓 <b>Бесплатную подписку</b> на VPN\n'
        f'• 💰 <b>Скидку {cfg.discount_percent}%</b> на все платные тарифы\n\n'
        '📝 <b>Как участвовать — пошагово:</b>\n'
        '1️⃣ Откройте <b>Настройки</b> Telegram\n'
        '2️⃣ Зайдите в раздел <b>«Изменить профиль»</b> (или «Edit profile»)\n'
        '3️⃣ Найдите поле <b>«О себе»</b> (или «Bio» / «Описание»)\n'
        '4️⃣ Скопируйте один из текстов ниже и вставьте его в это поле\n'
        '5️⃣ Сохраните изменения\n'
        '6️⃣ Вернитесь сюда и нажмите кнопку <b>«✅ Я участвую»</b>\n\n'
        '📋 <b>Подходящие тексты для описания:</b>\n'
        f'{accepted_lines}'
        f'{personal_block}\n\n'
        '⚙️ <b>Как это работает:</b>\n'
        '• Бот периодически проверяет описание вашего профиля\n'
        '• Пока нужный текст есть — подписка и скидка активны\n'
        f'• Если уберёте текст, у вас будет <b>{cfg.grace_period_hours} ч.</b>, чтобы вернуть его\n'
        '• Если не вернёте — подписка отключится автоматически\n'
        '• Если вы покупали платный тариф со скидкой по акции и убрали текст — '
        'с баланса спишется доплата за дни, использованные сверх «честного» срока без скидки\n\n'
        f'<b>📊 Ваш статус сейчас:</b>\n{state_line}'
    )

    rows: list[list[InlineKeyboardButton]] = []
    if participant is None or (
        participant is not None and participant.status == BioRewardStatus.REVOKED.value
    ):
        # First-time opt-in OR re-join after revoke: invite to participate.
        rows.append([
            _btn(
                '✅ Я участвую',
                callback_data=CB_OPT_IN,
                emoji_id=settings.BIO_REWARD_EMOJI_PARTICIPATE,
                style='success',
            )
        ])
    elif participant.status in (
        BioRewardStatus.PENDING.value,
        BioRewardStatus.ACTIVE.value,
        BioRewardStatus.GRACE.value,
    ):
        # Already opted in: button matches the status hint ("нажмите Проверить сейчас").
        rows.append([
            _btn(
                '🔄 Проверить сейчас',
                callback_data=CB_RECHECK,
                emoji_id=settings.BIO_REWARD_EMOJI_RECHECK,
                style='primary',
            )
        ])
    rows.append([_btn('⬅ Назад в меню', callback_data='nz!_back_to_menu')])
    return head, InlineKeyboardMarkup(inline_keyboard=rows)


async def _resolve_bot_username(message_or_callback) -> str | None:
    try:
        bot = message_or_callback.bot
        me = await bot.me()
        return me.username
    except Exception:
        return None


@error_handler
async def cmd_bio_reward(message: types.Message, db_user: User, db: AsyncSession):
    bot_username = await _resolve_bot_username(message)
    text, kb = await _build_status_text(db, db_user, bot_username)
    await message.answer(text, parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True)


@error_handler
async def open_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    bot_username = await _resolve_bot_username(callback)
    text, kb = await _build_status_text(db, db_user, bot_username)
    await callback.message.edit_text(
        text, parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True
    )
    await callback.answer()


@error_handler
async def opt_in(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    _, outcome = await bio_reward_service.opt_in(db, db_user)
    answers = {
        'activated': '✅ Подписка активирована! Спасибо 🙌',
        'extended': '✅ Подписка продлена',
        'recovered': '✅ Текст вернулся в описание — всё восстановлено',
        'pending': (
            'ℹ️ В описании пока нет нужного текста.\n'
            'Установите его и нажмите «🔄 Проверить сейчас».'
        ),
        'grace_started': '⚠️ Текст убран из описания — пошёл таймер на возврат',
        'grace_pending': '⏳ Ещё идёт время, чтобы вернуть текст в описание',
        'revoked': '❌ Подписка по акции отключена',
        'cooldown': (
            '⏸ Сейчас временная блокировка после отключения.\n'
            'Попробовать снова можно позже.'
        ),
        'disabled': '🚫 Эта акция временно недоступна',
        'no_user': 'ℹ️ Не получилось определить ваш Telegram-профиль',
        'noop': 'ℹ️ Изменений нет',
        'fetch_failed': '⚠️ Не удалось проверить профиль. Попробуйте позже',
    }
    await callback.answer(
        answers.get(outcome, outcome), show_alert=outcome in ('cooldown', 'disabled')
    )
    bot_username = await _resolve_bot_username(callback)
    text, kb = await _build_status_text(db, db_user, bot_username)
    await callback.message.edit_text(
        text, parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True
    )


@error_handler
async def recheck(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    participant = await bio_crud.get_participant_by_user_id(db, db_user.id)
    if participant is None:
        await callback.answer('Сначала нажмите «Я участвую»', show_alert=True)
        return
    outcome = await bio_reward_service.check_user(db, participant, user=db_user)
    if outcome == 'fetch_failed':
        await callback.answer('⚠️ Не удалось проверить профиль. Попробуйте позже', show_alert=True)
    else:
        await callback.answer(f'Проверено: {outcome}')
    bot_username = await _resolve_bot_username(callback)
    text, kb = await _build_status_text(db, db_user, bot_username)
    await callback.message.edit_text(
        text, parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(cmd_bio_reward, Command('bio_reward'))
    dp.callback_query.register(open_panel, F.data == CB_OPEN)
    dp.callback_query.register(opt_in, F.data == CB_OPT_IN)
    dp.callback_query.register(recheck, F.data == CB_RECHECK)
