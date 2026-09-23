"""Уведомление человеку об автоназначении промогруппы за траты (issue #3271).

До этого о переходе в новую группу узнавал только админ: клиент не знал, что у
него появилась постоянная скидка, а у email-пользователей не было вообще
никакого канала узнать о новом уровне.

Доставка через ``notification_delivery_service``: Telegram, если он у человека
есть, иначе подтверждённая почта. Весь канал выключает
``PROMO_GROUP_AUTO_ASSIGN_NOTIFY_USER``, письмо отдельно — выключатель типа
``promo_group_auto_assigned`` в редакторе email-шаблонов. Здесь ничего не
бросается: сбой доставки не должен откатывать саму выдачу группы.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

import structlog

from app.config import settings
from app.database.models import PromoGroup, User
from app.services.notification_types import NotificationType


logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PromoGroupDiscounts:
    server_percent: int
    traffic_percent: int
    device_percent: int
    #: (дней, процент) по возрастанию периода, только ненулевые.
    periods: tuple[tuple[int, int], ...]

    @property
    def is_empty(self) -> bool:
        return not (self.server_percent or self.traffic_percent or self.device_percent or self.periods)


def collect_promo_group_discounts(group: PromoGroup) -> PromoGroupDiscounts:
    periods: list[tuple[int, int]] = []
    raw = group.period_discounts if isinstance(group.period_discounts, dict) else {}
    for key in raw:
        try:
            days = int(key)
        except (TypeError, ValueError):
            continue
        percent = group.get_discount_percent('period', days)
        if percent > 0:
            periods.append((days, percent))
    return PromoGroupDiscounts(
        server_percent=max(0, int(group.server_discount_percent or 0)),
        traffic_percent=max(0, int(group.traffic_discount_percent or 0)),
        device_percent=max(0, int(group.device_discount_percent or 0)),
        periods=tuple(sorted(periods)),
    )


def format_discounts_for_telegram(discounts: PromoGroupDiscounts, texts, language: str) -> str:
    from app.utils.pricing_utils import format_period_description

    lines: list[str] = []
    if discounts.server_percent:
        lines.append(texts.PROMO_GROUP_DISCOUNT_SERVERS.format(percent=discounts.server_percent))
    if discounts.traffic_percent:
        lines.append(texts.PROMO_GROUP_DISCOUNT_TRAFFIC.format(percent=discounts.traffic_percent))
    if discounts.device_percent:
        lines.append(texts.PROMO_GROUP_DISCOUNT_DEVICES.format(percent=discounts.device_percent))
    if discounts.periods:
        if lines:
            lines.append('')
        lines.append(texts.PROMO_GROUP_PERIOD_DISCOUNTS_HEADER)
        lines.extend(
            texts.PROMO_GROUP_PERIOD_DISCOUNT_ITEM.format(
                period=format_period_description(days, language),
                percent=percent,
            )
            for days, percent in discounts.periods
        )
    return '\n'.join(lines)


def format_period_discounts_plain(discounts: PromoGroupDiscounts, language: str) -> str:
    """«30 дней — 10%, 90 дней — 15%» для письма; пусто, если скидок по периодам нет."""
    from app.utils.pricing_utils import format_period_description

    return ', '.join(f'{format_period_description(days, language)} — {percent}%' for days, percent in discounts.periods)


async def notify_user_about_auto_assignment(user: User, group: PromoGroup, total_spent_kopeks: int) -> None:
    """Сообщить человеку о новой промогруппе. Никогда не бросает."""
    if not settings.PROMO_GROUP_AUTO_ASSIGN_NOTIFY_USER:
        return
    try:
        await _deliver(user, group, total_spent_kopeks)
    except Exception:
        logger.exception(
            'Не удалось уведомить пользователя об автоназначении промогруппы',
            user_id=getattr(user, 'id', None),
            promo_group_id=getattr(group, 'id', None),
        )


async def _deliver(user: User, group: PromoGroup, total_spent_kopeks: int) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.bot_factory import create_bot
    from app.localization.texts import get_texts
    from app.services.notification_delivery_service import notification_delivery_service

    language = user.language or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    discounts = collect_promo_group_discounts(group)
    total_spent = settings.format_price(total_spent_kopeks)
    group_name = str(group.name or '')

    key = 'PROMO_GROUP_AUTO_ASSIGNED_NO_DISCOUNTS' if discounts.is_empty else 'PROMO_GROUP_AUTO_ASSIGNED'
    message = texts.get(key)
    if not message:
        logger.warning('Нет ключа локализации для уведомления о промогруппе', text_key=key, language=language)
        return
    message = message.format(
        group_name=html.escape(group_name, quote=False),
        total_spent=html.escape(total_spent, quote=False),
        discounts=format_discounts_for_telegram(discounts, texts, language),
    )
    # Письму — сырые значения: его шаблон и редактор писем экранируют сами.
    email_context = {
        'group_name': group_name,
        'total_spent': total_spent,
        'period_discounts': format_period_discounts_plain(discounts, language),
        'server_discount': discounts.server_percent,
        'traffic_discount': discounts.traffic_percent,
        'device_discount': discounts.device_percent,
    }
    close = InlineKeyboardButton(text=texts.get('WEBHOOK_CLOSE_BUTTON', '✖️ Закрыть'), callback_data='webhook:close')
    markup = InlineKeyboardMarkup(inline_keyboard=[[close]])

    bot = create_bot(token=settings.BOT_TOKEN) if user.telegram_id and settings.BOT_TOKEN else None
    try:
        await notification_delivery_service.send_notification(
            user=user,
            notification_type=NotificationType.PROMO_GROUP_AUTO_ASSIGNED,
            context=email_context,
            bot=bot,
            telegram_message=message,
            telegram_markup=markup,
        )
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception as error:
                # Сообщение уже ушло (или нет) — незакрытая HTTP-сессия бота на это не влияет.
                logger.debug('Не удалось закрыть сессию бота после уведомления', error=str(error))
