import asyncio
import html
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog
from aiogram import Bot, types
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.exc import MissingGreenlet, NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.subscription_event import create_subscription_event
from app.database.crud.transaction import get_transaction_by_id
from app.database.crud.user import get_user_by_id
from app.database.database import AsyncSessionLocal
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    GuestPurchase,
    PromoCodeType,
    PromoGroup,
    Subscription,
    Transaction,
    User,
    WithdrawalRequest,
    WithdrawalRequestStatus,
)
from app.keyboards.group_callbacks import strip_group_unsafe_buttons
from app.localization.texts import Texts, get_texts
from app.utils.formatters import format_username_link
from app.utils.message_patch import caption_exceeds_telegram_limit
from app.utils.rich_admin import classic_admin_html_to_rich, try_send_rich_admin_message
from app.utils.timezone import format_local_datetime


# Стандартный формат Telegram bot token: `<numeric_id>:<random_35chars>`.
# Может появиться в str(e) от aiogram при сетевых ошибках, если транспорт
# (httpx/aiohttp) сериализует URL `https://api.telegram.org/bot<TOKEN>/...`.
# Не светим токен в логи (структурированные логи могут уехать в Sentry / ELK).
# Trailing — negative lookahead, а не `\b`: иначе токены, оканчивающиеся
# на `-` или `_`, теряли последний символ при редакции (1-char leak).
# Leading `(?<![\w-])` — намеренно НЕ матчит, если перед токеном стоит word/digit
# (например `foo123456789:AAH...`). Это trade-off против false-positive'ов
# на timestamp/UUID-подобных последовательностях. Aiogram и httpx всегда
# префиксят токен либо `bot`, либо URL-границей (`/`, пробел, кавычка),
# так что реальный corpus ошибок не страдает.
_BOT_TOKEN_RE: re.Pattern[str] = re.compile(
    r'(?<![\w-])(?:bot)?\d{6,}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])',
)


def _redact_telegram_secrets(text: str) -> str:
    """Replace Telegram bot tokens in an arbitrary string with a placeholder."""
    return _BOT_TOKEN_RE.sub('bot[REDACTED]', text)


def _format_waiting(waited_minutes: int, texts: Texts | None = None) -> str:
    """Сколько заявка ждёт решения: «3 д 4 ч», «2 ч 5 мин», «15 мин»."""
    texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
    total = max(0, int(waited_minutes))
    days, rest = divmod(total, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return (
            texts.t('ADMIN_NOTIFY_005', '{v0} д {v1} ч').format(v0=days, v1=hours)
            if hours
            else texts.t('ADMIN_NOTIFY_006', '{v0} д').format(v0=days)
        )
    if hours:
        return (
            texts.t('ADMIN_NOTIFY_007', '{v0} ч {v1} мин').format(v0=hours, v1=minutes)
            if minutes
            else texts.t('ADMIN_NOTIFY_008', '{v0} ч').format(v0=hours)
        )
    return texts.t('ADMIN_NOTIFY_000', '{v0} мин').format(v0=minutes)


_HTML_TAG_RE = re.compile(r'<[^<>]+>')


def _telegram_text_length(text: str) -> int:
    """Длина текста так, как её считает Telegram: после разбора HTML, в единицах UTF-16.

    Лимит sendMessage (4096) действует после разбора entities, а эмодзи и
    другие символы вне BMP занимают по две единицы.
    """
    plain = html.unescape(_HTML_TAG_RE.sub('', text))
    return len(plain.encode('utf-16-le')) // 2


class NotificationCategory(StrEnum):
    """Категории уведомлений для маршрутизации по топикам."""

    PURCHASES = 'purchases'  # Покупки подписок, покупки с лендинга
    RENEWALS = 'renewals'  # Продления
    TRIALS = 'trials'  # Триалы
    BALANCE = 'balance'  # Пополнение баланса
    ADDONS = 'addons'  # Докупка трафика/устройств/серверов
    INFRASTRUCTURE = 'infrastructure'  # Ноды, техработы, статус панели, вебхуки
    ERRORS = 'errors'  # Ошибки бота, краши
    PROMO = 'promo'  # Промокоды, кампании, промогруппы
    PARTNERS = 'partners'  # Партнёрки, выводы, админ-действия
    TICKETS = 'tickets'  # Тикеты (уже существует)


logger = structlog.get_logger(__name__)


def _loaded_relationship(instance: object, name: str) -> Any:
    """Значение связи, только если она УЖЕ загружена; иначе None и никакого IO.

    ``getattr(obj, name, None)`` для этого не годится: у незагруженной связи
    async-сессия не отдаёт None, а лезет в базу — и падает MissingGreenlet, потому
    что default у getattr срабатывает лишь на AttributeError.

    Ровно на этом падало уведомление о регистрации по рекламной кампании:
    apply_campaign_bonus перечитывает пользователя через ``db.refresh(user)``
    (сам по себе — фикс прошлого MissingGreenlet), а refresh сбрасывает ранее
    загруженные связи, включая promo_group.
    """
    try:
        state = sa_inspect(instance)
    except NoInspectionAvailable:
        # Не ORM-объект (тестовые фейки, SimpleNamespace) — обычный доступ безопасен.
        return getattr(instance, name, None)

    if name in state.unloaded:
        return None
    return state.dict.get(name)


class AdminNotificationService:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
        self.topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None)
        self.ticket_topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID', None)
        self.enabled = getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)

        # Маппинг категорий на topic_id (None = fallback на self.topic_id)
        self.category_topics: dict[NotificationCategory, int | None] = {
            NotificationCategory.PURCHASES: getattr(settings, 'ADMIN_NOTIFICATIONS_PURCHASES_TOPIC_ID', None),
            NotificationCategory.RENEWALS: getattr(settings, 'ADMIN_NOTIFICATIONS_RENEWALS_TOPIC_ID', None),
            NotificationCategory.TRIALS: getattr(settings, 'ADMIN_NOTIFICATIONS_TRIALS_TOPIC_ID', None),
            NotificationCategory.BALANCE: getattr(settings, 'ADMIN_NOTIFICATIONS_BALANCE_TOPIC_ID', None),
            NotificationCategory.ADDONS: getattr(settings, 'ADMIN_NOTIFICATIONS_ADDONS_TOPIC_ID', None),
            NotificationCategory.INFRASTRUCTURE: getattr(settings, 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_TOPIC_ID', None),
            NotificationCategory.ERRORS: getattr(settings, 'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID', None),
            NotificationCategory.PROMO: getattr(settings, 'ADMIN_NOTIFICATIONS_PROMO_TOPIC_ID', None),
            NotificationCategory.PARTNERS: getattr(settings, 'ADMIN_NOTIFICATIONS_PARTNERS_TOPIC_ID', None),
            NotificationCategory.TICKETS: self.ticket_topic_id,
        }

        # Per-category enabled flags (default True — backwards compatible)
        self.category_enabled: dict[NotificationCategory, bool] = {}
        for cat in NotificationCategory:
            key = f'ADMIN_NOTIFICATIONS_{cat.value.upper()}_ENABLED'
            self.category_enabled[cat] = getattr(settings, key, True)

    async def _notification_texts(self, db: AsyncSession | None = None) -> Texts:
        """Resolve the recipient locale once per event, never the affected user's locale."""
        fallback = get_texts(settings.DEFAULT_LANGUAGE)
        if not self._is_enabled() or self.resolve_recipient_role() not in {'admin', 'moderator'}:
            return fallback
        # A shared forum topic has no individual recipient, even with a positive configured ID.
        if self.topic_id or self.ticket_topic_id or any(self.category_topics.values()):
            return fallback
        try:
            recipient_id = int(self.chat_id)
            query = select(User.language).where(User.telegram_id == recipient_id)
            if db is not None:
                # Presentation lookup must not flush the caller's pending transaction.
                with db.no_autoflush:
                    language = await db.scalar(query)
            else:
                async with AsyncSessionLocal() as session:
                    language = await session.scalar(query)
            return get_texts(language) if isinstance(language, str) and language else fallback
        except Exception:
            logger.debug('Admin notification recipient language unavailable; using configured default')
            return fallback

    async def _get_referrer_info(self, db: AsyncSession, referred_by_id: int | None, texts: Texts | None = None) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        if not referred_by_id:
            return texts.t('ADMIN_BACKUP_UI_035', 'Нет')

        try:
            referrer = await get_user_by_id(db, referred_by_id)
            if not referrer:
                return texts.t('ADMIN_NOTIFY_042', 'ID {v0} (не найден)').format(v0=referred_by_id)

            if referrer.username:
                return f'{format_username_link(referrer.username)} (ID: {referred_by_id})'
            if referrer.telegram_id:
                return f'ID {referrer.telegram_id}'
            if referrer.email:
                return f'📧 {html.escape(referrer.email)}'
            return f'User#{referred_by_id}'

        except Exception as e:
            logger.error('Ошибка получения данных рефера', referred_by_id=referred_by_id, error=e)
            return f'ID {referred_by_id}'

    async def _get_user_promo_group(self, db: AsyncSession, user: User) -> PromoGroup | None:
        promo_group = _loaded_relationship(user, 'promo_group')
        if promo_group:
            return promo_group

        try:
            promo_group_id = user.promo_group_id
        except Exception:
            # Инстанс отвязан от сессии или протух — колонка тоже ушла бы в ленивую
            # подгрузку. Берём последнее известное значение из __dict__.
            promo_group_id = user.__dict__.get('promo_group_id')
        if not promo_group_id:
            return None

        try:
            await db.refresh(user, attribute_names=['promo_group'])
        except Exception:
            # relationship might not be available — fallback to direct fetch
            pass

        promo_group = _loaded_relationship(user, 'promo_group')
        if promo_group:
            return promo_group

        try:
            return await get_promo_group_by_id(db, promo_group_id)
        except Exception as e:
            logger.error(
                'Ошибка загрузки промогруппы пользователя',
                promo_group_id=promo_group_id,
                telegram_id=user.__dict__.get('telegram_id'),
                e=e,
            )
            return None

    def _get_user_display(self, user: User) -> str:
        first_name = getattr(user, 'first_name', '') or ''
        if first_name:
            return html.escape(first_name)

        username = getattr(user, 'username', '') or ''
        if username:
            return html.escape(username)

        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id is None:
            email = getattr(user, 'email', None)
            if email:
                return html.escape(email)
            return f'User#{getattr(user, "id", "Unknown")}'
        return f'ID{telegram_id}'

    def _get_user_identifier_display(self, user: User) -> str:
        """Get user identifier for display in notifications (telegram_id or email)."""
        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id:
            return f'<code>{telegram_id}</code>'

        email = getattr(user, 'email', None)
        if email:
            return f'📧 {html.escape(email)}'

        return f'User#{getattr(user, "id", "Unknown")}'

    def _get_user_identifier_label(self, user: User) -> str:
        """Get label for user identifier (Telegram ID or Email)."""
        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id:
            return 'Telegram ID'
        email = getattr(user, 'email', None)
        if email:
            return 'Email'
        return 'ID'

    async def _record_subscription_event(
        self,
        db: AsyncSession,
        *,
        event_type: str,
        user: User,
        subscription: Subscription | None,
        transaction: Transaction | None = None,
        amount_kopeks: int | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> bool:
        """Persist an event; False means callers must reload ORM data after rollback."""

        user_id = user.id
        try:
            await create_subscription_event(
                db,
                user_id=user_id,
                event_type=event_type,
                subscription_id=subscription.id if subscription else None,
                transaction_id=transaction.id if transaction else None,
                amount_kopeks=amount_kopeks,
                currency=None,
                message=message,
                occurred_at=occurred_at,
                extra=extra or None,
            )
            return True
        except Exception:
            logger.error(
                'Не удалось сохранить событие подписки для пользователя',
                event_type=event_type,
                user_id=user_id,
                exc_info=True,
            )

            try:
                await db.rollback()
            except Exception:
                logger.error(
                    'Не удалось выполнить rollback после ошибки события подписки пользователя',
                    user_id=user_id,
                    exc_info=True,
                )
            return False

    def _format_promo_group_discounts(self, promo_group: PromoGroup, texts: Texts | None = None) -> list[str]:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        discount_lines: list[str] = []

        discount_map = {
            'servers': (texts.t('MINIAPP_PURCHASE_BREAKDOWN_SERVERS', 'Серверы'), promo_group.server_discount_percent),
            'traffic': (texts.t('TRAFFIC', 'Трафик'), promo_group.traffic_discount_percent),
            'devices': (texts.t('DEVICES', 'Устройства'), promo_group.device_discount_percent),
        }

        for title, percent in discount_map.values():
            if percent and percent > 0:
                discount_lines.append(f'• {title}: -{percent}%')

        period_discounts_raw = promo_group.period_discounts or {}
        period_items: list[tuple[int, int]] = []

        if isinstance(period_discounts_raw, dict):
            for raw_days, raw_percent in period_discounts_raw.items():
                try:
                    days = int(raw_days)
                    percent = int(raw_percent)
                except (TypeError, ValueError):
                    continue

                if percent > 0:
                    period_items.append((days, percent))

        period_items.sort(key=lambda item: item[0])

        if period_items:
            formatted_periods = ', '.join(
                texts.t('ADMIN_NOTIFY_113', '{v0} д. — -{v1}%').format(v0=days, v1=percent)
                for days, percent in period_items
            )
            discount_lines.append(texts.t('ADMIN_NOTIFY_046', '• Периоды: {v0}').format(v0=formatted_periods))

        if promo_group.apply_discounts_to_addons:
            discount_lines.append(texts.t('ADMIN_NOTIFY_047', '• Доп. услуги: ✅ скидка действует'))
        else:
            discount_lines.append(texts.t('ADMIN_NOTIFY_048', '• Доп. услуги: ❌ без скидки'))

        return discount_lines

    def _format_promo_group_block(
        self, promo_group: PromoGroup | None, *, title: str | None = None, icon: str = '🏷️', texts: Texts | None = None
    ) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        if title is None:
            title = texts.t('ADMIN_PROMOCODE_UI_050', 'Промогруппа')
        if not promo_group:
            return f'{icon} <b>{title}:</b> —'

        lines = [f'{icon} <b>{title}:</b> {html.escape(promo_group.name)}']

        discount_lines = self._format_promo_group_discounts(promo_group, texts=texts)
        if discount_lines:
            lines.append(texts.t('ADMIN_NOTIFY_049', '💸 <b>Скидки:</b>'))
            lines.extend(discount_lines)
        else:
            lines.append(texts.t('ADMIN_NOTIFY_050', '💸 <b>Скидки:</b> отсутствуют'))

        return '\n'.join(lines)

    def _get_promocode_type_display(self, promo_type: str | None, texts: Texts | None = None) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        mapping = {
            PromoCodeType.BALANCE.value: texts.t('ADMIN_CAMPAIGN_BONUS_BALANCE', '💰 Бонус на баланс'),
            PromoCodeType.SUBSCRIPTION_DAYS.value: texts.t('ADMIN_NOTIFY_011', '⏰ Доп. дни подписки'),
            PromoCodeType.TRIAL_SUBSCRIPTION.value: texts.t('ADMIN_NOTIFY_012', '🎁 Триал подписка'),
            PromoCodeType.PROMO_GROUP.value: texts.t('ADMIN_USER_PROMO_GROUP_BUTTON', '👥 Промогруппа'),
            PromoCodeType.DISCOUNT.value: texts.t('ADMIN_PROMO_OFFER_LOGS_EFFECT_DISCOUNT', '💸 Скидка'),
            PromoCodeType.BALANCE_AND_DAYS.value: texts.t('ADMIN_PROMOCODE_UI_029', '💰📅 Баланс + дни подписки'),
        }

        if not promo_type:
            return texts.t('ADMIN_NOTIFY_016', 'ℹ️ Не указан')

        return mapping.get(promo_type, f'ℹ️ {promo_type}')

    def _format_campaign_bonus(
        self, campaign: AdvertisingCampaign, *, tariff_name: str | None = None, texts: Texts | None = None
    ) -> list[str]:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        if campaign.is_balance_bonus:
            return [
                texts.t('ADMIN_NOTIFY_051', '💰 Баланс: {v0}').format(
                    v0=settings.format_price(campaign.balance_bonus_kopeks or 0)
                ),
            ]

        if campaign.is_subscription_bonus:
            default_devices = getattr(settings, 'DEFAULT_DEVICE_LIMIT', 1)
            details = [
                (
                    texts.t('ADMIN_NOTIFY_052', '📅 {v0} дн. • 📊 {v1} ГБ • 📱 {v2} устр.').format(
                        v0=campaign.subscription_duration_days or 0,
                        v1=campaign.subscription_traffic_gb or 0,
                        v2=campaign.subscription_device_limit or default_devices,
                    )
                ),
            ]
            if campaign.subscription_squads:
                details.append(
                    texts.t('ADMIN_NOTIFY_114', '🌐 Сквады: {v0} шт.').format(v0=len(campaign.subscription_squads))
                )
            return details

        if campaign.is_tariff_bonus:
            name = tariff_name or f'ID {campaign.tariff_id}'
            details = [texts.t('ADMIN_NOTIFY_053', '📦 Тариф: <b>{v0}</b>').format(v0=name)]
            if campaign.tariff_duration_days:
                details.append(
                    texts.t('ADMIN_NOTIFY_115', '📅 Период: {v0} дней').format(v0=campaign.tariff_duration_days)
                )
            return details

        if campaign.is_none_bonus:
            return [texts.t('ADMIN_NOTIFY_054', '🔗 Только отслеживание')]

        return [texts.t('ADMIN_NOTIFY_017', 'ℹ️ Бонусы не предусмотрены')]

    async def send_trial_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        *,
        charged_amount_kopeks: int | None = None,
    ) -> bool:
        texts = await self._notification_texts(db)
        try:
            await self._record_subscription_event(
                db,
                event_type='activation',
                user=user,
                subscription=subscription,
                transaction=None,
                amount_kopeks=charged_amount_kopeks,
                message='Trial activation',
                occurred_at=datetime.now(UTC),
                extra={
                    'charged_amount_kopeks': charged_amount_kopeks,
                    'trial_duration_days': (
                        max(1, round((subscription.end_date - subscription.start_date).total_seconds() / 86400))
                        if subscription.end_date and subscription.start_date
                        else settings.TRIAL_DURATION_DAYS
                    ),
                    'traffic_limit_gb': (
                        subscription.traffic_limit_gb
                        if subscription.traffic_limit_gb is not None
                        else settings.TRIAL_TRAFFIC_LIMIT_GB
                    ),
                    'device_limit': subscription.device_limit,
                },
            )

            if not self._is_enabled():
                return False

            user_status = (
                texts.t('ADMIN_NOTIFY_055', '🆕 Новый')
                if not user.has_had_paid_subscription
                else texts.t('ADMIN_NOTIFY_056', '🔄 Существующий')
            )
            promo_group = await self._get_user_promo_group(db, user)
            user_display = self._get_user_display(user)

            trial_device_limit = subscription.device_limit
            if trial_device_limit is None:
                fallback_forced_limit = settings.get_disabled_mode_device_limit()
                if fallback_forced_limit is not None:
                    trial_device_limit = fallback_forced_limit
                else:
                    trial_device_limit = settings.TRIAL_DEVICE_LIMIT

            payment_block = ''
            if charged_amount_kopeks and charged_amount_kopeks > 0:
                payment_block = texts.t('ADMIN_NOTIFY_057', '\n💳 <b>Оплата за активацию:</b> {v0}').format(
                    v0=settings.format_price(charged_amount_kopeks)
                )

            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            # Получаем название тарифа (если режим тарифов)
            tariff_name = await self._get_tariff_name(db, subscription)

            message_lines = [
                texts.t('ADMIN_NOTIFY_058', '🎯 <b>АКТИВАЦИЯ ТРИАЛА</b>'),
                '',
                texts.t('ADMIN_NOTIFY_059', '👤 <b>Пользователь:</b> {v0}').format(v0=user_display),
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> {format_username_link(getattr(user, "username", None), texts.t("ADMIN_NOTIFY_088", "отсутствует"))}',
                texts.t('ADMIN_NOTIFY_060', '👥 <b>Статус:</b> {v0}').format(v0=user_status),
                '',
            ]

            # Промогруппа — только название, без скидок
            if promo_group:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_116', '🏷️ <b>Промогруппа:</b> {v0}').format(v0=html.escape(promo_group.name))
                )
            else:
                message_lines.append(texts.t('ADMIN_NOTIFY_117', '🏷️ <b>Промогруппа:</b> —'))

            # Тариф триала (если есть)
            if tariff_name:
                message_lines.append(texts.t('ADMIN_NOTIFY_118', '📦 <b>Тариф:</b> {v0}').format(v0=tariff_name))

            message_lines.append('')

            trial_duration_days = settings.TRIAL_DURATION_DAYS
            if subscription.end_date and subscription.start_date:
                trial_duration_days = max(
                    1, round((subscription.end_date - subscription.start_date).total_seconds() / 86400)
                )

            trial_traffic_gb = (
                subscription.traffic_limit_gb
                if subscription.traffic_limit_gb is not None
                else settings.TRIAL_TRAFFIC_LIMIT_GB
            )

            message_lines.extend(
                [
                    texts.t('ADMIN_NOTIFY_119', '⏰ <b>Параметры триала:</b>'),
                    texts.t('ADMIN_NOTIFY_115', '📅 Период: {v0} дней').format(v0=trial_duration_days),
                    texts.t('ADMIN_NOTIFY_120', '📊 Трафик: {v0}').format(
                        v0=self._format_traffic(trial_traffic_gb, texts=texts)
                    ),
                    texts.t('ADMIN_NOTIFY_121', '📱 Устройства: {v0}').format(v0=trial_device_limit),
                    texts.t('ADMIN_NOTIFY_122', '🌐 Сервер: {v0}').format(
                        v0=subscription.connected_squads[0]
                        if subscription.connected_squads
                        else texts.t('ADMIN_NOTIFY_205', 'По умолчанию')
                    ),
                ]
            )

            if payment_block:
                message_lines.append(payment_block)

            message_lines.append('')
            message_lines.append(
                texts.t('ADMIN_NOTIFY_061', '📆 <b>Действует до:</b> {v0}').format(
                    v0=format_local_datetime(subscription.end_date, '%d.%m.%Y %H:%M')
                )
            )

            # Реферер — только если есть
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id, texts=texts)
                if referrer_info not in ('Нет', texts.t('ADMIN_BACKUP_UI_035', 'Нет')):
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_169', '🔗 <b>Реферер:</b> {v0}').format(v0=referrer_info)
                    )

            message_lines.append('')
            message_lines.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.TRIALS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о триале', error=e)
            return False

    async def _get_tariff_name(self, db: AsyncSession, subscription: Subscription) -> str | None:
        """Получает название тарифа подписки, если он есть."""
        if not subscription.tariff_id:
            return None

        try:
            from app.database.crud.tariff import get_tariff_by_id

            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff:
                return html.escape(tariff.name)
        except Exception:
            pass
        return None

    async def send_subscription_purchase_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        transaction: Transaction | None,
        period_days: int,
        was_trial_conversion: bool = False,
        amount_kopeks: int | None = None,
        purchase_type: str | None = None,  # 'first_purchase', 'renewal', 'tariff_switch', None (auto-detect)
    ) -> bool:
        texts = await self._notification_texts(db)
        try:
            total_amount = (
                amount_kopeks if amount_kopeks is not None else (abs(transaction.amount_kopeks) if transaction else 0)
            )

            await self._record_subscription_event(
                db,
                event_type='purchase',
                user=user,
                subscription=subscription,
                transaction=transaction,
                amount_kopeks=total_amount,
                message='Subscription purchase',
                occurred_at=transaction.completed_at or transaction.created_at if transaction else datetime.now(UTC),
                extra={
                    'period_days': period_days,
                    'was_trial_conversion': was_trial_conversion,
                    'payment_method': self._get_payment_method_display(transaction.payment_method)
                    if transaction
                    else 'Баланс',
                },
            )

            if not self._is_enabled():
                return False

            # Определяем тип операции и заголовок
            if purchase_type == 'tariff_switch':
                event_title = texts.t('ADMIN_NOTIFY_062', '🔄 СМЕНА ТАРИФА')
                user_status = texts.t('SETTINGS_CATEGORY_TARIFF_LABEL', 'Смена тарифа')
            elif was_trial_conversion:
                event_title = texts.t('ADMIN_NOTIFY_123', '🔄 КОНВЕРСИЯ ИЗ ТРИАЛА')
                user_status = texts.t('ADMIN_NOTIFY_124', 'Конверсия')
            elif purchase_type == 'first_purchase':
                event_title = texts.t('ADMIN_NOTIFY_170', '💎 ПОКУПКА ПОДПИСКИ')
                user_status = texts.t('ADMIN_NOTIFY_171', 'Первая покупка')
            elif purchase_type == 'renewal' or (purchase_type is None and user.has_had_paid_subscription):
                event_title = texts.t('ADMIN_NOTIFY_195', '💎 ПРОДЛЕНИЕ ПОДПИСКИ')
                user_status = texts.t('ADMIN_NOTIFY_196', 'Продление')
            else:
                event_title = texts.t('ADMIN_NOTIFY_170', '💎 ПОКУПКА ПОДПИСКИ')
                user_status = texts.t('ADMIN_NOTIFY_171', 'Первая покупка')

            # Получаем название тарифа
            tariff_name = await self._get_tariff_name(db, subscription)

            servers_info = await self._get_servers_info(subscription.connected_squads, texts=texts)
            payment_method = (
                self._get_payment_method_display(transaction.payment_method, texts=texts)
                if transaction
                else texts.t('ADMIN_NOTIFY_064', 'Баланс')
            )
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            # Формируем компактное сообщение
            message_lines = [
                f'<b>{event_title}</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            # Добавляем username только если есть
            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 {format_username_link(username)}')

            message_lines.append(f'📋 {user_status}')

            # Тариф (если есть)
            if tariff_name:
                message_lines.append(texts.t('ADMIN_NOTIFY_086', '🏷️ Тариф: <b>{v0}</b>').format(v0=tariff_name))

            message_lines.extend(
                [
                    '',
                    f'💵 <b>{settings.format_price(total_amount)}</b> • {payment_method}',
                    texts.t('ADMIN_NOTIFY_125', '📅 {v0} дн. • до {v1}').format(
                        v0=period_days, v1=format_local_datetime(subscription.end_date, '%d.%m.%Y')
                    ),
                    texts.t('ADMIN_NOTIFY_126', '📊 {v0} • 📱 {v1} устр.').format(
                        v0=self._format_traffic(subscription.traffic_limit_gb, texts=texts),
                        v1=subscription.device_limit,
                    ),
                    f'🌐 {servers_info}',
                ]
            )

            # Баланс после покупки
            message_lines.append(
                texts.t('ADMIN_NOTIFY_051', '💰 Баланс: {v0}').format(v0=settings.format_price(user.balance_kopeks))
            )

            # Реферер (только если есть)
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id, texts=texts)
                if referrer_info not in ('Нет', texts.t('ADMIN_BACKUP_UI_035', 'Нет')):
                    message_lines.append(texts.t('ADMIN_NOTIFY_172', '🔗 Реф: {v0}').format(v0=referrer_info))

            # ID транзакции (только если есть)
            if transaction:
                message_lines.append(f'🆔 #{transaction.id}')

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>',
                ]
            )

            # Маршрутизация по категориям (зеркалит логику заголовков выше)
            if purchase_type == 'renewal' or (
                not was_trial_conversion and purchase_type is None and user.has_had_paid_subscription
            ):
                cat = NotificationCategory.RENEWALS
            else:
                cat = NotificationCategory.PURCHASES

            return await self._send_message('\n'.join(message_lines), category=cat)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о покупке', error=e)
            return False

    async def send_version_update_notification(self, current_version: str, latest_version, total_updates: int) -> bool:
        """Отправляет уведомление о новых обновлениях."""
        texts = await self._notification_texts()
        if not self._is_enabled():
            return False

        try:
            from app.utils.markdown_to_telegram import github_markdown_to_telegram_html, truncate_for_blockquote

            repo = getattr(settings, 'VERSION_CHECK_REPO', 'fr1ngg/remnawave-bedolaga-telegram-bot')
            release_url = f'https://github.com/{repo}/releases/tag/{latest_version.tag_name}'
            repo_url = f'https://github.com/{repo}'
            timestamp = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')

            if latest_version.prerelease:
                header = '🧪 <b>Pre-release</b>'
            elif latest_version.is_dev:
                header = '🔧 <b>Dev build</b>'
            else:
                header = texts.t('ADMIN_NOTIFY_127', '🆕 <b>Доступно обновление</b>')

            # -- message prefix (everything before blockquote) --
            prefix_lines = [
                header,
                '',
                f'<code>{current_version}</code>  →  <b><a href="{release_url}">{latest_version.tag_name}</a></b>',
                f'📅 {latest_version.formatted_date}',
                '',
            ]
            message_prefix = '\n'.join(prefix_lines)

            # -- message suffix (everything after blockquote) --
            suffix_lines = ['']
            if total_updates > 1:
                suffix_lines.append(
                    texts.t('ADMIN_NOTIFY_128', 'Доступно обновлений: <b>{v0}</b>').format(v0=total_updates)
                )
            suffix_lines.extend(
                [
                    texts.t('ADMIN_NOTIFY_129', '<a href="{v0}">Репозиторий</a>').format(v0=repo_url),
                    '',
                    f'<i>{timestamp}</i>',
                ]
            )
            message_suffix = '\n'.join(suffix_lines)

            # -- description in blockquote --
            raw_description = getattr(latest_version, 'full_description', '') or latest_version.short_description
            description_html = github_markdown_to_telegram_html(raw_description)

            if description_html:
                description_html = truncate_for_blockquote(
                    description_html,
                    message_prefix=message_prefix,
                    message_suffix=message_suffix,
                )
                message = f'{message_prefix}<blockquote expandable>{description_html}</blockquote>{message_suffix}'
            else:
                message = f'{message_prefix}{message_suffix}'

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об обновлении', error=e)
            return False

    async def send_version_check_error_notification(self, error_message: str, current_version: str) -> bool:
        texts = await self._notification_texts()
        if not self._is_enabled():
            return False

        try:
            message = texts.t(
                'ADMIN_NOTIFY_018',
                '⚠️ <b>ОШИБКА ПРОВЕРКИ ОБНОВЛЕНИЙ</b>\n\n    📦 <b>Текущая версия:</b> <code>{v0}</code>\n    ❌ <b>Ошибка:</b> {v1}\n\n    🔄 Следующая попытка через час.\n    ⚙️ Проверьте доступность GitHub API и настройки сети.\n\n    ⚙️ <i>Система автоматических обновлений • {v2}</i>',
            ).format(
                v0=current_version, v1=error_message, v2=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
            )

            return await self._send_message(message, category=NotificationCategory.ERRORS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об ошибке проверки версий', error=e)
            return False

    def _build_balance_topup_message(
        self,
        user: User,
        transaction: Transaction,
        old_balance: int,
        *,
        topup_status: str,
        referrer_info: str,
        subscription: Subscription | None,
        promo_group: PromoGroup | None,
        texts: Texts | None = None,
    ) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        payment_method = self._get_payment_method_display(transaction.payment_method, texts=texts)
        balance_change = user.balance_kopeks - old_balance
        subscription_status = self._get_subscription_status(subscription, texts=texts)
        timestamp = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
        user_display = self._get_user_display(user)
        user_id_display = self._get_user_identifier_display(user)

        # --- Основной блок ---
        message_lines: list[str] = [
            texts.t('ADMIN_NOTIFY_019', '💰 <b>ПОПОЛНЕНИЕ БАЛАНСА</b>'),
            '',
            f'👤 {user_display} ({user_id_display})',
        ]

        username = getattr(user, 'username', None)
        if username:
            message_lines.append(f'📱 {format_username_link(username)}')

        # These four labels are generated by our payment senders; other text is caller-owned.
        topup_labels = {
            '🆕 Первое пополнение': texts.t('ADMIN_NOTIFY_FIRST_TOPUP_ICON', '🆕 Первое пополнение'),
            '🔄 Пополнение': texts.t('ADMIN_NOTIFY_TOPUP_ICON', '🔄 Пополнение'),
            'Первое пополнение': texts.t('ADMIN_NOTIFY_FIRST_TOPUP', 'Первое пополнение'),
            'Пополнение': texts.t('ADMIN_NOTIFY_TOPUP', 'Пополнение'),
        }
        message_lines.append(f'💳 {topup_labels.get(topup_status, topup_status)}')

        # Промогруппа -- только название
        if promo_group:
            message_lines.append(
                texts.t('ADMIN_NOTIFY_065', '🏷️ Промогруппа: {v0}').format(v0=html.escape(promo_group.name))
            )

        message_lines.append('')

        # --- Детали пополнения ---
        message_lines.extend(
            [
                f'💵 <b>{settings.format_price(transaction.amount_kopeks)}</b> | {payment_method}',
                '',
                (
                    f'📉 {settings.format_price(old_balance)} → 📈 {settings.format_price(user.balance_kopeks)}'
                    f' (<b>+{settings.format_price(balance_change)}</b>)'
                ),
            ]
        )

        # --- Подписка ---
        message_lines.append(texts.t('ADMIN_NOTIFY_020', '📱 Подписка: {v0}').format(v0=subscription_status))

        # --- Реферер (только если есть) ---
        if referrer_info and referrer_info not in ('Нет', texts.t('ADMIN_BACKUP_UI_035', 'Нет')):
            message_lines.append(texts.t('ADMIN_NOTIFY_066', '🔗 Реферер: {v0}').format(v0=referrer_info))

        # --- Expandable blockquote с техническими деталями ---
        detail_lines: list[str] = [
            texts.t('ADMIN_NOTIFY_021', 'ID транзакции: {v0}').format(v0=transaction.id),
            texts.t('ADMIN_NOTIFY_022', 'Способ оплаты: {v0}').format(v0=transaction.payment_method or 'balance'),
        ]

        if transaction.external_id:
            detail_lines.append(texts.t('ADMIN_NOTIFY_067', 'Внешний ID: {v0}').format(v0=transaction.external_id))

        if transaction.description:
            desc = transaction.description
            if len(desc) > 120:
                desc = desc[:117] + '...'
            detail_lines.append(texts.t('ADMIN_NOTIFY_068', 'Описание: {v0}').format(v0=html.escape(desc)))

        if transaction.created_at:
            detail_lines.append(
                texts.t('ADMIN_NOTIFY_069', 'Создана: {v0}').format(
                    v0=format_local_datetime(transaction.created_at, '%d.%m.%Y %H:%M:%S')
                )
            )

        if transaction.completed_at:
            detail_lines.append(
                texts.t('ADMIN_NOTIFY_070', 'Завершена: {v0}').format(
                    v0=format_local_datetime(transaction.completed_at, '%d.%m.%Y %H:%M:%S')
                )
            )

        if transaction.receipt_uuid:
            detail_lines.append(texts.t('ADMIN_NOTIFY_071', 'Чек UUID: {v0}').format(v0=transaction.receipt_uuid))

        blockquote_body = '\n'.join(detail_lines)
        message_lines.extend(
            [
                '',
                f'<blockquote expandable>{blockquote_body}</blockquote>',
            ]
        )

        message_lines.append(f'<i>{timestamp}</i>')

        return '\n'.join(message_lines)

    async def _reload_topup_notification_entities(
        self,
        db: AsyncSession,
        user: User,
        transaction: Transaction,
    ) -> tuple[User, Transaction, Subscription | None, PromoGroup | None]:
        refreshed_user = await get_user_by_id(db, user.id)
        if not refreshed_user:
            raise ValueError(f'Не удалось повторно загрузить пользователя {user.id} для уведомления о пополнении')

        refreshed_transaction = await get_transaction_by_id(db, transaction.id)
        if not refreshed_transaction:
            raise ValueError(f'Не удалось повторно загрузить транзакцию {transaction.id} для уведомления о пополнении')

        subscription = getattr(refreshed_user, 'subscription', None)
        promo_group = await self._get_user_promo_group(db, refreshed_user)

        return refreshed_user, refreshed_transaction, subscription, promo_group

    def _is_lazy_loading_error(self, error: Exception) -> bool:
        message = str(error).lower()
        return (
            isinstance(error, MissingGreenlet)
            or 'greenlet_spawn' in message
            or 'await_only' in message
            or 'missinggreenlet' in message
        )

    async def send_balance_topup_notification(
        self,
        user: User,
        transaction: Transaction,
        old_balance: int,
        *,
        topup_status: str,
        referrer_info: str,
        subscription: Subscription | None,
        promo_group: PromoGroup | None,
        db: AsyncSession | None = None,
    ) -> bool:
        texts = await self._notification_texts(db)
        logger.info('Начинаем отправку уведомления о пополнении баланса')

        if db:
            # Failed flush expires even primary keys. Keep scalar identities for
            # logging/reload before recording the optional notification event.
            user_id = user.id
            transaction_id = transaction.id
            try:
                recorded = await self._record_subscription_event(
                    db,
                    event_type='balance_topup',
                    user=user,
                    subscription=subscription,
                    transaction=transaction,
                    amount_kopeks=transaction.amount_kopeks,
                    message='Balance top-up',
                    occurred_at=transaction.completed_at or transaction.created_at,
                    extra={
                        'status': topup_status,
                        'balance_before': old_balance,
                        'balance_after': user.balance_kopeks,
                        'referrer_info': referrer_info,
                        'promo_group_id': getattr(promo_group, 'id', None),
                        'promo_group_name': getattr(promo_group, 'name', None),
                    },
                )
            except Exception:
                logger.error(
                    'Не удалось сохранить событие пополнения баланса пользователя',
                    user_id=user_id,
                    exc_info=True,
                )
                try:
                    await db.rollback()
                except Exception:
                    logger.error('Не удалось откатить сессию уведомления о пополнении', user_id=user_id, exc_info=True)
                    return False
                recorded = False

            if recorded is False:
                try:
                    user = await get_user_by_id(db, user_id)
                    transaction = await get_transaction_by_id(db, transaction_id)
                    if user is None or transaction is None:
                        logger.warning(
                            'Данные уведомления о пополнении отсутствуют после rollback',
                            user_id=user_id,
                            transaction_id=transaction_id,
                        )
                        return False
                    subscription = getattr(user, 'subscription', None)
                    promo_group = await self._get_user_promo_group(db, user)
                except Exception:
                    logger.error(
                        'Ошибка повторной загрузки данных уведомления после rollback',
                        user_id=user_id,
                        transaction_id=transaction_id,
                        exc_info=True,
                    )
                    return False

        if not self._is_enabled():
            return False

        try:
            logger.info('Пытаемся создать сообщение уведомления')
            message = self._build_balance_topup_message(
                user,
                transaction,
                old_balance,
                topup_status=topup_status,
                referrer_info=referrer_info,
                subscription=subscription,
                promo_group=promo_group,
                texts=texts,
            )
            logger.info('Сообщение уведомления создано успешно')
        except Exception as error:
            logger.info(
                'Перехвачена ошибка при создании сообщения уведомления', __name__=type(error).__name__, error=error
            )
            if not self._is_lazy_loading_error(error):
                logger.error('Ошибка подготовки уведомления о пополнении', error=error, exc_info=True)
                return False

            if db is None:
                logger.error(
                    'Недостаточно данных для уведомления о пополнении и отсутствует доступ к БД',
                    error=error,
                    exc_info=True,
                )
                return False

            logger.warning(
                'Повторная загрузка данных для уведомления о пополнении после ошибки ленивой загрузки', error=error
            )

            try:
                logger.info('Пытаемся перезагрузить данные для уведомления')
                (
                    user,
                    transaction,
                    subscription,
                    promo_group,
                ) = await self._reload_topup_notification_entities(db, user, transaction)
                logger.info('Данные успешно перезагружены')
            except Exception as reload_error:
                logger.error(
                    'Ошибка повторной загрузки данных для уведомления о пополнении',
                    reload_error=reload_error,
                    exc_info=True,
                )
                return False

            try:
                logger.info('Пытаемся создать сообщение после перезагрузки данных')
                message = self._build_balance_topup_message(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    texts=texts,
                )
                logger.info('Сообщение успешно создано после перезагрузки данных')
            except Exception as rebuild_error:
                logger.error(
                    'Ошибка повторной подготовки уведомления о пополнении после повторной загрузки',
                    rebuild_error=rebuild_error,
                    exc_info=True,
                )
                return False

        try:
            return await self._send_message(message, category=NotificationCategory.BALANCE)
        except Exception as e:
            logger.error('Ошибка отправки уведомления о пополнении', error=e, exc_info=True)
            return False

    async def send_subscription_extension_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        transaction: Transaction,
        extended_days: int,
        old_end_date: datetime,
        *,
        new_end_date: datetime | None = None,
        balance_after: int | None = None,
    ) -> bool:
        texts = await self._notification_texts(db)
        try:
            current_end_date = new_end_date or subscription.end_date
            current_balance = balance_after if balance_after is not None else user.balance_kopeks

            await self._record_subscription_event(
                db,
                event_type='renewal',
                user=user,
                subscription=subscription,
                transaction=transaction,
                amount_kopeks=abs(transaction.amount_kopeks),
                message='Subscription renewed',
                occurred_at=transaction.completed_at or transaction.created_at,
                extra={
                    'extended_days': extended_days,
                    'previous_end_date': old_end_date.isoformat(),
                    'new_end_date': current_end_date.isoformat(),
                    'payment_method': transaction.payment_method,
                    'balance_after': current_balance,
                },
            )

            if not self._is_enabled():
                return False

            payment_method = self._get_payment_method_display(transaction.payment_method, texts=texts)
            servers_info = await self._get_servers_info(subscription.connected_squads, texts=texts)
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group, texts=texts)
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message = texts.t(
                'ADMIN_NOTIFY_023',
                '⏰ <b>ПРОДЛЕНИЕ ПОДПИСКИ</b>\n\n👤 <b>Пользователь:</b> {v0}\n🆔 <b>{v1}:</b> {v2}\n📱 <b>Username:</b> {v3}\n\n{v4}\n\n💰 <b>Платеж:</b>\n💵 Сумма: {v5}\n💳 Способ: {v6}\n🆔 ID транзакции: {v7}\n\n📅 <b>Продление:</b>\n➕ Добавлено дней: {v8}\n📆 Было до: {v9}\n📆 Стало до: {v10}\n\n📱 <b>Текущие параметры:</b>\n📊 Трафик: {v11}\n📱 Устройства: {v12}\n🌐 Серверы: {v13}\n\n💰 <b>Баланс после операции:</b> {v14}\n\n⏰ <i>{v15}</i>',
            ).format(
                v0=user_display,
                v1=user_id_label,
                v2=user_id_display,
                v3=format_username_link(getattr(user, 'username', None), texts.t('ADMIN_NOTIFY_088', 'отсутствует')),
                v4=promo_block,
                v5=settings.format_price(abs(transaction.amount_kopeks)),
                v6=payment_method,
                v7=transaction.id,
                v8=extended_days,
                v9=format_local_datetime(old_end_date, '%d.%m.%Y %H:%M'),
                v10=format_local_datetime(current_end_date, '%d.%m.%Y %H:%M'),
                v11=self._format_traffic(subscription.traffic_limit_gb, texts=texts),
                v12=subscription.device_limit,
                v13=servers_info,
                v14=settings.format_price(current_balance),
                v15=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S'),
            )

            return await self._send_message(message, category=NotificationCategory.RENEWALS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о продлении', error=e)
            return False

    async def send_promocode_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        promocode_data: dict[str, Any],
        effect_description: str,
        balance_before_kopeks: int | None = None,
        balance_after_kopeks: int | None = None,
    ) -> bool:
        texts = await self._notification_texts(db)
        try:
            await self._record_subscription_event(
                db,
                event_type='promocode_activation',
                user=user,
                subscription=None,
                transaction=None,
                amount_kopeks=promocode_data.get('balance_bonus_kopeks'),
                message='Promocode activation',
                occurred_at=datetime.now(UTC),
                extra={
                    'code': promocode_data.get('code'),
                    'type': promocode_data.get('type'),
                    'subscription_days': promocode_data.get('subscription_days'),
                    'balance_bonus_kopeks': promocode_data.get('balance_bonus_kopeks'),
                    'description': effect_description,
                    'valid_until': (
                        promocode_data.get('valid_until').isoformat()
                        if isinstance(promocode_data.get('valid_until'), datetime)
                        else promocode_data.get('valid_until')
                    ),
                    'balance_before_kopeks': balance_before_kopeks,
                    'balance_after_kopeks': balance_after_kopeks,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие активации промокода пользователя',
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

        if not self._is_enabled():
            return False

        try:
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group, texts=texts)
            type_display = self._get_promocode_type_display(promocode_data.get('type'), texts=texts)
            usage_info = f'{promocode_data.get("current_uses", 0)}/{promocode_data.get("max_uses", 0)}'
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                texts.t('ADMIN_NOTIFY_074', '🎫 <b>АКТИВАЦИЯ ПРОМОКОДА</b>'),
                '',
                texts.t('ADMIN_NOTIFY_059', '👤 <b>Пользователь:</b> {v0}').format(v0=user_display),
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> {format_username_link(getattr(user, "username", None), texts.t("ADMIN_NOTIFY_088", "отсутствует"))}',
                '',
                promo_block,
                '',
                texts.t('ADMIN_NOTIFY_075', '🎟️ <b>Промокод:</b>'),
                texts.t('ADMIN_NOTIFY_076', '🔖 Код: <code>{v0}</code>').format(v0=promocode_data.get('code')),
                texts.t('ADMIN_NOTIFY_077', '🧾 Тип: {v0}').format(v0=type_display),
                texts.t('ADMIN_NOTIFY_078', '📊 Использования: {v0}').format(v0=usage_info),
            ]

            promo_type = promocode_data.get('type')
            balance_bonus = promocode_data.get('balance_bonus_kopeks', 0)
            subscription_days = promocode_data.get('subscription_days', 0)

            if promo_type == PromoCodeType.DISCOUNT.value:
                message_lines.append(texts.t('ADMIN_NOTIFY_130', '💸 Скидка: {v0}%').format(v0=balance_bonus))
                if subscription_days:
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_173', '⏳ Срок действия скидки: {v0} ч.').format(v0=subscription_days)
                    )
                else:
                    message_lines.append(texts.t('ADMIN_NOTIFY_174', '⏳ Срок действия скидки: до первой покупки'))
            else:
                if balance_bonus:
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_175', '💰 Бонус на баланс: {v0}').format(
                            v0=settings.format_price(balance_bonus)
                        )
                    )
                if subscription_days:
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_176', '📅 Доп. дни подписки: {v0}').format(v0=subscription_days)
                    )

            valid_until = promocode_data.get('valid_until')
            if valid_until:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_177', '⏳ Действует до: {v0}').format(
                        v0=format_local_datetime(valid_until, '%d.%m.%Y %H:%M')
                    )
                    if isinstance(valid_until, datetime)
                    else texts.t('ADMIN_NOTIFY_177', '⏳ Действует до: {v0}').format(v0=valid_until)
                )

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_131', '💼 <b>Баланс:</b>'),
                    (
                        f'{settings.format_price(balance_before_kopeks)} → {settings.format_price(balance_after_kopeks)}'
                        if balance_before_kopeks is not None and balance_after_kopeks is not None
                        else texts.t('ADMIN_NOTIFY_178', 'ℹ️ Баланс не изменился')
                    ),
                    '',
                    texts.t('ADMIN_NOTIFY_132', '📝 <b>Эффект:</b>'),
                    effect_description.strip() or texts.t('ADMIN_NOTIFY_179', '✅ Промокод активирован'),
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об активации промокода', error=e)
            return False

    async def send_campaign_link_visit_notification(
        self,
        db: AsyncSession,
        telegram_user: types.User,
        campaign: AdvertisingCampaign,
        user: User | None = None,
    ) -> bool:
        # Дедуп: если юзер уже зарегистрирован в этой кампании
        # (AdvertisingCampaignRegistration.UniqueConstraint(campaign_id, user_id))
        # — повторный /start не должен слать новое уведомление в админ-чат, иначе
        # кол-во сообщений в чате превышает реальное число регистраций в БД и
        # вводит админа в заблуждение. Для новых юзеров (user is None) уведомление
        # уходит как раньше — это первичный переход.
        texts = await self._notification_texts(db)
        if user:
            existing_registration = await db.execute(
                select(AdvertisingCampaignRegistration.id).where(
                    AdvertisingCampaignRegistration.campaign_id == campaign.id,
                    AdvertisingCampaignRegistration.user_id == user.id,
                )
            )
            if existing_registration.scalar_one_or_none() is not None:
                logger.debug(
                    'Skip campaign visit notification: user already registered in campaign',
                    user_id=user.id,
                    campaign_id=campaign.id,
                )
                return False

            try:
                await self._record_subscription_event(
                    db,
                    event_type='referral_link_visit',
                    user=user,
                    subscription=None,
                    transaction=None,
                    amount_kopeks=None,
                    message='Referral link visit',
                    occurred_at=datetime.now(UTC),
                    extra={
                        'campaign_id': campaign.id,
                        'campaign_name': campaign.name,
                        'start_parameter': campaign.start_parameter,
                        'was_registered': bool(user),
                    },
                )
            except Exception:
                logger.error(
                    'Не удалось сохранить событие перехода по кампании для пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

        if not self._is_enabled():
            return False

        try:
            full_name = telegram_user.full_name or telegram_user.username or str(telegram_user.id)
            user_status = (
                texts.t('ADMIN_NOTIFY_055', '🆕 Новый') if not user else texts.t('ADMIN_NOTIFY_079', '👥 Существующий')
            )

            message_lines = [
                texts.t('ADMIN_NOTIFY_080', '📣 <b>ПЕРЕХОД ПО РК</b>'),
                '',
                f'🧾 {html.escape(campaign.name)} (<code>{html.escape(campaign.start_parameter)}</code>)',
                '',
                f'👤 {html.escape(full_name)} (<code>{telegram_user.id}</code>)',
            ]

            if telegram_user.username:
                message_lines.append(f'📱 {format_username_link(telegram_user.username)}')

            message_lines.append(f'📋 {user_status}')

            # Промогруппа — только если есть
            if user:
                promo_group = await self._get_user_promo_group(db, user)
                if promo_group:
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_065', '🏷️ Промогруппа: {v0}').format(v0=html.escape(promo_group.name))
                    )

            message_lines.append('')

            # Загружаем название тарифа для tariff-бонуса
            tariff_name = None
            if campaign.is_tariff_bonus and campaign.tariff_id:
                try:
                    from app.database.crud.tariff import get_tariff_by_id

                    tariff = await get_tariff_by_id(db, campaign.tariff_id)
                    if tariff:
                        tariff_name = html.escape(tariff.name)
                except Exception:
                    pass

            # Бонус кампании
            bonus_lines = self._format_campaign_bonus(campaign, tariff_name=tariff_name, texts=texts)
            message_lines.extend(bonus_lines)

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о переходе по кампании', error=e)
            return False

    async def send_campaign_registration_notification(
        self,
        db: AsyncSession,
        telegram_user_id: int,
        telegram_user_name: str,
        telegram_username: str | None,
        campaign: AdvertisingCampaign,
        user: User,
        *,
        bonus_type: str,
        balance_kopeks: int = 0,
        subscription_days: int | None = None,
        subscription_traffic_gb: int | None = None,
        subscription_device_limit: int | None = None,
        tariff_name: str | None = None,
    ) -> bool:
        """Уведомление о СОВЕРШЁННОЙ регистрации по рекламной кампании.

        Шлётся ровно один раз на каждую новую запись в advertising_campaign_registrations
        (caller передаёт is_new_registration=True). Это даёт паритет: число сообщений
        в админ-чате равно числу регистраций в кабинете.
        """
        texts = await self._notification_texts(db)
        if not self._is_enabled():
            return False

        try:
            await self._record_subscription_event(
                db,
                event_type='campaign_registration',
                user=user,
                subscription=None,
                transaction=None,
                amount_kopeks=balance_kopeks or None,
                message='Campaign registration completed',
                occurred_at=datetime.now(UTC),
                extra={
                    'campaign_id': campaign.id,
                    'campaign_name': campaign.name,
                    'start_parameter': campaign.start_parameter,
                    'bonus_type': bonus_type,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие регистрации по кампании',
                user_id=user.id,
                campaign_id=campaign.id,
                exc_info=True,
            )

        try:
            message_lines = [
                texts.t('ADMIN_NOTIFY_081', '✅ <b>РЕГИСТРАЦИЯ ПО РК</b>'),
                '',
                f'🧾 {html.escape(campaign.name)} (<code>{html.escape(campaign.start_parameter)}</code>)',
                '',
                f'👤 {html.escape(telegram_user_name)} (<code>{telegram_user_id}</code>)',
            ]
            if telegram_username:
                message_lines.append(f'📱 {format_username_link(telegram_username)}')

            promo_group = await self._get_user_promo_group(db, user)
            if promo_group:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_065', '🏷️ Промогруппа: {v0}').format(v0=html.escape(promo_group.name))
                )

            message_lines.append('')

            bonus_lines = self._format_campaign_bonus(campaign, tariff_name=tariff_name, texts=texts)
            message_lines.extend(bonus_lines)

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error(
                'Ошибка отправки уведомления о регистрации по кампании',
                error=str(e),
                user_id=user.id,
                campaign_id=campaign.id,
                exc_info=True,
            )
            return False

    async def send_user_promo_group_change_notification(
        self,
        db: AsyncSession,
        user: User,
        old_group: PromoGroup | None,
        new_group: PromoGroup,
        *,
        reason: str | None = None,
        initiator: User | None = None,
        automatic: bool = False,
    ) -> bool:
        texts = await self._notification_texts(db)
        try:
            await self._record_subscription_event(
                db,
                event_type='promo_group_change',
                user=user,
                subscription=None,
                transaction=None,
                message='Promo group change',
                occurred_at=datetime.now(UTC),
                extra={
                    'old_group_id': getattr(old_group, 'id', None),
                    'old_group_name': getattr(old_group, 'name', None),
                    'new_group_id': new_group.id,
                    'new_group_name': new_group.name,
                    'reason': reason,
                    'initiator_id': getattr(initiator, 'id', None),
                    'initiator_telegram_id': getattr(initiator, 'telegram_id', None),
                    'automatic': automatic,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие смены промогруппы пользователя',
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

        if not self._is_enabled():
            return False

        try:
            title = (
                texts.t('ADMIN_NOTIFY_082', '🤖 АВТОМАТИЧЕСКАЯ СМЕНА ПРОМОГРУППЫ')
                if automatic
                else texts.t('ADMIN_NOTIFY_083', '👥 СМЕНА ПРОМОГРУППЫ')
            )
            initiator_line = None
            if initiator:
                initiator_line = texts.t('ADMIN_NOTIFY_084', '👮 <b>Инициатор:</b> {v0} (ID: {v1})').format(
                    v0=html.escape(initiator.full_name), v1=initiator.telegram_id
                )
            elif automatic:
                initiator_line = texts.t('ADMIN_NOTIFY_133', '🤖 Автоматическое назначение')
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                f'{title}',
                '',
                texts.t('ADMIN_NOTIFY_059', '👤 <b>Пользователь:</b> {v0}').format(v0=user_display),
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> {format_username_link(getattr(user, "username", None), texts.t("ADMIN_NOTIFY_088", "отсутствует"))}',
                '',
                self._format_promo_group_block(
                    new_group, title=texts.t('ADMIN_NOTIFY_180', 'Новая промогруппа'), icon='🏆', texts=texts
                ),
            ]

            if old_group and old_group.id != new_group.id:
                message_lines.extend(
                    [
                        '',
                        self._format_promo_group_block(
                            old_group,
                            title=texts.t('ADMIN_NOTIFY_206', 'Предыдущая промогруппа'),
                            icon='♻️',
                            texts=texts,
                        ),
                    ]
                )

            if initiator_line:
                message_lines.extend(['', initiator_line])

            if reason:
                message_lines.extend(['', texts.t('ADMIN_NOTIFY_181', '📝 Причина: {v0}').format(v0=reason)])

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_134', '💰 Баланс пользователя: {v0}').format(
                        v0=settings.format_price(user.balance_kopeks)
                    ),
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о смене промогруппы', error=e)
            return False

    def _resolve_topic_id(self, category: NotificationCategory | None = None) -> int | None:
        """Определяет topic_id для сообщения.

        Если указана category и для неё настроен топик — возвращает его.
        Иначе — fallback на self.topic_id (общий топик).
        """
        if category:
            topic = self.category_topics.get(category)
            if topic is not None:
                return topic
        return self.topic_id

    def resolve_recipient_role(self) -> str:
        """Определяет роль получателя уведомления по chat_id.

        В личном чате chat_id совпадает с telegram_id получателя, что позволяет
        проверить права до отправки без I/O (данные читаются из памяти).

        Returns:
            'admin'     — полный набор кнопок;
            'moderator' — набор без «👤 К пользователю» (@admin_required);
            'group'     — групповой/супергруппа/канал админ-чат: только надёжные
                          (не-FSM) кнопки, т.к. конкретного получателя не определить
                          и FSM-ввод в общем чате не работает (privacy mode бота);
            'none'      — без кнопок (посторонний в личке, либо
                          ADMIN_NOTIFICATIONS_CHAT_ID задан строкой @username).
        """
        try:
            chat_id = int(self.chat_id)
        except (TypeError, ValueError):
            return 'none'  # строка @username или None — тип чата не определить
        if chat_id < 0:
            # супергруппа / канал / старая группа — доверенный админ-чат оператора,
            # но конкретного получателя не определить → только надёжные кнопки.
            return 'group'
        if chat_id == 0:
            return 'none'  # невалидный chat_id
        if settings.is_admin(chat_id):
            return 'admin'

        from app.services.support_settings_service import SupportSettingsService

        if SupportSettingsService.is_moderator(chat_id):
            return 'moderator'

        return 'none'  # личка постороннего — не показываем кнопки

    async def _send_message(
        self,
        text: str,
        reply_markup: types.InlineKeyboardMarkup | None = None,
        *,
        category: NotificationCategory | None = None,
        thread_id: int | None = None,
    ) -> bool:
        if not self._is_enabled():
            return False

        # Per-category suppression
        if category and not self.category_enabled.get(category, True):
            logger.debug('Уведомление подавлено (категория отключена)', category=category.value)
            return False

        # Явный thread_id (например, топик заявок на вывод) важнее топика категории
        if thread_id is None:
            thread_id = self._resolve_topic_id(category)

        # В групповом админ-чате работают только разрешённые callback-кнопки
        # (фильтр чатов глушит остальные): такие выкидываем здесь, а не рисуем
        # мёртвыми. URL-кнопки и разрешённые действия остаются.
        if reply_markup is not None and self.resolve_recipient_role() == 'group':
            reply_markup, dropped = strip_group_unsafe_buttons(reply_markup)
            if dropped:
                logger.warning(
                    'Кнопки не работают в групповом админ-чате — убраны из уведомления',
                    chat_id=self.chat_id,
                    dropped=dropped,
                )

        # Rich-вид (Bot API 10.1): заголовок, разделители, footer с tg-time.
        # При недоступности/ошибке молча продолжаем классическим путём ниже
        # (там ретраи и обработка flood control).
        try:
            rich_html = classic_admin_html_to_rich(text)
            if await try_send_rich_admin_message(
                self.bot, self.chat_id, rich_html, thread_id=thread_id, reply_markup=reply_markup
            ):
                logger.info('Rich-уведомление отправлено в чат', chat_id=self.chat_id, category=category)
                return True
        except Exception as rich_error:
            logger.warning('Сбой rich-рендера админ-уведомления', error=str(rich_error))

        message_kwargs: dict[str, Any] = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        if thread_id:
            message_kwargs['message_thread_id'] = thread_id
        if reply_markup is not None:
            message_kwargs['reply_markup'] = reply_markup

        # ВАЖНО: вся ветка ошибок ниже логируется через logger.warning, а не
        # logger.error. Иначе TelegramNotifierProcessor попытается переслать
        # ошибку в этот же админ-чат, упрётся в тот же flood control — петля
        # усиления (баг с node.connection_lost/restored, 7-8 webhook'ов подряд).
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                await self.bot.send_message(**message_kwargs)
                logger.info('Уведомление отправлено в чат', chat_id=self.chat_id, category=category)
                return True

            except TelegramForbiddenError:
                logger.warning('Бот не имеет прав для отправки в чат', chat_id=self.chat_id)
                return False

            except TelegramBadRequest as e:
                logger.warning(
                    'Ошибка отправки уведомления в админ-чат',
                    error=_redact_telegram_secrets(str(e))[:200],
                )
                return False

            except TelegramRetryAfter as e:
                # Flood control: ждём столько, сколько сказал Telegram (cap 30s),
                # потом ретраим. До фикса исключение проваливалось в bare
                # except → logger.error → петля через TelegramNotifierProcessor.
                requested_retry_after = max(1, int(getattr(e, 'retry_after', 1)))
                retry_after = min(requested_retry_after, 30)
                log_kwargs: dict[str, Any] = {
                    'chat_id': self.chat_id,
                    'retry_after': retry_after,
                    'attempt': attempt,
                }
                if requested_retry_after > retry_after:
                    # Telegram реально просит дольше cap'а — видимый сигнал,
                    # что бот аккаунт перегружен сильнее обычного flood-control'а.
                    log_kwargs['retry_after_requested'] = requested_retry_after
                    log_kwargs['clamped'] = True
                logger.warning('Telegram flood control при отправке в админ-чат', **log_kwargs)
                if attempt < max_attempts:
                    await asyncio.sleep(retry_after)
                    continue
                return False

            except (TelegramNetworkError, TelegramServerError) as e:
                # Транзиентные сетевые/5xx — warning, не error.
                logger.warning(
                    'Транзиентная сетевая ошибка отправки в админ-чат',
                    chat_id=self.chat_id,
                    error=_redact_telegram_secrets(str(e))[:200],
                    error_type=type(e).__name__,
                    attempt=attempt,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                    continue
                return False

            except Exception as e:
                logger.warning(
                    'Неожиданная ошибка при отправке в админ-чат',
                    chat_id=self.chat_id,
                    error=_redact_telegram_secrets(str(e))[:200],
                    error_type=type(e).__name__,
                )
                return False

        return False

    def _is_enabled(self) -> bool:
        return self.enabled and bool(self.chat_id)

    @property
    def is_enabled(self) -> bool:
        """Public check for whether admin notifications are configured and active."""
        return self._is_enabled()

    async def send_admin_notification(
        self,
        text: str,
        reply_markup: types.InlineKeyboardMarkup | None = None,
        *,
        category: NotificationCategory | None = None,
    ) -> bool:
        """Send a generic notification to admin chat with optional inline keyboard."""
        if not self._is_enabled():
            return False
        return await self._send_message(text, reply_markup=reply_markup, category=category)

    async def send_guest_purchase_notification(
        self,
        purchase: GuestPurchase,
        tariff_name: str,
        *,
        is_pending_activation: bool = False,
    ) -> bool:
        """Send admin notification for a guest/gift purchase (landing or cabinet)."""
        texts = await self._notification_texts()
        if not self._is_enabled():
            return False

        try:
            is_cabinet = purchase.source == 'cabinet'

            # Event title
            if is_cabinet and purchase.is_gift:
                event_title = texts.t('ADMIN_NOTIFY_085', '🎁 ПОДАРОК ИЗ КАБИНЕТА')
            elif is_pending_activation:
                event_title = texts.t('ADMIN_NOTIFY_135', '⏳ ПОКУПКА С ЛЕНДИНГА (ожидает активации)')
            elif purchase.is_gift:
                event_title = texts.t('ADMIN_NOTIFY_182', '🎁 ПОКУПКА В ПОДАРОК С ЛЕНДИНГА')
            else:
                event_title = texts.t('ADMIN_NOTIFY_183', '🛒 ПОКУПКА С ЛЕНДИНГА')

            # Contact info
            contact_display = html.escape(purchase.contact_value or '—')
            contact_icon = '📧' if purchase.contact_type == 'email' else '📱'

            payment_method = self._get_payment_method_display(purchase.payment_method, texts=texts)

            message_lines = [
                f'<b>{event_title}</b>',
                '',
            ]

            if is_cabinet:
                # Cabinet gift: show buyer with link to user profile
                buyer = getattr(purchase, 'buyer', None)
                if buyer:
                    if buyer.username:
                        buyer_display = format_username_link(buyer.username)
                    else:
                        buyer_name = buyer.email or f'id:{buyer.id}'
                        buyer_display = f'<code>{html.escape(buyer_name)}</code>'
                    message_lines.append(texts.t('ADMIN_NOTIFY_184', '👤 Покупатель: {v0}').format(v0=buyer_display))
                else:
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_137', '{v0} Покупатель: <code>{v1}</code>').format(
                            v0=contact_icon, v1=contact_display
                        )
                    )
            else:
                # Landing: show page slug and buyer contact
                landing_slug = '—'
                try:
                    landing = purchase.landing
                    if landing:
                        landing_slug = landing.slug
                    elif purchase.landing_id:
                        landing_slug = f'ID:{purchase.landing_id}'
                except Exception:
                    if purchase.landing_id:
                        landing_slug = f'ID:{purchase.landing_id}'
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_136', '🌐 Страница: <b>/buy/{v0}</b>').format(v0=html.escape(landing_slug))
                )
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_137', '{v0} Покупатель: <code>{v1}</code>').format(
                        v0=contact_icon, v1=contact_display
                    )
                )

            if purchase.is_gift:
                if purchase.gift_recipient_value:
                    recipient_icon = '📧' if purchase.gift_recipient_type == 'email' else '📱'
                    recipient_value = html.escape(purchase.gift_recipient_value)
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_185', '{v0} Получатель: <code>{v1}</code>').format(
                            v0=recipient_icon, v1=recipient_value
                        )
                    )
                else:
                    message_lines.append(texts.t('ADMIN_NOTIFY_186', '🔗 Получатель: <i>по коду активации</i>'))
                if purchase.gift_message:
                    raw_msg = purchase.gift_message[:100]
                    suffix = '…' if len(purchase.gift_message) > 100 else ''
                    message_lines.append(f'💬 <i>{html.escape(raw_msg)}{suffix}</i>')

            # Payment details in blockquote
            payment_lines = [
                '<blockquote>',
                texts.t('ADMIN_NOTIFY_086', '🏷️ Тариф: <b>{v0}</b>').format(v0=html.escape(tariff_name)),
                texts.t('ADMIN_NOTIFY_087', '📅 Период: {v0} дн.').format(v0=purchase.period_days),
                f'💵 <b>{settings.format_price(purchase.amount_kopeks)}</b> • {payment_method}',
            ]

            if purchase.payment_id:
                payment_lines.append(f'🆔 {html.escape(str(purchase.payment_id))}')

            payment_lines.append('</blockquote>')
            message_lines.extend(payment_lines)

            message_lines.append(f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>')

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PURCHASES)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о гостевой покупке', error=e)
            return False

    async def send_grace_access_notification(
        self,
        *,
        event: str,
        user: User,
        subscription: Subscription,
        tariff_name: str | None,
        reason: str,
        grace_until: datetime,
        hours: int,
        quota_gb: float,
        allowed: str,
        completion_reason: str | None = None,
        last_error: str | None = None,
    ) -> bool:
        """Выдача или завершение grace-доступа — в чат админов, категория «Продления».

        Владелец: «выдача втухлую — это тупо»: админ обязан видеть, кому, почему и до
        какого срока бот временно оставил доступ к тому, что оператор назвал в
        GRACE_ACCESS_ALLOWED_SERVICES (``allowed``, уже экранировано), и чем это
        закончилось — продлением, истечением срока или конфликтом с панелью.
        """
        texts = await self._notification_texts()
        try:
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)
            username = format_username_link(getattr(user, 'username', None), texts.t('ADMIN_NOTIFY_088', 'отсутствует'))
            subscription_line = f'#{subscription.id}'
            if tariff_name:
                subscription_line += f' «{html.escape(tariff_name)}»'
            reason_line = (
                texts.t('ADMIN_NOTIFY_089', 'исчерпан трафик')
                if reason == 'limited'
                else texts.t('ADMIN_NOTIFY_090', 'срок подписки истёк')
            )
            quota_text = texts.t('ADMIN_NOTIFY_024', '{v0:g} ГБ').format(v0=quota_gb)
            until_text = format_local_datetime(grace_until, '%d.%m.%Y %H:%M')

            if event == 'granted':
                message = texts.t(
                    'ADMIN_NOTIFY_091',
                    '🛟 <b>GRACE-ДОСТУП ВЫДАН</b>\n\n👤 <b>Пользователь:</b> {v0}\n🆔 <b>{v1}:</b> {v2}\n📱 <b>Username:</b> {v3}\n\n📋 <b>Подписка:</b> {v4}\n⚠️ <b>Почему:</b> {v5}\n🛟 <b>Что выдано:</b> {v6}, {v7} на {v8} ч.\n⏳ <b>Действует до:</b> {v9}\n\n⏰ <i>{v10}</i>',
                ).format(
                    v0=user_display,
                    v1=user_id_label,
                    v2=user_id_display,
                    v3=username,
                    v4=subscription_line,
                    v5=reason_line,
                    v6=allowed,
                    v7=quota_text,
                    v8=hours,
                    v9=until_text,
                    v10=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S'),
                )
            else:
                outcomes = {
                    'paid': texts.t('ADMIN_NOTIFY_138', '✅ человек продлил подписку — вернули обычный тариф'),
                    'timeout': texts.t('ADMIN_NOTIFY_139', '⌛ срок grace вышел, подписку не продлили — доступ закрыт'),
                    'drained': texts.t('ADMIN_NOTIFY_140', '🚰 grace выключают (слив) — доступ закрыт'),
                    'revoked': texts.t(
                        'ADMIN_NOTIFY_141', '🚫 отозван: пользователь заблокирован или подписка отключена'
                    ),
                    'conflict': texts.t('ADMIN_NOTIFY_142', '⚠️ конфликт с панелью — доступ закрыт'),
                }
                outcome = outcomes.get(
                    completion_reason or '',
                    texts.t('ADMIN_NOTIFY_143', 'завершён ({v0})').format(v0=completion_reason or '?'),
                )
                error_line = f'\n❗ <code>{html.escape(last_error)}</code>' if last_error else ''
                message = texts.t(
                    'ADMIN_NOTIFY_092',
                    '🛟 <b>GRACE-ДОСТУП ЗАВЕРШЁН</b>\n\n👤 <b>Пользователь:</b> {v0}\n🆔 <b>{v1}:</b> {v2}\n📱 <b>Username:</b> {v3}\n\n📋 <b>Подписка:</b> {v4}\n⚠️ <b>Был выдан:</b> {v5}, до {v6}\n🏁 <b>Итог:</b> {v7}{v8}\n\n⏰ <i>{v9}</i>',
                ).format(
                    v0=user_display,
                    v1=user_id_label,
                    v2=user_id_display,
                    v3=username,
                    v4=subscription_line,
                    v5=reason_line,
                    v6=until_text,
                    v7=outcome,
                    v8=error_line,
                    v9=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S'),
                )

            return await self._send_message(message, category=NotificationCategory.RENEWALS)
        except Exception as error:
            logger.error('Ошибка отправки уведомления о grace-доступе', error=error)
            return False

    async def send_webhook_notification(self, text: str) -> bool:
        """Send a generic webhook/infrastructure notification to admin chat.

        Used by RemnaWaveWebhookService for node, service, and CRM events.
        The caller is responsible for HTML-escaping all untrusted data in `text`.
        """
        if not self._is_enabled():
            return False
        return await self._send_message(text, category=NotificationCategory.INFRASTRUCTURE)

    def _get_payment_method_display(self, payment_method: str | None, texts: Texts | None = None) -> str:
        # The no-context form is also persisted in historical subscription events.
        texts = texts or get_texts('ru')
        if not payment_method:
            return texts.t('ADMIN_NOTIFY_025', '💰 С баланса')

        method_names: dict[str, str] = {
            'telegram_stars': '⭐ Telegram Stars',
            'yookassa': texts.t('ADMIN_NOTIFY_026', '💳 YooKassa (карта)'),
            'tribute': texts.t('ADMIN_NOTIFY_027', '💎 Tribute (карта)'),
            'mulenpay': texts.t('ADMIN_NOTIFY_028', '💳 {v0} (карта)').format(v0=settings.get_mulenpay_display_name()),
            'pal24': texts.t('ADMIN_NOTIFY_029', '🏦 {v0} (СБП)').format(v0=settings.get_pal24_display_name()),
            'cryptobot': texts.t('ADMIN_NOTIFY_030', '🪙 {v0} (крипто)').format(
                v0=settings.get_cryptobot_display_name()
            ),
            'heleket': texts.t('ADMIN_NOTIFY_030', '🪙 {v0} (крипто)').format(v0=settings.get_heleket_display_name()),
            'wata': f'💳 {settings.get_wata_display_name()}',
            'platega': f'💳 {settings.get_platega_display_name()}',
            'cloudpayments': f'💳 {settings.get_cloudpayments_display_name()}',
            'freekassa': f'💳 {settings.get_freekassa_display_name()}',
            'kassa_ai': f'💳 {settings.get_kassa_ai_display_name()}',
            'cispay': f'💳 {settings.get_cispay_display_name()}',
            'cashera': f'💳 {settings.get_cashera_display_name()}',
            'anore': f'💳 {settings.get_anore_display_name()}',
            'tabpay': f'💳 {settings.get_tabpay_display_name()}',
            'paritypay': f'💳 {settings.get_paritypay_display_name()}',
            'manual': texts.t('ADMIN_NOTIFY_031', '🛠️ Вручную (админ)'),
            'balance': texts.t('ADMIN_NOTIFY_025', '💰 С баланса'),
        }

        return method_names.get(payment_method, f'💳 {html.escape(payment_method)}')

    def _format_traffic(self, traffic_gb: int, texts: Texts | None = None) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        if traffic_gb == 0:
            return texts.t('ADMIN_NOTIFY_032', '∞ Безлимит')
        return texts.t('PAYMENT_NOTICE_017', '{v0} ГБ').format(v0=traffic_gb)

    def _get_subscription_status(self, subscription: Subscription | None, texts: Texts | None = None) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        if not subscription:
            return texts.t('ADMIN_SERVER_UI_045', '❌ Нет подписки')

        if subscription.is_trial:
            return texts.t('ADMIN_NOTIFY_034', '🎯 Триал (до {v0})').format(
                v0=format_local_datetime(subscription.end_date, '%d.%m')
            )
        if subscription.is_active:
            return texts.t('ADMIN_NOTIFY_035', '✅ Активна (до {v0})').format(
                v0=format_local_datetime(subscription.end_date, '%d.%m')
            )
        return texts.t('FORMAT_STATUS_INACTIVE', '❌ Неактивна')

    async def _get_servers_info(self, squad_uuids: list, texts: Texts | None = None) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        if not squad_uuids:
            return texts.t('ADMIN_NOTIFY_036', '❌ Нет серверов')

        try:
            from app.handlers.subscription.devices import get_servers_display_names

            servers_names = await get_servers_display_names(squad_uuids, language=texts.language)
            return texts.t('ADMIN_NOTIFY_037', '{v0} шт. ({v1})').format(v0=len(squad_uuids), v1=servers_names)
        except Exception as e:
            logger.warning('Не удалось получить названия серверов', error=e)
            return texts.t('ADMIN_NOTIFY_093', '{v0} шт.').format(v0=len(squad_uuids))

    async def send_maintenance_status_notification(
        self, event_type: str, status: str, details: dict[str, Any] = None
    ) -> bool:
        texts = await self._notification_texts()
        if not self._is_enabled():
            return False

        try:
            details = details or {}

            if event_type == 'enable':
                if details.get('auto_enabled', False):
                    icon = '⚠️'
                    title = texts.t('ADMIN_NOTIFY_144', 'АВТОМАТИЧЕСКОЕ ВКЛЮЧЕНИЕ ТЕХРАБОТ')
                else:
                    icon = '🔧'
                    title = texts.t('ADMIN_NOTIFY_145', 'ВКЛЮЧЕНИЕ ТЕХРАБОТ')

            elif event_type == 'disable':
                icon = '✅'
                title = texts.t('ADMIN_NOTIFY_146', 'ОТКЛЮЧЕНИЕ ТЕХРАБОТ')

            elif event_type == 'api_status':
                if status == 'online':
                    icon = '🟢'
                    title = texts.t('ADMIN_NOTIFY_197', 'API REMNAWAVE ВОССТАНОВЛЕНО')
                else:
                    icon = '🔴'
                    title = texts.t('ADMIN_NOTIFY_198', 'API REMNAWAVE НЕДОСТУПНО')

            elif event_type == 'monitoring':
                if status == 'started':
                    icon = '🔍'
                    title = texts.t('ADMIN_NOTIFY_207', 'МОНИТОРИНГ ЗАПУЩЕН')
                else:
                    icon = '⏹️'
                    title = texts.t('ADMIN_NOTIFY_208', 'МОНИТОРИНГ ОСТАНОВЛЕН')
            else:
                icon = 'ℹ️'
                title = texts.t('ADMIN_NOTIFY_199', 'СИСТЕМА ТЕХРАБОТ')

            message_parts = [f'{icon} <b>{title}</b>', '']

            if event_type == 'enable':
                if details.get('reason'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_187', '📋 <b>Причина:</b> {v0}').format(v0=details['reason'])
                    )

                if details.get('enabled_at'):
                    enabled_at = details['enabled_at']
                    if isinstance(enabled_at, str):
                        enabled_at = datetime.fromisoformat(enabled_at)
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_188', '🕐 <b>Время включения:</b> {v0}').format(
                            v0=format_local_datetime(enabled_at, '%d.%m.%Y %H:%M:%S')
                        )
                    )

                message_parts.append(
                    texts.t('ADMIN_NOTIFY_147', '🤖 <b>Автоматически:</b> {v0}').format(
                        v0=texts.t('ADMIN_BACKUP_UI_034', 'Да')
                        if details.get('auto_enabled', False)
                        else texts.t('ADMIN_BACKUP_UI_035', 'Нет')
                    )
                )
                message_parts.append('')
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_148', '❗ Обычные пользователи временно не могут использовать бота.')
                )

            elif event_type == 'disable':
                if details.get('disabled_at'):
                    disabled_at = details['disabled_at']
                    if isinstance(disabled_at, str):
                        disabled_at = datetime.fromisoformat(disabled_at)
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_200', '🕐 <b>Время отключения:</b> {v0}').format(
                            v0=format_local_datetime(disabled_at, '%d.%m.%Y %H:%M:%S')
                        )
                    )

                if details.get('duration'):
                    duration = details['duration']
                    if isinstance(duration, (int, float)):
                        hours = int(duration // 3600)
                        minutes = int((duration % 3600) // 60)
                        if hours > 0:
                            duration_str = texts.t('ADMIN_NOTIFY_210', '{v0}ч {v1}мин').format(v0=hours, v1=minutes)
                        else:
                            duration_str = texts.t('ADMIN_NOTIFY_211', '{v0}мин').format(v0=minutes)
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_212', '⏱️ <b>Длительность:</b> {v0}').format(v0=duration_str)
                        )

                message_parts.append(
                    texts.t('ADMIN_NOTIFY_189', '🤖 <b>Было автоматическим:</b> {v0}').format(
                        v0=texts.t('ADMIN_BACKUP_UI_034', 'Да')
                        if details.get('was_auto', False)
                        else texts.t('ADMIN_BACKUP_UI_035', 'Нет')
                    )
                )
                message_parts.append('')
                message_parts.append(texts.t('ADMIN_NOTIFY_190', '✅ Сервис снова доступен для пользователей.'))

            elif event_type == 'api_status':
                message_parts.append(
                    f'🔗 <b>API URL:</b> {details.get("api_url", texts.t("ADMIN_BACKUP_RESULT_UNKNOWN_DATE", "неизвестно"))}'
                )

                if status == 'online':
                    if details.get('response_time'):
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_153', '⚡ <b>Время отклика:</b> {v0} сек').format(
                                v0=details['response_time']
                            )
                        )

                    if details.get('consecutive_failures', 0) > 0:
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_216', '🔄 <b>Неудачных попыток было:</b> {v0}').format(
                                v0=details['consecutive_failures']
                            )
                        )

                    message_parts.append('')
                    message_parts.append(texts.t('ADMIN_NOTIFY_213', 'API снова отвечает на запросы.'))

                else:
                    if details.get('consecutive_failures'):
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_217', '🔄 <b>Попытка №:</b> {v0}').format(
                                v0=details['consecutive_failures']
                            )
                        )

                    if details.get('error'):
                        error_msg = str(details['error'])[:100]
                        message_parts.append(texts.t('ADMIN_NOTIFY_201', '❌ <b>Ошибка:</b> {v0}').format(v0=error_msg))

                    message_parts.append('')
                    message_parts.append(texts.t('ADMIN_NOTIFY_214', '⚠️ Началась серия неудачных проверок API.'))

            elif event_type == 'monitoring':
                if status == 'started':
                    if details.get('check_interval'):
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_225', '🔄 <b>Интервал проверки:</b> {v0} сек').format(
                                v0=details['check_interval']
                            )
                        )

                    if details.get('auto_enable_configured') is not None:
                        auto_enable = (
                            texts.t('ADMIN_MAINTENANCE_043', 'Включено')
                            if details['auto_enable_configured']
                            else texts.t('ADMIN_MAINTENANCE_044', 'Отключено')
                        )
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_228', '🤖 <b>Автовключение:</b> {v0}').format(v0=auto_enable)
                        )

                    if details.get('max_failures'):
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_229', '🎯 <b>Порог ошибок:</b> {v0}').format(
                                v0=details['max_failures']
                            )
                        )

                    message_parts.append('')
                    message_parts.append(texts.t('ADMIN_NOTIFY_218', 'Система будет следить за доступностью API.'))

                else:
                    message_parts.append(texts.t('ADMIN_NOTIFY_219', 'Автоматический мониторинг API остановлен.'))

            message_parts.append('')
            message_parts.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            message = '\n'.join(message_parts)

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о техработах', error=e)
            return False

    async def send_remnawave_panel_status_notification(self, status: str, details: dict[str, Any] = None) -> bool:
        texts = await self._notification_texts()
        if not self._is_enabled():
            return False

        try:
            details = details or {}

            status_config = {
                'online': {
                    'icon': '🟢',
                    'title': texts.t('ADMIN_NOTIFY_149', 'ПАНЕЛЬ REMNAWAVE ДОСТУПНА'),
                    'alert_type': 'success',
                },
                'offline': {
                    'icon': '🔴',
                    'title': texts.t('ADMIN_NOTIFY_150', 'ПАНЕЛЬ REMNAWAVE НЕДОСТУПНА'),
                    'alert_type': 'error',
                },
                'degraded': {
                    'icon': '🟡',
                    'title': texts.t('ADMIN_NOTIFY_151', 'ПАНЕЛЬ REMNAWAVE РАБОТАЕТ СО СБОЯМИ'),
                    'alert_type': 'warning',
                },
                'maintenance': {
                    'icon': '🔧',
                    'title': texts.t('ADMIN_NOTIFY_152', 'ПАНЕЛЬ REMNAWAVE НА ОБСЛУЖИВАНИИ'),
                    'alert_type': 'info',
                },
            }

            config = status_config.get(status, status_config['offline'])

            message_parts = [f'{config["icon"]} <b>{config["title"]}</b>', '']

            if details.get('api_url'):
                message_parts.append(f'🔗 <b>URL:</b> {details["api_url"]}')

            if details.get('response_time'):
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_153', '⚡ <b>Время отклика:</b> {v0} сек').format(v0=details['response_time'])
                )

            if details.get('last_check'):
                last_check = details['last_check']
                if isinstance(last_check, str):
                    last_check = datetime.fromisoformat(last_check)
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_154', '🕐 <b>Последняя проверка:</b> {v0}').format(
                        v0=format_local_datetime(last_check, '%H:%M:%S')
                    )
                )

            if status == 'online':
                if details.get('uptime'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_191', '⏱️ <b>Время работы:</b> {v0}').format(v0=details['uptime'])
                    )

                if details.get('users_online'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_192', '👥 <b>Пользователей онлайн:</b> {v0}').format(
                            v0=details['users_online']
                        )
                    )

                message_parts.append('')
                message_parts.append(texts.t('ADMIN_NOTIFY_155', '✅ Все системы работают нормально.'))

            elif status == 'offline':
                if details.get('error'):
                    error_msg = str(details['error'])[:150]
                    message_parts.append(texts.t('ADMIN_NOTIFY_201', '❌ <b>Ошибка:</b> {v0}').format(v0=error_msg))

                if details.get('consecutive_failures'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_202', '🔄 <b>Неудачных попыток:</b> {v0}').format(
                            v0=details['consecutive_failures']
                        )
                    )

                message_parts.append('')
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_193', '⚠️ Панель недоступна. Проверьте соединение и статус сервера.')
                )

            elif status == 'degraded':
                if details.get('issues'):
                    issues = details['issues']
                    if isinstance(issues, list):
                        message_parts.append(texts.t('ADMIN_NOTIFY_220', '⚠️ <b>Обнаруженные проблемы:</b>'))
                        for issue in issues[:3]:
                            message_parts.append(f'   • {issue}')
                    else:
                        message_parts.append(texts.t('ADMIN_NOTIFY_221', '⚠️ <b>Проблема:</b> {v0}').format(v0=issues))

                message_parts.append('')
                message_parts.append(texts.t('ADMIN_NOTIFY_203', 'Панель работает, но возможны задержки или сбои.'))

            elif status == 'maintenance':
                if details.get('maintenance_reason'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_222', '🔧 <b>Причина:</b> {v0}').format(
                            v0=html.escape(details['maintenance_reason'])
                        )
                    )

                if details.get('estimated_duration'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_223', '⏰ <b>Ожидаемая длительность:</b> {v0}').format(
                            v0=details['estimated_duration']
                        )
                    )

                message_parts.append('')
                message_parts.append(texts.t('ADMIN_NOTIFY_215', 'Панель временно недоступна для обслуживания.'))

            message_parts.append('')
            message_parts.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            message = '\n'.join(message_parts)

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о статусе панели Remnawave', error=e)
            return False

    async def send_subscription_update_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        update_type: str,
        old_value: Any,
        new_value: Any,
        price_paid: int = 0,
    ) -> bool:
        texts = await self._notification_texts(db)
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            # Определяем заголовок по типу операции
            update_titles = {
                'traffic': texts.t('ADMIN_NOTIFY_094', '📊 ДОКУПКА ТРАФИКА'),
                'devices': texts.t('ADMIN_NOTIFY_095', '📱 ДОКУПКА УСТРОЙСТВ'),
                'servers': texts.t('ADMIN_NOTIFY_096', '🌐 СМЕНА СЕРВЕРОВ'),
            }
            title = update_titles.get(update_type, texts.t('ADMIN_NOTIFY_097', '⚙️ ИЗМЕНЕНИЕ ПОДПИСКИ'))

            # Получаем название тарифа
            tariff_name = await self._get_tariff_name(db, subscription)

            # Формируем компактное сообщение
            message_lines = [
                f'<b>{title}</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            # Добавляем username только если есть
            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 {format_username_link(username)}')

            # Тариф (если есть)
            if tariff_name:
                message_lines.append(texts.t('ADMIN_NOTIFY_086', '🏷️ Тариф: <b>{v0}</b>').format(v0=tariff_name))

            message_lines.append('')

            # Форматируем изменение в зависимости от типа
            if update_type == 'servers':
                old_servers_info = await self._format_servers_detailed(old_value, texts=texts)
                new_servers_info = await self._format_servers_detailed(new_value, texts=texts)
                message_lines.append(f'🔄 {old_servers_info} → {new_servers_info}')
            elif update_type == 'traffic':
                old_formatted = self._format_update_value(old_value, update_type, texts=texts)
                new_formatted = self._format_update_value(new_value, update_type, texts=texts)
                message_lines.append(f'🔄 {old_formatted} → {new_formatted}')
            elif update_type == 'devices':
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_204', '🔄 {v0} → {v1} устр.').format(v0=old_value, v1=new_value)
                )
            else:
                message_lines.append(f'🔄 {old_value} → {new_value}')

            # Стоимость операции
            if price_paid > 0:
                message_lines.append(f'💵 <b>{settings.format_price(price_paid)}</b>')
            else:
                message_lines.append(texts.t('ADMIN_NOTIFY_156', '💸 Бесплатно'))

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_157', '📅 До {v0}').format(
                        v0=format_local_datetime(subscription.end_date, '%d.%m.%Y')
                    ),
                    texts.t('ADMIN_NOTIFY_051', '💰 Баланс: {v0}').format(
                        v0=settings.format_price(user.balance_kopeks)
                    ),
                ]
            )

            # Реферер (только если есть)
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id, texts=texts)
                if referrer_info not in ('Нет', texts.t('ADMIN_BACKUP_UI_035', 'Нет')):
                    message_lines.append(texts.t('ADMIN_NOTIFY_172', '🔗 Реф: {v0}').format(v0=referrer_info))

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.ADDONS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об изменении подписки', error=e)
            return False

    # Обычное sendMessage — до 4096 символов после разбора entities; запас — на
    # случай, если наш подсчёт разойдётся с серверным.
    DEVICE_OVERFLOW_PART_LIMIT = 4000

    async def send_device_limit_overflow_notification(self, overflows: list, *, total: int) -> list:
        """Сводка подписок, где устройств в панели больше лимита (app/services/device_limit_overflow.py).

        ``overflows`` — новые и изменившиеся случаи, ``total`` — сколько превышений сейчас всего.
        Список уходит целиком, при необходимости частями: каждая не длиннее
        DEVICE_OVERFLOW_PART_LIMIT, чтобы дойти и обычной отправкой, когда rich
        выключен или недоступен. Возвращает случаи из доставленных частей —
        показанными можно считать только их.
        """
        texts = await self._notification_texts()
        if not self._is_enabled() or not overflows:
            return []

        delivered: list = []
        try:
            from app.services.device_limit_overflow import DEVICE_OVERFLOW_ACTIVE_DAYS

            summary = texts.t(
                'ADMIN_NOTIFY_038',
                'Подписок, где устройств больше лимита: <b>{v0}</b>, новых или изменившихся: <b>{v1}</b>',
            ).format(v0=total, v1=len(overflows))
            footer = f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>'

            def render(lines: list[str], number: int, count: int) -> str:
                title = texts.t('ADMIN_NOTIFY_098', '<b>📱 ПРЕВЫШЕНИЕ ЛИМИТА УСТРОЙСТВ</b>')
                if count > 1:
                    title += texts.t('ADMIN_NOTIFY_158', ' · часть {v0} из {v1}').format(v0=number, v1=count)
                return '\n'.join([title, '', summary, '', *lines, '', footer])

            case_lines = []
            for overflow in overflows:
                subscription = overflow.subscription
                user = subscription.user
                tariff = _loaded_relationship(subscription, 'tariff')
                tariff_part = f' · {html.escape(tariff.name)}' if tariff is not None and tariff.name else ''
                case_lines.append(
                    texts.t(
                        'ADMIN_NOTIFY_159',
                        '• {v0} ({v1}) · #{v2}{v3}: {v4} устр. при лимите {v5}, за {v6} дн. в работе {v7}',
                    ).format(
                        v0=self._get_user_display(user),
                        v1=self._get_user_identifier_display(user),
                        v2=subscription.id,
                        v3=tariff_part,
                        v4=overflow.devices,
                        v5=overflow.device_limit,
                        v6=DEVICE_OVERFLOW_ACTIVE_DAYS,
                        v7=overflow.active_devices,
                    )
                )

            # Части набираются по длине, а не по числу строк: имя пользователя и
            # название тарифа бывают длинными. Заголовок считается по самому
            # длинному варианту — номер части не больше числа случаев. Каждая
            # строка добавляет к части свою длину и перевод строки.
            budget = self.DEVICE_OVERFLOW_PART_LIMIT - _telegram_text_length(render([], len(overflows), len(overflows)))
            parts: list[list[int]] = [[]]
            used = 0
            for index, line in enumerate(case_lines):
                size = _telegram_text_length(line) + 1
                if parts[-1] and used + size > budget:
                    parts.append([])
                    used = 0
                parts[-1].append(index)
                used += size

            for number, indexes in enumerate(parts, start=1):
                text = render([case_lines[i] for i in indexes], number, len(parts))
                if await self._send_message(text, category=NotificationCategory.ADDONS):
                    delivered.extend(overflows[i] for i in indexes)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о превышении лимита устройств', error=e)
        return delivered

    async def _format_servers_detailed(self, server_uuids: list[str], texts: Texts | None = None) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        if not server_uuids:
            return texts.t('SUBSCRIPTION_NO_SERVERS', 'Нет серверов')

        try:
            from app.handlers.subscription.devices import get_servers_display_names

            servers_names = await get_servers_display_names(server_uuids, language=texts.language)

            if servers_names and servers_names not in (
                'Нет серверов',
                texts.t('SUBSCRIPTION_UI_NO_SERVERS', 'Нет серверов'),
            ):
                return texts.t('ADMIN_NOTIFY_099', '{v0} серверов ({v1})').format(
                    v0=len(server_uuids), v1=servers_names
                )
            return texts.t('ADMIN_TARIFF_SERVER_COUNT', '{v0} серверов').format(v0=len(server_uuids))

        except Exception as e:
            logger.warning('Ошибка получения названий серверов для уведомления', error=e)
            return texts.t('ADMIN_TARIFF_SERVER_COUNT', '{v0} серверов').format(v0=len(server_uuids))

    def _format_update_value(self, value: Any, update_type: str, texts: Texts | None = None) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        if update_type == 'traffic':
            if value == 0:
                return texts.t('ADMIN_NOTIFY_100', '♾ Безлимитный')
            return texts.t('PAYMENT_NOTICE_017', '{v0} ГБ').format(v0=value)
        if update_type == 'devices':
            return texts.t('ADMIN_NOTIFY_041', '{v0} устройств').format(v0=value)
        if update_type == 'servers':
            if isinstance(value, list):
                return texts.t('ADMIN_TARIFF_SERVER_COUNT', '{v0} серверов').format(v0=len(value))
            return str(value)
        return str(value)

    async def send_partner_application_notification(
        self,
        user: User,
        application_data: dict[str, Any],
    ) -> bool:
        """Уведомление о новой заявке на партнёрку."""
        texts = await self._notification_texts()
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                texts.t('ADMIN_NOTIFY_101', '🤝 <b>ЗАЯВКА НА ПАРТНЁРКУ</b>'),
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 {format_username_link(username)}')

            message_lines.append('')

            if application_data.get('company_name'):
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_160', '🏢 Компания: {v0}').format(
                        v0=html.escape(str(application_data['company_name']))
                    )
                )
            if application_data.get('telegram_channel'):
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_161', '📢 Канал: {v0}').format(
                        v0=html.escape(str(application_data['telegram_channel']))
                    )
                )
            if application_data.get('website_url'):
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_162', '🌐 Сайт: {v0}').format(
                        v0=html.escape(str(application_data['website_url']))
                    )
                )
            if application_data.get('description'):
                desc = str(application_data['description'])
                if len(desc) > 200:
                    desc = desc[:197] + '...'
                message_lines.append(f'📝 {html.escape(desc)}')
            if application_data.get('expected_monthly_referrals'):
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_163', '👥 Ожидаемых рефералов: {v0}/мес').format(
                        v0=application_data['expected_monthly_referrals']
                    )
                )
            if application_data.get('desired_commission_percent'):
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_164', '💰 Желаемая комиссия: {v0}%').format(
                        v0=application_data['desired_commission_percent']
                    )
                )

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PARTNERS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о заявке на партнёрку', error=e)
            return False

    async def send_withdrawal_request_notification(
        self,
        user: User,
        amount_kopeks: int,
        payment_details: str | None = None,
        *,
        request_id: int | None = None,
    ) -> bool:
        """Уведомление о запросе на вывод средств.

        С ``request_id`` к уведомлению прикладываются кнопки «Одобрить»/«Отклонить»
        по роли получателя — как у заявки, поданной из бота.
        """
        texts = await self._notification_texts()
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                texts.t('ADMIN_NOTIFY_102', '💸 <b>ЗАПРОС НА ВЫВОД СРЕДСТВ</b>'),
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 {format_username_link(username)}')

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_165', '💵 <b>Сумма: {v0}</b>').format(
                        v0=settings.format_price(amount_kopeks)
                    ),
                    texts.t('ADMIN_NOTIFY_051', '💰 Баланс: {v0}').format(
                        v0=settings.format_price(user.balance_kopeks)
                    ),
                ]
            )

            if payment_details:
                details = str(payment_details)
                if len(details) > 200:
                    details = details[:197] + '...'
                message_lines.extend(
                    ['', texts.t('ADMIN_NOTIFY_194', '💳 Реквизиты: {v0}').format(v0=html.escape(details))]
                )

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            reply_markup = None
            if request_id is not None:
                from app.keyboards.withdrawal import get_withdrawal_request_keyboard

                reply_markup = get_withdrawal_request_keyboard(
                    request_id,
                    WithdrawalRequestStatus.PENDING.value,
                    user_db_id=getattr(user, 'id', None),
                    role=self.resolve_recipient_role(),
                    language=texts.language,
                )

            return await self._send_message(
                '\n'.join(message_lines), reply_markup=reply_markup, category=NotificationCategory.PARTNERS
            )

        except Exception as e:
            logger.error('Ошибка отправки уведомления о запросе на вывод', error=e)
            return False

    async def send_withdrawal_pending_reminder(self, request: WithdrawalRequest, waited_minutes: int) -> bool:
        """Напоминание о заявке на вывод, которая ждёт решения дольше лимита.

        Аналог SLA-напоминания по тикетам. Уходит в топик заявок на вывод
        (REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID), а без него — по категории
        PARTNERS, то есть туда же, куда пришло исходное уведомление о заявке.
        Кнопки — та же клавиатура, что у исходного уведомления, по роли получателя:
        решить заявку можно прямо отсюда.
        """
        texts = await self._notification_texts()
        if not self._is_enabled():
            return False

        try:
            from app.keyboards.withdrawal import get_withdrawal_request_keyboard

            user = getattr(request, 'user', None)
            user_display = self._get_user_display(user) if user else 'Unknown'
            user_id_display = self._get_user_identifier_display(user) if user else '—'
            username = getattr(user, 'username', None) if user else None

            message_lines = [
                texts.t('ADMIN_NOTIFY_103', '⏰ <b>Заявка на вывод ждёт решения</b>'),
                '',
                texts.t('ADMIN_NOTIFY_104', '🆔 <b>Заявка:</b> #{v0}').format(v0=request.id),
                texts.t('ADMIN_NOTIFY_105', '👤 <b>Пользователь:</b> {v0} ({v1})').format(
                    v0=user_display, v1=user_id_display
                ),
            ]
            if username:
                message_lines.append(f'📱 <b>Username:</b> {format_username_link(username)}')
            message_lines.extend(
                [
                    texts.t('ADMIN_NOTIFY_166', '💵 <b>Сумма:</b> {v0}').format(
                        v0=settings.format_price(request.amount_kopeks)
                    ),
                    texts.t('ADMIN_NOTIFY_167', '⏱️ <b>Ожидает решения:</b> {v0}').format(
                        v0=_format_waiting(waited_minutes, texts=texts)
                    ),
                ]
            )

            # Та же клавиатура, что у исходного уведомления. Собранная вручную вела на
            # admin_user_<telegram_id> — у такого callback обработчика нет, — рисовала
            # профиль в групповом чате и давала модератору кнопки, которые ответят
            # «нет доступа».
            keyboard = get_withdrawal_request_keyboard(
                request.id,
                WithdrawalRequestStatus.PENDING.value,
                user_db_id=getattr(request, 'user_id', None) or getattr(user, 'id', None),
                role=self.resolve_recipient_role(),
                language=texts.language,
            )

            topic_id = getattr(settings, 'REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID', None) or None
            return await self._send_message(
                '\n'.join(message_lines),
                reply_markup=keyboard,
                category=NotificationCategory.PARTNERS,
                thread_id=topic_id,
            )
        except Exception as e:
            logger.error(
                'Ошибка отправки напоминания о заявке на вывод',
                request_id=getattr(request, 'id', None),
                error=e,
            )
            return False

    async def send_bulk_ban_notification(
        self,
        admin_user_id: int,
        successfully_banned: int,
        not_found: int,
        errors: int,
        admin_name: str | None = None,
    ) -> bool:
        """Отправляет уведомление о массовой блокировке пользователей"""
        texts = await self._notification_texts()
        if admin_name is None:
            admin_name = texts.t('ADMIN_NOTIFY_004', 'Администратор')
        if not self._is_enabled():
            return False

        try:
            message_lines = [
                texts.t('ADMIN_NOTIFY_106', '🛑 <b>МАССОВАЯ БЛОКИРОВКА ПОЛЬЗОВАТЕЛЕЙ</b>'),
                '',
                texts.t('ADMIN_NOTIFY_107', '👮 <b>Администратор:</b> {v0}').format(v0=html.escape(admin_name)),
                texts.t('ADMIN_NOTIFY_108', '🆔 <b>ID администратора:</b> {v0}').format(v0=admin_user_id),
                '',
                texts.t('ADMIN_NOTIFY_109', '📊 <b>Результаты:</b>'),
                texts.t('ADMIN_NOTIFY_110', '✅ Успешно заблокировано: {v0}').format(v0=successfully_banned),
                texts.t('ADMIN_NOTIFY_111', '❌ Не найдено: {v0}').format(v0=not_found),
                texts.t('ADMIN_NOTIFY_112', '💥 Ошибок: {v0}').format(v0=errors),
            ]

            total_processed = successfully_banned + not_found + errors
            if total_processed > 0:
                success_rate = (successfully_banned / total_processed) * 100
                message_lines.append(texts.t('ADMIN_NOTIFY_168', '📈 Успешность: {v0:.1f}%').format(v0=success_rate))

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            message = '\n'.join(message_lines)
            return await self._send_message(message, category=NotificationCategory.PARTNERS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о массовой блокировке', error=e)
            return False

    async def send_ticket_event_notification(
        self,
        text: str,
        keyboard: types.InlineKeyboardMarkup | None = None,
        *,
        media_file_id: str | None = None,
        media_type: str | None = None,
    ) -> bool:
        """Публичный метод для отправки уведомлений по тикетам в админ-топик.
        Учитывает настройки включенности в settings.
        Если передан media_file_id, отправляет медиа в тот же топик вместе с текстом.
        """
        # Respect runtime toggle for admin ticket notifications
        try:
            from app.services.support_settings_service import SupportSettingsService

            runtime_enabled = SupportSettingsService.get_admin_ticket_notifications_enabled()
        except Exception:
            runtime_enabled = True
        if not (self._is_enabled() and runtime_enabled):
            logger.info(
                'Ticket notification skipped',
                _is_enabled=self._is_enabled(),
                runtime_enabled=runtime_enabled,
            )
            return False

        # Сохраняем тип вложения; длинное уведомление отправляется отдельно.
        if media_file_id and media_type in {'photo', 'video'}:
            return await self._send_ticket_media_notification(text, media_file_id, media_type, keyboard)

        return await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)

    async def _send_ticket_media_notification(
        self,
        text: str,
        media_file_id: str,
        media_type: str,
        keyboard: types.InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Отправить фото или видео с текстом в тикет-топик.
        Если текст помещается в caption (≤1024 символов после парсинга HTML) — медиа с caption.
        Иначе — сначала текст, потом медиа в тот же топик.
        """
        if not self.chat_id:
            return False

        thread_id = self._resolve_topic_id(category=NotificationCategory.TICKETS)
        sender = self.bot.send_video if media_type == 'video' else self.bot.send_photo

        try:
            if not caption_exceeds_telegram_limit(text):
                # Медиа с caption — всё в одном сообщении
                media_kwargs: dict = {
                    'chat_id': self.chat_id,
                    media_type: media_file_id,
                    'caption': text,
                    'parse_mode': 'HTML',
                }
                if thread_id:
                    media_kwargs['message_thread_id'] = thread_id
                if keyboard:
                    media_kwargs['reply_markup'] = keyboard
                await sender(**media_kwargs)
            else:
                # Текст отдельно, медиа следом в тот же топик
                await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)
                media_kwargs = {
                    'chat_id': self.chat_id,
                    media_type: media_file_id,
                }
                if thread_id:
                    media_kwargs['message_thread_id'] = thread_id
                await sender(**media_kwargs)

            return True
        except Exception as e:
            logger.error('Ошибка отправки медиа-уведомления тикета', error=e)
            # Fallback: отправляем хотя бы текст
            return await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)

    async def send_suspicious_traffic_notification(self, message: str, bot: Bot, topic_id: int | None = None) -> bool:
        """
        Отправляет уведомление о подозрительной активности трафика

        Args:
            message: текст уведомления
            bot: экземпляр бота для отправки сообщения
            topic_id: ID топика для отправки уведомления (если не указан, использует стандартный)
        """
        if not self._is_enabled() or not self.category_enabled.get(NotificationCategory.INFRASTRUCTURE, True):
            return False

        # Используем специальный топик для подозрительной активности, если он задан
        notification_topic_id = topic_id or self.topic_id

        try:
            message_kwargs = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }

            if notification_topic_id:
                message_kwargs['message_thread_id'] = notification_topic_id

            await bot.send_message(**message_kwargs)
            logger.info(
                'Уведомление о подозрительной активности отправлено в чат топик',
                chat_id=self.chat_id,
                notification_topic_id=notification_topic_id,
            )
            return True

        except TelegramForbiddenError:
            logger.error('Бот не имеет прав для отправки в чат', chat_id=self.chat_id)
            return False
        except TelegramBadRequest as e:
            logger.error('Ошибка отправки уведомления о подозрительной активности', error=e)
            return False
        except Exception as e:
            logger.error('Неожиданная ошибка при отправке уведомления о подозрительной активности', error=e)
            return False
