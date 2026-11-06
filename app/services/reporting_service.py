import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as datetime_time, timedelta
from enum import Enum
from html import escape
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import cast, func, not_, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import false, true

from app.config import settings
from app.database.crud.subscription import get_subscriptions_statistics
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.database import AsyncSessionLocal
from app.database.models import (
    Subscription,
    SubscriptionConversion,
    SubscriptionStatus,
    Ticket,
    TicketStatus,
    Transaction,
    TransactionType,
    User,
)
from app.localization.texts import Texts, get_texts


logger = structlog.get_logger(__name__)


class ReportingServiceError(RuntimeError):
    """Base error for the reporting service."""


class ReportPeriod(Enum):
    DAILY = 'daily'
    WEEKLY = 'weekly'
    MONTHLY = 'monthly'


@dataclass(slots=True)
class ReportPeriodRange:
    start_msk: datetime
    end_msk: datetime
    label: str


class ReportingService:
    """Generates admin summary reports (text only, no charts)."""

    def __init__(self) -> None:
        self.bot: Bot | None = None
        self._task: asyncio.Task | None = None
        self._moscow_tz = ZoneInfo('Europe/Moscow')

    def set_bot(self, bot: Bot) -> None:
        self.bot = bot

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        await self.stop()

        if not settings.ADMIN_REPORTS_ENABLED:
            logger.info('Сервис отчетов отключен настройками')
            return

        if not self.bot:
            logger.warning('Невозможно запустить сервис отчетов без экземпляра бота')
            return

        chat_id = settings.get_reports_chat_id()
        if not chat_id:
            logger.warning('Сервис отчетов не запущен: не указан чат для отправки отчетов')
            return

        send_time = settings.get_reports_send_time()
        if not send_time:
            logger.warning('Сервис отчетов не запущен: не указано время ежедневной отправки')
            return

        self._task = asyncio.create_task(self._auto_daily_loop(send_time))
        logger.info('📊 Сервис отчетов запущен: ежедневная отправка в по МСК', send_time=send_time.strftime('%H:%M'))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def send_report(
        self,
        period: ReportPeriod,
        *,
        report_date: date | None = None,
        send_to_topic: bool = False,
        language: str | None = None,
    ) -> str:
        texts = get_texts(language or settings.DEFAULT_LANGUAGE)
        report_text = await self._build_report(period, report_date, texts=texts)

        if send_to_topic:
            await self._deliver_report(report_text, texts=texts)

        return report_text

    async def _auto_daily_loop(self, send_time: datetime_time) -> None:
        try:
            next_run_utc, report_date = self._calculate_next_run(send_time)

            while True:
                now_utc = datetime.now(UTC)
                delay = (next_run_utc - now_utc).total_seconds()

                if delay > 0:
                    await asyncio.sleep(delay)

                try:
                    await self.send_report(
                        ReportPeriod.DAILY,
                        report_date=report_date,
                        send_to_topic=True,
                    )
                    logger.info('📊 Автоматический отчет за отправлен', report_date=report_date.strftime('%d.%m.%Y'))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error('Ошибка автоматической отправки отчета', exc=exc)

                next_run_utc, report_date = self._calculate_next_run(send_time)

        except asyncio.CancelledError:
            logger.info('Сервис отчетов остановлен')
            raise
        except Exception as exc:
            logger.error('Критическая ошибка в сервисе отчетов', exc=exc)

    def _calculate_next_run(
        self,
        send_time: datetime_time,
    ) -> tuple[datetime, date]:
        now_msk = datetime.now(self._moscow_tz)
        candidate = datetime.combine(now_msk.date(), send_time, tzinfo=self._moscow_tz)

        if now_msk >= candidate:
            candidate += timedelta(days=1)

        report_date = (candidate - timedelta(days=1)).date()
        return candidate.astimezone(UTC), report_date

    async def _deliver_report(self, report_text: str, texts: Texts | None = None) -> None:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        if not self.bot:
            raise ReportingServiceError(texts.t('ADMIN_REPORT_UI_026', 'Бот не инициализирован для отправки отчета'))

        chat_id = settings.get_reports_chat_id()
        if not chat_id:
            raise ReportingServiceError(texts.t('ADMIN_REPORT_UI_027', 'Не задан чат для отправки отчета'))

        topic_id = settings.get_reports_topic_id()

        # Rich-вид (Bot API 10.1): заголовок, разделители, footer с tg-time.
        # При недоступности — классический HTML ниже.
        try:
            from app.utils.rich_admin import classic_admin_html_to_rich, try_send_rich_admin_message

            rich_html = classic_admin_html_to_rich(report_text, footer_label=texts.t('ADMIN_REPORT_UI_029', 'Отчёт'))
            if await try_send_rich_admin_message(self.bot, chat_id, rich_html, thread_id=topic_id):
                logger.info('Rich-отчёт отправлен в чат', chat_id=chat_id)
                return
        except Exception as rich_error:
            logger.warning('Сбой rich-рендера отчёта', error=str(rich_error))

        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=report_text,
                message_thread_id=topic_id,
                parse_mode='HTML',
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.error('Не удалось отправить отчет', exc=exc)
            raise ReportingServiceError(texts.t('ADMIN_REPORT_UI_030', 'Не удалось отправить отчет в чат')) from exc

    # ---------- referral helpers ----------

    def _referral_markers(self) -> list:
        """
        Набор условий, по которым операция помечается как реферальная (если вдруг записана типом DEPOSIT).
        """
        clauses = []

        # Явные флаги
        if hasattr(Transaction, 'is_referral_bonus'):
            clauses.append(Transaction.is_referral_bonus == true())
        if hasattr(Transaction, 'is_bonus'):
            clauses.append(Transaction.is_bonus == true())

        # Источник/причина
        if hasattr(Transaction, 'source'):
            clauses.append(Transaction.source == 'referral')
            clauses.append(Transaction.source == 'referral_bonus')
        if hasattr(Transaction, 'reason'):
            clauses.append(Transaction.reason == 'referral')
            clauses.append(Transaction.reason == 'referral_bonus')
            clauses.append(Transaction.reason == 'referral_reward')

        # Текстовые поля
        like_patterns = ['%реферал%', '%реферальн%', '%referral%']
        if hasattr(Transaction, 'description'):
            for pattern in like_patterns:
                try:
                    clauses.append(Transaction.description.ilike(pattern))
                except Exception:
                    pass
        if hasattr(Transaction, 'comment'):
            for pattern in like_patterns:
                try:
                    clauses.append(Transaction.comment.ilike(pattern))
                except Exception:
                    pass

        return [clause for clause in clauses if clause is not None]

    def _exclude_referral_deposits_condition(self):
        """
        Условие «это НЕ реферальный бонус».
        Если нет ни одного маркера — ничего не исключаем.
        """
        markers = self._referral_markers()
        if not markers:
            return true()
        return not_(or_(*markers))

    # --------------------------------------

    async def _build_report(
        self,
        period: ReportPeriod,
        report_date: date | None,
        texts: Texts | None = None,
    ) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        try:
            period_range = self._get_period_range(period, report_date)
        except ReportingServiceError as exc:
            raise ReportingServiceError(
                texts.t('ADMIN_REPORT_UI_032', 'Неизвестный период отчета: {value1}').format(value1=period)
            ) from exc
        start_utc = period_range.start_msk.astimezone(UTC)
        end_utc = period_range.end_msk.astimezone(UTC)

        async with AsyncSessionLocal() as session:
            totals = await self._collect_current_totals(session)
            stats = await self._collect_period_stats(session, start_utc, end_utc)
            top_referrers = await self._get_top_referrers(session, start_utc, end_utc, limit=5, texts=texts)
            usage = await self._get_user_usage_stats(session)

        conversion_rate = (
            (stats['trial_to_paid_conversions'] / stats['new_trials'] * 100) if stats['new_trials'] > 0 else 0.0
        )

        lines: list[str] = []
        header = (
            texts.t('ADMIN_REPORT_UI_004', '📊 <b>Отчет за {value1}</b>').format(value1=period_range.label)
            if period == ReportPeriod.DAILY
            else texts.t('ADMIN_REPORT_UI_005', '📊 <b>Отчет за период {value1}</b>').format(value1=period_range.label)
        )
        lines += [header, '']

        # TL;DR
        lines += [
            texts.t('ADMIN_REPORT_UI_006', '🧭 <b>Итог по периоду</b>'),
            texts.t('ADMIN_REPORT_UI_007', '• Новых пользователей: <b>{value1}</b>').format(value1=stats['new_users']),
            texts.t('ADMIN_REPORT_UI_008', '• Новых триалов: <b>{value1}</b>').format(value1=stats['new_trials']),
            (
                texts.t(
                    'ADMIN_REPORT_UI_009', '• Конверсий триал → платная: <b>{value1}</b> (<i>{value2:.1f}%</i>)'
                ).format(value1=stats['trial_to_paid_conversions'], value2=conversion_rate)
            ),
            texts.t('ADMIN_REPORT_UI_010', '• Новых платных (всего): <b>{value1}</b>').format(
                value1=stats['new_paid_subscriptions']
            ),
            texts.t('ADMIN_REPORT_UI_011', '• Поступления всего (только пополнения): <b>{value1}</b>').format(
                value1=self._format_amount(stats['deposits_amount'])
            ),
            '',
        ]

        # Подписки
        lines += [
            texts.t('ADMIN_REPORT_UI_012', '💎 <b>Подписки</b>'),
            texts.t('ADMIN_REPORT_UI_013', '• Активные триалы сейчас: {value1}').format(value1=totals['active_trials']),
            texts.t('ADMIN_REPORT_UI_014', '• Активные платные сейчас: {value1}').format(value1=totals['active_paid']),
            '',
        ]

        # Финансы
        lines += [
            texts.t('ADMIN_REPORT_UI_015', '💰 <b>Финансы</b>'),
            (
                texts.t('ADMIN_REPORT_UI_016', '• Оплаты подписок: {value1} на сумму {value2}').format(
                    value1=stats['subscription_payments_count'],
                    value2=self._format_amount(stats['subscription_payments_amount']),
                )
            ),
            (
                texts.t('ADMIN_REPORT_UI_017', '• Пополнения: {value1} на сумму {value2}').format(
                    value1=stats['deposits_count'], value2=self._format_amount(stats['deposits_amount'])
                )
            ),
            (
                texts.t(
                    'ADMIN_REPORT_UI_018',
                    '<i>Примечание: «Поступления всего» учитывают только пополнения; покупки подписок и реферальные бонусы исключены.</i>',
                )
            ),
            '',
        ]

        # Поддержка
        lines += [
            texts.t('ADMIN_REPORT_UI_019', '🎟️ <b>Поддержка</b>'),
            texts.t('ADMIN_REPORT_UI_020', '• Новых тикетов: {value1}').format(value1=stats['new_tickets']),
            texts.t('ADMIN_REPORT_UI_021', '• Активных тикетов сейчас: {value1}').format(value1=totals['open_tickets']),
            '',
        ]

        # Активность пользователей
        lines += [
            texts.t('ADMIN_REPORT_UI_022', '👤 <b>Активность пользователей</b>'),
            texts.t('ADMIN_REPORT_UI_023', '• Пользователей с активной платной подпиской: {value1}').format(
                value1=usage['active_paid_users']
            ),
            texts.t('ADMIN_REPORT_UI_024', '• Пользователей, ни разу не подключившихся: {value1}').format(
                value1=usage['never_connected_users']
            ),
            '',
        ]

        # Топ по рефералам
        lines += [texts.t('ADMIN_REPORT_UI_025', '🤝 <b>Топ по рефералам (за период)</b>')]
        if top_referrers:
            for index, row in enumerate(top_referrers, 1):
                referrer_label = escape(row['referrer_label'], quote=False)
                lines.append(
                    texts.t('ADMIN_REPORT_UI_031', '{value1}. {value2}: {value3} приглашений').format(
                        value1=index, value2=referrer_label, value3=row['count']
                    )
                )
        else:
            lines.append(texts.t('ADMIN_REPORT_UI_028', '— данных нет'))

        return '\n'.join(lines)

    def _get_period_range(
        self,
        period: ReportPeriod,
        report_date: date | None,
    ) -> ReportPeriodRange:
        now_msk = datetime.now(self._moscow_tz)

        if period == ReportPeriod.DAILY:
            target_date = report_date or (now_msk.date() - timedelta(days=1))
            start = datetime.combine(target_date, datetime_time.min, tzinfo=self._moscow_tz)
            end = start + timedelta(days=1)
        elif period == ReportPeriod.WEEKLY:
            end_date = report_date or now_msk.date()
            start_date = end_date - timedelta(days=7)
            start = datetime.combine(start_date, datetime_time.min, tzinfo=self._moscow_tz)
            end = datetime.combine(end_date, datetime_time.min, tzinfo=self._moscow_tz)
        elif period == ReportPeriod.MONTHLY:
            end_date = report_date or now_msk.date()
            start_date = end_date - timedelta(days=30)
            start = datetime.combine(start_date, datetime_time.min, tzinfo=self._moscow_tz)
            end = datetime.combine(end_date, datetime_time.min, tzinfo=self._moscow_tz)
        else:  # pragma: no cover - defensive branch
            raise ReportingServiceError(f'Неизвестный период отчета: {period}')

        label = self._format_period_label(start, end)
        return ReportPeriodRange(start, end, label)

    async def _collect_current_totals(self, session) -> dict:
        stats = await get_subscriptions_statistics(session)
        open_tickets_result = await session.execute(
            select(func.count(Ticket.id)).where(
                Ticket.status.in_(
                    [
                        TicketStatus.OPEN.value,
                        TicketStatus.ANSWERED.value,
                        TicketStatus.PENDING.value,
                    ]
                )
            )
        )
        open_tickets = int(open_tickets_result.scalar() or 0)
        return {
            'active_trials': stats.get('trial_subscriptions', 0) or 0,
            'active_paid': stats.get('paid_subscriptions', 0) or 0,
            'open_tickets': open_tickets,
        }

    async def _collect_period_stats(
        self,
        session,
        start_utc: datetime,
        end_utc: datetime,
    ) -> dict:
        new_users = int(
            (
                await session.execute(
                    select(func.count(User.id)).where(
                        User.created_at >= start_utc,
                        User.created_at < end_utc,
                    )
                )
            ).scalar()
            or 0
        )

        new_trials = int(
            (
                await session.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.created_at >= start_utc,
                        Subscription.created_at < end_utc,
                        Subscription.is_trial == true(),
                    )
                )
            ).scalar()
            or 0
        )

        direct_paid = int(
            (
                await session.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.created_at >= start_utc,
                        Subscription.created_at < end_utc,
                        Subscription.is_trial == false(),
                    )
                )
            ).scalar()
            or 0
        )

        trial_to_paid_conversions = int(
            (
                await session.execute(
                    select(func.count(SubscriptionConversion.id)).where(
                        SubscriptionConversion.converted_at >= start_utc,
                        SubscriptionConversion.converted_at < end_utc,
                    )
                )
            ).scalar()
            or 0
        )

        subscription_payments_count, subscription_payments_amount = (
            await session.execute(
                self._txn_query_base(
                    TransactionType.SUBSCRIPTION_PAYMENT.value,
                    start_utc,
                    end_utc,
                )
            )
        ).one()

        deposits_count, deposits_amount = (
            await session.execute(self._deposit_query_excluding_referrals(start_utc, end_utc))
        ).one()

        new_tickets = int(
            (
                await session.execute(
                    select(func.count(Ticket.id)).where(
                        Ticket.created_at >= start_utc,
                        Ticket.created_at < end_utc,
                    )
                )
            ).scalar()
            or 0
        )

        return {
            'new_users': new_users,
            'new_trials': new_trials,
            'new_paid_subscriptions': direct_paid + trial_to_paid_conversions,
            'trial_to_paid_conversions': trial_to_paid_conversions,
            'subscription_payments_count': int(subscription_payments_count or 0),
            'subscription_payments_amount': int(subscription_payments_amount or 0),
            'deposits_count': int(deposits_count or 0),
            'deposits_amount': int(deposits_amount or 0),
            'new_tickets': new_tickets,
        }

    def _txn_query_base(self, txn_type: str, start_utc: datetime, end_utc: datetime):
        return select(
            func.count(Transaction.id),
            func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0),
        ).where(
            Transaction.type == txn_type,
            Transaction.is_completed == true(),
            Transaction.created_at >= start_utc,
            Transaction.created_at < end_utc,
        )

    def _deposit_query_excluding_referrals(self, start_utc: datetime, end_utc: datetime):
        """Запрос депозитов только по реальным платежам.

        Исключаются: колесо удачи, промокоды, админские пополнения, оплата с баланса.
        """
        return select(
            func.count(Transaction.id),
            func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0),
        ).where(
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.is_completed == true(),
            Transaction.created_at >= start_utc,
            Transaction.created_at < end_utc,
            self._exclude_referral_deposits_condition(),
            # Только реальные платежи (исключаем колесо, промокоды, админские, баланс)
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
        )

    async def _get_top_referrers(
        self,
        session,
        start_utc: datetime,
        end_utc: datetime,
        limit: int = 5,
        texts: Texts | None = None,
    ) -> list[dict]:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        rows = await session.execute(
            select(
                User.referred_by_id,
                func.count(User.id).label('cnt'),
            )
            .where(
                User.created_at >= start_utc,
                User.created_at < end_utc,
                User.referred_by_id.isnot(None),
            )
            .group_by(User.referred_by_id)
            .order_by(func.count(User.id).desc())
            .limit(limit)
        )
        rows = rows.all()
        if not rows:
            return []
        ref_ids = [row[0] for row in rows if row[0] is not None]
        users_map: dict[int, str] = {}
        if ref_ids:
            urows = await session.execute(select(User).where(User.id.in_(ref_ids)))
            for user in urows.scalars().all():
                users_map[user.id] = self._user_label(user, texts=texts)
        return [
            {
                'referrer_label': users_map.get(
                    ref_id, texts.t('ADMIN_REPORT_UI_003', 'User #{value1}').format(value1=ref_id)
                ),
                'count': int(count or 0),
            }
            for ref_id, count in rows
        ]

    async def _get_user_usage_stats(self, session) -> dict[str, int]:
        now_utc = datetime.now(UTC)

        active_paid_q = await session.execute(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.is_trial == false(),
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date > now_utc,
            )
        )
        active_paid_users = int(active_paid_q.scalar() or 0)

        never_connected_q = await session.execute(
            select(func.count(func.distinct(Subscription.user_id))).where(
                or_(
                    Subscription.connected_squads.is_(None),
                    cast(Subscription.connected_squads, JSONB) == cast('[]', JSONB),
                    func.jsonb_typeof(cast(Subscription.connected_squads, JSONB)) != 'array',
                )
            )
        )
        never_connected_users = int(never_connected_q.scalar() or 0)

        return {
            'active_paid_users': active_paid_users,
            'never_connected_users': never_connected_users,
        }

    def _user_label(self, user: User, texts: Texts | None = None) -> str:
        texts = texts or get_texts(settings.DEFAULT_LANGUAGE)
        if getattr(user, 'username', None):
            return f'@{user.username}'
        parts = []
        if getattr(user, 'first_name', None):
            parts.append(user.first_name)
        if getattr(user, 'last_name', None):
            parts.append(user.last_name)
        if parts:
            return ' '.join(parts)
        return texts.t('ADMIN_REPORT_UI_003', 'User #{value1}').format(value1=getattr(user, 'id', '?'))

    def _format_period_label(self, start: datetime, end: datetime) -> str:
        start_date = start.astimezone(self._moscow_tz).date()
        end_boundary = (end - timedelta(seconds=1)).astimezone(self._moscow_tz)
        end_date = end_boundary.date()

        if start_date == end_date:
            return start_date.strftime('%d.%m.%Y')

        return f'{start_date.strftime("%d.%m.%Y")} - {end_date.strftime("%d.%m.%Y")}'

    def _format_amount(self, amount_kopeks: int) -> str:
        rubles = (amount_kopeks or 0) / 100
        return f'{rubles:,.2f} ₽'.replace(',', ' ')


reporting_service = ReportingService()
