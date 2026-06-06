"""Сборка «шапки» главного меню в виде дашборда из двух карточек.

Единая точка визуала главного меню для классического режима бота: используется
и из :mod:`app.handlers.menu`, и из :mod:`app.handlers.start`, чтобы вид меню был
одинаковым независимо от того, как пользователь в него попал.
"""

from __future__ import annotations

import html

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


def _format_traffic_line(subscription, texts) -> str:
    """Строка с трафиком для карточки подписки (или пустая, если нет данных)."""
    if not subscription:
        return ''

    limit = getattr(subscription, 'traffic_limit_gb', None)
    if limit is None:
        return ''

    if limit == 0:
        return texts.t('MENU_DASH_TRAFFIC_UNLIMITED', '📊 Трафик: ∞ безлимит')

    used = getattr(subscription, 'traffic_used_gb', 0) or 0
    return texts.t('MENU_DASH_TRAFFIC', '📊 Трафик: {used:.0f} / {limit} ГБ').format(
        used=float(used),
        limit=limit,
    )


async def _format_referral_line(user, texts, db: AsyncSession) -> str:
    """Строка со статистикой рефералов для карточки кабинета."""
    from app.config import settings

    if not settings.is_referral_program_enabled():
        return ''

    try:
        from app.database.crud.referral import get_user_referral_stats

        stats = await get_user_referral_stats(db, user.id)
    except Exception as error:  # noqa: BLE001 — карточка не должна ронять меню
        logger.debug('Не удалось получить статистику рефералов для дашборда', error=error)
        return ''

    if not stats:
        return ''

    invited = stats.get('invited_count', 0) or 0
    earned = stats.get('total_earned_kopeks', 0) or 0
    return texts.t('MENU_DASH_REFERRALS', '👥 Рефералов: {count} · +{earned}').format(
        count=invited,
        earned=texts.format_price(earned),
    )


async def build_main_menu_dashboard(
    user,
    texts,
    db: AsyncSession,
    *,
    subscription_status: str,
    include_traffic: bool = True,
) -> str:
    """Собирает текст-дашборд главного меню.

    Возвращает строку: имя пользователя + карточка подписки + карточка кабинета +
    приглашение к действию (``MAIN_MENU_ACTION_PROMPT``). Приглашение остаётся
    последним блоком, чтобы вызывающий код мог вставлять перед ним подсказки и
    случайные сообщения.
    """
    name = html.escape(getattr(user, 'full_name', '') or '')
    subscription = getattr(user, 'subscription', None)

    # --- Карточка подписки ---
    sub_lines = [line for line in (subscription_status or '').split('\n') if line.strip()]
    if include_traffic:
        traffic_line = _format_traffic_line(subscription, texts)
        if traffic_line:
            sub_lines.append(traffic_line)

    sub_title = texts.t('MENU_SECTION_SUBSCRIPTION', '📱 Подписка')
    sub_body = '\n'.join(sub_lines) if sub_lines else texts.t('SUB_STATUS_NONE', '❌ Отсутствует')
    sub_card = f'<blockquote><b>{sub_title}</b>\n{sub_body}</blockquote>'

    # --- Карточка кабинета ---
    cabinet_lines: list[str] = []
    balance = texts.format_price(getattr(user, 'balance_kopeks', 0) or 0)
    cabinet_lines.append(texts.t('MENU_DASH_BALANCE', '💰 Баланс: {balance}').format(balance=balance))

    referral_line = await _format_referral_line(user, texts, db)
    if referral_line:
        cabinet_lines.append(referral_line)

    cab_title = texts.t('MENU_DASH_CABINET', '💼 Кабинет')
    cabinet_card = f'<blockquote><b>{cab_title}</b>\n' + '\n'.join(cabinet_lines) + '</blockquote>'

    action_prompt = texts.t('MAIN_MENU_ACTION_PROMPT', 'Выберите действие:')

    return f'👤 <b>{name}</b>\n\n{sub_card}\n\n{cabinet_card}\n\n{action_prompt}'
