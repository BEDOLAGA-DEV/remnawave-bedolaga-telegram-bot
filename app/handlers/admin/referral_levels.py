"""Админский редактор уровней реферальных наград.

Отдельный модуль, а не ещё одна секция в ``referrals.py``: тот уже на полторы
тысячи строк и держит статистику, диагностику и заявки на вывод.

Что здесь настраивается на каждом уровне цепочки:

* **какие бонусы активны** — деньги, дни подписки или оба сразу;
* **повод** — регистрация, первое пополнение или каждое пополнение;
* **сколько получает пригласивший** — процент от суммы и/или фиксированная
  сумма и/или дни подписки в конкретном тарифе;
* **сколько получает приглашённый** — фиксированная сумма и/или дни;
* **лимит оплаченных комиссий** для пары.

Правила живут в таблице, а не в ``Settings``. Причина практическая: ключ,
прописанный в ``.env``, попадает в ``ENV_OVERRIDE_KEYS`` и перестаёт меняться из
админки — запись ложится в БД и не применяется. Реферальная секция на типовой
установке залочена именно так, и складывать туда ещё десяток ключей на уровень
значило бы повторить ту же ловушку.
"""

import math

from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral_reward_level import (
    LEVELS_MODE_CHAIN,
    LEVELS_MODE_TIERS,
    MAX_SUPPORTED_LEVEL,
    delete_reward_level,
    get_all_reward_levels,
    get_reward_level,
    upsert_reward_level,
)
from app.database.crud.tariff import get_all_tariffs
from app.database.models import ReferralRewardMode, ReferralRewardTrigger, User
from app.services.system_settings_service import bot_configuration_service
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


_MODE_LABELS = {
    ReferralRewardMode.MONEY.value: '💰 Только деньги',
    ReferralRewardMode.DAYS.value: '📅 Только дни',
    ReferralRewardMode.BOTH.value: '💰📅 Деньги и дни',
}

_TRIGGER_LABELS = {
    ReferralRewardTrigger.REGISTRATION.value: '👥 За регистрацию',
    ReferralRewardTrigger.FIRST_TOPUP.value: '🎉 За первое пополнение',
    ReferralRewardTrigger.EVERY_TOPUP.value: '🔁 С каждого пополнения',
}

# Порядок перебора по кругу: одна кнопка вместо подменю на два пункта.
_MODE_CYCLE = [ReferralRewardMode.MONEY.value, ReferralRewardMode.DAYS.value, ReferralRewardMode.BOTH.value]
_TRIGGER_CYCLE = [
    ReferralRewardTrigger.REGISTRATION.value,
    ReferralRewardTrigger.FIRST_TOPUP.value,
    ReferralRewardTrigger.EVERY_TOPUP.value,
]

# Поля, которые правятся вводом числа: подпись, единица, максимум.
_NUMERIC_FIELDS = {
    'referrer_percent': ('Процент пригласившему', '%', 100),
    'referrer_fixed_kopeks': ('Фикс. сумма пригласившему', '₽', None),
    'referrer_days': ('Дни пригласившему', 'дн.', 3650),
    'referee_fixed_kopeks': ('Фикс. сумма приглашённому', '₽', None),
    'referee_days': ('Дни приглашённому', 'дн.', 3650),
    'max_payments': ('Лимит оплаченных комиссий (0 = без лимита)', 'шт.', None),
    'required_referrals': ('Рефералов для открытия уровня (0 = сразу)', 'чел.', None),
}

_MONEY_FIELDS = frozenset({'referrer_fixed_kopeks', 'referee_fixed_kopeks'})

# Сколько тарифов помещается в одно сообщение с кнопками. Превышение не молчит:
# «показаны первые N из M» честнее, чем список, из которого тариф просто исчез.
_TARIFF_PICKER_LIMIT = 40


def _fmt_threshold(level) -> str:
    """За сколько рефералов открывается уровень.

    Это ответ на вопрос, которого экрану не хватало: номер уровня говорит, ЧЬЁ
    пополнение приносит награду, а порог — с какого момента партнёр начинает
    получать доход с этого звена вообще.
    """
    required = int(getattr(level, 'required_referrals', 0) or 0)
    if required <= 0:
        return 'доступен сразу'

    population = (
        'рефералов с пополнением' if getattr(level, 'required_referrals_active_only', True) else 'приглашённых (любых)'
    )
    return f'{required} {population}'


def _fmt_optional_percent(value: int | None) -> str:
    """Пустой процент — это ноль, и так и надо писать.

    Показывать «—» было бы двусмысленно: админ прочитал бы это как «берётся
    откуда-то ещё», хотя откат к глобальному ``REFERRAL_COMMISSION_PERCENT``
    убран намеренно.
    """
    return f'{value}%' if value else 'не начисляется'


def _fmt_percent_for_card(level, money_on: bool, tier_mode: bool) -> str:
    """Процент так, как он действительно применится к этому правилу.

    «Не начисляется» было прямой ложью для правил, где получатель ПРЯМОЙ:
    личный процент партнёра перебивает процент правила и тогда, когда тот не
    задан. Карточка печатала «Процент: не начисляется», а рядом — оговорку, что
    личный всё равно платится, и читались эти две строки как противоречие.
    """
    if not money_on:
        return 'выключено режимом'
    if level.referrer_percent:
        return f'{level.referrer_percent}%'
    if tier_mode or level.level == 1:
        return 'не задан — платится личный процент партнёра, если он назначен'
    return 'не начисляется'


def _fmt_optional_money(value: int | None) -> str:
    return settings.format_price(value) if value else 'не начисляется'


def _fmt_days(days: int, tariff_name: str | None) -> str:
    if not days:
        return 'не начисляются'
    suffix = f' → {tariff_name}' if tariff_name else ' → основная подписка'
    return f'{days} дн.{suffix}'


async def _tariff_names(db: AsyncSession) -> dict[int, str]:
    tariffs = await get_all_tariffs(db, include_inactive=True)
    return {tariff.id: tariff.name for tariff in tariffs}


# Telegram отклоняет answerCallbackQuery с текстом длиннее 200 символов, а
# aiogram его не подрезает: вызов падает уже ПОСЛЕ того, как действие выполнено.
# Админ видит generic-ошибку поверх неперерисованного экрана и считает, что
# ничего не произошло, — хотя настройка уже записана и выплаты идут по-новому.
_CALLBACK_ANSWER_LIMIT = 200


async def _answer_capped(callback: types.CallbackQuery, text: str, *, show_alert: bool = False) -> None:
    """Ответить на callback, не превысив лимит Telegram.

    Подробностям длинных сообщений место на ЭКРАНЕ, а не во всплывающем окне;
    здесь только страховка, чтобы обработчик не падал на границе.
    """
    if len(text) > _CALLBACK_ANSWER_LIMIT:
        text = text[: _CALLBACK_ANSWER_LIMIT - 1].rstrip() + '…'
    await callback.answer(text, show_alert=show_alert)


def _tier_ladder_warnings(levels) -> list[str]:
    """Чем эта лестница рангов молча перестанет платить.

    Возвращаются ВСЕ подходящие предупреждения, а не первое: у лестницы,
    перенесённой из цепочки, обычно сразу и нет стартовой ступени, и пороги
    совпадают, а показанное поодиночке выглядит как единственная проблема.

    Пустой список в режиме цепочки: там ни одно из этих условий не мешает —
    уровни действуют одновременно, а не вытесняют друг друга.
    """
    if not settings.is_referral_tier_levels():
        return []

    active = [lvl for lvl in levels if lvl.is_active]
    if not active:
        return []

    warnings: list[str] = []
    thresholds = [int(lvl.required_referrals or 0) for lvl in active]

    if all(t > 0 for t in thresholds):
        warnings.append(
            f'Ни у одного уровня нет нулевого порога (минимальный — {min(thresholds)}): '
            'партнёры, не набравшие его, не получат ничего. Заведите стартовый уровень с порогом 0.'
        )

    # Порог сравнивается ВМЕСТЕ с популяцией подсчёта: «5 приглашённых» и
    # «5 из них с пополнением» — разные условия, достигаются в разное время и
    # конфликта не создают. Сравнение одних чисел объявляло такую лестницу
    # сломанной, хотя она работает как задумано.
    keys = [(int(lvl.required_referrals or 0), bool(lvl.required_referrals_active_only)) for lvl in active]
    duplicate = next((k for i, k in enumerate(keys) if k in keys[:i]), None)
    if duplicate is not None:
        population = 'с пополнением' if duplicate[1] else 'любых приглашённых'
        warnings.append(
            f'У нескольких активных уровней одинаковое условие ({duplicate[0]} {population}) — '
            'применится только тот, у которого номер больше. Остальные не сработают никогда.'
        )

    empty = [lvl.level for lvl in active if not _pays_referrer(lvl)]
    if empty:
        warnings.append(
            f'Уровень {", ".join(str(n) for n in empty)} ничего не начисляет пригласившему. '
            'Набрав его порог, партнёр перестанет получать доход: действует ровно один уровень.'
        )

    triggers = {lvl.trigger for lvl in active}
    if len(triggers) > 1:
        warnings.append(
            'У уровней разные поводы начисления. Действует повод того уровня, который партнёр набрал, '
            'поэтому награда за другой повод ему не достанется — задайте нужные поводы на каждом уровне.'
        )

    return warnings


def _pays_referrer(level) -> bool:
    """Начисляет ли правило хоть что-то ПРИГЛАСИВШЕМУ."""
    money_on = level.reward_mode in (ReferralRewardMode.MONEY.value, ReferralRewardMode.BOTH.value)
    days_on = level.reward_mode in (ReferralRewardMode.DAYS.value, ReferralRewardMode.BOTH.value)
    if money_on and (level.referrer_percent or level.referrer_fixed_kopeks):
        return True
    return bool(days_on and level.referrer_days)


def _scheme_line() -> str:
    if not settings.is_referral_levels_scheme():
        return '⚠️ Схема наград: классическая — уровни ниже НЕ применяются'
    if settings.is_referral_tier_levels():
        return '✅ Многоуровневая схема включена (режим: уровни за приглашённых)'
    return f'✅ Многоуровневая схема включена (режим: цепочка, глубина до {settings.get_referral_max_level_depth()})'


async def _render_levels(callback: types.CallbackQuery, db: AsyncSession) -> None:
    """Отрисовать список уровней. Намеренно БЕЗ ``callback.answer()``.

    На один callback Telegram принимает ровно один ответ. Хендлеры, которые
    сначала подтверждают действие своим текстом, а потом перерисовывают экран,
    иначе отвечали бы дважды — второй вызов падает с «query is invalid».
    """
    levels = await get_all_reward_levels(db)
    names = await _tariff_names(db)

    tier_mode_header = settings.is_referral_tier_levels()
    header_caption = 'Уровень'
    lines = [
        '🪜 <b>Уровни реферальных наград</b>',
        '',
        _scheme_line(),
        '',
    ]

    if not levels:
        lines.append('Уровни не заведены — награды по этой схеме не начисляются.')
    else:
        # Ранги перечисляются по возрастанию порога: это лестница, и читать её
        # надо в том порядке, в котором по ней поднимаются.
        if tier_mode_header:
            levels = sorted(levels, key=lambda lvl: ((lvl.required_referrals or 0), lvl.level))
        for level in levels:
            status = '✅' if level.is_active else '⛔️'
            lines.append(
                f'{status} <b>{header_caption} {level.level}</b> — '
                f'{_MODE_LABELS.get(level.reward_mode, level.reward_mode)}'
            )
            if tier_mode_header:
                lines.append(f'   Действует с: {_fmt_threshold(level)}')
            lines.append(f'   Повод: {_TRIGGER_LABELS.get(level.trigger, level.trigger)}')

            referrer_parts = []
            if level.reward_mode in (ReferralRewardMode.MONEY.value, ReferralRewardMode.BOTH.value):
                if level.referrer_percent:
                    referrer_parts.append(f'{level.referrer_percent}%')
                if level.referrer_fixed_kopeks:
                    referrer_parts.append(settings.format_price(level.referrer_fixed_kopeks))
            if level.reward_mode in (ReferralRewardMode.DAYS.value, ReferralRewardMode.BOTH.value):
                if level.referrer_days:
                    referrer_parts.append(_fmt_days(level.referrer_days, names.get(level.referrer_tariff_id)))
            lines.append(f'   Пригласившему: {" + ".join(referrer_parts) or "ничего"}')

            referee_parts = []
            if level.reward_mode in (ReferralRewardMode.MONEY.value, ReferralRewardMode.BOTH.value):
                if level.referee_fixed_kopeks:
                    referee_parts.append(settings.format_price(level.referee_fixed_kopeks))
            if level.reward_mode in (ReferralRewardMode.DAYS.value, ReferralRewardMode.BOTH.value):
                if level.referee_days:
                    referee_parts.append(_fmt_days(level.referee_days, names.get(level.referee_tariff_id)))
            # Показывается только когда есть что показать: у большинства правил
            # приглашённому не платят, и пустая строка была бы шумом. Но правило
            # «только приглашённому» без неё читалось как «не платит ничего».
            if referee_parts:
                lines.append(f'   Приглашённому: {" + ".join(referee_parts)}')

            lines.append('')

    # Предупреждения ПОСТОЯННЫЕ, а не тост в момент переключения: типовой порядок
    # действий — сначала переключить режим, потом расставить пороги, и опасная
    # лестница складывается уже после того, как тост показан и забыт.
    for warning in _tier_ladder_warnings(levels):
        lines.append('')
        lines.append(f'<i>⚠️ {warning}</i>')

    lines.append(
        '<i>Правила хранятся в базе, а не в .env, поэтому меняются отсюда и из кабинета и переживают перезапуск.</i>'
    )

    max_level = settings.get_referral_effective_max_level()
    tier_mode = settings.is_referral_tier_levels()
    caption = 'Уровень'
    keyboard_rows = []
    for level in levels:
        # Уровень глубже предела обхода не платит вовсе: помечаем прямо на кнопке,
        # иначе «✅ Уровень 4» неотличим от работающего. В режиме рангов предела
        # нет — там работают все заведённые.
        mark = '✅' if level.is_active else '⛔️'
        suffix = ' (не платит)' if level.level > max_level else ''
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'{mark} {caption} {level.level}{suffix}', callback_data=f'admin_ref_lvl:{level.level}'
                )
            ]
        )

    next_level = _next_free_level(levels)
    if next_level <= MAX_SUPPORTED_LEVEL:
        keyboard_rows.append(
            [types.InlineKeyboardButton(text=f'➕ Добавить уровень {next_level}', callback_data='admin_ref_lvl_add')]
        )

    if not levels:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text='📥 Перенести текущие настройки в уровень 1', callback_data='admin_ref_lvl_import'
                )
            ]
        )

    # Ярлык — по СОХРАНЁННОМУ значению, а не по is_referral_tier_levels(): та
    # требует включённой схемы уровней, а переключатель от схемы не зависит. При
    # классической схеме кнопка иначе показывала бы «цепочка» поверх сохранённых
    # «рангов», и первое нажатие не меняло бы ничего видимого.
    stored_tiers = settings.get_referral_levels_mode() == LEVELS_MODE_TIERS
    keyboard_rows.append(
        [
            types.InlineKeyboardButton(
                text=f'🎚 Режим: {"за приглашённых" if stored_tiers else "по цепочке"}',
                callback_data='admin_ref_lvl_tiers',
            )
        ]
    )
    # Что разрешено выбирать самому пользователю. Обе настройки видны рядом с
    # уровнями, потому что относятся к тому же — как выдаётся награда уровня.
    keyboard_rows.append(
        [
            types.InlineKeyboardButton(
                text=f'🎁 Выбор награды: {"разрешён" if settings.REFERRAL_ALLOW_REWARD_KIND_CHOICE else "запрещён"}',
                callback_data='admin_ref_lvl_kindchoice',
            )
        ]
    )
    keyboard_rows.append(
        [
            types.InlineKeyboardButton(
                text=f'📅 Выбор подписки: {"разрешён" if settings.REFERRAL_ALLOW_DAYS_TARGET_CHOICE else "запрещён"}',
                callback_data='admin_ref_lvl_targetchoice',
            )
        ]
    )
    # Глубина имеет смысл только в цепочке. В режиме рангов кнопка не прячется,
    # а прямо говорит, что настройка не действует: исчезнувшая кнопка выглядит
    # как пропавшая настройка, и админ идёт искать её в общем списке конфигурации.
    if tier_mode:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text='📏 Глубина цепочки: не используется', callback_data='admin_ref_lvl_depth'
                )
            ]
        )
    else:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'📏 Глубина цепочки: {settings.get_referral_max_level_depth()}',
                    callback_data='admin_ref_lvl_depth',
                )
            ]
        )

    scheme_toggle = '🔻 Вернуть классическую' if settings.is_referral_levels_scheme() else '🔺 Включить многоуровневую'
    keyboard_rows.append([types.InlineKeyboardButton(text=scheme_toggle, callback_data='admin_ref_lvl_scheme')])
    keyboard_rows.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_referrals_settings')])

    await callback.message.edit_text(
        '\n'.join(lines), reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    )


# Состояния, которые обязана снимать любая кнопка возврата. Оставленное взведённым
# состояние превращает следующее произвольное сообщение админа в правку настройки.
_PENDING_INPUT_STATES = frozenset(
    {
        AdminStates.referral_level_value_input.state,
        AdminStates.referral_depth_input.state,
    }
)


async def _cancel_pending_input(state: FSMContext | None) -> None:
    """Снять ожидание ввода значения при возврате на любой экран уровней.

    «Отмена» в редакторе поля ведёт на карточку уровня, а состояние оставалось
    взведённым: следующее произвольное сообщение админа в чат попадало в
    ``process_level_value`` и переписывало денежное поле. Набранное позже «100»
    превращалось в «процент пригласившему = 100%» без единого вопроса.

    Глобальный фоллбек неизвестных сообщений сюда не помогает: он навешен с
    ``StateFilter(None)`` и такое сообщение не перехватывает.

    Снимаются ОБА состояния редактора. У глубины цепочки та же кнопка «Отмена» и
    та же ловушка, но последствие хуже: следующее число переписывало
    ``REFERRAL_MAX_LEVEL_DEPTH`` и обрубало цепочку — уровни глубже переставали
    платить, и связать это с набранным в чат числом было нельзя.
    """
    if state is None:
        return
    if await state.get_state() in _PENDING_INPUT_STATES:
        await state.clear()


def _next_free_level(levels) -> int:
    """Наименьший свободный номер уровня, а не «последний плюс один».

    Уровни — это звенья цепочки, а не очередь. Взяв максимум, редактор после
    удаления второго из трёх предлагал бы только четвёртый, и дыра в середине
    становилась невосстановимой ни из одного интерфейса.

    Сам обход отсутствующий уровень переживает — он его просто пропускает и идёт
    дальше, — но админ остаётся с конфигурацией, которую больше не может починить.
    """
    taken = {lvl.level for lvl in levels}
    candidate = 1
    while candidate in taken:
        candidate += 1
    return candidate


@admin_required
@error_handler
async def show_reward_levels(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    await _cancel_pending_input(state)
    await _render_levels(callback, db)
    await callback.answer()


@admin_required
@error_handler
async def toggle_reward_scheme(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    """Переключить схему наград.

    Смена схемы меняет то, что бот платит живым людям, поэтому она сознательно
    сделана отдельным действием, а не побочным эффектом создания уровня.
    """
    await _cancel_pending_input(state)
    if bot_configuration_service.is_env_locked('REFERRAL_REWARD_SCHEME'):
        await callback.answer(
            'REFERRAL_REWARD_SCHEME задан в .env и не меняется из админки. '
            'Уберите строку из .env и перезапустите бота.',
            show_alert=True,
        )
        return

    new_value = 'legacy' if settings.is_referral_levels_scheme() else 'levels'
    await bot_configuration_service.set_value(db, 'REFERRAL_REWARD_SCHEME', new_value)

    if new_value == 'levels':
        active = await get_all_reward_levels(db, only_active=True)
        max_depth = settings.get_referral_effective_max_level()
        reachable = [lvl for lvl in active if lvl.level <= max_depth]
        if not active:
            await callback.answer(
                'Схема включена, но активных уровней нет — награды начисляться не будут.',
                show_alert=True,
            )
        elif not reachable:
            # Активные уровни есть, но все глубже предела обхода: цепочка до них
            # не доходит, и «схема включена» без этой оговорки означало бы, что
            # награды пошли, хотя не пойдёт ни одна.
            await callback.answer(
                f'Схема включена, но все активные уровни глубже {max_depth} — '
                'цепочка до них не доходит, награды начисляться не будут.',
                show_alert=True,
            )
        else:
            await callback.answer(f'Схема наград: {new_value}')
    else:
        await callback.answer(f'Схема наград: {new_value}')

    await _render_levels(callback, db)


@admin_required
@error_handler
async def add_reward_level(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    await _cancel_pending_input(state)
    levels = await get_all_reward_levels(db)
    next_level = _next_free_level(levels)
    if next_level > MAX_SUPPORTED_LEVEL:
        await callback.answer(f'Максимум {MAX_SUPPORTED_LEVEL} уровней', show_alert=True)
        return

    # Новый уровень заводится ВЫКЛЮЧЕННЫМ и пустым: включение сразу при создании
    # начало бы платить по недозаполненному правилу с ближайшего пополнения.
    await upsert_reward_level(
        db,
        next_level,
        is_active=False,
        reward_mode=ReferralRewardMode.MONEY.value,
        trigger=ReferralRewardTrigger.EVERY_TOPUP.value,
    )
    await callback.answer(f'Уровень {next_level} создан (выключен)')
    await _render_level(callback, db, next_level)


@admin_required
@error_handler
async def import_legacy_settings(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    """Перенести действующие настройки ``REFERRAL_*`` в уровень 1.

    Явное действие вместо неявного отката: отката к ``REFERRAL_COMMISSION_PERCENT``
    в расчёте нет, поэтому включение схемы на пустой таблице ничего не платит.
    Кнопка даёт понятный переход — то, что было, становится видимым правилом.

    Повод — «первое пополнение», и это не деталь оформления. В классической схеме
    фиксированные бонусы (пригласившему и приглашённому) разовые: они выдаются один
    раз, за первое пополнение реферала. Повод уровня один на всё правило, поэтому
    перенос с «каждым пополнением» превратил бы оба разовых бонуса в регулярную
    выплату — на живой базе это деньги, которых никто не обещал.

    Плата за такой выбор — процент здесь тоже становится разовым. Недоплатить и
    попросить админа осознанно поменять повод безопаснее, чем переплатить молча;
    правило создаётся выключенным и подписано ровно этим текстом.
    """
    await _cancel_pending_input(state)
    if await get_reward_level(db, 1) is not None:
        await callback.answer('Уровень 1 уже существует', show_alert=True)
        return

    from app.services.referral_reward_service import legacy_percent_for_import

    percent, notes = legacy_percent_for_import()
    await upsert_reward_level(
        db,
        1,
        is_active=False,
        reward_mode=ReferralRewardMode.MONEY.value,
        trigger=ReferralRewardTrigger.FIRST_TOPUP.value,
        referrer_percent=percent,
        referrer_fixed_kopeks=settings.REFERRAL_INVITER_BONUS_KOPEKS or None,
        referee_fixed_kopeks=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS or None,
        max_payments=settings.REFERRAL_MAX_COMMISSION_PAYMENTS,
    )
    # Заметки о непереносимом уходят на КАРТОЧКУ, а не во всплывающее окно: с
    # ними текст переваливал за лимит Telegram, вызов падал, и админ не узнавал
    # ни что перенесено, ни что потеряно, — при уже созданном уровне.
    await _answer_capped(
        callback,
        'Перенесено в уровень 1 (выключен).' + (f' Не перенесено: {len(notes)} — см. карточку.' if notes else ''),
        show_alert=True,
    )
    await _render_level(callback, db, 1, notes=notes)


async def _render_level(
    callback: types.CallbackQuery, db: AsyncSession, level_number: int, *, notes: list[str] | None = None
) -> bool:
    """Отрисовать карточку уровня. ``False`` — уровня нет. Без ``callback.answer()``.

    ``notes`` — что перенос не смог выразить уровнем. Печатается на карточке,
    потому что во всплывающем окне такой текст не помещается и теряется навсегда:
    повторить перенос на непустой таблице сервер уже не даст.
    """
    level = await get_reward_level(db, level_number)
    if level is None:
        return False

    names = await _tariff_names(db)
    money_on = level.reward_mode in (ReferralRewardMode.MONEY.value, ReferralRewardMode.BOTH.value)
    days_on = level.reward_mode in (ReferralRewardMode.DAYS.value, ReferralRewardMode.BOTH.value)

    tier_mode = settings.is_referral_tier_levels()
    caption = 'Уровень'
    beyond_depth = level.level > settings.get_referral_effective_max_level()
    lines = [
        f'🪜 <b>{caption} {level.level}</b>',
        '',
        f'Состояние: {"✅ активен" if level.is_active else "⛔️ выключен"}',
        f'Активные бонусы: {_MODE_LABELS.get(level.reward_mode, level.reward_mode)}',
        f'Повод: {_TRIGGER_LABELS.get(level.trigger, level.trigger)}',
        '',
        '<b>Пригласившему:</b>',
        f'• Процент: {_fmt_percent_for_card(level, money_on, tier_mode)}',
        f'• Фикс. сумма: {_fmt_optional_money(level.referrer_fixed_kopeks) if money_on else "выключено режимом"}',
        f'• Дни: {_fmt_days(level.referrer_days, names.get(level.referrer_tariff_id)) if days_on else "выключено режимом"}',
        '',
        '<b>Приглашённому:</b>',
        f'• Фикс. сумма: {_fmt_optional_money(level.referee_fixed_kopeks) if money_on else "выключено режимом"}',
        f'• Дни: {_fmt_days(level.referee_days, names.get(level.referee_tariff_id)) if days_on else "выключено режимом"}',
        '',
        f'Лимит оплаченных комиссий: {level.max_payments or "без лимита"}',
        '',
        f'<b>{"Действует с:" if tier_mode else "Открывается за:"}</b> {_fmt_threshold(level)}',
    ]

    for note in notes or []:
        lines.append('')
        lines.append(f'<i>⚠️ {note}</i>')

    if tier_mode:
        lines.append('')
        lines.append(
            '<i>Уровни за приглашённых: платят только прямому пригласившему, и применяется '
            'ровно один уровень — старший из достигнутых. Уровни выше по номеру не '
            'складываются с этим.</i>'
        )

    if beyond_depth:
        lines.append('')
        lines.append(
            f'<i>❗️ Цепочка обходится только до {settings.get_referral_max_level_depth()} уровней '
            '(REFERRAL_MAX_LEVEL_DEPTH), поэтому этот уровень не начисляет ничего, '
            'сколько бы ни был настроен.</i>'
        )

    # За регистрацию пополнения не было, и процент считать не от чего: правило
    # с одним процентом на этом поводе не начисляет ничего никогда. Соседняя
    # ловушка того же повода (дни без тарифа) предупреждение уже имела.
    if (
        level.trigger == ReferralRewardTrigger.REGISTRATION.value
        and money_on
        and level.referrer_percent
        and not level.referrer_fixed_kopeks
    ):
        lines.append('')
        lines.append(
            '<i>❗️ Повод «за регистрацию»: пополнения не было, и процент считать не от чего — '
            'этот уровень не начислит пригласившему ничего. Задайте фиксированную сумму '
            'или смените повод.</i>'
        )

    if days_on and not level.referrer_tariff_id and level.referrer_days:
        lines.append('')
        lines.append(
            '<i>Без тарифа дни идут в оплаченную подписку получателя — при нескольких '
            'выбирается с самым поздним сроком; триал берётся, только если платной нет.</i>'
        )

    if not settings.is_referral_levels_scheme():
        lines.append('')
        lines.append(
            '<i>⚠️ Схема наград — классическая: это правило настроено, но НЕ применяется. '
            'Включите многоуровневую схему на экране уровней.</i>'
        )

    # Личный процент партнёра перебивает процент правила на выплате ПРЯМОМУ
    # пригласившему. В цепочке прямой — только уровень 1; в рангах прямой всегда.
    if tier_mode or level.level == 1:
        lines.append('')
        lines.append(
            '<i>Личный процент партнёра перебивает процент этого правила — '
            'в том числе когда процент правила не задан.</i>'
        )

    # Предупреждение — по каждой стороне отдельно. Общее условие через `and`
    # молчало при половинчатой настройке: тариф выбран пригласившему, а дни
    # приглашённому всё равно теряются у всех, кто без подписки.
    warnings = []
    if days_on and level.referrer_days and not level.referrer_tariff_id:
        warnings.append('пригласившему')
    if days_on and level.referee_days and not level.referee_tariff_id:
        warnings.append('приглашённому')

    if warnings:
        lines.append('')
        lines.append(
            f'<i>⚠️ Тариф не выбран для дней {" и ".join(warnings)}: они лягут в основную '
            'подписку получателя, а если подписки нет — не начислятся вовсе.</i>'
        )

    # В классическом режиме у подписок нет тарифа: правило с тарифом не найдёт
    # подходящую подписку и не начислит НИЧЕГО. Это не поломка движка, а
    # несовместимая настройка, и сказать о ней надо там, где её задают.
    if days_on and not settings.is_multi_tariff_enabled() and (level.referrer_tariff_id or level.referee_tariff_id):
        lines.append('')
        lines.append(
            '<i>❗️ Мультитариф выключен: у подписок нет тарифа, и дни с выбранным '
            'тарифом не начислятся. Уберите тариф — дни пойдут в основную подписку.</i>'
        )

    if (
        days_on
        and level.trigger == ReferralRewardTrigger.REGISTRATION.value
        and level.referee_days
        and not level.referee_tariff_id
    ):
        lines.append(
            '<i>❗️ При поводе «за регистрацию» у приглашённого подписки ещё нет: '
            'без тарифа дни не начислятся никому и никогда.</i>'
        )

    prefix = f'admin_ref_lvl_edit:{level.level}'
    rows = [
        [
            types.InlineKeyboardButton(
                text='⛔️ Выключить' if level.is_active else '✅ Включить',
                callback_data=f'admin_ref_lvl_active:{level.level}',
            )
        ],
        [types.InlineKeyboardButton(text='🎁 Активные бонусы', callback_data=f'admin_ref_lvl_mode:{level.level}')],
        [types.InlineKeyboardButton(text='⚡️ Повод начисления', callback_data=f'admin_ref_lvl_trigger:{level.level}')],
    ]

    if money_on:
        rows.append([types.InlineKeyboardButton(text='％ Процент', callback_data=f'{prefix}:referrer_percent')])
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='💰 Фикс. пригласившему', callback_data=f'{prefix}:referrer_fixed_kopeks'
                ),
                types.InlineKeyboardButton(
                    text='🎁 Фикс. приглашённому', callback_data=f'{prefix}:referee_fixed_kopeks'
                ),
            ]
        )
    if days_on:
        rows.append(
            [
                types.InlineKeyboardButton(text='📅 Дни пригласившему', callback_data=f'{prefix}:referrer_days'),
                types.InlineKeyboardButton(text='📅 Дни приглашённому', callback_data=f'{prefix}:referee_days'),
            ]
        )
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='🎯 Тариф пригласившему', callback_data=f'admin_ref_lvl_tariff:{level.level}:referrer'
                ),
                types.InlineKeyboardButton(
                    text='🎯 Тариф приглашённому', callback_data=f'admin_ref_lvl_tariff:{level.level}:referee'
                ),
            ]
        )

    rows.append([types.InlineKeyboardButton(text='🔢 Лимит комиссий', callback_data=f'{prefix}:max_payments')])
    rows.append(
        [
            types.InlineKeyboardButton(text='🎖 Рефералов для открытия', callback_data=f'{prefix}:required_referrals'),
            types.InlineKeyboardButton(
                text='👥 Считать: '
                + ('с пополнением' if getattr(level, 'required_referrals_active_only', True) else 'всех'),
                callback_data=f'admin_ref_lvl_countmode:{level.level}',
            ),
        ]
    )
    rows.append(
        [types.InlineKeyboardButton(text='🗑 Удалить уровень', callback_data=f'admin_ref_lvl_delask:{level.level}')]
    )
    rows.append([types.InlineKeyboardButton(text='⬅️ К уровням', callback_data='admin_ref_levels')])

    await callback.message.edit_text('\n'.join(lines), reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows))
    return True


@admin_required
@error_handler
async def show_reward_level(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    await _cancel_pending_input(state)
    level_number = int(callback.data.split(':')[1])
    if not await _render_level(callback, db, level_number):
        await callback.answer('Уровень не найден', show_alert=True)
        return
    await callback.answer()


@admin_required
@error_handler
async def toggle_level_active(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    await _cancel_pending_input(state)
    level_number = int(callback.data.split(':')[1])
    level = await get_reward_level(db, level_number)
    if level is None:
        await callback.answer('Уровень не найден', show_alert=True)
        return

    # Новое состояние вычисляется ДО записи. upsert правит тот же ORM-объект, и
    # чтение level.is_active после него возвращает уже новое значение — тост
    # сообщал ровно противоположное тому, что произошло.
    now_active = not level.is_active
    await upsert_reward_level(db, level_number, is_active=now_active)
    await callback.answer('Уровень включён' if now_active else 'Уровень выключен')
    await _render_level(callback, db, level_number)


@admin_required
@error_handler
async def cycle_level_mode(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    """Перебрать активные бонусы уровня: деньги → дни → оба."""
    await _cancel_pending_input(state)
    level_number = int(callback.data.split(':')[1])
    level = await get_reward_level(db, level_number)
    if level is None:
        await callback.answer('Уровень не найден', show_alert=True)
        return

    current_index = _MODE_CYCLE.index(level.reward_mode) if level.reward_mode in _MODE_CYCLE else 0
    new_mode = _MODE_CYCLE[(current_index + 1) % len(_MODE_CYCLE)]
    await upsert_reward_level(db, level_number, reward_mode=new_mode)
    await callback.answer(_MODE_LABELS[new_mode])
    await _render_level(callback, db, level_number)


@admin_required
@error_handler
async def cycle_level_trigger(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    await _cancel_pending_input(state)
    level_number = int(callback.data.split(':')[1])
    level = await get_reward_level(db, level_number)
    if level is None:
        await callback.answer('Уровень не найден', show_alert=True)
        return

    current_index = _TRIGGER_CYCLE.index(level.trigger) if level.trigger in _TRIGGER_CYCLE else 0
    new_trigger = _TRIGGER_CYCLE[(current_index + 1) % len(_TRIGGER_CYCLE)]
    await upsert_reward_level(db, level_number, trigger=new_trigger)
    await callback.answer(_TRIGGER_LABELS[new_trigger])
    await _render_level(callback, db, level_number)


@admin_required
@error_handler
async def confirm_delete_level(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    """Спросить перед удалением.

    Правило уровня собирают руками, и восстановить его можно только заново набрав
    все поля. Удаление с одного касания, рядом с остальными кнопками карточки,
    слишком легко нажать мимо.
    """
    await _cancel_pending_input(state)
    level_number = int(callback.data.split(':')[1])

    await callback.message.edit_text(
        f'🗑 <b>Удалить уровень {level_number}?</b>\n\n'
        'Настройки правила будут потеряны: восстановить их можно только заново.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🗑 Да, удалить', callback_data=f'admin_ref_lvl_del:{level_number}')],
                [types.InlineKeyboardButton(text='⬅️ Отмена', callback_data=f'admin_ref_lvl:{level_number}')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_threshold_population(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    """Кого считать при проверке порога: всех приглашённых или только с пополнением.

    Разница не косметическая. Порог по всем регистрациям берётся накруткой пустых
    аккаунтов, и уровень открывается, не принеся программе ни рубля.
    """
    await _cancel_pending_input(state)
    level_number = int(callback.data.split(':')[1])
    level = await get_reward_level(db, level_number)
    if level is None:
        await callback.answer('Уровень не найден', show_alert=True)
        return

    active_only = not bool(getattr(level, 'required_referrals_active_only', True))
    await upsert_reward_level(db, level_number, required_referrals_active_only=active_only)
    await callback.answer('Считаем рефералов с пополнением' if active_only else 'Считаем всех приглашённых')
    await _render_level(callback, db, level_number)


@admin_required
@error_handler
async def delete_level(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None):
    await _cancel_pending_input(state)
    level_number = int(callback.data.split(':')[1])
    removed = await delete_reward_level(db, level_number)
    await callback.answer(f'Уровень {level_number} удалён' if removed else 'Уровень уже удалён')
    await _render_levels(callback, db)


@admin_required
@error_handler
async def choose_level_tariff(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    await _cancel_pending_input(state)
    _, level_raw, side = callback.data.split(':')
    level_number = int(level_raw)
    level = await get_reward_level(db, level_number)
    current_id = None
    if level is not None:
        current_id = level.referrer_tariff_id if side == 'referrer' else level.referee_tariff_id

    tariffs = list(await get_all_tariffs(db, include_inactive=False))

    # Уже назначенный тариф мог стать неактивным. Список активных его не вернёт, и
    # выбор выглядел бы как «тариф не выбран» — при том, что он выбран и работает.
    # Дописываем его отдельно и помечаем, иначе админ снял бы его не глядя.
    if current_id and all(tariff.id != current_id for tariff in tariffs):
        from app.database.crud.tariff import get_tariff_by_id

        assigned = await get_tariff_by_id(db, current_id)
        if assigned is not None:
            tariffs.insert(0, assigned)

    side_label = 'пригласившему' if side == 'referrer' else 'приглашённому'
    rows = [
        [
            types.InlineKeyboardButton(
                text=('✅ ' if not current_id else '') + '➖ Без тарифа (основная подписка)',
                callback_data=f'admin_ref_lvl_settariff:{level_number}:{side}:0',
            )
        ]
    ]

    shown = tariffs[:_TARIFF_PICKER_LIMIT]
    for tariff in shown:
        mark = '✅ ' if tariff.id == current_id else '🎯 '
        suffix = '' if tariff.is_active else ' (неактивен)'
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'{mark}{tariff.name}{suffix}',
                    callback_data=f'admin_ref_lvl_settariff:{level_number}:{side}:{tariff.id}',
                )
            ]
        )
    rows.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data=f'admin_ref_lvl:{level_number}')])

    text = (
        f'🎯 <b>Тариф для дней {side_label}</b>\n\n'
        'Дни лягут в подписку выбранного тарифа. Если такой подписки у получателя нет, '
        'она будет создана — но только когда у него нет живого триала.\n\n'
        '<i>Без тарифа дни идут в оплаченную подписку получателя; при нескольких '
        'выбирается с самым поздним сроком.</i>'
    )
    # Молчаливое обрезание списка означало бы «такого тарифа нет», хотя он есть.
    if len(tariffs) > len(shown):
        text += f'\n\n<i>⚠️ Показаны первые {len(shown)} из {len(tariffs)} тарифов.</i>'

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@admin_required
@error_handler
async def set_level_tariff(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    await _cancel_pending_input(state)
    _, level_raw, side, tariff_raw = callback.data.split(':')
    level_number = int(level_raw)
    tariff_id = int(tariff_raw) or None

    field = 'referrer_tariff_id' if side == 'referrer' else 'referee_tariff_id'
    await upsert_reward_level(db, level_number, **{field: tariff_id})
    await callback.answer('Тариф сохранён')
    await _render_level(callback, db, level_number)


@admin_required
@error_handler
async def start_level_value_edit(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    _, level_raw, field = callback.data.split(':')
    level_number = int(level_raw)
    label, unit, maximum = _NUMERIC_FIELDS[field]

    await state.update_data(referral_level=level_number, referral_field=field)
    await state.set_state(AdminStates.referral_level_value_input)

    hint = f'Введите значение ({unit}).'
    if maximum is not None:
        hint += f' Максимум: {maximum}.'
    if field in _MONEY_FIELDS:
        hint += ' Сумма в рублях, можно дробную.'

    await callback.message.edit_text(
        f'✏️ <b>{label}</b>\nУровень {level_number}\n\n{hint}\n\n0 — не начислять.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='⬅️ Отмена', callback_data=f'admin_ref_lvl:{level_number}')]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_level_value(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    level_number = data.get('referral_level')
    field = data.get('referral_field')
    if not level_number or field not in _NUMERIC_FIELDS:
        await state.clear()
        await message.answer('❌ Не понял, какое поле правим. Откройте уровень заново.')
        return

    label, unit, maximum = _NUMERIC_FIELDS[field]
    raw = (message.text or '').strip().replace(',', '.')

    try:
        parsed = float(raw)
    except ValueError:
        await message.answer(f'❌ Нужно число. {label} ({unit}).')
        return

    # float() принимает 'inf' и 'nan', проверка на отрицательность их пропускает,
    # а int() ниже падает OverflowError/ValueError. Обработчик при этом уходит с
    # ошибкой, НЕ сняв состояние: следующее произвольное сообщение админа
    # попадает сюда же и переписывает денежное поле.
    if not math.isfinite(parsed):
        await message.answer(f'❌ Нужно обычное число. {label} ({unit}).')
        return

    if parsed < 0:
        await message.answer('❌ Отрицательные значения недопустимы.')
        return

    # Деньги вводятся в рублях, а хранятся в копейках — как и везде в админке.
    value = int(round(parsed * 100)) if field in _MONEY_FIELDS else int(parsed)
    if maximum is not None and value > maximum:
        await message.answer(f'❌ Максимум: {maximum} {unit}.')
        return

    # Ноль в проценте и фиксированной сумме хранится как NULL: в расчёте NULL и 0
    # значат одно и то же — «не начисляется», и держать два представления одного
    # состояния значило бы однажды их спутать.
    if field in _MONEY_FIELDS or field == 'referrer_percent':
        stored = value or None
    else:
        stored = value

    await upsert_reward_level(db, level_number, **{field: stored})
    await state.clear()

    display = settings.format_price(value) if field in _MONEY_FIELDS else f'{value} {unit}'
    await message.answer(
        f'✅ {label}: {display}\n\nОткройте «Уровни наград», чтобы продолжить настройку.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🪜 К уровням', callback_data='admin_ref_levels')],
            ]
        ),
    )


@admin_required
@error_handler
async def toggle_levels_mode(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    """Переключить, что означает номер уровня: глубину цепочки или ранг партнёра.

    Отдельным действием, как и смена схемы: переключение меняет и получателей, и
    число сработавших правил на одном пополнении, то есть реальные выплаты живым
    людям. Побочным эффектом создания уровня такое быть не должно.
    """
    await _cancel_pending_input(state)
    if bot_configuration_service.is_env_locked('REFERRAL_LEVELS_MODE'):
        await callback.answer(
            'REFERRAL_LEVELS_MODE задан в .env и не меняется из админки. Уберите строку из .env и перезапустите бота.',
            show_alert=True,
        )
        return

    to_tiers = settings.get_referral_levels_mode() != LEVELS_MODE_TIERS
    new_value = LEVELS_MODE_TIERS if to_tiers else LEVELS_MODE_CHAIN
    await bot_configuration_service.set_value(db, 'REFERRAL_LEVELS_MODE', new_value)

    if to_tiers:
        levels = await get_all_reward_levels(db, only_active=True)
        # Все подходящие сразу: у лестницы, перенесённой из цепочки, обычно и
        # стартовой ступени нет, и пороги совпадают — показанное поодиночке
        # выглядит как единственная проблема.
        warnings = _tier_ladder_warnings(levels)
        if warnings:
            # Сами предупреждения печатает _render_levels ниже — целиком и без
            # обрезки. В окне только счёт, чтобы админ понял, куда смотреть.
            await _answer_capped(
                callback,
                f'Режим: уровни за приглашённых. Внимание: предупреждений к лестнице — {len(warnings)}, '
                'смотрите экран.',
                show_alert=True,
            )
        else:
            await _answer_capped(
                callback,
                'Режим: уровни за приглашённых. Платят только прямому пригласившему, применяется один уровень.',
                show_alert=True,
            )
    else:
        await callback.answer(
            f'Режим: уровни по цепочке. Обход до {settings.get_referral_max_level_depth()} уровней вверх.',
            show_alert=True,
        )

    await _render_levels(callback, db)


@admin_required
@error_handler
async def toggle_user_choice(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext | None = None
):
    """Разрешить или запретить пользователю выбирать вид награды и подписку для дней.

    Обе настройки только РАЗРЕШАЮТ выбор, но не делают его за человека: пока они
    выключены, сохранённые предпочтения игнорируются целиком, а начисления идут
    ровно так, как настроено правилом.
    """
    await _cancel_pending_input(state)
    key = (
        'REFERRAL_ALLOW_REWARD_KIND_CHOICE'
        if callback.data == 'admin_ref_lvl_kindchoice'
        else 'REFERRAL_ALLOW_DAYS_TARGET_CHOICE'
    )

    if bot_configuration_service.is_env_locked(key):
        await _answer_capped(
            callback,
            f'{key} задан в .env и не меняется из админки. Уберите строку из .env и перезапустите бота.',
            show_alert=True,
        )
        return

    new_value = not bool(getattr(settings, key))
    await bot_configuration_service.set_value(db, key, new_value)

    label = 'Выбор вида награды' if key == 'REFERRAL_ALLOW_REWARD_KIND_CHOICE' else 'Выбор подписки для дней'
    await _answer_capped(callback, f'{label}: {"разрешён" if new_value else "запрещён"} пользователю.', show_alert=True)
    await _render_levels(callback, db)


@admin_required
@error_handler
async def start_depth_edit(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Правка глубины обхода цепочки.

    Настройка живёт в общем списке конфигурации, и добраться до неё из редактора
    было нельзя: уровни глубже неё помечались как неплатящие, а способа поднять
    предел экран не давал — со стороны это выглядело как «уровни выше третьего
    просто не работают».
    """
    if settings.is_referral_tier_levels():
        # Правку не открываем вовсе: в режиме рангов цепочки нет, и сохранённое
        # здесь число ни на что не повлияет. Форма, которая принимает значение и
        # ничего не меняет, хуже отсутствующей кнопки.
        await callback.answer(
            'В режиме «за приглашённых» цепочка не обходится — глубина не применяется. '
            'Переключите режим на «по цепочке», чтобы её настроить.',
            show_alert=True,
        )
        return

    if bot_configuration_service.is_env_locked('REFERRAL_MAX_LEVEL_DEPTH'):
        await callback.answer(
            'REFERRAL_MAX_LEVEL_DEPTH задан в .env и не меняется из админки. '
            'Уберите строку из .env и перезапустите бота.',
            show_alert=True,
        )
        return

    await state.set_state(AdminStates.referral_depth_input)
    await callback.message.edit_text(
        f'📏 <b>Глубина реферальной цепочки</b>\n\n'
        f'Сейчас: {settings.get_referral_max_level_depth()}\n\n'
        f'Сколько звеньев вверх обходить при начислении. Уровень 1 — тот, кто пригласил '
        f'напрямую; уровень 2 — пригласивший его, и так далее. Правила глубже этого числа '
        f'не начисляют ничего.\n\n'
        f'Введите число от 1 до {MAX_SUPPORTED_LEVEL}.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Отмена', callback_data='admin_ref_levels')]]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_depth_value(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    raw = (message.text or '').strip()
    try:
        depth = int(raw)
    except ValueError:
        await message.answer(f'❌ Нужно целое число от 1 до {MAX_SUPPORTED_LEVEL}.')
        return

    if depth < 1 or depth > MAX_SUPPORTED_LEVEL:
        await message.answer(f'❌ Допустимо от 1 до {MAX_SUPPORTED_LEVEL}.')
        return

    await bot_configuration_service.set_value(db, 'REFERRAL_MAX_LEVEL_DEPTH', depth)
    await state.clear()
    await message.answer(
        f'✅ Глубина цепочки: {depth}\n\nПравила уровней до {depth} включительно теперь начисляют награды.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='🪜 К уровням', callback_data='admin_ref_levels')]]
        ),
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_reward_levels, F.data == 'admin_ref_levels')
    dp.callback_query.register(toggle_reward_scheme, F.data == 'admin_ref_lvl_scheme')
    dp.callback_query.register(add_reward_level, F.data == 'admin_ref_lvl_add')
    dp.callback_query.register(import_legacy_settings, F.data == 'admin_ref_lvl_import')
    dp.callback_query.register(start_depth_edit, F.data == 'admin_ref_lvl_depth')
    # Точное сравнение, а не префикс: 'admin_ref_lvl_mode:' уже занято сменой
    # набора бонусов уровня, и второй похожий префикс путал бы маршрутизацию.
    dp.callback_query.register(toggle_levels_mode, F.data == 'admin_ref_lvl_tiers')
    dp.callback_query.register(toggle_user_choice, F.data == 'admin_ref_lvl_kindchoice')
    dp.callback_query.register(toggle_user_choice, F.data == 'admin_ref_lvl_targetchoice')
    # Двоеточие в 'admin_ref_lvl:' обязательно: без него префикс поглотил бы все
    # соседние строки, и любая кнопка уровня открывала бы его карточку. Порядок
    # регистрации при таком разделителе значения не имеет — маршрутизацию
    # целиком проверяет TestCallbackRouting.
    dp.callback_query.register(toggle_level_active, F.data.startswith('admin_ref_lvl_active:'))
    dp.callback_query.register(cycle_level_mode, F.data.startswith('admin_ref_lvl_mode:'))
    dp.callback_query.register(cycle_level_trigger, F.data.startswith('admin_ref_lvl_trigger:'))
    dp.callback_query.register(toggle_threshold_population, F.data.startswith('admin_ref_lvl_countmode:'))
    # Более длинный префикс регистрируется раньше: 'admin_ref_lvl_del:' —
    # начало строки 'admin_ref_lvl_delask:', и порядок здесь важен.
    dp.callback_query.register(confirm_delete_level, F.data.startswith('admin_ref_lvl_delask:'))
    dp.callback_query.register(delete_level, F.data.startswith('admin_ref_lvl_del:'))
    dp.callback_query.register(set_level_tariff, F.data.startswith('admin_ref_lvl_settariff:'))
    dp.callback_query.register(choose_level_tariff, F.data.startswith('admin_ref_lvl_tariff:'))
    dp.callback_query.register(start_level_value_edit, F.data.startswith('admin_ref_lvl_edit:'))
    dp.callback_query.register(show_reward_level, F.data.startswith('admin_ref_lvl:'))
    dp.message.register(process_level_value, AdminStates.referral_level_value_input)
    dp.message.register(process_depth_value, AdminStates.referral_depth_input)
