"""Экран, на котором пользователь настраивает СВОИ реферальные награды.

Две настройки, и обе появляются на экране только когда админ их разрешил. Пункт,
который ничего не меняет, хуже отсутствующего: он обещает влияние, которого нет.

Первая — что получать, когда правило платит и деньгами, и днями. Вторая — в какую
подписку класть дни: награда приходит асинхронно, на чужом пополнении, и спросить
человека в этот момент невозможно, поэтому его ответ берётся заранее.
"""

import structlog
from aiogram import Dispatcher, F, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral_reward_level import (
    REWARD_PREFERENCE_DAYS,
    REWARD_PREFERENCE_MONEY,
    normalize_reward_preference,
)
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

# Выбор двоичный: деньги ИЛИ дни. Варианта «и то и другое» здесь нет — настройка
# ровно про то, чтобы получать что-то одно.
_PREFERENCES = (REWARD_PREFERENCE_MONEY, REWARD_PREFERENCE_DAYS)


def _preference_label(value: str, texts) -> str:
    if value == REWARD_PREFERENCE_DAYS:
        return texts.t('REFERRAL_PREF_DAYS', '📅 Дни подписки')
    return texts.t('REFERRAL_PREF_MONEY', '💰 Деньги на баланс')


def _subscription_label(subscription, tariff_names: dict[int, str], texts) -> str:
    """Подпись подписки в списке выбора.

    Тариф и срок вместе: у пользователя может быть две подписки одного тарифа, и
    только по названию их не различить.
    """
    name = tariff_names.get(subscription.tariff_id) or texts.t('REFERRAL_DAYS_TARGET_AUTO', 'Подписка')
    if subscription.end_date:
        until = texts.t('REFERRAL_SUBSCRIPTION_UNTIL', 'до {date}').format(
            date=subscription.end_date.strftime('%d.%m.%Y')
        )
        return f'{name} — {until}'
    return name


async def _user_subscriptions(db: AsyncSession, user: User) -> list:
    """Подписки, между которыми есть смысл выбирать.

    Триальные и неживые не предлагаются: положить в них награду всё равно нельзя,
    а пункт в списке обещал бы обратное.
    """
    from app.database.crud.subscription import get_active_subscriptions_by_user_id

    subscriptions = await get_active_subscriptions_by_user_id(db, user.id)
    return [sub for sub in subscriptions if not sub.is_trial]


async def _tariff_names(db: AsyncSession, subscriptions) -> dict[int, str]:
    tariff_ids = {sub.tariff_id for sub in subscriptions if sub.tariff_id}
    if not tariff_ids:
        return {}

    from sqlalchemy import select

    from app.database.models import Tariff

    rows = await db.execute(select(Tariff.id, Tariff.name).where(Tariff.id.in_(tariff_ids)))
    return {row.id: row.name for row in rows.all()}


async def _render(callback: types.CallbackQuery, db: AsyncSession, db_user: User) -> None:
    """Собрать экран. Намеренно БЕЗ ``callback.answer()``.

    На один callback Telegram принимает ровно один ответ, а вызывающие сначала
    подтверждают действие своим текстом и только потом перерисовывают экран.
    """
    from app.services.referral_reward_service import describe_reward_choice_sides

    texts = get_texts(db_user.language)
    kind_choice = settings.is_referral_reward_kind_choice_enabled()
    target_choice = settings.is_referral_days_target_choice_enabled()
    # Не выбиравший получает деньги — так же, как их выдаст расчёт. Показать
    # «ничего не выбрано» значило бы разойтись с начислением.
    current_kind = normalize_reward_preference(db_user.referral_reward_preference) or REWARD_PREFERENCE_MONEY

    choice_money, choice_days = await describe_reward_choice_sides(db, db_user, language=db_user.language)
    side_labels = {REWARD_PREFERENCE_MONEY: choice_money, REWARD_PREFERENCE_DAYS: choice_days}

    lines = [
        texts.t('REFERRAL_REWARD_SETTINGS_TITLE', '⚙️ <b>Настройки наград</b>'),
        '',
        texts.t(
            'REFERRAL_REWARD_SETTINGS_INTRO',
            'Здесь вы решаете, в каком виде получать реферальные награды и куда зачислять дни подписки.',
        ),
    ]
    keyboard: list[list[types.InlineKeyboardButton]] = []

    if kind_choice:
        lines += ['', texts.t('REFERRAL_PREF_HEADER', '🎁 <b>Что получать</b>')]
        lines.append(
            f'<i>{texts.t("REFERRAL_PREF_HINT", "Выбор действует только там, где уровень даёт и то и другое.")}</i>'
        )
        for value in _PREFERENCES:
            # На кнопке — сколько именно даёт эта сторона по правилу, которое
            # человеку и применяется. Без суммы выбор делается вслепую.
            mark = '🔘' if value == current_kind else '⚪️'
            side = side_labels.get(value)
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{mark} {_preference_label(value, texts)}' + (f' — {side}' if side else ''),
                        callback_data=f'ref_pref:{value}',
                    )
                ]
            )

    # Куда класть дни спрашиваем, только когда человек выбрал дни: выбравшему
    # деньги эта настройка ни на что не влияет, и пункт обещал бы влияние,
    # которого нет. Если выбор вида админ не разрешил, спрашиваем всегда — дни
    # тогда приходят по правилу, и цель у них есть.
    if target_choice and (not kind_choice or current_kind == REWARD_PREFERENCE_DAYS):
        subscriptions = await _user_subscriptions(db, db_user)
        names = await _tariff_names(db, subscriptions)
        lines += ['', texts.t('REFERRAL_DAYS_TARGET_HEADER', '📅 <b>Куда зачислять дни</b>')]
        lines.append(
            f'<i>{texts.t("REFERRAL_DAYS_TARGET_HINT", "Если у уровня задан тариф, дни пойдут в подписку этого тарифа.")}</i>'
        )

        if not subscriptions:
            lines.append(
                texts.t('REFERRAL_DAYS_TARGET_NONE', 'У вас пока нет подписок, между которыми можно выбирать.')
            )
        else:
            chosen = db_user.referral_days_subscription_id
            # Автовыбор — тоже вариант, и он должен быть виден как выбранный,
            # иначе непонятно, что происходит сейчас.
            mark = '🔘' if not chosen else '⚪️'
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{mark} {texts.t("REFERRAL_DAYS_TARGET_AUTO", "Выбирать автоматически")}',
                        callback_data='ref_days_target:auto',
                    )
                ]
            )
            for sub in subscriptions:
                mark = '🔘' if chosen == sub.id else '⚪️'
                keyboard.append(
                    [
                        types.InlineKeyboardButton(
                            text=f'{mark} {_subscription_label(sub, names, texts)}',
                            callback_data=f'ref_days_target:{sub.id}',
                        )
                    ]
                )

    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')])
    await callback.message.edit_text(
        '\n'.join(lines), reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    )


@error_handler
async def show_reward_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await _render(callback, db, db_user)
    await callback.answer()


@error_handler
async def set_reward_preference(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Сохранить, что получать. Неразрешённая настройка не сохраняется вовсе."""
    if not settings.is_referral_reward_kind_choice_enabled():
        await callback.answer(get_texts(db_user.language).ERROR, show_alert=True)
        return

    # Неизвестное значение не сохраняется вовсе: молча переключить человека на
    # другой вид награды из-за мусора в callback'е нельзя.
    chosen = normalize_reward_preference(callback.data.split(':', 1)[1])
    if chosen is None:
        await callback.answer(get_texts(db_user.language).ERROR, show_alert=True)
        return

    db_user.referral_reward_preference = chosen
    await db.commit()

    logger.info(
        'Пользователь выбрал вид реферальной награды',
        user_id=db_user.id,
        preference=db_user.referral_reward_preference,
    )
    await callback.answer(get_texts(db_user.language).t('REFERRAL_SETTINGS_SAVED', '✅ Сохранено'))
    await _render(callback, db, db_user)


@error_handler
async def set_days_target(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Сохранить подписку для дней.

    Принадлежность проверяется здесь, а не только при начислении: сохранённый
    чужой идентификатор был бы тихой попыткой увести награду, и место ей не в БД.
    """
    if not settings.is_referral_days_target_choice_enabled():
        await callback.answer(get_texts(db_user.language).ERROR, show_alert=True)
        return

    raw = callback.data.split(':', 1)[1]
    if raw == 'auto':
        db_user.referral_days_subscription_id = None
    else:
        allowed = {sub.id for sub in await _user_subscriptions(db, db_user)}
        chosen = int(raw)
        if chosen not in allowed:
            await callback.answer(get_texts(db_user.language).ERROR, show_alert=True)
            return
        db_user.referral_days_subscription_id = chosen

    await db.commit()
    logger.info(
        'Пользователь выбрал подписку для дней реферальной награды',
        user_id=db_user.id,
        subscription_id=db_user.referral_days_subscription_id,
    )
    await callback.answer(get_texts(db_user.language).t('REFERRAL_SETTINGS_SAVED', '✅ Сохранено'))
    await _render(callback, db, db_user)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_reward_settings, F.data == 'referral_reward_settings')
    # Двоеточие обязательно: без него префикс поглотил бы соседние строки.
    dp.callback_query.register(set_reward_preference, F.data.startswith('ref_pref:'))
    dp.callback_query.register(set_days_target, F.data.startswith('ref_days_target:'))
