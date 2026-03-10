import asyncio
import html
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import Bot, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import yandex_offline_conv_service as yandex_conv
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.subscription_event import create_subscription_event
from app.database.crud.transaction import get_transaction_by_id
from app.database.crud.user import get_user_by_id
from app.database.models import (
    AdvertisingCampaign,
    GuestPurchase,
    PromoCodeType,
    PromoGroup,
    Subscription,
    Transaction,
    User,
)
from app.utils.message_patch import caption_exceeds_telegram_limit
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)





class AdminNotificationService:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
        self.topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None)
        self.ticket_topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID', None)
        self.enabled = getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)

    async def _get_referrer_info(self, db: AsyncSession, referred_by_id: int | None) -> str:
        if not referred_by_id:
            return 'Нет'

        try:
            referrer = await get_user_by_id(db, referred_by_id)
            if not referrer:
                return f'ID {referred_by_id} (не найден)'

            if referrer.username:
                return f'@{referrer.username} (ID: {referred_by_id})'
            if referrer.telegram_id:
                return f'ID {referrer.telegram_id}'
            if referrer.email:
                return f'📧 {referrer.email}'
            return f'User#{referred_by_id}'

        except Exception as e:
            logger.error('Ошибка получения данных рефера', referred_by_id=referred_by_id, error=e)
            return f'ID {referred_by_id}'

    async def _get_user_promo_group(self, db: AsyncSession, user: User) -> PromoGroup | None:
        if getattr(user, 'promo_group', None):
            return user.promo_group

        if not user.promo_group_id:
            return None

        try:
            await db.refresh(user, attribute_names=['promo_group'])
        except Exception:
            # relationship might not be available — fallback to direct fetch
            pass

        if getattr(user, 'promo_group', None):
            return user.promo_group

        try:
            return await get_promo_group_by_id(db, user.promo_group_id)
        except Exception as e:
            logger.error(
                'Ошибка загрузки промогруппы пользователя',
                promo_group_id=user.promo_group_id,
                telegram_id=user.telegram_id,
                e=e,
            )
            return None

    def _get_user_display(self, user: User) -> str:
        first_name = getattr(user, 'first_name', '') or ''
        if first_name:
            return first_name

        username = getattr(user, 'username', '') or ''
        if username:
            return username

        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id is None:
            email = getattr(user, 'email', None)
            if email:
                return email
            return f'User#{getattr(user, "id", "Unknown")}'
        return f'ID{telegram_id}'

    def _get_user_identifier_display(self, user: User) -> str:
        """Get user identifier for display in notifications (telegram_id or email)."""
        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id:
            return f'<code>{telegram_id}</code>'

        email = getattr(user, 'email', None)
        if email:
            return f'📧 {email}'

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
    ) -> None:
        """Persist subscription-related event for external dashboards."""

        try:
            await create_subscription_event(
                db,
                user_id=user.id,
                event_type=event_type,
                subscription_id=subscription.id if subscription else None,
                transaction_id=transaction.id if transaction else None,
                amount_kopeks=amount_kopeks,
                currency=None,
                message=message,
                occurred_at=occurred_at,
                extra=extra or None,
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие подписки для пользователя',
                event_type=event_type,
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

            try:
                await db.rollback()
            except Exception:
                logger.error(
                    'Не удалось выполнить rollback после ошибки события подписки пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

    def _format_promo_group_discounts(self, promo_group: PromoGroup) -> list[str]:
        discount_lines: list[str] = []

        discount_map = {
            'servers': ('Серверы', promo_group.server_discount_percent),
            'traffic': ('Трафик', promo_group.traffic_discount_percent),
            'devices': ('Устройства', promo_group.device_discount_percent),
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
            formatted_periods = ', '.join(f'{days} д. — -{percent}%' for days, percent in period_items)
            discount_lines.append(f'• Периоды: {formatted_periods}')

        if promo_group.apply_discounts_to_addons:
            discount_lines.append('• Доп. услуги: ✅ скидка действует')
        else:
            discount_lines.append('• Доп. услуги: ❌ без скидки')

        return discount_lines

    def _format_promo_group_block(
        self,
        promo_group: PromoGroup | None,
        *,
        title: str = 'Промогруппа',
        icon: str = '🏷️',
    ) -> str:
        if not promo_group:
            return f'{icon} <b>{title}:</b> —'

        lines = [f'{icon} <b>{title}:</b> {promo_group.name}']

        discount_lines = self._format_promo_group_discounts(promo_group)
        if discount_lines:
            lines.append('💸 <b>Скидки:</b>')
            lines.extend(discount_lines)
        else:
            lines.append('💸 <b>Скидки:</b> отсутствуют')

        return '\n'.join(lines)

    def _get_promocode_type_display(self, promo_type: str | None) -> str:
        mapping = {
            PromoCodeType.BALANCE.value: '💰 Бонус на баланс',
            PromoCodeType.SUBSCRIPTION_DAYS.value: '⏰ Доп. дни подписки',
            PromoCodeType.TRIAL_SUBSCRIPTION.value: '🎁 Триал подписка',
        }

        if not promo_type:
            return 'ℹ️ Не указан'

        return mapping.get(promo_type, f'ℹ️ {promo_type}')

    def _format_campaign_bonus(self, campaign: AdvertisingCampaign, *, tariff_name: str | None = None) -> list[str]:
        if campaign.is_balance_bonus:
            return [
                f'💰 Баланс: {settings.format_price(campaign.balance_bonus_kopeks or 0)}',
            ]

        if campaign.is_subscription_bonus:
            default_devices = getattr(settings, 'DEFAULT_DEVICE_LIMIT', 1)
            details = [
                f'📅 {campaign.subscription_duration_days or 0} дн. '
                f'• 📊 {campaign.subscription_traffic_gb or 0} ГБ '
                f'• 📱 {campaign.subscription_device_limit or default_devices} устр.',
            ]
            if campaign.subscription_squads:
                details.append(f'🌐 Сквады: {len(campaign.subscription_squads)} шт.')
            return details

        if campaign.is_tariff_bonus:
            name = tariff_name or f'ID {campaign.tariff_id}'
            details = [f'📦 Тариф: <b>{name}</b>']
            if campaign.tariff_duration_days:
                details.append(f'📅 Период: {campaign.tariff_duration_days} дней')
            return details

        if campaign.is_none_bonus:
            return ['🔗 Только отслеживание']

        return ['ℹ️ Бонусы не предусмотрены']

    async def send_trial_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        *,
        charged_amount_kopeks: int | None = None,
    ) -> bool:
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
                    'trial_duration_days': settings.TRIAL_DURATION_DAYS,
                    'traffic_limit_gb': settings.TRIAL_TRAFFIC_LIMIT_GB,
                    'device_limit': subscription.device_limit,
                },
            )

            if not self._is_enabled():
                return False

            user_status = '🆕 Новый' if not user.has_had_paid_subscription else '🔄 Существующий'
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
                payment_block = f'\n💳 <b>Оплата за активацию:</b> {settings.format_price(charged_amount_kopeks)}'

            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            # Получаем название тарифа (если режим тарифов)
            tariff_name = await self._get_tariff_name(db, subscription)

            message_lines = [
                '🎯 <b>АКТИВАЦИЯ ТРИАЛА</b>',
                '',
                f'👤 <b>Пользователь:</b> {user_display}',
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> @{getattr(user, "username", None) or "отсутствует"}',
                f'👥 <b>Статус:</b> {user_status}',
                '',
            ]

            # Промогруппа — только название, без скидок
            if promo_group:
                message_lines.append(f'🏷️ <b>Промогруппа:</b> {promo_group.name}')
            else:
                message_lines.append('🏷️ <b>Промогруппа:</b> —')

            # Тариф триала (если есть)
            if tariff_name:
                message_lines.append(f'📦 <b>Тариф:</b> {tariff_name}')

            message_lines.append('')

            message_lines.extend(
                [
                    '⏰ <b>Параметры триала:</b>',
                    f'📅 Период: {settings.TRIAL_DURATION_DAYS} дней',
                    f'📊 Трафик: {self._format_traffic(settings.TRIAL_TRAFFIC_LIMIT_GB)}',
                    f'📱 Устройства: {trial_device_limit}',
                    f'🌐 Сервер: {subscription.connected_squads[0] if subscription.connected_squads else "По умолчанию"}',
                ]
            )

            if payment_block:
                message_lines.append(payment_block)

            message_lines.append('')
            message_lines.append(
                f'📆 <b>Действует до:</b> {format_local_datetime(subscription.end_date, "%d.%m.%Y %H:%M")}'
            )

            # Реферер — только если есть
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id)
                if referrer_info != 'Нет':
                    message_lines.append(f'🔗 <b>Реферер:</b> {referrer_info}')

            message_lines.append('')
            message_lines.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            return await self._send_message('\n'.join(message_lines))

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
                return tariff.name
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
        try:
            total_amount = (
                amount_kopeks if amount_kopeks is not None else (abs(transaction.amount_kopeks) if transaction else 0)
            )

            # Yandex offline conversion: purchase event (fire-and-forget, uses own session)
            yandex_conv.spawn_bg(yandex_conv.fire_purchase_bg(user.id, total_amount))

            await self._record_subscription_event(
                db,
                event_type='purchase',
                user=user,
                subscription=subscription,
                transaction=transaction,
                amount_kopeks=total_amount,
                message='Subscription purchase',
                occurred_at=(transaction.completed_at or transaction.created_at) if transaction else datetime.now(UTC),
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
                event_title = '🔄 СМЕНА ТАРИФА'
                user_status = 'Смена тарифа'
            elif was_trial_conversion:
                event_title = '🔄 КОНВЕРСИЯ ИЗ ТРИАЛА'
                user_status = 'Конверсия'
            elif purchase_type == 'first_purchase':
                event_title = '💎 ПОКУПКА ПОДПИСКИ'
                user_status = 'Первая покупка'
            elif purchase_type == 'renewal' or (purchase_type is None and user.has_had_paid_subscription):
                event_title = '💎 ПРОДЛЕНИЕ ПОДПИСКИ'
                user_status = 'Продление'
            else:
                event_title = '💎 ПОКУПКА ПОДПИСКИ'
                user_status = 'Первая покупка'

            # Получаем название тарифа
            tariff_name = await self._get_tariff_name(db, subscription)

            servers_info = await self._get_servers_info(subscription.connected_squads)
            payment_method = self._get_payment_method_display(transaction.payment_method) if transaction else 'Баланс'
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
                message_lines.append(f'📱 @{username}')

            message_lines.append(f'📋 {user_status}')

            # Тариф (если есть)
            if tariff_name:
                message_lines.append(f'🏷️ Тариф: <b>{tariff_name}</b>')

            message_lines.extend(
                [
                    '',
                    f'💵 <b>{settings.format_price(total_amount)}</b> • {payment_method}',
                    f'📅 {period_days} дн. • до {format_local_datetime(subscription.end_date, "%d.%m.%Y")}',
                    f'📊 {self._format_traffic(subscription.traffic_limit_gb)} • 📱 {subscription.device_limit} устр.',
                    f'🌐 {servers_info}',
                ]
            )

            # Баланс после покупки
            message_lines.append(f'💰 Баланс: {settings.format_price(user.balance_kopeks)}')

            # Реферер (только если есть)
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id)
                if referrer_info != 'Нет':
                    message_lines.append(f'🔗 Реф: {referrer_info}')

            # ID транзакции (только если есть)
            if transaction:
                message_lines.append(f'🆔 #{transaction.id}')

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines))

        except Exception as e:
            logger.error('Ошибка отправки уведомления о покупке', error=e)
            return False

    async def send_version_update_notification(self, current_version: str, latest_version, total_updates: int) -> bool:
        """Отправляет уведомление о новых обновлениях."""
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
                header = '🆕 <b>Доступно обновление</b>'

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
                suffix_lines.append(f'Доступно обновлений: <b>{total_updates}</b>')
            suffix_lines.extend(
                [
                    f'<a href="{repo_url}">Репозиторий</a>',
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

            return await self._send_message(message)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об обновлении', error=e)
            return False

    async def send_version_check_error_notification(self, error_message: str, current_version: str) -> bool:
        if not self._is_enabled():
            return False

        try:
            message = f"""⚠️ <b>ОШИБКА ПРОВЕРКИ ОБНОВЛЕНИЙ</b>

    📦 <b>Текущая версия:</b> <code>{current_version}</code>
    ❌ <b>Ошибка:</b> {error_message}

    🔄 Следующая попытка через час.
    ⚙️ Проверьте доступность GitHub API и настройки сети.

    ⚙️ <i>Система автоматических обновлений • {format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')}</i>"""

            return await self._send_message(message)

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
    ) -> str:
        payment_method = self._get_payment_method_display(transaction.payment_method)
        balance_change = user.balance_kopeks - old_balance
        subscription_status = self._get_subscription_status(subscription)
        timestamp = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
        user_display = self._get_user_display(user)
        user_id_display = self._get_user_identifier_display(user)

        # --- Основной блок ---
        message_lines: list[str] = [
            '💰 <b>ПОПОЛНЕНИЕ БАЛАНСА</b>',
            '',
            f'👤 {user_display} ({user_id_display})',
        ]

        username = getattr(user, 'username', None)
        if username:
            message_lines.append(f'📱 @{username}')

        message_lines.append(f'💳 {topup_status}')

        # Промогруппа -- только название
        if promo_group:
            message_lines.append(f'🏷️ Промогруппа: {promo_group.name}')

        message_lines.append('')

        # --- Детали пополнения ---
        message_lines.extend(
            [
                f'💵 <b>{settings.format_price(transaction.amount_kopeks)}</b> | {payment_method}',
                '',
                f'📉 {settings.format_price(old_balance)} → 📈 {settings.format_price(user.balance_kopeks)}'
                f' (<b>+{settings.format_price(balance_change)}</b>)',
            ]
        )

        # --- Подписка ---
        message_lines.append(f'📱 Подписка: {subscription_status}')

        # --- Реферер (только если есть) ---
        if referrer_info and referrer_info != 'Нет':
            message_lines.append(f'🔗 Реферер: {referrer_info}')

        # --- Expandable blockquote с техническими деталями ---
        detail_lines: list[str] = [
            f'ID транзакции: {transaction.id}',
            f'Способ оплаты: {transaction.payment_method or "balance"}',
        ]

        if transaction.external_id:
            detail_lines.append(f'Внешний ID: {transaction.external_id}')

        if transaction.description:
            desc = transaction.description
            if len(desc) > 120:
                desc = desc[:117] + '...'
            detail_lines.append(f'Описание: {desc}')

        if transaction.created_at:
            detail_lines.append(f'Создана: {format_local_datetime(transaction.created_at, "%d.%m.%Y %H:%M:%S")}')

        if transaction.completed_at:
            detail_lines.append(f'Завершена: {format_local_datetime(transaction.completed_at, "%d.%m.%Y %H:%M:%S")}')

        if transaction.receipt_uuid:
            detail_lines.append(f'Чек UUID: {transaction.receipt_uuid}')

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
        logger.info('Начинаем отправку уведомления о пополнении баланса')

        if db:
            try:
                await self._record_subscription_event(
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
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

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
            return await self._send_message(message)
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

            payment_method = self._get_payment_method_display(transaction.payment_method)
            servers_info = await self._get_servers_info(subscription.connected_squads)
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message = f"""⏰ <b>ПРОДЛЕНИЕ ПОДПИСКИ</b>

👤 <b>Пользователь:</b> {user_display}
🆔 <b>{user_id_label}:</b> {user_id_display}
📱 <b>Username:</b> @{getattr(user, 'username', None) or 'отсутствует'}

{promo_block}

💰 <b>Платеж:</b>
💵 Сумма: {settings.format_price(abs(transaction.amount_kopeks))}
💳 Способ: {payment_method}
🆔 ID транзакции: {transaction.id}

📅 <b>Продление:</b>
➕ Добавлено дней: {extended_days}
📆 Было до: {format_local_datetime(old_end_date, '%d.%m.%Y %H:%M')}
📆 Стало до: {format_local_datetime(current_end_date, '%d.%m.%Y %H:%M')}

📱 <b>Текущие параметры:</b>
📊 Трафик: {self._format_traffic(subscription.traffic_limit_gb)}
📱 Устройства: {subscription.device_limit}
🌐 Серверы: {servers_info}

💰 <b>Баланс после операции:</b> {settings.format_price(current_balance)}

⏰ <i>{format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')}</i>"""

            return await self._send_message(message)

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
            promo_block = self._format_promo_group_block(promo_group)
            type_display = self._get_promocode_type_display(promocode_data.get('type'))
            usage_info = f'{promocode_data.get("current_uses", 0)}/{promocode_data.get("max_uses", 0)}'
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                '🎫 <b>АКТИВАЦИЯ ПРОМОКОДА</b>',
                '',
                f'👤 <b>Пользователь:</b> {user_display}',
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> @{getattr(user, "username", None) or "отсутствует"}',
                '',
                promo_block,
                '',
                '🎟️ <b>Промокод:</b>',
                f'🔖 Код: <code>{promocode_data.get("code")}</code>',
                f'🧾 Тип: {type_display}',
                f'📊 Использования: {usage_info}',
            ]

            balance_bonus = promocode_data.get('balance_bonus_kopeks', 0)
            if balance_bonus:
                message_lines.append(f'💰 Бонус на баланс: {settings.format_price(balance_bonus)}')

            subscription_days = promocode_data.get('subscription_days', 0)
            if subscription_days:
                message_lines.append(f'📅 Доп. дни подписки: {subscription_days}')

            valid_until = promocode_data.get('valid_until')
            if valid_until:
                message_lines.append(
                    f'⏳ Действует до: {format_local_datetime(valid_until, "%d.%m.%Y %H:%M")}'
                    if isinstance(valid_until, datetime)
                    else f'⏳ Действует до: {valid_until}'
                )

            message_lines.extend(
                [
                    '',
                    '💼 <b>Баланс:</b>',
                    (
                        f'{settings.format_price(balance_before_kopeks)} → {settings.format_price(balance_after_kopeks)}'
                        if balance_before_kopeks is not None and balance_after_kopeks is not None
                        else 'ℹ️ Баланс не изменился'
                    ),
                    '',
                    '📝 <b>Эффект:</b>',
                    effect_description.strip() or '✅ Промокод активирован',
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines))

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
        if user:
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
            user_status = '🆕 Новый' if not user else '👥 Существующий'

            message_lines = [
                '📣 <b>ПЕРЕХОД ПО РК</b>',
                '',
                f'🧾 {campaign.name} (<code>{campaign.start_parameter}</code>)',
                '',
                f'👤 {full_name} (<code>{telegram_user.id}</code>)',
            ]

            if telegram_user.username:
                message_lines.append(f'📱 @{telegram_user.username}')

            message_lines.append(f'📋 {user_status}')

            # Промогруппа — только если есть
            if user:
                promo_group = await self._get_user_promo_group(db, user)
                if promo_group:
                    message_lines.append(f'🏷️ Промогруппа: {promo_group.name}')

            message_lines.append('')

            # Загружаем название тарифа для tariff-бонуса
            tariff_name = None
            if campaign.is_tariff_bonus and campaign.tariff_id:
                try:
                    from app.database.crud.tariff import get_tariff_by_id

                    tariff = await get_tariff_by_id(db, campaign.tariff_id)
                    if tariff:
                        tariff_name = tariff.name
                except Exception:
                    pass

            # Бонус кампании
            bonus_lines = self._format_campaign_bonus(campaign, tariff_name=tariff_name)
            message_lines.extend(bonus_lines)

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines))

        except Exception as e:
            logger.error('Ошибка отправки уведомления о переходе по кампании', error=e)
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
            title = '🤖 АВТОМАТИЧЕСКАЯ СМЕНА ПРОМОГРУППЫ' if automatic else '👥 СМЕНА ПРОМОГРУППЫ'
            initiator_line = None
            if initiator:
                initiator_line = f'👮 <b>Инициатор:</b> {initiator.full_name} (ID: {initiator.telegram_id})'
            elif automatic:
                initiator_line = '🤖 Автоматическое назначение'
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                f'{title}',
                '',
                f'👤 <b>Пользователь:</b> {user_display}',
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> @{getattr(user, "username", None) or "отсутствует"}',
                '',
                self._format_promo_group_block(new_group, title='Новая промогруппа', icon='🏆'),
            ]

            if old_group and old_group.id != new_group.id:
                message_lines.extend(
                    [
                        '',
                        self._format_promo_group_block(old_group, title='Предыдущая промогруппа', icon='♻️'),
                    ]
                )

            if initiator_line:
                message_lines.extend(['', initiator_line])

            if reason:
                message_lines.extend(['', f'📝 Причина: {reason}'])

            message_lines.extend(
                [
                    '',
                    f'💰 Баланс пользователя: {settings.format_price(user.balance_kopeks)}',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines))

        except Exception as e:
            logger.error('Ошибка отправки уведомления о смене промогруппы', error=e)
            return False

    async def _send_message(
        self, text: str, reply_markup: types.InlineKeyboardMarkup | None = None, *, ticket_event: bool = False
    ) -> bool:
        if not self.chat_id:
            logger.warning('ADMIN_NOTIFICATIONS_CHAT_ID не настроен')
            return False

        try:
            message_kwargs = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }

            # route to ticket-specific topic if provided
            thread_id = None
            if ticket_event and self.ticket_topic_id:
                thread_id = self.ticket_topic_id
            elif self.topic_id:
                thread_id = self.topic_id
            if thread_id:
                message_kwargs['message_thread_id'] = thread_id
            if reply_markup is not None:
                message_kwargs['reply_markup'] = reply_markup

            await self.bot.send_message(**message_kwargs)
            logger.info('Уведомление отправлено в чат', chat_id=self.chat_id)
            return True

        except TelegramForbiddenError:
            logger.error('Бот не имеет прав для отправки в чат', chat_id=self.chat_id)
            return False
        except TelegramBadRequest as e:
            logger.error('Ошибка отправки уведомления', error=e)
            return False
        except Exception as e:
            logger.error('Неожиданная ошибка при отправке уведомления', error=e)
            return False

    def _is_enabled(self) -> bool:
        return self.enabled and bool(self.chat_id)

    @property
    def is_enabled(self) -> bool:
        """Public check for whether admin notifications are configured and active."""
        return self._is_enabled()

    async def send_admin_notification(self, text: str, reply_markup: types.InlineKeyboardMarkup | None = None) -> bool:
        """Send a generic notification to admin chat with optional inline keyboard."""
        if not self._is_enabled():
            return False
        return await self._send_message(text, reply_markup=reply_markup)

    async def send_guest_purchase_notification(
        self,
        purchase: GuestPurchase,
        tariff_name: str,
        *,
        is_pending_activation: bool = False,
    ) -> bool:
        """Send admin notification for a guest (landing page) purchase."""
        if not self._is_enabled():
            return False

        try:
            if is_pending_activation:
                event_title = '⏳ ПОКУПКА С ЛЕНДИНГА (ожидает активации)'
            elif purchase.is_gift:
                event_title = '🎁 ПОКУПКА В ПОДАРОК С ЛЕНДИНГА'
            else:
                event_title = '🛒 ПОКУПКА С ЛЕНДИНГА'

            # Landing page slug
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

            # Contact info
            contact_display = html.escape(purchase.contact_value or '—')
            contact_icon = '📧' if purchase.contact_type == 'email' else '📱'

            payment_method = self._get_payment_method_display(purchase.payment_method)

            message_lines = [
                f'<b>{event_title}</b>',
                '',
                f'🌐 Страница: <b>/buy/{html.escape(landing_slug)}</b>',
                f'{contact_icon} Покупатель: <code>{contact_display}</code>',
            ]

            if purchase.is_gift:
                recipient_value = html.escape(purchase.gift_recipient_value or '—')
                recipient_icon = '📧' if purchase.gift_recipient_type == 'email' else '📱'
                message_lines.append(f'{recipient_icon} Получатель: <code>{recipient_value}</code>')
                if purchase.gift_message:
                    raw_msg = purchase.gift_message[:100]
                    suffix = '…' if len(purchase.gift_message) > 100 else ''
                    message_lines.append(f'💬 <i>{html.escape(raw_msg)}{suffix}</i>')

            # Payment details in blockquote
            payment_lines = [
                '<blockquote>',
                f'🏷️ Тариф: <b>{html.escape(tariff_name)}</b>',
                f'📅 Период: {purchase.period_days} дн.',
                f'💵 <b>{settings.format_price(purchase.amount_kopeks)}</b> • {payment_method}',
            ]

            if purchase.payment_id:
                payment_lines.append(f'🆔 {html.escape(str(purchase.payment_id))}')

            payment_lines.append('</blockquote>')
            message_lines.extend(payment_lines)

            message_lines.append(f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>')

            return await self._send_message('\n'.join(message_lines))

        except Exception as e:
            logger.error('Ошибка отправки уведомления о гостевой покупке', error=e)
            return False

    async def send_webhook_notification(self, text: str) -> bool:
        """Send a generic webhook/infrastructure notification to admin chat.

        Used by RemnaWaveWebhookService for node, service, and CRM events.
        The caller is responsible for HTML-escaping all untrusted data in `text`.
        """
        if not self._is_enabled():
            return False
        return await self._send_message(text)

    def _get_payment_method_display(self, payment_method: str | None) -> str:
        if not payment_method:
            return '💰 С баланса'

        method_names: dict[str, str] = {
            'telegram_stars': '⭐ Telegram Stars',
            'yookassa': '💳 YooKassa (карта)',
            'tribute': '💎 Tribute (карта)',
            'mulenpay': f'💳 {settings.get_mulenpay_display_name()} (карта)',
            'pal24': f'🏦 {settings.get_pal24_display_name()} (СБП)',
            'cryptobot': f'🪙 {settings.get_cryptobot_display_name()} (крипто)',
            'heleket': f'🪙 {settings.get_heleket_display_name()} (крипто)',
            'wata': f'💳 {settings.get_wata_display_name()}',
            'platega': f'💳 {settings.get_platega_display_name()}',
            'cloudpayments': f'💳 {settings.get_cloudpayments_display_name()}',
            'freekassa': f'💳 {settings.get_freekassa_display_name()}',
            'kassa_ai': f'💳 {settings.get_kassa_ai_display_name()}',
            'manual': '🛠️ Вручную (админ)',
            'balance': '💰 С баланса',
        }

        return method_names.get(payment_method, f'💳 {html.escape(payment_method)}')

    def _format_traffic(self, traffic_gb: int) -> str:
        if traffic_gb == 0:
            return '∞ Безлимит'
        return f'{traffic_gb} ГБ'

    def _get_subscription_status(self, subscription: Subscription | None) -> str:
        if not subscription:
            return '❌ Нет подписки'

        if subscription.is_trial:
            return f'🎯 Триал (до {format_local_datetime(subscription.end_date, "%d.%m")})'
        if subscription.is_active:
            return f'✅ Активна (до {format_local_datetime(subscription.end_date, "%d.%m")})'
        return '❌ Неактивна'

    async def _get_servers_info(self, squad_uuids: list) -> str:
        if not squad_uuids:
            return '❌ Нет серверов'

        try:
            from app.handlers.subscription import get_servers_display_names

            servers_names = await get_servers_display_names(squad_uuids)
            return f'{len(squad_uuids)} шт. ({servers_names})'
        except Exception as e:
            logger.warning('Не удалось получить названия серверов', error=e)
            return f'{len(squad_uuids)} шт.'

    async def send_maintenance_status_notification(
        self, event_type: str, status: str, details: dict[str, Any] = None
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            details = details or {}

            if event_type == 'enable':
                if details.get('auto_enabled', False):
                    icon = '⚠️'
                    title = 'АВТОМАТИЧЕСКОЕ ВКЛЮЧЕНИЕ ТЕХРАБОТ'
                else:
                    icon = '🔧'
                    title = 'ВКЛЮЧЕНИЕ ТЕХРАБОТ'

            elif event_type == 'disable':
                icon = '✅'
                title = 'ОТКЛЮЧЕНИЕ ТЕХРАБОТ'

            elif event_type == 'api_status':
                if status == 'online':
                    icon = '🟢'
                    title = 'API REMNAWAVE ВОССТАНОВЛЕНО'
                else:
                    icon = '🔴'
                    title = 'API REMNAWAVE НЕДОСТУПНО'

            elif event_type == 'monitoring':
                if status == 'started':
                    icon = '🔍'
                    title = 'МОНИТОРИНГ ЗАПУЩЕН'
                else:
                    icon = '⏹️'
                    title = 'МОНИТОРИНГ ОСТАНОВЛЕН'
            else:
                icon = 'ℹ️'
                title = 'СИСТЕМА ТЕХРАБОТ'

            message_parts = [f'{icon} <b>{title}</b>', '']

            if event_type == 'enable':
                if details.get('reason'):
                    message_parts.append(f'📋 <b>Причина:</b> {details["reason"]}')

                if details.get('enabled_at'):
                    enabled_at = details['enabled_at']
                    if isinstance(enabled_at, str):
                        enabled_at = datetime.fromisoformat(enabled_at)
                    message_parts.append(
                        f'🕐 <b>Время включения:</b> {format_local_datetime(enabled_at, "%d.%m.%Y %H:%M:%S")}'
                    )

                message_parts.append(
                    f'🤖 <b>Автоматически:</b> {"Да" if details.get("auto_enabled", False) else "Нет"}'
                )
                message_parts.append('')
                message_parts.append('❗ Обычные пользователи временно не могут использовать бота.')

            elif event_type == 'disable':
                if details.get('disabled_at'):
                    disabled_at = details['disabled_at']
                    if isinstance(disabled_at, str):
                        disabled_at = datetime.fromisoformat(disabled_at)
                    message_parts.append(
                        f'🕐 <b>Время отключения:</b> {format_local_datetime(disabled_at, "%d.%m.%Y %H:%M:%S")}'
                    )

                if details.get('duration'):
                    duration = details['duration']
                    if isinstance(duration, (int, float)):
                        hours = int(duration // 3600)
                        minutes = int((duration % 3600) // 60)
                        if hours > 0:
                            duration_str = f'{hours}ч {minutes}мин'
                        else:
                            duration_str = f'{minutes}мин'
                        message_parts.append(f'⏱️ <b>Длительность:</b> {duration_str}')

                message_parts.append(
                    f'🤖 <b>Было автоматическим:</b> {"Да" if details.get("was_auto", False) else "Нет"}'
                )
                message_parts.append('')
                message_parts.append('✅ Сервис снова доступен для пользователей.')

            elif event_type == 'api_status':
                message_parts.append(f'🔗 <b>API URL:</b> {details.get("api_url", "неизвестно")}')

                if status == 'online':
                    if details.get('response_time'):
                        message_parts.append(f'⚡ <b>Время отклика:</b> {details["response_time"]} сек')

                    if details.get('consecutive_failures', 0) > 0:
                        message_parts.append(f'🔄 <b>Неудачных попыток было:</b> {details["consecutive_failures"]}')

                    message_parts.append('')
                    message_parts.append('API снова отвечает на запросы.')

                else:
                    if details.get('consecutive_failures'):
                        message_parts.append(f'🔄 <b>Попытка №:</b> {details["consecutive_failures"]}')

                    if details.get('error'):
                        error_msg = str(details['error'])[:100]
                        message_parts.append(f'❌ <b>Ошибка:</b> {error_msg}')

                    message_parts.append('')
                    message_parts.append('⚠️ Началась серия неудачных проверок API.')

            elif event_type == 'monitoring':
                if status == 'started':
                    if details.get('check_interval'):
                        message_parts.append(f'🔄 <b>Интервал проверки:</b> {details["check_interval"]} сек')

                    if details.get('auto_enable_configured') is not None:
                        auto_enable = 'Включено' if details['auto_enable_configured'] else 'Отключено'
                        message_parts.append(f'🤖 <b>Автовключение:</b> {auto_enable}')

                    if details.get('max_failures'):
                        message_parts.append(f'🎯 <b>Порог ошибок:</b> {details["max_failures"]}')

                    message_parts.append('')
                    message_parts.append('Система будет следить за доступностью API.')

                else:
                    message_parts.append('Автоматический мониторинг API остановлен.')

            message_parts.append('')
            message_parts.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            message = '\n'.join(message_parts)

            return await self._send_message(message)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о техработах', error=e)
            return False

    async def send_remnawave_panel_status_notification(self, status: str, details: dict[str, Any] = None) -> bool:
        if not self._is_enabled():
            return False

        try:
            details = details or {}

            status_config = {
                'online': {'icon': '🟢', 'title': 'ПАНЕЛЬ REMNAWAVE ДОСТУПНА', 'alert_type': 'success'},
                'offline': {'icon': '🔴', 'title': 'ПАНЕЛЬ REMNAWAVE НЕДОСТУПНА', 'alert_type': 'error'},
                'degraded': {'icon': '🟡', 'title': 'ПАНЕЛЬ REMNAWAVE РАБОТАЕТ СО СБОЯМИ', 'alert_type': 'warning'},
                'maintenance': {'icon': '🔧', 'title': 'ПАНЕЛЬ REMNAWAVE НА ОБСЛУЖИВАНИИ', 'alert_type': 'info'},
            }

            config = status_config.get(status, status_config['offline'])

            message_parts = [f'{config["icon"]} <b>{config["title"]}</b>', '']

            if details.get('api_url'):
                message_parts.append(f'🔗 <b>URL:</b> {details["api_url"]}')

            if details.get('response_time'):
                message_parts.append(f'⚡ <b>Время отклика:</b> {details["response_time"]} сек')

            if details.get('last_check'):
                last_check = details['last_check']
                if isinstance(last_check, str):
                    last_check = datetime.fromisoformat(last_check)
                message_parts.append(f'🕐 <b>Последняя проверка:</b> {format_local_datetime(last_check, "%H:%M:%S")}')

            if status == 'online':
                if details.get('uptime'):
                    message_parts.append(f'⏱️ <b>Время работы:</b> {details["uptime"]}')

                if details.get('users_online'):
                    message_parts.append(f'👥 <b>Пользователей онлайн:</b> {details["users_online"]}')

                message_parts.append('')
                message_parts.append('✅ Все системы работают нормально.')

            elif status == 'offline':
                if details.get('error'):
                    error_msg = str(details['error'])[:150]
                    message_parts.append(f'❌ <b>Ошибка:</b> {error_msg}')

                if details.get('consecutive_failures'):
                    message_parts.append(f'🔄 <b>Неудачных попыток:</b> {details["consecutive_failures"]}')

                message_parts.append('')
                message_parts.append('⚠️ Панель недоступна. Проверьте соединение и статус сервера.')

            elif status == 'degraded':
                if details.get('issues'):
                    issues = details['issues']
                    if isinstance(issues, list):
                        message_parts.append('⚠️ <b>Обнаруженные проблемы:</b>')
                        for issue in issues[:3]:
                            message_parts.append(f'   • {issue}')
                    else:
                        message_parts.append(f'⚠️ <b>Проблема:</b> {issues}')

                message_parts.append('')
                message_parts.append('Панель работает, но возможны задержки или сбои.')

            elif status == 'maintenance':
                if details.get('maintenance_reason'):
                    message_parts.append(f'🔧 <b>Причина:</b> {details["maintenance_reason"]}')

                if details.get('estimated_duration'):
                    message_parts.append(f'⏰ <b>Ожидаемая длительность:</b> {details["estimated_duration"]}')

                message_parts.append('')
                message_parts.append('Панель временно недоступна для обслуживания.')

            message_parts.append('')
            message_parts.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            message = '\n'.join(message_parts)

            return await self._send_message(message)

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
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            # Определяем заголовок по типу операции
            update_titles = {
                'traffic': '📊 ДОКУПКА ТРАФИКА',
                'devices': '📱 ДОКУПКА УСТРОЙСТВ',
                'servers': '🌐 СМЕНА СЕРВЕРОВ',
            }
            title = update_titles.get(update_type, '⚙️ ИЗМЕНЕНИЕ ПОДПИСКИ')

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
                message_lines.append(f'📱 @{username}')

            # Тариф (если есть)
            if tariff_name:
                message_lines.append(f'🏷️ Тариф: <b>{tariff_name}</b>')

            message_lines.append('')

            # Форматируем изменение в зависимости от типа
            if update_type == 'servers':
                old_servers_info = await self._format_servers_detailed(old_value)
                new_servers_info = await self._format_servers_detailed(new_value)
                message_lines.append(f'🔄 {old_servers_info} → {new_servers_info}')
            elif update_type == 'traffic':
                old_formatted = self._format_update_value(old_value, update_type)
                new_formatted = self._format_update_value(new_value, update_type)
                message_lines.append(f'🔄 {old_formatted} → {new_formatted}')
            elif update_type == 'devices':
                message_lines.append(f'🔄 {old_value} → {new_value} устр.')
            else:
                message_lines.append(f'🔄 {old_value} → {new_value}')

            # Стоимость операции
            if price_paid > 0:
                message_lines.append(f'💵 <b>{settings.format_price(price_paid)}</b>')
            else:
                message_lines.append('💸 Бесплатно')

            message_lines.extend(
                [
                    '',
                    f'📅 До {format_local_datetime(subscription.end_date, "%d.%m.%Y")}',
                    f'💰 Баланс: {settings.format_price(user.balance_kopeks)}',
                ]
            )

            # Реферер (только если есть)
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id)
                if referrer_info != 'Нет':
                    message_lines.append(f'🔗 Реф: {referrer_info}')

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines))

        except Exception as e:
            logger.error('Ошибка отправки уведомления об изменении подписки', error=e)
            return False

    async def _format_servers_detailed(self, server_uuids: list[str]) -> str:
        if not server_uuids:
            return 'Нет серверов'

        try:
            from app.handlers.subscription import get_servers_display_names

            servers_names = await get_servers_display_names(server_uuids)

            if servers_names and servers_names != 'Нет серверов':
                return f'{len(server_uuids)} серверов ({servers_names})'
            return f'{len(server_uuids)} серверов'

        except Exception as e:
            logger.warning('Ошибка получения названий серверов для уведомления', error=e)
            return f'{len(server_uuids)} серверов'

    def _format_update_value(self, value: Any, update_type: str) -> str:
        if update_type == 'traffic':
            if value == 0:
                return '♾ Безлимитный'
            return f'{value} ГБ'
        if update_type == 'devices':
            return f'{value} устройств'
        if update_type == 'servers':
            if isinstance(value, list):
                return f'{len(value)} серверов'
            return str(value)
        return str(value)

    async def send_partner_application_notification(
        self,
        user: User,
        application_data: dict[str, Any],
    ) -> bool:
        """Уведомление о новой заявке на партнёрку."""
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                '🤝 <b>ЗАЯВКА НА ПАРТНЁРКУ</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 @{username}')

            message_lines.append('')

            if application_data.get('company_name'):
                message_lines.append(f'🏢 Компания: {html.escape(str(application_data["company_name"]))}')
            if application_data.get('telegram_channel'):
                message_lines.append(f'📢 Канал: {html.escape(str(application_data["telegram_channel"]))}')
            if application_data.get('website_url'):
                message_lines.append(f'🌐 Сайт: {html.escape(str(application_data["website_url"]))}')
            if application_data.get('description'):
                desc = str(application_data['description'])
                if len(desc) > 200:
                    desc = desc[:197] + '...'
                message_lines.append(f'📝 {html.escape(desc)}')
            if application_data.get('expected_monthly_referrals'):
                message_lines.append(f'👥 Ожидаемых рефералов: {application_data["expected_monthly_referrals"]}/мес')
            if application_data.get('desired_commission_percent'):
                message_lines.append(f'💰 Желаемая комиссия: {application_data["desired_commission_percent"]}%')

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines))

        except Exception as e:
            logger.error('Ошибка отправки уведомления о заявке на партнёрку', error=e)
            return False

    async def send_withdrawal_request_notification(
        self,
        user: User,
        amount_kopeks: int,
        payment_details: str | None = None,
    ) -> bool:
        """Уведомление о запросе на вывод средств."""
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                '💸 <b>ЗАПРОС НА ВЫВОД СРЕДСТВ</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 @{username}')

            message_lines.extend(
                [
                    '',
                    f'💵 <b>Сумма: {settings.format_price(amount_kopeks)}</b>',
                    f'💰 Баланс: {settings.format_price(user.balance_kopeks)}',
                ]
            )

            if payment_details:
                details = str(payment_details)
                if len(details) > 200:
                    details = details[:197] + '...'
                message_lines.extend(['', f'💳 Реквизиты: {html.escape(details)}'])

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines))

        except Exception as e:
            logger.error('Ошибка отправки уведомления о запросе на вывод', error=e)
            return False

    async def send_bulk_ban_notification(
        self,
        admin_user_id: int,
        successfully_banned: int,
        not_found: int,
        errors: int,
        admin_name: str = 'Администратор',
    ) -> bool:
        """Отправляет уведомление о массовой блокировке пользователей"""
        if not self._is_enabled():
            return False

        try:
            message_lines = [
                '🛑 <b>МАССОВАЯ БЛОКИРОВКА ПОЛЬЗОВАТЕЛЕЙ</b>',
                '',
                f'👮 <b>Администратор:</b> {admin_name}',
                f'🆔 <b>ID администратора:</b> {admin_user_id}',
                '',
                '📊 <b>Результаты:</b>',
                f'✅ Успешно заблокировано: {successfully_banned}',
                f'❌ Не найдено: {not_found}',
                f'💥 Ошибок: {errors}',
            ]

            total_processed = successfully_banned + not_found + errors
            if total_processed > 0:
                success_rate = (successfully_banned / total_processed) * 100
                message_lines.append(f'📈 Успешность: {success_rate:.1f}%')

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            message = '\n'.join(message_lines)
            return await self._send_message(message)

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
                'Ticket notification skipped: _is_enabled=, runtime_enabled',
                _is_enabled=self._is_enabled(),
                runtime_enabled=runtime_enabled,
            )
            return False

        # Если есть медиа, отправляем фото с текстом как caption (если влезает) или текст + фото
        if media_file_id and media_type == 'photo':
            return await self._send_ticket_photo_notification(text, media_file_id, keyboard)

        return await self._send_message(text, reply_markup=keyboard, ticket_event=True)

    async def _send_ticket_photo_notification(
        self,
        text: str,
        photo_file_id: str,
        keyboard: types.InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Отправить фото с текстом в тикет-топик.
        Если текст помещается в caption (≤1024 символов после парсинга HTML) — фото с caption.
        Иначе — сначала текст, потом фото в тот же топик.
        """
        if not self.chat_id:
            return False

        thread_id = self.ticket_topic_id or self.topic_id

        try:
            if not caption_exceeds_telegram_limit(text):
                # Фото с caption — всё в одном сообщении
                photo_kwargs: dict = {
                    'chat_id': self.chat_id,
                    'photo': photo_file_id,
                    'caption': text,
                    'parse_mode': 'HTML',
                }
                if thread_id:
                    photo_kwargs['message_thread_id'] = thread_id
                if keyboard:
                    photo_kwargs['reply_markup'] = keyboard
                await self.bot.send_photo(**photo_kwargs)
            else:
                # Текст отдельно, фото следом в тот же топик
                await self._send_message(text, reply_markup=keyboard, ticket_event=True)
                photo_kwargs = {
                    'chat_id': self.chat_id,
                    'photo': photo_file_id,
                }
                if thread_id:
                    photo_kwargs['message_thread_id'] = thread_id
                await self.bot.send_photo(**photo_kwargs)

            return True
        except Exception as e:
            logger.error('Ошибка отправки фото-уведомления тикета', error=e)
            # Fallback: отправляем хотя бы текст
            return await self._send_message(text, reply_markup=keyboard, ticket_event=True)

    async def send_suspicious_traffic_notification(self, message: str, bot: Bot, topic_id: int | None = None) -> bool:
        """
        Отправляет уведомление о подозрительной активности трафика

        Args:
            message: текст уведомления
            bot: экземпляр бота для отправки сообщения
            topic_id: ID топика для отправки уведомления (если не указан, использует стандартный)
        """
        if not self.chat_id:
            logger.warning('ADMIN_NOTIFICATIONS_CHAT_ID не настроен')
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
