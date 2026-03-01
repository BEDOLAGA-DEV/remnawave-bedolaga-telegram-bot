import asyncio
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.contest import (
    create_round,
    get_active_round_by_template,
    list_templates,
    upsert_template,
)
from app.database.database import AsyncSessionLocal
from app.database.models import ContestTemplate, SubscriptionStatus, User
from app.services.contests.enums import GameType, PrizeType
from app.services.contests.games import get_game_strategy


logger = structlog.get_logger(__name__)

# Legacy aliases for backward compatibility
GAME_QUEST = GameType.QUEST_BUTTONS.value
GAME_LOCKS = GameType.LOCK_HACK.value
GAME_CIPHER = GameType.LETTER_CIPHER.value
GAME_SERVER = GameType.SERVER_LOTTERY.value
GAME_BLITZ = GameType.BLITZ_REACTION.value
GAME_EMOJI = GameType.EMOJI_GUESS.value
GAME_ANAGRAM = GameType.ANAGRAM.value


DEFAULT_TEMPLATES = [
    {
        'slug': GAME_QUEST,
        'name': 'Квест-кнопки',
        'description': 'Найди секретную кнопку 3×3',
        'prize_type': 'days',
        'prize_value': '1',
        'max_winners': 3,
        'attempts_per_user': 1,
        'times_per_day': 2,
        'schedule_times': '10:00,18:00',
        'payload': {'rows': 3, 'cols': 3},
        'is_enabled': False,
    },
    {
        'slug': GAME_LOCKS,
        'name': 'Кнопочный взлом',
        'description': 'Найди взломанную кнопку среди 20 замков',
        'prize_type': 'days',
        'prize_value': '5',
        'max_winners': 1,
        'attempts_per_user': 1,
        'times_per_day': 2,
        'schedule_times': '09:00,19:00',
        'payload': {'buttons': 20},
        'is_enabled': False,
    },
    {
        'slug': GAME_CIPHER,
        'name': 'Шифр букв',
        'description': 'Расшифруй слово по номерам',
        'prize_type': 'days',
        'prize_value': '1',
        'max_winners': 1,
        'attempts_per_user': 1,
        'times_per_day': 2,
        'schedule_times': '12:00,20:00',
        'payload': {'words': ['VPN', 'SERVER', 'PROXY', 'XRAY']},
        'is_enabled': False,
    },
    {
        'slug': GAME_SERVER,
        'name': 'Сервер-лотерея',
        'description': 'Угадай доступный сервер',
        'prize_type': 'days',
        'prize_value': '7',
        'max_winners': 1,
        'attempts_per_user': 1,
        'times_per_day': 1,
        'schedule_times': '15:00',
        'payload': {'flags': ['🇸🇪', '🇸🇬', '🇺🇸', '🇷🇺', '🇩🇪', '🇯🇵', '🇧🇷', '🇦🇺', '🇨🇦', '🇫🇷']},
        'is_enabled': False,
    },
    {
        'slug': GAME_BLITZ,
        'name': 'Блиц-реакция',
        'description': 'Нажми кнопку за 10 секунд',
        'prize_type': 'days',
        'prize_value': '1',
        'max_winners': 1,
        'attempts_per_user': 1,
        'times_per_day': 2,
        'schedule_times': '11:00,21:00',
        'payload': {'timeout_seconds': 10},
        'is_enabled': False,
    },
    {
        'slug': GAME_EMOJI,
        'name': 'Угадай сервис по эмодзи',
        'description': 'Определи сервис по эмодзи',
        'prize_type': 'days',
        'prize_value': '1',
        'max_winners': 1,
        'attempts_per_user': 1,
        'times_per_day': 1,
        'schedule_times': '13:00',
        'payload': {'pairs': [{'question': '🔐📡🌐', 'answer': 'VPN'}]},
        'is_enabled': False,
    },
    {
        'slug': GAME_ANAGRAM,
        'name': 'Анаграмма дня',
        'description': 'Собери слово из букв',
        'prize_type': 'days',
        'prize_value': '1',
        'max_winners': 1,
        'attempts_per_user': 1,
        'times_per_day': 1,
        'schedule_times': '17:00',
        'payload': {'words': ['SERVER', 'XRAY', 'VPN']},
        'is_enabled': False,
    },
]


class ContestRotationService:
    def __init__(self) -> None:
        self.bot: Bot | None = None
        self._task: asyncio.Task | None = None
        self._interval_seconds = 60

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_bot(self, bot: Bot) -> None:
        self.bot = bot

    async def start(self) -> None:
        await self.stop()

        if not settings.is_contests_enabled():
            logger.info('Сервис игр отключён настройками')
            return

        await self._ensure_default_templates()

        self._task = asyncio.create_task(self._loop())
        logger.info('🎲 Сервис ротационных конкурсов запущен')

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _ensure_default_templates(self) -> None:
        async with AsyncSessionLocal() as db:
            for tpl in DEFAULT_TEMPLATES:
                try:
                    await upsert_template(db, **tpl)
                except Exception as exc:
                    logger.error('Не удалось создать шаблон', tpl=tpl['slug'], exc=exc)

    async def _loop(self) -> None:
        try:
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error('Ошибка в ротации конкурсов', exc=exc)
                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            logger.info('Сервис ротации конкурсов остановлен')
            raise

    def _parse_times(self, times_str: str | None) -> list[time]:
        if not times_str:
            return []
        times: list[time] = []
        for part in times_str.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                hh, mm = part.split(':')
                times.append(time(int(hh), int(mm)))
            except Exception:
                continue
        return times

    async def _tick(self) -> None:
        async with AsyncSessionLocal() as db:
            templates = await list_templates(db)
            # Get current time in configured timezone
            tz = self._get_timezone()
            now_utc = datetime.now(UTC)
            now_local = now_utc.astimezone(tz)

            for tpl in templates:
                times = self._parse_times(tpl.schedule_times) or []
                for slot in times[: tpl.times_per_day]:
                    # Apply schedule time to local date
                    starts_at_local = now_local.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
                    if starts_at_local > now_local:
                        starts_at_local -= timedelta(days=1)
                    ends_at_local = starts_at_local + timedelta(hours=tpl.cooldown_hours)
                    if not (starts_at_local <= now_local <= ends_at_local):
                        continue

                    exists = await get_active_round_by_template(db, tpl.id)
                    if exists:
                        continue

                    # Convert to UTC for storage
                    starts_at_utc = starts_at_local.astimezone(UTC)
                    ends_at_utc = ends_at_local.astimezone(UTC)

                    # Анонс перед созданием раунда
                    await self._announce_round_start(tpl, starts_at_local, ends_at_local)
                    payload = self._build_payload_for_template(tpl)
                    round_obj = await create_round(
                        db,
                        template=tpl,
                        starts_at=starts_at_utc,
                        ends_at=ends_at_utc,
                        payload=payload,
                    )
                    logger.info('Создан раунд для шаблона', round_obj_id=round_obj.id, slug=tpl.slug)

    def _get_timezone(self) -> ZoneInfo:
        tz_name = settings.TIMEZONE or 'UTC'
        try:
            return ZoneInfo(tz_name)
        except Exception:
            logger.warning('Не удалось загрузить TZ , используем UTC', tz_name=tz_name)
            return ZoneInfo('UTC')

    def _build_payload_for_template(self, tpl: ContestTemplate) -> dict:
        """Build round-specific payload using game strategy."""
        strategy = get_game_strategy(tpl.slug)
        if strategy:
            return strategy.build_payload(tpl.payload or {})
        # Fallback for unknown game types
        return tpl.payload or {}

    async def _announce_round_start(
        self,
        tpl: ContestTemplate,
        starts_at_local: datetime,
        ends_at_local: datetime,
    ) -> None:
        if not self.bot:
            return

        from app.localization.texts import get_texts

        texts = get_texts('ru')  # Default to ru for announcements

        # Format prize display based on prize_type
        prize_type = tpl.prize_type or PrizeType.DAYS.value
        prize_value = tpl.prize_value or '1'

        if prize_type == PrizeType.DAYS.value:
            prize_display = f'{prize_value} {texts.t("DAYS", "дн. подписки")}'
        elif prize_type == PrizeType.BALANCE.value:
            prize_display = f'{prize_value} коп.'
        elif prize_type == PrizeType.CUSTOM.value:
            prize_display = prize_value
        else:
            prize_display = prize_value

        text = (
            f'🎲 {texts.t("CONTEST_START_ANNOUNCEMENT", "Стартует игра")}: <b>{tpl.name}</b>\n'
            f'{texts.t("CONTEST_PRIZE", "Приз")}: {prize_display} • {texts.t("CONTEST_WINNERS", "Победителей")}: {tpl.max_winners}\n'
            f'{texts.t("CONTEST_ATTEMPTS", "Попыток/польз")}: {tpl.attempts_per_user}\n\n'
            f'{texts.t("CONTEST_ELIGIBILITY", "Участвовать могут только с активной или триальной подпиской")}.\n'
            f'💡 <b>{texts.t("REMINDER", "Напоминание")}:</b> {texts.t("CONTEST_REMINDER_TEXT", "Не забудьте участвовать в конкурсах для получения бонусов")}!'
        )

        await asyncio.gather(
            self._send_channel_announce(text),
            self._broadcast_to_users(text),
            return_exceptions=True,
        )

    async def _send_channel_announce(self, text: str) -> None:
        if not self.bot:
            return
        from app.services.channel_subscription_service import channel_subscription_service

        channel_id = await channel_subscription_service.get_first_channel_id()
        if not channel_id:
            return

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='🎲 Играть', callback_data='contests_menu')]]
        )

        try:
            await self.bot.send_message(
                chat_id=channel_id,
                text=text,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.error('Не удалось отправить анонс в канал', channel_id=channel_id, exc=exc)

    async def _broadcast_to_users(self, text: str) -> None:
        """Отправляет анонс всем пользователям с активной/триальной подпиской."""
        if not self.bot:
            return

        try:
            batch_size = 500
            offset = 0
            sent = failed = 0

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='🎲 Играть', callback_data='contests_menu')]]
            )

            while True:
                async with AsyncSessionLocal() as db:
                    users_batch = await self._load_users_batch(db, offset, batch_size)
                if not users_batch:
                    break
                offset += batch_size

                tasks = []
                semaphore = asyncio.Semaphore(15)

                async def _send(u: User):
                    nonlocal sent, failed
                    # Skip email-only users (no telegram_id)
                    if not u.telegram_id:
                        return
                    async with semaphore:
                        try:
                            await self.bot.send_message(
                                chat_id=u.telegram_id,
                                text=text,
                                disable_web_page_preview=True,
                                reply_markup=keyboard,
                            )
                            sent += 1
                        except Exception:
                            failed += 1
                        await asyncio.sleep(0.02)

                for user in users_batch:
                    tasks.append(asyncio.create_task(_send(user)))

                await asyncio.gather(*tasks, return_exceptions=True)

            logger.info('Анонс игр: отправлено ошибок', sent=sent, failed=failed)
        except Exception as exc:
            logger.error('Ошибка рассылки анонса игр пользователям', exc=exc)

    async def _load_users_batch(self, db: AsyncSession, offset: int, limit: int) -> list[User]:
        from app.database.crud.user import get_users_list

        users = await get_users_list(
            db,
            offset=offset,
            limit=limit,
            status=None,
        )
        allowed: list[User] = []
        for u in users:
            sub = getattr(u, 'subscription', None)
            if not sub:
                continue
            if sub.status in {SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value}:
                allowed.append(u)
        return allowed


contest_rotation_service = ContestRotationService()
