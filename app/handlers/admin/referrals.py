import asyncio
import html
import json
from datetime import UTC, datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ENV_OVERRIDE_KEYS, settings
from app.database.crud.referral import (
    get_referral_statistics,
    get_top_referrers_by_period,
)
from app.database.crud.user import get_user_by_id, get_user_by_telegram_id
from app.database.models import ReferralEarning, User, WithdrawalRequest, WithdrawalRequestStatus
from app.keyboards.withdrawal import get_withdrawal_request_keyboard
from app.localization.texts import get_texts
from app.services.referral_withdrawal_service import localize_withdrawal_detail, referral_withdrawal_service
from app.states import AdminStates
from app.utils.chat_scope import callback_from_group
from app.utils.decorators import admin_required, error_handler
from app.utils.timezone import format_local_datetime, local_day_bounds, local_day_start


logger = structlog.get_logger(__name__)


from app.services.referral_reward_service import format_reward_total as _paid_line


_DIAGNOSTIC_ERROR_KEYS = {
    'Пользователь не найден в БД': 'ADMIN_REFERRALS_DIAGNOSTIC_USER_NOT_FOUND',
    'Реферер не найден': 'ADMIN_REFERRALS_DIAGNOSTIC_REFERRER_NOT_FOUND',
    'Бонусы уже начислены ранее': 'ADMIN_REFERRALS_DIAGNOSTIC_ALREADY_CREDITED',
    'Пользователь не найден': 'ADMIN_WITHDRAWAL_DETAIL_USER_NOT_FOUND',
}


def _localize_diagnostic_error(error: object, language: str) -> str:
    value = str(error)
    if key := _DIAGNOSTIC_ERROR_KEYS.get(value):
        return get_texts(language).t(key, value)
    return value


def _levels_breakdown_block(by_level: list[dict], language: str = 'ru') -> str:
    """Разбивка по уровням — главная новая величина многоуровневой схемы.

    Без неё админ видит общий итог и не понимает, какую его часть создаёт глубина
    цепочки, то есть не может судить, окупается ли она.
    """
    meaningful = [row for row in by_level if row.get('money_kopeks') or row.get('days')]
    if len(meaningful) < 2:
        # Единственный уровень — это обычная одноуровневая программа, и блок
        # только зашумил бы экран.
        return ''

    lines = [get_texts(language).t('ADMIN_REFERRALS_UI_006', '\n<b>По уровням:</b>')]
    for row in meaningful:
        lines.append(
            get_texts(language)
            .t('ADMIN_REFERRALS_UI_042', '- Уровень {v0}: {v1}')
            .format(v0=row['level'], v1=_paid_line(row.get('money_kopeks', 0), row.get('days', 0), language=language))
        )
    return '\n'.join(lines) + '\n'


async def _program_rules_block(db: AsyncSession, language: str = 'ru') -> str:
    """Действующие правила программы.

    В многоуровневой схеме печатать легаси-ключи нельзя: они не управляют ни одним
    начислением, и админ читал бы суммы, которых бот не платит. Описание берётся из
    того же источника, что и расчёт.
    """
    if not settings.is_referral_levels_scheme():
        return (
            get_texts(language)
            .t(
                'ADMIN_REFERRALS_UI_007',
                '<b>Настройки реферальной системы:</b>\n- Минимальное пополнение: {v0}\n- Бонус за первое пополнение: {v1}\n- Бонус пригласившему: {v2}\n- Комиссия с покупок: {v3}%',
            )
            .format(
                v0=settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
                v1=settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
                v2=settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS),
                v3=settings.REFERRAL_COMMISSION_PERCENT,
            )
        )

    from app.services.referral_reward_service import describe_active_levels, describe_referee_bonus

    tariff_names = await _reward_tariff_names(db)
    tier_mode = settings.is_referral_tier_levels()
    header = (
        get_texts(language).t('ADMIN_REFERRALS_UI_008', 'уровни за приглашённых')
        if tier_mode
        else get_texts(language).t('ADMIN_REFERRALS_UI_009', 'уровни по цепочке')
    )
    lines = [
        get_texts(language)
        .t('ADMIN_REFERRALS_UI_010', '<b>Правила программы (многоуровневая схема, {v0}):</b>')
        .format(v0=header)
    ]
    level_lines = await describe_active_levels(db, tariff_names=tariff_names, language=language)
    if level_lines:
        lines.extend(f'- {line}' for line in level_lines)
    else:
        lines.append(
            get_texts(language).t('ADMIN_REFERRALS_UI_043', '- ⚠️ Ни один уровень не настроен — награды не начисляются')
        )

    referee_bonus = await describe_referee_bonus(db, tariff_names=tariff_names, language=language)
    if referee_bonus:
        lines.append(get_texts(language).t('ADMIN_REFERRALS_UI_044', '- Приглашённому: {v0}').format(v0=referee_bonus))
    if tier_mode:
        # Про глубину здесь писать нечего: цепочка не обходится. Зато admin
        # обязан видеть главное отличие режима — платят одному и по одному правилу.
        lines.append(
            get_texts(language).t(
                'ADMIN_REFERRALS_UI_045',
                '- Платят только прямому пригласившему, применяется один уровень — старший из достигнутых',
            )
        )
    else:
        lines.append(
            get_texts(language)
            .t('ADMIN_REFERRALS_UI_046', '- Глубина цепочки: до {v0} уровней')
            .format(v0=settings.get_referral_max_level_depth())
        )
    return '\n'.join(lines)


async def _reward_tariff_names(db: AsyncSession) -> dict[int, str]:
    """Названия тарифов, на которые ссылаются уровни.

    Без них описание обещает «7 дн. подписки», умалчивая, в какой тариф они лягут,
    — а это ровно то, что настраивает админ.
    """
    from sqlalchemy import select as _select

    from app.database.models import Tariff
    from app.services.referral_reward_service import ReferralRewardLevelService

    configs = await ReferralRewardLevelService.get_all(db)
    ids = {cfg.referrer_tariff_id for cfg in configs.values() if cfg.referrer_tariff_id}
    ids |= {cfg.referee_tariff_id for cfg in configs.values() if cfg.referee_tariff_id}
    if not ids:
        return {}

    result = await db.execute(_select(Tariff.id, Tariff.name).where(Tariff.id.in_(ids)))
    return {row.id: row.name for row in result.all()}


@admin_required
@error_handler
async def show_referral_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        stats = await get_referral_statistics(db)

        avg_per_referrer = 0
        if stats.get('active_referrers', 0) > 0:
            avg_per_referrer = stats.get('total_paid_kopeks', 0) / stats['active_referrers']

        current_time = format_local_datetime(datetime.now(UTC), '%H:%M:%S')

        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_REFERRALS_UI_011',
                '\n🤝 <b>Реферальная статистика</b>\n\n<b>Общие показатели:</b>\n- Пользователей с рефералами: {v0}\n- Активных рефереров: {v1}\n- Выплачено всего: {v2}\n\n<b>За период:</b>\n- Сегодня: {v3}\n- За неделю: {v4}\n- За месяц: {v5}\n\n<b>Средние показатели:</b>\n- На одного реферера: {v6}\n',
            )
            .format(
                v0=stats.get('users_with_referrals', 0),
                v1=stats.get('active_referrers', 0),
                v2=_paid_line(
                    stats.get('total_paid_kopeks', 0), stats.get('total_paid_days', 0), language=db_user.language
                ),
                v3=_paid_line(
                    stats.get('today_earnings_kopeks', 0),
                    stats.get('today_earnings_days', 0),
                    language=db_user.language,
                ),
                v4=_paid_line(
                    stats.get('week_earnings_kopeks', 0), stats.get('week_earnings_days', 0), language=db_user.language
                ),
                v5=_paid_line(
                    stats.get('month_earnings_kopeks', 0),
                    stats.get('month_earnings_days', 0),
                    language=db_user.language,
                ),
                v6=settings.format_price(int(avg_per_referrer)),
            )
        )

        text += _levels_breakdown_block(stats.get('by_level') or [], language=db_user.language)
        text += get_texts(db_user.language).t('ADMIN_REFERRALS_UI_012', '\n<b>Топ-5 рефереров:</b>\n')

        top_referrers = stats.get('top_referrers', [])
        if top_referrers:
            for i, referrer in enumerate(top_referrers[:5], 1):
                earned = referrer.get('total_earned_kopeks', 0)
                days = referrer.get('total_earned_days', 0)
                count = referrer.get('referrals_count', 0)
                user_id = referrer.get('user_id', 'N/A')

                if count > 0:
                    text += (
                        get_texts(db_user.language)
                        .t('ADMIN_REFERRALS_UI_103', '{v0}. ID {v1}: {v2} ({v3} реф.)\n')
                        .format(v0=i, v1=user_id, v2=_paid_line(earned, days, language=db_user.language), v3=count)
                    )
                else:
                    logger.warning('Реферер имеет рефералов, но есть в топе', user_id=user_id, count=count)
        else:
            text += get_texts(db_user.language).t('ADMIN_REFERRALS_UI_047', 'Нет данных\n')

        text += f'\n{await _program_rules_block(db, language=db_user.language)}'
        text += (
            get_texts(db_user.language)
            .t('ADMIN_REFERRALS_UI_013', '\n- Уведомления: {v0}\n\n<i>🕐 Обновлено: {v1}</i>\n')
            .format(
                v0=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_074', '✅ Включены')
                if settings.REFERRAL_NOTIFICATIONS_ENABLED
                else get_texts(db_user.language).t('ADMIN_REFERRALS_UI_075', '❌ Отключены'),
                v1=current_time,
            )
        )

        keyboard_rows = [
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_131', '🔄 Обновить'),
                    callback_data='admin_referrals',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_132', '👥 Топ рефереров'),
                    callback_data='admin_referrals_top',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_133', '🔍 Диагностика логов'),
                    callback_data='admin_referral_diagnostics',
                )
            ],
        ]

        # Кнопка заявок на вывод (если функция включена)
        if settings.is_referral_withdrawal_enabled():
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_145', '💸 Заявки на вывод'),
                        callback_data='admin_withdrawal_requests',
                    )
                ]
            )

        keyboard_rows.extend(
            [
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_146', '⚙️ Настройки'),
                        callback_data='admin_referrals_settings',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_109', '⬅️ Назад'),
                        callback_data='admin_panel',
                    )
                ],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_104', 'Обновлено'))
        except Exception as edit_error:
            if 'message is not modified' in str(edit_error):
                await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_147', 'Данные актуальны'))
            else:
                logger.error('Ошибка редактирования сообщения', edit_error=edit_error)
                await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_148', 'Ошибка обновления'))

    except Exception as e:
        logger.error('Ошибка в show_referral_statistics', error=e, exc_info=True)

        current_time = format_local_datetime(datetime.now(UTC), '%H:%M:%S')
        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_REFERRALS_UI_048',
                '\n🤝 <b>Реферальная статистика</b>\n\n❌ <b>Ошибка загрузки данных</b>\n\n<b>Текущие настройки:</b>\n- Минимальное пополнение: {v0}\n- Бонус за первое пополнение: {v1}\n- Бонус пригласившему: {v2}\n- Комиссия с покупок: {v3}%\n\n<i>🕐 Время: {v4}</i>\n',
            )
            .format(
                v0=settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
                v1=settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
                v2=settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS),
                v3=settings.REFERRAL_COMMISSION_PERCENT,
                v4=current_time,
            )
        )

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_157', '🔄 Повторить'),
                        callback_data='admin_referrals',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_109', '⬅️ Назад'),
                        callback_data='admin_panel',
                    )
                ],
            ]
        )

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except:
            pass
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_105', 'Произошла ошибка при загрузке статистики')
        )


def _get_top_keyboard(period: str, sort_by: str, language: str = 'ru') -> types.InlineKeyboardMarkup:
    """Создаёт клавиатуру для выбора периода и сортировки."""
    period_week = (
        get_texts(language).t('ADMIN_REFERRALS_UI_014', '✅ Неделя')
        if period == 'week'
        else get_texts(language).t('ADMIN_REFERRALS_UI_015', 'Неделя')
    )
    period_month = (
        get_texts(language).t('ADMIN_REFERRALS_UI_016', '✅ Месяц')
        if period == 'month'
        else get_texts(language).t('ADMIN_REFERRALS_UI_017', 'Месяц')
    )
    sort_earnings = (
        get_texts(language).t('ADMIN_REFERRALS_UI_018', '✅ По заработку')
        if sort_by == 'earnings'
        else get_texts(language).t('ADMIN_REFERRALS_UI_019', 'По заработку')
    )
    sort_invited = (
        get_texts(language).t('ADMIN_REFERRALS_UI_020', '✅ По приглашённым')
        if sort_by == 'invited'
        else get_texts(language).t('ADMIN_REFERRALS_UI_021', 'По приглашённым')
    )

    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text=period_week, callback_data=f'admin_top_ref:week:{sort_by}'),
                types.InlineKeyboardButton(text=period_month, callback_data=f'admin_top_ref:month:{sort_by}'),
            ],
            [
                types.InlineKeyboardButton(text=sort_earnings, callback_data=f'admin_top_ref:{period}:earnings'),
                types.InlineKeyboardButton(text=sort_invited, callback_data=f'admin_top_ref:{period}:invited'),
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_REFERRALS_UI_131', '🔄 Обновить'),
                    callback_data=f'admin_top_ref:{period}:{sort_by}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_REFERRALS_UI_143', '⬅️ К статистике'),
                    callback_data='admin_referrals',
                )
            ],
        ]
    )


@admin_required
@error_handler
async def show_top_referrers(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает топ рефереров (по умолчанию: неделя, по заработку)."""
    await _show_top_referrers_filtered(callback, db, period='week', sort_by='earnings', language=db_user.language)


@admin_required
@error_handler
async def show_top_referrers_filtered(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Обрабатывает выбор периода и сортировки."""
    # Парсим callback_data: admin_top_ref:period:sort_by
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_073', 'Ошибка параметров'))
        return

    period = parts[1]  # week или month
    sort_by = parts[2]  # earnings или invited

    if period not in ('week', 'month'):
        period = 'week'
    if sort_by not in ('earnings', 'invited'):
        sort_by = 'earnings'

    await _show_top_referrers_filtered(callback, db, period, sort_by, language=db_user.language)


async def _show_top_referrers_filtered(
    callback: types.CallbackQuery, db: AsyncSession, period: str, sort_by: str, language: str = 'ru'
):
    """Внутренняя функция отображения топа с фильтрами."""
    try:
        top_referrers = await get_top_referrers_by_period(db, period=period, sort_by=sort_by)

        period_text = (
            get_texts(language).t('ADMIN_REFERRALS_UI_049', 'за неделю')
            if period == 'week'
            else get_texts(language).t('ADMIN_REFERRALS_UI_050', 'за месяц')
        )
        sort_text = (
            get_texts(language).t('ADMIN_REFERRALS_UI_051', 'по заработку')
            if sort_by == 'earnings'
            else get_texts(language).t('ADMIN_REFERRALS_UI_052', 'по приглашённым')
        )

        text = get_texts(language).t('ADMIN_REFERRALS_UI_022', '🏆 <b>Топ рефереров {v0}</b>\n').format(v0=period_text)
        text += get_texts(language).t('ADMIN_REFERRALS_UI_023', '<i>Сортировка: {v0}</i>\n\n').format(v0=sort_text)

        if top_referrers:
            for i, referrer in enumerate(top_referrers[:20], 1):
                earned = _paid_line(
                    referrer.get('earnings_kopeks', 0), referrer.get('earnings_days', 0), language=language
                )
                count = referrer.get('invited_count', 0)
                display_name = referrer.get('display_name', 'N/A')
                username = referrer.get('username', '')
                telegram_id = referrer.get('telegram_id')
                user_email = referrer.get('email', '')
                user_id = referrer.get('user_id', '')
                id_display = telegram_id or user_email or f'#{user_id}' if user_id else 'N/A'

                if username:
                    display_text = f'@{html.escape(username)} (ID{id_display})'
                elif display_name and display_name != f'ID{id_display}':
                    display_text = f'{html.escape(display_name)} (ID{id_display})'
                else:
                    display_text = f'ID{id_display}'

                emoji = ''
                if i == 1:
                    emoji = '🥇 '
                elif i == 2:
                    emoji = '🥈 '
                elif i == 3:
                    emoji = '🥉 '

                # Выделяем основную метрику в зависимости от сортировки
                if sort_by == 'invited':
                    text += f'{emoji}{i}. {display_text}\n'
                    text += (
                        get_texts(language)
                        .t('ADMIN_REFERRALS_UI_106', '   👥 <b>{v0} приглашённых</b> | 💰 {v1}\n\n')
                        .format(v0=count, v1=earned)
                    )
                else:
                    text += f'{emoji}{i}. {display_text}\n'
                    text += (
                        get_texts(language)
                        .t('ADMIN_REFERRALS_UI_107', '   💰 <b>{v0}</b> | 👥 {v1} приглашённых\n\n')
                        .format(v0=earned, v1=count)
                    )
        else:
            text += get_texts(language).t('ADMIN_REFERRALS_UI_053', 'Нет данных за выбранный период\n')

        keyboard = _get_top_keyboard(period, sort_by, language=language)

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer()
        except Exception as edit_error:
            if 'message is not modified' in str(edit_error):
                await callback.answer(get_texts(language).t('ADMIN_REFERRALS_UI_147', 'Данные актуальны'))
            else:
                raise

    except Exception as e:
        logger.error('Ошибка в show_top_referrers_filtered', error=e, exc_info=True)
        await callback.answer(get_texts(language).t('ADMIN_REFERRALS_UI_108', 'Ошибка загрузки топа рефереров'))


def _settings_hint(language: str = 'ru') -> str:
    """Подсказка о том, ГДЕ настройка реально меняется.

    Реферальные ключи редактируются из админки и кабинета — но только если они не
    заданы в .env: такой ключ попадает в ENV_OVERRIDE_KEYS, и запись из UI ложится
    в БД, не применяясь к настройкам. Прежний текст звал править .env всегда, из-за
    чего залоченность выглядела нормой, а не следствием конфигурации.
    """
    locked = sorted(key for key in ENV_OVERRIDE_KEYS if key.startswith('REFERRAL_'))
    if not locked:
        return get_texts(language).t(
            'ADMIN_REFERRALS_UI_024', '<i>💡 Настройки меняются здесь и в кабинете, раздел «Реферальная программа».</i>'
        )
    listed = ', '.join(locked[:5]) + ('…' if len(locked) > 5 else '')
    return (
        get_texts(language)
        .t(
            'ADMIN_REFERRALS_UI_000',
            '<i>⚠️ Часть настроек задана в .env и поэтому не меняется из админки: {v0}. Уберите эти строки из .env и перезапустите бота, чтобы управлять программой отсюда.</i>',
        )
        .format(v0=listed)
    )


@admin_required
@error_handler
async def show_referral_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    if settings.is_referral_levels_scheme():
        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_REFERRALS_UI_025',
                '\n⚙️ <b>Настройки реферальной системы</b>\n\n{v0}\n\n<b>Уведомления:</b>\n• Статус: {v1}\n\n{v2}\n',
            )
            .format(
                v0=await _program_rules_block(db, language=db_user.language),
                v1=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_074', '✅ Включены')
                if settings.REFERRAL_NOTIFICATIONS_ENABLED
                else get_texts(db_user.language).t('ADMIN_REFERRALS_UI_075', '❌ Отключены'),
                v2=_settings_hint(language=db_user.language),
            )
        )
        await callback.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_149', '🪜 Уровни наград'),
                            callback_data='admin_ref_levels',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_143', '⬅️ К статистике'),
                            callback_data='admin_referrals',
                        )
                    ],
                ]
            ),
        )
        await callback.answer()
        return

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_REFERRALS_UI_001',
            '\n⚙️ <b>Настройки реферальной системы</b>\n\n<b>Бонусы и награды:</b>\n• Минимальная сумма пополнения для участия: {v0}\n• Бонус за первое пополнение реферала: {v1}\n• Бонус пригласившему за первое пополнение: {v2}\n\n<b>Комиссионные:</b>\n• Процент с каждой покупки реферала: {v3}%\n\n<b>Уведомления:</b>\n• Статус: {v4}\n\n{v6}\n',
        )
        .format(
            v0=settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
            v1=settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
            v2=settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS),
            v3=settings.REFERRAL_COMMISSION_PERCENT,
            v4=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_074', '✅ Включены')
            if settings.REFERRAL_NOTIFICATIONS_ENABLED
            else get_texts(db_user.language).t('ADMIN_REFERRALS_UI_075', '❌ Отключены'),
            v6=_settings_hint(language=db_user.language),
        )
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_149', '🪜 Уровни наград'),
                    callback_data='admin_ref_levels',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_143', '⬅️ К статистике'),
                    callback_data='admin_referrals',
                )
            ],
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_pending_withdrawal_requests(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает список ожидающих заявок на вывод."""
    requests = await referral_withdrawal_service.get_pending_requests(db)

    if not requests:
        text = get_texts(db_user.language).t(
            'ADMIN_REFERRALS_UI_026', '📋 <b>Заявки на вывод</b>\n\nНет ожидающих заявок.'
        )

        keyboard_rows = []
        # Кнопка тестового начисления (только в тестовом режиме)
        if settings.REFERRAL_WITHDRAWAL_TEST_MODE:
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_134', '🧪 Тестовое начисление'),
                        callback_data='admin_test_referral_earning',
                    )
                ]
            )
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_109', '⬅️ Назад'),
                    callback_data='admin_referrals',
                )
            ]
        )

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
        await callback.answer()
        return

    text = (
        get_texts(db_user.language)
        .t('ADMIN_REFERRALS_UI_002', '📋 <b>Заявки на вывод ({v0})</b>\n\n')
        .format(v0=len(requests))
    )

    for req in requests[:10]:
        user = await get_user_by_id(db, req.user_id)
        user_name = (
            html.escape(user.full_name)
            if user and user.full_name
            else get_texts(db_user.language).t('ADMIN_REFERRALS_UI_028', 'Неизвестно')
        )
        user_tg_id = user.telegram_id if user else 'N/A'

        risk_emoji = (
            '🟢' if req.risk_score < 30 else '🟡' if req.risk_score < 50 else '🟠' if req.risk_score < 70 else '🔴'
        )

        text += f'<b>#{req.id}</b> — {user_name} (ID{user_tg_id})\n'
        text += (
            get_texts(db_user.language)
            .t('ADMIN_REFERRALS_UI_027', '💰 {v0:.0f}₽ | {v1} Риск: {v2}/100\n')
            .format(v0=req.amount_kopeks / 100, v1=risk_emoji, v2=req.risk_score)
        )
        text += f'📅 {format_local_datetime(req.created_at, "%d.%m.%Y %H:%M")}\n\n'

    keyboard_rows = []
    for req in requests[:5]:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'#{req.id} — {req.amount_kopeks / 100:.0f}₽', callback_data=f'admin_withdrawal_view_{req.id}'
                )
            ]
        )

    # Кнопка тестового начисления (только в тестовом режиме)
    if settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_134', '🧪 Тестовое начисление'),
                    callback_data='admin_test_referral_earning',
                )
            ]
        )

    keyboard_rows.append(
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_109', '⬅️ Назад'), callback_data='admin_referrals'
            )
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
    await callback.answer()


@admin_required
@error_handler
async def view_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает детали заявки на вывод."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_076', 'Заявка не найдена'), show_alert=True
        )
        return

    user = await get_user_by_id(db, request.user_id)
    user_name = (
        html.escape(user.full_name)
        if user and user.full_name
        else get_texts(db_user.language).t('ADMIN_REFERRALS_UI_028', 'Неизвестно')
    )
    user_tg_id = (user.telegram_id or user.email or f'#{user.id}') if user else 'N/A'

    analysis = json.loads(request.risk_analysis) if request.risk_analysis else {}

    status_text = {
        WithdrawalRequestStatus.PENDING.value: get_texts(db_user.language).t('ADMIN_REFERRALS_UI_077', '⏳ Ожидает'),
        WithdrawalRequestStatus.APPROVED.value: get_texts(db_user.language).t('ADMIN_REFERRALS_UI_078', '✅ Одобрена'),
        WithdrawalRequestStatus.REJECTED.value: get_texts(db_user.language).t('ADMIN_REFERRALS_UI_079', '❌ Отклонена'),
        WithdrawalRequestStatus.COMPLETED.value: get_texts(db_user.language).t(
            'ADMIN_REFERRALS_UI_080', '✅ Выполнена'
        ),
        WithdrawalRequestStatus.CANCELLED.value: get_texts(db_user.language).t('ADMIN_REFERRALS_UI_081', '🚫 Отменена'),
    }.get(request.status, request.status)

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_REFERRALS_UI_003',
            '\n📋 <b>Заявка #{v0}</b>\n\n👤 Пользователь: {v1}\n🆔 ID: <code>{v2}</code>\n💰 Сумма: <b>{v3:.0f}₽</b>\n📊 Статус: {v4}\n\n💳 <b>Реквизиты:</b>\n<code>{v5}</code>\n\n📅 Создана: {v6}\n\n{v7}\n',
        )
        .format(
            v0=request.id,
            v1=user_name,
            v2=user_tg_id,
            v3=request.amount_kopeks / 100,
            v4=status_text,
            v5=html.escape(request.payment_details or ''),
            v6=format_local_datetime(request.created_at, '%d.%m.%Y %H:%M'),
            v7=referral_withdrawal_service.format_analysis_for_admin(analysis, language=db_user.language),
        )
    )

    # В группе (уведомление в админ-чате) — только действия: профиль и список
    # открыли бы админку в общем чате. В личке — полная карточка.
    role = 'group' if callback_from_group(callback) else 'admin'
    keyboard = get_withdrawal_request_keyboard(
        request.id,
        request.status,
        user_db_id=user.id if user else None,
        role=role,
        navigation=(role == 'admin'),
        language=db_user.language,
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def approve_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Одобряет заявку на вывод."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_076', 'Заявка не найдена'), show_alert=True
        )
        return

    success, error = await referral_withdrawal_service.approve_request(db, request_id, db_user.id)

    if success:
        # Уведомляем пользователя (только если есть telegram_id)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    texts.t(
                        'REFERRAL_WITHDRAWAL_APPROVED',
                        '✅ <b>Заявка на вывод #{id} одобрена!</b>\n\n'
                        'Сумма: <b>{amount}</b>\n'
                        'Средства списаны с баланса.\n\n'
                        'Ожидайте перевод на указанные реквизиты.',
                    ).format(id=request.id, amount=texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error('Ошибка отправки уведомления пользователю', error=e)

        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_082', '✅ Заявка одобрена, средства списаны с баланса')
        )

        # Обновляем отображение
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer(f'❌ {localize_withdrawal_detail(error, db_user.language)}', show_alert=True)


@admin_required
@error_handler
async def reject_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Отклоняет заявку на вывод."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_076', 'Заявка не найдена'), show_alert=True
        )
        return

    success, _error = await referral_withdrawal_service.reject_request(
        db, request_id, db_user.id, 'Отклонено администратором'
    )

    if success:
        # Уведомляем пользователя (только если есть telegram_id)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    texts.t(
                        'REFERRAL_WITHDRAWAL_REJECTED',
                        '❌ <b>Заявка на вывод #{id} отклонена</b>\n\n'
                        'Сумма: <b>{amount}</b>\n\n'
                        'Если у вас есть вопросы, обратитесь в поддержку.',
                    ).format(id=request.id, amount=texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error('Ошибка отправки уведомления пользователю', error=e)

        await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_083', '❌ Заявка отклонена'))

        # Обновляем отображение
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_084', '❌ Ошибка отклонения'), show_alert=True
        )


@admin_required
@error_handler
async def complete_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Отмечает заявку как выполненную (деньги переведены)."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_076', 'Заявка не найдена'), show_alert=True
        )
        return

    success, _error = await referral_withdrawal_service.complete_request(db, request_id, db_user.id, 'Перевод выполнен')

    if success:
        # Уведомляем пользователя (только если есть telegram_id)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    texts.t(
                        'REFERRAL_WITHDRAWAL_COMPLETED',
                        '💸 <b>Выплата по заявке #{id} выполнена!</b>\n\n'
                        'Сумма: <b>{amount}</b>\n\n'
                        'Деньги отправлены на указанные реквизиты.',
                    ).format(id=request.id, amount=texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error('Ошибка отправки уведомления пользователю', error=e)

        await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_085', '✅ Заявка выполнена'))

        # Обновляем отображение
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_086', '❌ Ошибка выполнения'), show_alert=True
        )


@admin_required
@error_handler
async def start_test_referral_earning(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Начинает процесс тестового начисления реферального дохода."""
    if not settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_087', 'Тестовый режим отключён'), show_alert=True
        )
        return

    await state.set_state(AdminStates.test_referral_earning_input)

    text = get_texts(db_user.language).t(
        'ADMIN_REFERRALS_UI_004',
        '\n🧪 <b>Тестовое начисление реферального дохода</b>\n\nВведите данные в формате:\n<code>telegram_id сумма_в_рублях</code>\n\nПримеры:\n• <code>123456789 500</code> — начислит 500₽ пользователю 123456789\n• <code>987654321 1000</code> — начислит 1000₽ пользователю 987654321\n\n⚠️ Это создаст реальную запись ReferralEarning, как будто пользователь заработал с реферала.\n',
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_150', '❌ Отмена'),
                    callback_data='admin_withdrawal_requests',
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def process_test_referral_earning(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Обрабатывает ввод тестового начисления."""
    if not settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        await message.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_088', '❌ Тестовый режим отключён'))
        await state.clear()
        return

    text_input = message.text.strip()
    parts = text_input.split()

    if len(parts) != 2:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_REFERRALS_UI_089',
                '❌ Неверный формат. Введите: <code>telegram_id сумма</code>\n\nНапример: <code>123456789 500</code>',
            )
        )
        return

    try:
        target_telegram_id = int(parts[0])
        amount_rubles = float(parts[1].replace(',', '.'))
        amount_kopeks = int(amount_rubles * 100)

        if amount_kopeks <= 0:
            await message.answer(
                get_texts(db_user.language).t('ADMIN_REFERRALS_UI_110', '❌ Сумма должна быть положительной')
            )
            return

        if amount_kopeks > 10000000:  # Лимит 100 000₽
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_REFERRALS_UI_111', '❌ Максимальная сумма тестового начисления: 100 000₽'
                )
            )
            return

    except ValueError:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_REFERRALS_UI_112',
                '❌ Неверный формат чисел. Введите: <code>telegram_id сумма</code>\n\nНапример: <code>123456789 500</code>',
            )
        )
        return

    # Ищем целевого пользователя
    target_user = await get_user_by_telegram_id(db, target_telegram_id)
    if not target_user:
        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_REFERRALS_UI_090', '❌ Пользователь с ID {v0} не найден в базе')
            .format(v0=target_telegram_id)
        )
        return

    # Создаём тестовое начисление
    earning = ReferralEarning(
        user_id=target_user.id,
        referral_id=target_user.id,  # Сам на себя (тестовое)
        amount_kopeks=amount_kopeks,
        reason='test_earning',
    )
    db.add(earning)

    # Добавляем на баланс пользователя
    from app.database.crud.user import lock_user_for_update

    target_user = await lock_user_for_update(db, target_user)
    target_user.balance_kopeks += amount_kopeks

    await db.commit()
    await state.clear()

    await message.answer(
        get_texts(db_user.language)
        .t(
            'ADMIN_REFERRALS_UI_056',
            '✅ <b>Тестовое начисление создано!</b>\n\n👤 Пользователь: {v0}\n🆔 ID: <code>{v1}</code>\n💰 Сумма: <b>{v2:.0f}₽</b>\n💳 Новый баланс: <b>{v3:.0f}₽</b>\n\nНачисление добавлено как реферальный доход.',
        )
        .format(
            v0=html.escape(target_user.full_name)
            if target_user.full_name
            else get_texts(db_user.language).t('ADMIN_REFERRALS_UI_135', 'Без имени'),
            v1=target_telegram_id,
            v2=amount_rubles,
            v3=target_user.balance_kopeks / 100,
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_159', '📋 К заявкам'),
                        callback_data='admin_withdrawal_requests',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_160', '👤 Профиль'),
                        callback_data=f'admin_user_manage_{target_user.id}',
                    )
                ],
            ]
        ),
    )

    logger.info(
        'Тестовое начисление: админ начислил ₽ пользователю',
        telegram_id=db_user.telegram_id,
        amount_rubles=amount_rubles,
        target_telegram_id=target_telegram_id,
    )


def _get_period_dates(period: str) -> tuple[datetime, datetime]:
    """Границы периода — календарные дни settings.TIMEZONE, как моменты в UTC."""
    now = datetime.now(UTC)
    today_start, tomorrow_start = local_day_bounds(now)

    if period == 'yesterday':
        return local_day_start(now, days_back=1), today_start
    if period == 'week':
        return local_day_start(now, days_back=7), tomorrow_start
    if period == 'month':
        return local_day_start(now, days_back=30), tomorrow_start
    # 'today' и всё неизвестное — сегодня
    return today_start, tomorrow_start


def _get_period_display_name(period: str, language: str = 'ru') -> str:
    """Возвращает человекочитаемое название периода."""
    names = {
        'today': get_texts(language).t('ADMIN_REFERRALS_UI_029', 'сегодня'),
        'yesterday': get_texts(language).t('ADMIN_REFERRALS_UI_030', 'вчера'),
        'week': get_texts(language).t('ADMIN_REFERRALS_UI_031', '7 дней'),
        'month': get_texts(language).t('ADMIN_REFERRALS_UI_032', '30 дней'),
    }
    return names.get(period, get_texts(language).t('ADMIN_REFERRALS_UI_029', 'сегодня'))


async def _show_diagnostics_for_period(
    callback: types.CallbackQuery, db: AsyncSession, state: FSMContext, period: str, language: str = 'ru'
):
    """Внутренняя функция для отображения диагностики за указанный период."""
    try:
        await callback.answer(get_texts(language).t('ADMIN_REFERRALS_UI_091', 'Анализирую логи...'))

        from app.services.referral_diagnostics_service import referral_diagnostics_service

        # Сохраняем период в state
        await state.update_data(diagnostics_period=period)
        from app.states import AdminStates

        await state.set_state(AdminStates.referral_diagnostics_period)

        # Получаем даты периода
        start_date, end_date = _get_period_dates(period)

        # Анализируем логи
        report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)

        # Формируем отчёт
        period_display = _get_period_display_name(period, language=language)

        text = (
            get_texts(language)
            .t(
                'ADMIN_REFERRALS_UI_033',
                '\n🔍 <b>Диагностика рефералов — {v0}</b>\n\n<b>📊 Статистика переходов:</b>\n• Всего кликов по реф-ссылкам: {v1}\n• Уникальных пользователей: {v2}\n• Потерянных рефералов: {v3}\n',
            )
            .format(
                v0=period_display,
                v1=report.total_ref_clicks,
                v2=report.unique_users_clicked,
                v3=len(report.lost_referrals),
            )
        )

        if report.lost_referrals:
            text += get_texts(language).t('ADMIN_REFERRALS_UI_057', '\n<b>❌ Потерянные рефералы:</b>\n')
            text += get_texts(language).t(
                'ADMIN_REFERRALS_UI_058', '<i>(пришли по ссылке, но реферер не засчитался)</i>\n\n'
            )

            for i, lost in enumerate(report.lost_referrals[:15], 1):
                # Статус пользователя
                if not lost.registered:
                    status = get_texts(language).t('ADMIN_REFERRALS_UI_113', '⚠️ Не в БД')
                elif not lost.has_referrer:
                    status = get_texts(language).t('ADMIN_REFERRALS_UI_136', '❌ Без реферера')
                else:
                    status = (
                        get_texts(language)
                        .t('ADMIN_REFERRALS_UI_137', '⚡ Другой реферер (ID{v0})')
                        .format(v0=lost.current_referrer_id)
                    )

                # Имя или ID
                if lost.username:
                    user_name = f'@{html.escape(lost.username)}'
                elif lost.full_name:
                    user_name = html.escape(lost.full_name)
                else:
                    user_name = f'ID{lost.telegram_id}'

                # Ожидаемый реферер
                referrer_info = ''
                if lost.expected_referrer_name:
                    referrer_info = f' → {html.escape(lost.expected_referrer_name)}'
                elif lost.expected_referrer_id:
                    referrer_info = f' → ID{lost.expected_referrer_id}'

                # Время
                time_str = lost.click_time.strftime('%H:%M')

                text += f'{i}. {user_name} — {status}\n'
                text += f'   <code>{html.escape(lost.referral_code)}</code>{referrer_info} ({time_str})\n'

            if len(report.lost_referrals) > 15:
                text += (
                    get_texts(language)
                    .t('ADMIN_REFERRALS_UI_063', '\n<i>... и ещё {v0}</i>\n')
                    .format(v0=len(report.lost_referrals) - 15)
                )
        else:
            text += get_texts(language).t('ADMIN_REFERRALS_UI_059', '\n✅ <b>Все рефералы засчитаны!</b>\n')

        # Информация о логах
        log_path = referral_diagnostics_service.log_path
        log_exists = await asyncio.to_thread(log_path.exists)
        log_size = (await asyncio.to_thread(log_path.stat)).st_size if log_exists else 0

        text += f'\n<i>📂 {log_path.name}'
        if log_exists:
            text += f' ({log_size / 1024:.0f} KB)'
            text += get_texts(language).t('ADMIN_REFERRALS_UI_060', ' | Строк: {v0}').format(v0=report.lines_in_period)
        else:
            text += get_texts(language).t('ADMIN_REFERRALS_UI_061', ' (не найден!)')
        text += '</i>'

        # Кнопки: только "Сегодня" (текущий лог) и "Загрузить файл" (старые логи)
        keyboard_rows = [
            [
                types.InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_REFERRALS_UI_138', '📅 Сегодня (текущий лог)'),
                    callback_data='admin_ref_diag:today',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_REFERRALS_UI_139', '📤 Загрузить лог-файл'),
                    callback_data='admin_ref_diag_upload',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_REFERRALS_UI_140', '🔍 Проверить бонусы (по БД)'),
                    callback_data='admin_ref_check_bonuses',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_REFERRALS_UI_141', '🏆 Синхронизировать с конкурсом'),
                    callback_data='admin_ref_sync_contest',
                )
            ],
        ]

        # Кнопки действий (только если есть потерянные рефералы)
        if report.lost_referrals:
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=get_texts(language).t('ADMIN_REFERRALS_UI_151', '📋 Предпросмотр исправлений'),
                        callback_data='admin_ref_fix_preview',
                    )
                ]
            )

        keyboard_rows.extend(
            [
                [
                    types.InlineKeyboardButton(
                        text=get_texts(language).t('ADMIN_REFERRALS_UI_131', '🔄 Обновить'),
                        callback_data=f'admin_ref_diag:{period}',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=get_texts(language).t('ADMIN_REFERRALS_UI_143', '⬅️ К статистике'),
                        callback_data='admin_referrals',
                    )
                ],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в _show_diagnostics_for_period', error=e, exc_info=True)
        await callback.answer(
            get_texts(language).t('ADMIN_REFERRALS_UI_114', 'Ошибка при анализе логов'), show_alert=True
        )


@admin_required
@error_handler
async def show_referral_diagnostics(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Показывает диагностику реферальной системы по логам."""
    # Определяем период из callback_data или используем "today" по умолчанию
    if ':' in callback.data:
        period = callback.data.split(':')[1]
    else:
        period = 'today'

    await _show_diagnostics_for_period(callback, db, state, period, language=db_user.language)


@admin_required
@error_handler
async def preview_referral_fixes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Показывает предпросмотр исправлений потерянных рефералов."""
    try:
        await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_092', 'Анализирую...'))

        # Получаем период из state
        state_data = await state.get_data()
        period = state_data.get('diagnostics_period', 'today')

        from app.services.referral_diagnostics_service import DiagnosticReport, referral_diagnostics_service

        # Проверяем, работаем ли с загруженным файлом
        if period == 'uploaded_file':
            # Используем сохранённый отчёт из загруженного файла (десериализуем)
            report_data = state_data.get('uploaded_file_report')
            if not report_data:
                await callback.answer(
                    get_texts(db_user.language).t('ADMIN_REFERRALS_UI_142', 'Отчёт загруженного файла не найден'),
                    show_alert=True,
                )
                return
            report = DiagnosticReport.from_dict(report_data)
            period_display = get_texts(db_user.language).t('ADMIN_REFERRALS_UI_062', 'загруженный файл')
        else:
            # Получаем даты периода
            start_date, end_date = _get_period_dates(period)

            # Анализируем логи
            report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)
            period_display = _get_period_display_name(period, language=db_user.language)

        if not report.lost_referrals:
            await callback.answer(
                get_texts(db_user.language).t('ADMIN_REFERRALS_UI_115', 'Нет потерянных рефералов для исправления'),
                show_alert=True,
            )
            return

        # Запускаем предпросмотр исправлений
        fix_report = await referral_diagnostics_service.fix_lost_referrals(db, report.lost_referrals, apply=False)

        # Формируем отчёт
        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_REFERRALS_UI_034',
                '\n📋 <b>Предпросмотр исправлений — {v0}</b>\n\n<b>📊 Что будет сделано:</b>\n• Исправлено рефералов: {v1}\n• Бонусов рефералам: {v2}\n• Бонусов рефереам: {v3}\n• Ошибок: {v4}\n\n<b>🔍 Детали:</b>\n',
            )
            .format(
                v0=period_display,
                v1=fix_report.users_fixed,
                v2=settings.format_price(fix_report.bonuses_to_referrals),
                v3=settings.format_price(fix_report.bonuses_to_referrers),
                v4=fix_report.errors,
            )
        )

        # Показываем первые 10 деталей
        for i, detail in enumerate(fix_report.details[:10], 1):
            if detail.username:
                user_name = f'@{html.escape(detail.username)}'
            elif detail.full_name:
                user_name = html.escape(detail.full_name)
            else:
                user_name = f'ID{detail.telegram_id}'

            if detail.error:
                text += (
                    f'{i}. {user_name} — ❌ {html.escape(_localize_diagnostic_error(detail.error, db_user.language))}\n'
                )
            else:
                text += f'{i}. {user_name}\n'
                if detail.referred_by_set:
                    referrer_display = (
                        html.escape(detail.referrer_name) if detail.referrer_name else f'ID{detail.referrer_id}'
                    )
                    text += (
                        get_texts(db_user.language)
                        .t('ADMIN_REFERRALS_UI_116', '   • Реферер: {v0}\n')
                        .format(v0=referrer_display)
                    )
                if detail.had_first_topup:
                    text += (
                        get_texts(db_user.language)
                        .t('ADMIN_REFERRALS_UI_117', '   • Первое пополнение: {v0}\n')
                        .format(v0=settings.format_price(detail.topup_amount_kopeks))
                    )
                if detail.bonus_to_referral_kopeks > 0:
                    text += (
                        get_texts(db_user.language)
                        .t('ADMIN_REFERRALS_UI_118', '   • Бонус рефералу: {v0}\n')
                        .format(v0=settings.format_price(detail.bonus_to_referral_kopeks))
                    )
                if detail.bonus_to_referrer_kopeks > 0:
                    text += (
                        get_texts(db_user.language)
                        .t('ADMIN_REFERRALS_UI_119', '   • Бонус рефереру: {v0}\n')
                        .format(v0=settings.format_price(detail.bonus_to_referrer_kopeks))
                    )

        if len(fix_report.details) > 10:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_REFERRALS_UI_063', '\n<i>... и ещё {v0}</i>\n')
                .format(v0=len(fix_report.details) - 10)
            )

        text += get_texts(db_user.language).t(
            'ADMIN_REFERRALS_UI_035',
            '\n⚠️ <b>Внимание!</b> Это только предпросмотр. Нажмите "Применить", чтобы выполнить исправления.',
        )

        # Кнопка назад зависит от источника
        back_button_text = get_texts(db_user.language).t('ADMIN_REFERRALS_UI_036', '⬅️ К диагностике')
        back_button_callback = f'admin_ref_diag:{period}' if period != 'uploaded_file' else 'admin_referral_diagnostics'

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_154', '✅ Применить исправления'),
                        callback_data='admin_ref_fix_apply',
                    )
                ],
                [types.InlineKeyboardButton(text=back_button_text, callback_data=back_button_callback)],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в preview_referral_fixes', error=e, exc_info=True)
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_120', 'Ошибка при создании предпросмотра'),
            show_alert=True,
        )


@admin_required
@error_handler
async def apply_referral_fixes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Применяет исправления потерянных рефералов."""
    try:
        await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_093', 'Применяю исправления...'))

        # Получаем период из state
        state_data = await state.get_data()
        period = state_data.get('diagnostics_period', 'today')

        from app.services.referral_diagnostics_service import DiagnosticReport, referral_diagnostics_service

        # Проверяем, работаем ли с загруженным файлом
        if period == 'uploaded_file':
            # Используем сохранённый отчёт из загруженного файла (десериализуем)
            report_data = state_data.get('uploaded_file_report')
            if not report_data:
                await callback.answer(
                    get_texts(db_user.language).t('ADMIN_REFERRALS_UI_142', 'Отчёт загруженного файла не найден'),
                    show_alert=True,
                )
                return
            report = DiagnosticReport.from_dict(report_data)
            period_display = get_texts(db_user.language).t('ADMIN_REFERRALS_UI_062', 'загруженный файл')
        else:
            # Получаем даты периода
            start_date, end_date = _get_period_dates(period)

            # Анализируем логи
            report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)
            period_display = _get_period_display_name(period, language=db_user.language)

        if not report.lost_referrals:
            await callback.answer(
                get_texts(db_user.language).t('ADMIN_REFERRALS_UI_115', 'Нет потерянных рефералов для исправления'),
                show_alert=True,
            )
            return

        # Применяем исправления
        fix_report = await referral_diagnostics_service.fix_lost_referrals(db, report.lost_referrals, apply=True)

        # Формируем отчёт
        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_REFERRALS_UI_037',
                '\n✅ <b>Исправления применены — {v0}</b>\n\n<b>📊 Результаты:</b>\n• Исправлено рефералов: {v1}\n• Бонусов рефералам: {v2}\n• Бонусов рефереам: {v3}\n• Ошибок: {v4}\n\n<b>🔍 Детали:</b>\n',
            )
            .format(
                v0=period_display,
                v1=fix_report.users_fixed,
                v2=settings.format_price(fix_report.bonuses_to_referrals),
                v3=settings.format_price(fix_report.bonuses_to_referrers),
                v4=fix_report.errors,
            )
        )

        # Показываем первые 10 успешных деталей
        success_count = 0
        for detail in fix_report.details:
            if not detail.error and success_count < 10:
                success_count += 1
                if detail.username:
                    user_name = f'@{html.escape(detail.username)}'
                elif detail.full_name:
                    user_name = html.escape(detail.full_name)
                else:
                    user_name = f'ID{detail.telegram_id}'

                text += f'{success_count}. {user_name}\n'
                if detail.referred_by_set:
                    referrer_display = (
                        html.escape(detail.referrer_name) if detail.referrer_name else f'ID{detail.referrer_id}'
                    )
                    text += (
                        get_texts(db_user.language)
                        .t('ADMIN_REFERRALS_UI_116', '   • Реферер: {v0}\n')
                        .format(v0=referrer_display)
                    )
                if detail.bonus_to_referral_kopeks > 0:
                    text += (
                        get_texts(db_user.language)
                        .t('ADMIN_REFERRALS_UI_118', '   • Бонус рефералу: {v0}\n')
                        .format(v0=settings.format_price(detail.bonus_to_referral_kopeks))
                    )
                if detail.bonus_to_referrer_kopeks > 0:
                    text += (
                        get_texts(db_user.language)
                        .t('ADMIN_REFERRALS_UI_119', '   • Бонус рефереру: {v0}\n')
                        .format(v0=settings.format_price(detail.bonus_to_referrer_kopeks))
                    )

        if fix_report.users_fixed > 10:
            text += (
                get_texts(db_user.language)
                .t('ADMIN_REFERRALS_UI_064', '\n<i>... и ещё {v0} исправлений</i>\n')
                .format(v0=fix_report.users_fixed - 10)
            )

        # Показываем ошибки
        if fix_report.errors > 0:
            text += get_texts(db_user.language).t('ADMIN_REFERRALS_UI_065', '\n<b>❌ Ошибки:</b>\n')
            error_count = 0
            for detail in fix_report.details:
                if detail.error and error_count < 5:
                    error_count += 1
                    if detail.username:
                        user_name = f'@{html.escape(detail.username)}'
                    elif detail.full_name:
                        user_name = html.escape(detail.full_name)
                    else:
                        user_name = f'ID{detail.telegram_id}'
                    text += (
                        f'• {user_name}: {html.escape(_localize_diagnostic_error(detail.error, db_user.language))}\n'
                    )
            if fix_report.errors > 5:
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_REFERRALS_UI_094', '<i>... и ещё {v0} ошибок</i>\n')
                    .format(v0=fix_report.errors - 5)
                )

        # Кнопки зависят от источника
        keyboard_rows = []
        if period != 'uploaded_file':
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_152', '🔄 Обновить диагностику'),
                        callback_data=f'admin_ref_diag:{period}',
                    )
                ]
            )
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_143', '⬅️ К статистике'),
                    callback_data='admin_referrals',
                )
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.message.edit_text(text, reply_markup=keyboard)

        # Очищаем сохранённый отчёт из state
        if period == 'uploaded_file':
            await state.update_data(uploaded_file_report=None)

    except Exception as e:
        logger.error('Ошибка в apply_referral_fixes', error=e, exc_info=True)
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_121', 'Ошибка при применении исправлений'),
            show_alert=True,
        )


# =============================================================================
# Проверка бонусов по БД
# =============================================================================


@admin_required
@error_handler
async def check_missing_bonuses(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Проверяет по БД — всем ли рефералам начислены бонусы."""
    from app.services.referral_diagnostics_service import (
        referral_diagnostics_service,
    )

    await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_066', '🔍 Проверяю бонусы...'))

    try:
        report = await referral_diagnostics_service.check_missing_bonuses(db)

        # Проверка неприменима к многоуровневой схеме: она ищет отсутствие строки
        # с легаси-причиной и считает суммы по ключам REFERRAL_*. Нули в отчёте
        # означают «не проверяли», и показать вместо этого «✅ Все бонусы
        # начислены!» — выдать админу заведомо ложное «всё в порядке» по аудиту,
        # который не выполнялся.
        if report.unsupported_scheme:
            await callback.message.edit_text(
                get_texts(db_user.language).t(
                    'ADMIN_REFERRALS_UI_122',
                    '🔍 <b>Проверка бонусов по БД</b>\n\n⚠️ Проверка недоступна: включена многоуровневая схема наград.\n\nДетектор ищет пропущенные бонусы по правилам классической схемы и считает суммы по настройкам REFERRAL_*. В многоуровневой схеме награда могла быть выдана по другому поводу или днями подписки — такая пара выглядела бы «пропущенной», и доначисление заплатило бы второй раз поверх уже выданного.',
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_149', '🪜 Уровни наград'),
                                callback_data='admin_ref_levels',
                            )
                        ],
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_036', '⬅️ К диагностике'),
                                callback_data='admin_referral_diagnostics',
                            )
                        ],
                    ]
                ),
            )
            return

        # Сохраняем отчёт в state для последующего применения
        await state.update_data(missing_bonuses_report=report.to_dict())

        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_REFERRALS_UI_038',
                '\n🔍 <b>Проверка бонусов по БД</b>\n\n📊 <b>Статистика:</b>\n• Всего рефералов: {v0}\n• С пополнением ≥ минимума: {v1}\n• <b>Без бонусов: {v2}</b>\n',
            )
            .format(v0=report.total_referrals_checked, v1=report.referrals_with_topup, v2=len(report.missing_bonuses))
        )

        if report.missing_bonuses:
            text += (
                get_texts(db_user.language)
                .t(
                    'ADMIN_REFERRALS_UI_067',
                    '\n💰 <b>Требуется начислить:</b>\n• Рефералам: {v0:.0f}₽\n• Рефереерам: {v1:.0f}₽\n• <b>Итого: {v2:.0f}₽</b>\n\n👤 <b>Список ({v3} чел.):</b>\n',
                )
                .format(
                    v0=report.total_missing_to_referrals / 100,
                    v1=report.total_missing_to_referrers / 100,
                    v2=(report.total_missing_to_referrals + report.total_missing_to_referrers) / 100,
                    v3=len(report.missing_bonuses),
                )
            )
            for i, mb in enumerate(report.missing_bonuses[:15], 1):
                referral_name = html.escape(
                    mb.referral_full_name or mb.referral_username or str(mb.referral_telegram_id)
                )
                referrer_name = html.escape(
                    mb.referrer_full_name or mb.referrer_username or str(mb.referrer_telegram_id)
                )
                text += f'\n{i}. <b>{referral_name}</b>'
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_REFERRALS_UI_095', '\n   └ Пригласил: {v0}')
                    .format(v0=referrer_name)
                )
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_REFERRALS_UI_096', '\n   └ Пополнение: {v0:.0f}₽')
                    .format(v0=mb.first_topup_amount_kopeks / 100)
                )
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_REFERRALS_UI_097', '\n   └ Бонусы: {v0:.0f}₽ + {v1:.0f}₽')
                    .format(v0=mb.referral_bonus_amount / 100, v1=mb.referrer_bonus_amount / 100)
                )

            if len(report.missing_bonuses) > 15:
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_REFERRALS_UI_098', '\n\n<i>... и ещё {v0} чел.</i>')
                    .format(v0=len(report.missing_bonuses) - 15)
                )

            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_158', '✅ Начислить все бонусы'),
                            callback_data='admin_ref_bonus_apply',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_131', '🔄 Обновить'),
                            callback_data='admin_ref_check_bonuses',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_036', '⬅️ К диагностике'),
                            callback_data='admin_referral_diagnostics',
                        )
                    ],
                ]
            )
        else:
            text += get_texts(db_user.language).t('ADMIN_REFERRALS_UI_068', '\n✅ <b>Все бонусы начислены!</b>')
            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_131', '🔄 Обновить'),
                            callback_data='admin_ref_check_bonuses',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_036', '⬅️ К диагностике'),
                            callback_data='admin_referral_diagnostics',
                        )
                    ],
                ]
            )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в check_missing_bonuses', error=e, exc_info=True)
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_123', 'Ошибка при проверке бонусов'), show_alert=True
        )


@admin_required
@error_handler
async def apply_missing_bonuses(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Применяет начисление пропущенных бонусов."""
    from app.services.referral_diagnostics_service import (
        MissingBonusReport,
        referral_diagnostics_service,
    )

    await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_069', '💰 Начисляю бонусы...'))

    try:
        # Получаем сохранённый отчёт
        data = await state.get_data()
        report_dict = data.get('missing_bonuses_report')

        if not report_dict:
            await callback.answer(
                get_texts(db_user.language).t('ADMIN_REFERRALS_UI_124', '❌ Отчёт не найден. Обновите проверку.'),
                show_alert=True,
            )
            return

        report = MissingBonusReport.from_dict(report_dict)

        if not report.missing_bonuses:
            await callback.answer(
                get_texts(db_user.language).t('ADMIN_REFERRALS_UI_125', '✅ Нет бонусов для начисления'),
                show_alert=True,
            )
            return

        # Применяем исправления
        fix_report = await referral_diagnostics_service.fix_missing_bonuses(db, report.missing_bonuses, apply=True)

        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_REFERRALS_UI_039',
                '\n✅ <b>Бонусы начислены!</b>\n\n📊 <b>Результат:</b>\n• Обработано: {v0} пользователей\n• Начислено рефералам: {v1:.0f}₽\n• Начислено рефереерам: {v2:.0f}₽\n• <b>Итого: {v3:.0f}₽</b>\n',
            )
            .format(
                v0=fix_report.users_fixed,
                v1=fix_report.bonuses_to_referrals / 100,
                v2=fix_report.bonuses_to_referrers / 100,
                v3=(fix_report.bonuses_to_referrals + fix_report.bonuses_to_referrers) / 100,
            )
        )

        if fix_report.errors > 0:
            text += (
                get_texts(db_user.language).t('ADMIN_REFERRALS_UI_070', '\n⚠️ Ошибок: {v0}').format(v0=fix_report.errors)
            )

        # Очищаем отчёт из state
        await state.update_data(missing_bonuses_report=None)

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_155', '🔍 Проверить снова'),
                        callback_data='admin_ref_check_bonuses',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_036', '⬅️ К диагностике'),
                        callback_data='admin_referral_diagnostics',
                    )
                ],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в apply_missing_bonuses', error=e, exc_info=True)
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_126', 'Ошибка при начислении бонусов'), show_alert=True
        )


@admin_required
@error_handler
async def sync_referrals_with_contest(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Синхронизирует всех рефералов с активными конкурсами."""
    from app.database.crud.referral_contest import get_contests_for_events
    from app.services.referral_contest_service import referral_contest_service

    await callback.answer(get_texts(db_user.language).t('ADMIN_REFERRALS_UI_071', '🏆 Синхронизирую с конкурсами...'))

    try:
        now_utc = datetime.now(UTC)

        # Получаем активные конкурсы
        paid_contests = await get_contests_for_events(db, now_utc, contest_types=['referral_paid'])
        reg_contests = await get_contests_for_events(db, now_utc, contest_types=['referral_registered'])

        all_contests = list(paid_contests) + list(reg_contests)

        if not all_contests:
            await callback.message.edit_text(
                get_texts(db_user.language).t(
                    'ADMIN_REFERRALS_UI_127',
                    '❌ <b>Нет активных конкурсов рефералов</b>\n\nСоздайте конкурс в разделе "Конкурсы" для синхронизации.',
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_036', '⬅️ К диагностике'),
                                callback_data='admin_referral_diagnostics',
                            )
                        ]
                    ]
                ),
            )
            return

        # Синхронизируем каждый конкурс
        total_created = 0
        total_updated = 0
        total_skipped = 0
        contest_results = []

        for contest in all_contests:
            stats = await referral_contest_service.sync_contest(db, contest.id)
            if 'error' not in stats:
                total_created += stats.get('created', 0)
                total_updated += stats.get('updated', 0)
                total_skipped += stats.get('skipped', 0)
                contest_results.append(
                    get_texts(db_user.language)
                    .t('ADMIN_REFERRALS_UI_128', '• {v0}: +{v1} новых')
                    .format(v0=html.escape(contest.title), v1=stats.get('created', 0))
                )
            else:
                contest_results.append(
                    get_texts(db_user.language)
                    .t('ADMIN_REFERRALS_UI_129', '• {v0}: ошибка')
                    .format(v0=html.escape(contest.title))
                )

        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_REFERRALS_UI_040',
                '\n🏆 <b>Синхронизация с конкурсами завершена!</b>\n\n📊 <b>Результат:</b>\n• Конкурсов обработано: {v0}\n• Новых событий добавлено: {v1}\n• Обновлено: {v2}\n• Пропущено (уже есть): {v3}\n\n📋 <b>По конкурсам:</b>\n',
            )
            .format(v0=len(all_contests), v1=total_created, v2=total_updated, v3=total_skipped)
        )
        text += '\n'.join(contest_results)

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_156', '🔄 Синхронизировать снова'),
                        callback_data='admin_ref_sync_contest',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_036', '⬅️ К диагностике'),
                        callback_data='admin_referral_diagnostics',
                    )
                ],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в sync_referrals_with_contest', error=e, exc_info=True)
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_130', 'Ошибка при синхронизации'), show_alert=True
        )


@admin_required
@error_handler
async def request_log_file_upload(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Запрашивает загрузку лог-файла для анализа."""
    await state.set_state(AdminStates.waiting_for_log_file)

    text = get_texts(db_user.language).t(
        'ADMIN_REFERRALS_UI_005',
        '\n📤 <b>Загрузка лог-файла для анализа</b>\n\nОтправьте файл лога (расширение .log или .txt).\n\nФайл будет проанализирован на наличие потерянных рефералов за ВСЕ время, записанное в логе.\n\n⚠️ <b>Важно:</b>\n• Файл должен быть текстовым (.log, .txt)\n• Максимальный размер: 50 MB\n• После анализа файл будет автоматически удалён\n\nЕсли ротация логов удалила старые данные — загрузите резервную копию.\n',
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_150', '❌ Отмена'),
                    callback_data='admin_referral_diagnostics',
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def receive_log_file(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Получает и анализирует загруженный лог-файл."""
    import tempfile
    from pathlib import Path

    if not message.document:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_REFERRALS_UI_099', '❌ Пожалуйста, отправьте файл документом.'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_150', '❌ Отмена'),
                            callback_data='admin_referral_diagnostics',
                        )
                    ]
                ]
            ),
        )
        return

    # Проверяем расширение файла
    file_name = message.document.file_name or 'unknown'
    file_ext = Path(file_name).suffix.lower()

    if file_ext not in ['.log', '.txt']:
        await message.answer(
            get_texts(db_user.language)
            .t(
                'ADMIN_REFERRALS_UI_100',
                '❌ Неверный формат файла: {v0}\n\nПоддерживаются только текстовые файлы (.log, .txt)',
            )
            .format(v0=html.escape(file_ext)),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_150', '❌ Отмена'),
                            callback_data='admin_referral_diagnostics',
                        )
                    ]
                ]
            ),
        )
        return

    # Проверяем размер файла
    max_size = 50 * 1024 * 1024  # 50 MB
    if message.document.file_size > max_size:
        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_REFERRALS_UI_101', '❌ Файл слишком большой: {v0:.1f} MB\n\nМаксимальный размер: 50 MB')
            .format(v0=message.document.file_size / 1024 / 1024),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_150', '❌ Отмена'),
                            callback_data='admin_referral_diagnostics',
                        )
                    ]
                ]
            ),
        )
        return

    # Информируем о начале загрузки
    status_message = await message.answer(
        get_texts(db_user.language)
        .t('ADMIN_REFERRALS_UI_072', '📥 Загружаю файл {v0} ({v1:.1f} MB)...')
        .format(v0=html.escape(file_name), v1=message.document.file_size / 1024 / 1024)
    )

    temp_file_path = None

    try:
        # Скачиваем файл во временную директорию
        temp_dir = tempfile.gettempdir()
        temp_file_path = str(Path(temp_dir) / f'ref_diagnostics_{message.from_user.id}_{file_name}')

        # Скачиваем файл
        file = await message.bot.get_file(message.document.file_id)
        await message.bot.download_file(file.file_path, temp_file_path)

        logger.info('📥 Файл загружен', temp_file_path=temp_file_path, file_size=message.document.file_size)

        # Обновляем статус
        await status_message.edit_text(
            get_texts(db_user.language)
            .t('ADMIN_REFERRALS_UI_102', '🔍 Анализирую файл {v0}...\n\nЭто может занять некоторое время.')
            .format(v0=html.escape(file_name))
        )

        # Анализируем файл
        from app.services.referral_diagnostics_service import referral_diagnostics_service

        report = await referral_diagnostics_service.analyze_file(db, temp_file_path)

        # Формируем отчёт
        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_REFERRALS_UI_041',
                '\n🔍 <b>Анализ лог-файла: {v0}</b>\n\n<b>📊 Статистика переходов:</b>\n• Всего кликов по реф-ссылкам: {v1}\n• Уникальных пользователей: {v2}\n• Потерянных рефералов: {v3}\n• Строк в файле: {v4}\n',
            )
            .format(
                v0=html.escape(file_name),
                v1=report.total_ref_clicks,
                v2=report.unique_users_clicked,
                v3=len(report.lost_referrals),
                v4=report.lines_in_period,
            )
        )

        if report.lost_referrals:
            text += get_texts(db_user.language).t('ADMIN_REFERRALS_UI_057', '\n<b>❌ Потерянные рефералы:</b>\n')
            text += get_texts(db_user.language).t(
                'ADMIN_REFERRALS_UI_058', '<i>(пришли по ссылке, но реферер не засчитался)</i>\n\n'
            )

            for i, lost in enumerate(report.lost_referrals[:15], 1):
                # Статус пользователя
                if not lost.registered:
                    status = get_texts(db_user.language).t('ADMIN_REFERRALS_UI_113', '⚠️ Не в БД')
                elif not lost.has_referrer:
                    status = get_texts(db_user.language).t('ADMIN_REFERRALS_UI_136', '❌ Без реферера')
                else:
                    status = (
                        get_texts(db_user.language)
                        .t('ADMIN_REFERRALS_UI_137', '⚡ Другой реферер (ID{v0})')
                        .format(v0=lost.current_referrer_id)
                    )

                # Имя или ID
                if lost.username:
                    user_name = f'@{html.escape(lost.username)}'
                elif lost.full_name:
                    user_name = html.escape(lost.full_name)
                else:
                    user_name = f'ID{lost.telegram_id}'

                # Ожидаемый реферер
                referrer_info = ''
                if lost.expected_referrer_name:
                    referrer_info = f' → {html.escape(lost.expected_referrer_name)}'
                elif lost.expected_referrer_id:
                    referrer_info = f' → ID{lost.expected_referrer_id}'

                # Время
                time_str = lost.click_time.strftime('%d.%m.%Y %H:%M')

                text += f'{i}. {user_name} — {status}\n'
                text += f'   <code>{html.escape(lost.referral_code)}</code>{referrer_info} ({time_str})\n'

            if len(report.lost_referrals) > 15:
                text += (
                    get_texts(db_user.language)
                    .t('ADMIN_REFERRALS_UI_063', '\n<i>... и ещё {v0}</i>\n')
                    .format(v0=len(report.lost_referrals) - 15)
                )
        else:
            text += get_texts(db_user.language).t('ADMIN_REFERRALS_UI_059', '\n✅ <b>Все рефералы засчитаны!</b>\n')

        # Сохраняем отчёт в state для дальнейшего использования (сериализуем в dict)
        await state.update_data(
            diagnostics_period='uploaded_file',
            uploaded_file_report=report.to_dict(),
        )

        # Кнопки действий
        keyboard_rows = []

        if report.lost_referrals:
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_151', '📋 Предпросмотр исправлений'),
                        callback_data='admin_ref_fix_preview',
                    )
                ]
            )

        keyboard_rows.extend(
            [
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_036', '⬅️ К диагностике'),
                        callback_data='admin_referral_diagnostics',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_143', '⬅️ К статистике'),
                        callback_data='admin_referrals',
                    )
                ],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        # Удаляем статусное сообщение
        await status_message.delete()

        # Отправляем результат
        await message.answer(text, reply_markup=keyboard)

        # Очищаем состояние
        await state.set_state(AdminStates.referral_diagnostics_period)

    except Exception as e:
        logger.error('❌ Ошибка при обработке файла', error=e, exc_info=True)

        try:
            await status_message.edit_text(
                get_texts(db_user.language)
                .t(
                    'ADMIN_REFERRALS_UI_144',
                    '❌ <b>Ошибка при анализе файла</b>\n\nФайл: {v0}\nОшибка: {v1}\n\nПроверьте, что файл является текстовым логом бота.',
                )
                .format(v0=html.escape(file_name), v1=html.escape(str(e))),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_161', '🔄 Попробовать снова'),
                                callback_data='admin_ref_diag_upload',
                            )
                        ],
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_036', '⬅️ К диагностике'),
                                callback_data='admin_referral_diagnostics',
                            )
                        ],
                    ]
                ),
            )
        except:
            await message.answer(
                get_texts(db_user.language)
                .t('ADMIN_REFERRALS_UI_153', '❌ Ошибка при анализе файла: {v0}')
                .format(v0=html.escape(str(e))),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_REFERRALS_UI_109', '⬅️ Назад'),
                                callback_data='admin_referral_diagnostics',
                            )
                        ]
                    ]
                ),
            )

    finally:
        # Удаляем временный файл
        if temp_file_path and await asyncio.to_thread(Path(temp_file_path).exists):
            try:
                await asyncio.to_thread(Path(temp_file_path).unlink)
                logger.info('🗑️ Временный файл удалён', temp_file_path=temp_file_path)
            except Exception as e:
                logger.error('Ошибка удаления временного файла', error=e)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_referral_statistics, F.data == 'admin_referrals')
    dp.callback_query.register(show_top_referrers, F.data == 'admin_referrals_top')
    dp.callback_query.register(show_top_referrers_filtered, F.data.startswith('admin_top_ref:'))
    dp.callback_query.register(show_referral_settings, F.data == 'admin_referrals_settings')
    dp.callback_query.register(show_referral_diagnostics, F.data == 'admin_referral_diagnostics')
    dp.callback_query.register(show_referral_diagnostics, F.data.startswith('admin_ref_diag:'))
    dp.callback_query.register(preview_referral_fixes, F.data == 'admin_ref_fix_preview')
    dp.callback_query.register(apply_referral_fixes, F.data == 'admin_ref_fix_apply')

    # Загрузка лог-файла
    dp.callback_query.register(request_log_file_upload, F.data == 'admin_ref_diag_upload')
    dp.message.register(receive_log_file, AdminStates.waiting_for_log_file)

    # Проверка бонусов по БД
    dp.callback_query.register(check_missing_bonuses, F.data == 'admin_ref_check_bonuses')
    dp.callback_query.register(apply_missing_bonuses, F.data == 'admin_ref_bonus_apply')
    dp.callback_query.register(sync_referrals_with_contest, F.data == 'admin_ref_sync_contest')

    # Хендлеры заявок на вывод
    dp.callback_query.register(show_pending_withdrawal_requests, F.data == 'admin_withdrawal_requests')
    dp.callback_query.register(view_withdrawal_request, F.data.startswith('admin_withdrawal_view_'))
    dp.callback_query.register(approve_withdrawal_request, F.data.startswith('admin_withdrawal_approve_'))
    dp.callback_query.register(reject_withdrawal_request, F.data.startswith('admin_withdrawal_reject_'))
    dp.callback_query.register(complete_withdrawal_request, F.data.startswith('admin_withdrawal_complete_'))

    # Тестовое начисление
    dp.callback_query.register(start_test_referral_earning, F.data == 'admin_test_referral_earning')
    dp.message.register(process_test_referral_earning, AdminStates.test_referral_earning_input)
