import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral_contest import (
    add_contest_event,
    get_contest_events_count,
    get_contest_leaderboard_with_virtual,
    get_contests_for_events,
    get_contests_for_summaries,
    get_referrer_score,
    list_virtual_participants,
    mark_daily_summary_sent,
    mark_final_summary_sent,
)
from app.database.crud.user import get_user_by_id
from app.database.database import AsyncSessionLocal
from app.database.models import ReferralContest, User


logger = structlog.get_logger(__name__)


class ReferralContestService:
    def __init__(self) -> None:
        self.bot: Bot | None = None
        self._task: asyncio.Task | None = None
        self._poll_interval_seconds = 60

    def set_bot(self, bot: Bot) -> None:
        self.bot = bot

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        await self.stop()

        if not settings.is_contests_enabled():
            logger.info('Сервис конкурсов отключен настройками')
            return

        if not self.bot:
            logger.warning('Невозможно запустить сервис конкурсов без экземпляра бота')
            return

        self._task = asyncio.create_task(self._run_loop())
        logger.info('🏆 Сервис конкурсов запущен')

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run_loop(self) -> None:
        try:
            while True:
                try:
                    await self._process_summaries()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error('Ошибка сервиса конкурсов', exc=exc)

                await asyncio.sleep(self._poll_interval_seconds)
        except asyncio.CancelledError:
            logger.info('Сервис конкурсов остановлен')
            raise

    async def _process_summaries(self) -> None:
        if not self.bot:
            return

        async with AsyncSessionLocal() as db:
            contests = await get_contests_for_summaries(db)
            now_utc = datetime.now(UTC)

            for contest in contests:
                try:
                    await self._maybe_send_daily_summary(db, contest, now_utc)
                    await self._maybe_send_final_summary(db, contest, now_utc)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error('Ошибка обработки конкурса', contest_id=contest.id, title=contest.title, exc=exc)

    async def _maybe_send_daily_summary(
        self,
        db: AsyncSession,
        contest: ReferralContest,
        now_utc: datetime,
    ) -> None:
        tz = self._get_timezone(contest)
        now_local = now_utc.replace(tzinfo=UTC).astimezone(tz)
        start_local = self._to_local(contest.start_at, tz)
        end_local = self._to_local(contest.end_at, tz)

        if now_local.date() < start_local.date() or now_local.date() > end_local.date():
            return

        summary_times = self._get_summary_times(contest)
        for summary_time in summary_times:
            summary_dt = datetime.combine(now_local.date(), summary_time, tzinfo=tz)
            summary_dt_utc = summary_dt.astimezone(UTC)

            if now_utc < summary_dt_utc:
                continue
            last_sent = contest.last_daily_summary_at
            if last_sent and last_sent >= summary_dt_utc:
                continue

            await self._send_summary(
                db,
                contest,
                now_utc,
                now_local.date(),
                is_final=False,
                summary_dt_utc=summary_dt_utc,
            )

    async def _maybe_send_final_summary(
        self,
        db: AsyncSession,
        contest: ReferralContest,
        now_utc: datetime,
    ) -> None:
        if contest.final_summary_sent:
            return

        tz = self._get_timezone(contest)
        end_local = self._to_local(contest.end_at, tz)
        summary_times = self._get_summary_times(contest)
        summary_time = summary_times[-1] if summary_times else time(hour=12, minute=0)
        summary_dt = datetime.combine(end_local.date(), summary_time, tzinfo=tz)
        summary_dt_utc = summary_dt.astimezone(UTC)

        if now_utc < contest.end_at:
            return

        if now_utc < summary_dt_utc:
            return

        await self._send_summary(db, contest, now_utc, end_local.date(), is_final=True)

    async def _send_summary(
        self,
        db: AsyncSession,
        contest: ReferralContest,
        now_utc: datetime,
        target_date: date,
        *,
        is_final: bool,
        summary_dt_utc: datetime | None = None,
    ) -> None:
        tz = self._get_timezone(contest)
        day_start_local = datetime.combine(target_date, time.min, tzinfo=tz)
        day_end_local = day_start_local + timedelta(days=1)
        day_start_utc = day_start_local.astimezone(UTC)
        day_end_utc = day_end_local.astimezone(UTC)

        leaderboard = await get_contest_leaderboard_with_virtual(db, contest.id)
        virtual_participants = await list_virtual_participants(db, contest.id)
        virtual_count = sum(vp.referral_count for vp in virtual_participants)
        total_events = await get_contest_events_count(db, contest.id) + virtual_count
        today_events = await get_contest_events_count(
            db,
            contest.id,
            start=day_start_utc,
            end=day_end_utc,
        )

        await self._notify_admins(
            contest=contest,
            leaderboard=leaderboard,
            total_events=total_events,
            today_events=today_events,
            is_final=is_final,
            tz=tz,
        )

        await self._notify_public_channel(
            contest=contest,
            leaderboard=leaderboard,
            total_events=total_events,
            today_events=today_events,
            is_final=is_final,
            tz=tz,
        )

        if not leaderboard:
            logger.info('Конкурс : пока нет участников', contest_id=contest.id)

        if is_final:
            await mark_final_summary_sent(db, contest)
        else:
            await mark_daily_summary_sent(db, contest, target_date, summary_dt_utc)

    async def _notify_participants(
        self,
        db: AsyncSession,
        *,
        contest: ReferralContest,
        leaderboard: Sequence[tuple[User, int, int]],
        total_events: int,
        today_events: int,
        day_start_utc: datetime,
        day_end_utc: datetime,
        is_final: bool,
    ) -> None:
        if not self.bot:
            return

        # leaderboard already sorted by helper
        score_map = {user.id: (idx + 1, score) for idx, (user, score, _) in enumerate(leaderboard)}

        for user, score, _ in leaderboard:
            rank = score_map.get(user.id, (None, score))[0]
            today_score = (
                await get_referrer_score(
                    db=db,
                    contest_id=contest.id,
                    referrer_id=user.id,
                    start=day_start_utc,
                    end=day_end_utc,
                )
                if score
                else 0
            )

            text = self._build_participant_message(
                contest=contest,
                rank=rank or 0,
                score=score,
                today_score=today_score,
                total_events=total_events,
                today_events=today_events,
                is_final=is_final,
            )

            # Skip email-only users (no telegram_id)
            if not user.telegram_id:
                logger.debug('Skipping contest notification for email-only user', user_id=user.id)
                continue

            try:
                await self.bot.send_message(user.telegram_id, text, disable_web_page_preview=True)
            except (TelegramForbiddenError, TelegramNotFound):
                logger.info(
                    'Не удалось отправить сообщение участнику (вероятно, блокировка)', telegram_id=user.telegram_id
                )
            except Exception as exc:
                logger.error('Ошибка отправки участнику конкурса', telegram_id=user.telegram_id, exc=exc)

    async def _notify_admins(
        self,
        *,
        contest: ReferralContest,
        leaderboard: Sequence[tuple[str, int, int, bool]],
        total_events: int,
        today_events: int,
        is_final: bool,
        tz: ZoneInfo,
    ) -> None:
        if not self.bot:
            return

        chat_id = settings.ADMIN_NOTIFICATIONS_CHAT_ID
        if not chat_id:
            return

        lines = [
            '🏆 <b>Конкурс рефералов</b>',
            f'Название: <b>{contest.title}</b>',
            f'Статус: {"финал" if is_final else "дневная сводка"}',
            f'Временная зона: <code>{tz.key}</code>',
            f'Всего рефералов: <b>{total_events}</b>',
            '',
            'Топ участников:',
        ]

        if leaderboard:
            for idx, (name, score, _, is_virtual) in enumerate(leaderboard[:5], start=1):
                virt_mark = ' 👻' if is_virtual else ''
                lines.append(f'{idx}. {name}{virt_mark} — {score}')
        else:
            lines.append('Пока нет участников.')

        if contest.prize_text:
            lines.append('')
            lines.append(f'Приз: {contest.prize_text}')

        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text='\n'.join(lines),
                disable_web_page_preview=True,
                message_thread_id=settings.ADMIN_NOTIFICATIONS_TOPIC_ID,
            )
        except Exception as exc:
            logger.error('Не удалось отправить админскую сводку конкурса', exc=exc)

    async def _notify_public_channel(
        self,
        *,
        contest: ReferralContest,
        leaderboard: Sequence[tuple[str, int, int, bool]],
        total_events: int,
        today_events: int,
        is_final: bool,
        tz: ZoneInfo,
    ) -> None:
        if not self.bot:
            return

        from app.services.channel_subscription_service import channel_subscription_service

        channel_id = await channel_subscription_service.get_first_channel_id()
        if not channel_id:
            return

        lines = [
            f'🏆 {contest.title}',
            '🏁 Итоги конкурса' if is_final else '📊 Промежуточные итоги',
            f'Время зоны: {tz.key}',
            f'Всего участников: <b>{len(leaderboard)}</b>',
            '',
            'Топ участников:',
        ]

        if leaderboard:
            for idx, (name, score, _, _is_virtual) in enumerate(leaderboard[:5], start=1):
                lines.append(f'{idx}. {name} — {score}')
        else:
            lines.append('Пока нет участников.')

        if contest.prize_text:
            lines.append('')
            lines.append(f'Приз: {contest.prize_text}')

        try:
            await self.bot.send_message(
                chat_id=channel_id,
                text='\n'.join(lines),
                disable_web_page_preview=True,
            )
        except (TelegramForbiddenError, TelegramNotFound):
            logger.info('Не удалось отправить сводку конкурса в канал', channel_id=channel_id)
        except Exception as exc:
            logger.error('Ошибка отправки сводки конкурса в канал', channel_id=channel_id, exc=exc)

    def _build_participant_message(
        self,
        *,
        contest: ReferralContest,
        rank: int,
        score: int,
        today_score: int,
        total_events: int,
        today_events: int,
        is_final: bool,
    ) -> str:
        status_line = '🏁 Итоги конкурса' if is_final else '📊 Промежуточные итоги'
        lines = [
            f'🏆 {contest.title}',
            status_line,
            '',
            f'Ваше место: <b>{rank}</b>',
            f'Зачётов за всё время: <b>{score}</b>',
            f'За сегодня: <b>{today_score}</b>',
            f'Общий пул зачётов: <b>{total_events}</b> (сегодня {today_events})',
        ]

        if contest.prize_text:
            lines.append('')
            lines.append(f'Призовой фонд: {contest.prize_text}')

        if not is_final:
            remaining = contest.end_at - datetime.now(UTC)
            if remaining.total_seconds() > 0:
                hours_left = int(remaining.total_seconds() // 3600)
                lines.append('')
                lines.append(f'До окончания: ~{hours_left} ч.')

        return '\n'.join(lines)

    async def get_detailed_contest_stats(self, db: AsyncSession, contest_id: int) -> dict:
        from app.database.crud.referral_contest import (
            get_contest_leaderboard,
            get_contest_payment_stats,
            get_contest_transaction_breakdown,
            get_referral_contest,
        )

        contest = await get_referral_contest(db, contest_id)
        if not contest:
            return {
                'total_participants': 0,
                'total_invited': 0,
                'total_paid_amount': 0,
                'total_unpaid': 0,
                'paid_count': 0,
                'unpaid_count': 0,
                'subscription_total': 0,
                'deposit_total': 0,
                'participants': [],
            }

        # Get leaderboard - already includes User objects
        leaderboard = await get_contest_leaderboard(db, contest_id)

        # Получаем статистику оплат
        payment_stats = await get_contest_payment_stats(db, contest_id)

        # Получаем разбивку по типам транзакций
        breakdown = await get_contest_transaction_breakdown(db, contest_id)

        if not leaderboard:
            return {
                'total_participants': 0,
                'total_invited': 0,
                'total_paid_amount': payment_stats['total_amount'],
                'total_unpaid': payment_stats['unpaid_count'],
                'paid_count': payment_stats['paid_count'],
                'unpaid_count': payment_stats['unpaid_count'],
                'subscription_total': breakdown['subscription_total'],
                'deposit_total': breakdown['deposit_total'],
                'participants': [],
            }

        total_participants = len(leaderboard)
        total_invited = sum(score for _, score, _ in leaderboard)
        total_paid_amount = payment_stats['total_amount']
        total_unpaid = payment_stats['unpaid_count']

        # Build participants stats directly from leaderboard (already has User objects)
        participants_stats = []
        for user, score, amount in leaderboard:
            participants_stats.append(
                {
                    'referrer_id': user.id,
                    'full_name': user.full_name,
                    'total_referrals': score,
                    'paid_referrals': score if amount > 0 else 0,
                    'unpaid_referrals': 0 if amount > 0 else score,
                    'total_paid_amount': amount,
                }
            )

        return {
            'total_participants': total_participants,
            'total_invited': total_invited,
            'total_paid_amount': total_paid_amount,
            'total_unpaid': total_unpaid,
            'paid_count': payment_stats['paid_count'],
            'unpaid_count': payment_stats['unpaid_count'],
            'subscription_total': breakdown['subscription_total'],
            'deposit_total': breakdown['deposit_total'],
            'participants': participants_stats,
        }

    def _get_timezone(self, contest: ReferralContest) -> ZoneInfo:
        tz_name = contest.timezone or settings.TIMEZONE
        try:
            return ZoneInfo(tz_name)
        except Exception:
            logger.warning('Не удалось загрузить TZ , используем UTC', tz_name=tz_name)
            return ZoneInfo('UTC')

    def _parse_times(self, times_str: str | None) -> list[time]:
        if not times_str:
            return []
        parsed: list[time] = []
        for part in times_str.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                parsed.append(datetime.strptime(part, '%H:%M').time())
            except Exception:
                continue
        return parsed

    def _get_summary_times(self, contest: ReferralContest) -> list[time]:
        times = self._parse_times(contest.daily_summary_times)
        if not times and contest.daily_summary_time:
            times.append(contest.daily_summary_time)
        if not times:
            times.append(time(hour=12, minute=0))
        return sorted(times)

    def _to_local(self, dt_value: datetime, tz: ZoneInfo) -> datetime:
        base = dt_value
        if dt_value.tzinfo is None:
            base = dt_value.replace(tzinfo=UTC)
        return base.astimezone(tz)

    async def on_subscription_payment(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int = 0,
    ) -> None:
        if not settings.is_contests_enabled():
            return

        user = await get_user_by_id(db, user_id)
        if not user or not user.referred_by_id:
            return

        now_utc = datetime.now(UTC)
        contests = await get_contests_for_events(
            db,
            now_utc,
            contest_types=['referral_paid'],
        )
        if not contests:
            return

        for contest in contests:
            try:
                # Проверяем что реферал зарегистрировался В ПЕРИОД конкурса
                user_created_at = user.created_at
                contest_start = contest.start_at
                contest_end = contest.end_at

                if user_created_at < contest_start or user_created_at > contest_end:
                    logger.debug(
                        'Реферал зарегистрирован вне периода конкурса (создан , период -)',
                        user_id=user.id,
                        contest_id=contest.id,
                        user_created_at=user_created_at,
                        contest_start=contest_start,
                        contest_end=contest_end,
                    )
                    continue

                event = await add_contest_event(
                    db,
                    contest_id=contest.id,
                    referrer_id=user.referred_by_id,
                    referral_id=user.id,
                    amount_kopeks=amount_kopeks,
                    event_type='subscription_purchase',
                )
                if event:
                    logger.info(
                        'Записан зачёт конкурса : реферер , реферал',
                        contest_id=contest.id,
                        referred_by_id=user.referred_by_id,
                        user_id=user.id,
                    )
            except Exception as exc:
                logger.error('Не удалось записать зачёт конкурса', contest_id=contest.id, exc=exc)

    async def on_referral_registration(
        self,
        db: AsyncSession,
        user_id: int,
    ) -> None:
        if not settings.is_contests_enabled():
            return

        user = await get_user_by_id(db, user_id)
        if not user or not user.referred_by_id:
            return

        now_utc = datetime.now(UTC)
        contests = await get_contests_for_events(
            db,
            now_utc,
            contest_types=['referral_registered'],
        )
        if not contests:
            return

        for contest in contests:
            try:
                # Проверяем что реферал зарегистрировался В ПЕРИОД конкурса
                user_created_at = user.created_at
                contest_start = contest.start_at
                contest_end = contest.end_at

                if user_created_at < contest_start or user_created_at > contest_end:
                    logger.debug(
                        'Реферал зарегистрирован вне периода конкурса (создан , период -)',
                        user_id=user.id,
                        contest_id=contest.id,
                        user_created_at=user_created_at,
                        contest_start=contest_start,
                        contest_end=contest_end,
                    )
                    continue

                event = await add_contest_event(
                    db,
                    contest_id=contest.id,
                    referrer_id=user.referred_by_id,
                    referral_id=user.id,
                    amount_kopeks=0,
                    event_type='referral_registration',
                )
                if event:
                    logger.info(
                        'Записан зачёт конкурса регистрации : реферер , реферал',
                        contest_id=contest.id,
                        referred_by_id=user.referred_by_id,
                        user_id=user.id,
                    )
            except Exception as exc:
                logger.error('Не удалось записать зачёт регистрации для конкурса', contest_id=contest.id, exc=exc)

    async def sync_contest(
        self,
        db: AsyncSession,
        contest_id: int,
    ) -> dict:
        """Синхронизировать события конкурса с реальными данными.

        Проверяет всех рефералов и их платежи за период конкурса.
        Учитывает ВСЕ платёжные системы (Stars, YooKassa, Platega, CryptoBot и др.).
        """
        from app.database.crud.referral_contest import sync_contest_events

        try:
            stats = await sync_contest_events(db, contest_id)
            if 'error' not in stats:
                logger.info(
                    'Синхронизация конкурса : создано , обновлено , пропущено',
                    contest_id=contest_id,
                    stats=stats.get('created', 0),
                    stats_2=stats.get('updated', 0),
                    stats_3=stats.get('skipped', 0),
                )
            return stats
        except Exception as exc:
            logger.error('Ошибка синхронизации конкурса', contest_id=contest_id, exc=exc)
            return {'error': str(exc)}

    async def cleanup_contest(
        self,
        db: AsyncSession,
        contest_id: int,
    ) -> dict:
        """Очистить неправильные события конкурса.

        Удаляет события для рефералов, зарегистрированных ВНЕ периода конкурса.
        Используется для исправления данных после бага.
        """
        from app.database.crud.referral_contest import cleanup_invalid_contest_events

        try:
            stats = await cleanup_invalid_contest_events(db, contest_id)
            if 'error' not in stats:
                logger.info(
                    'Очистка конкурса : удалено невалидных событий, осталось',
                    contest_id=contest_id,
                    stats=stats.get('deleted', 0),
                    stats_2=stats.get('remaining', 0),
                )
            return stats
        except Exception as exc:
            logger.error('Ошибка очистки конкурса', contest_id=contest_id, exc=exc)
            return {'error': str(exc)}


referral_contest_service = ReferralContestService()
