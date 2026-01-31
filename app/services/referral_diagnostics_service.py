"""
Сервис диагностики реферальной системы по логам.

Анализирует логи бота для выявления проблем с реферальной системой:
- Переходы по реф-ссылкам
- Сверка с БД — засчитался ли реферал
- Выявление потерянных рефералов
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import create_referral_earning
from app.database.crud.user import add_user_balance
from app.database.models import ReferralEarning, User


logger = logging.getLogger(__name__)


@dataclass
class ReferralClick:
    """Информация о переходе по реф-ссылке."""

    timestamp: datetime
    telegram_id: int
    raw_code: str  # Код как в логе (может быть ref_refXXX)
    clean_code: str  # Очищенный код (refXXX)
    log_line: str


@dataclass
class LostReferral:
    """Потерянный реферал — пришёл по ссылке, но реферер не засчитался."""

    telegram_id: int
    username: Optional[str]
    full_name: Optional[str]
    referral_code: str  # По какому коду пришёл
    expected_referrer_code: str  # Код реферера
    expected_referrer_id: Optional[int]  # ID реферера в БД
    expected_referrer_name: Optional[str]  # Имя реферера
    click_time: datetime
    registered: bool  # Есть в БД?
    has_referrer: bool  # Есть referred_by_id?
    current_referrer_id: Optional[int]  # Текущий referred_by_id

    def to_dict(self) -> dict:
        """Сериализация в dict для хранения в Redis."""
        return {
            'telegram_id': self.telegram_id,
            'username': self.username,
            'full_name': self.full_name,
            'referral_code': self.referral_code,
            'expected_referrer_code': self.expected_referrer_code,
            'expected_referrer_id': self.expected_referrer_id,
            'expected_referrer_name': self.expected_referrer_name,
            'click_time': self.click_time.isoformat() if self.click_time else None,
            'registered': self.registered,
            'has_referrer': self.has_referrer,
            'current_referrer_id': self.current_referrer_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'LostReferral':
        """Десериализация из dict."""
        click_time = data.get('click_time')
        if click_time and isinstance(click_time, str):
            click_time = datetime.fromisoformat(click_time)
        return cls(
            telegram_id=data['telegram_id'],
            username=data.get('username'),
            full_name=data.get('full_name'),
            referral_code=data['referral_code'],
            expected_referrer_code=data['expected_referrer_code'],
            expected_referrer_id=data.get('expected_referrer_id'),
            expected_referrer_name=data.get('expected_referrer_name'),
            click_time=click_time,
            registered=data.get('registered', False),
            has_referrer=data.get('has_referrer', False),
            current_referrer_id=data.get('current_referrer_id'),
        )


@dataclass
class DiagnosticReport:
    """Отчёт о диагностике реферальной системы."""

    # Статистика
    total_ref_clicks: int = 0  # Всего переходов по реф-ссылкам
    unique_users_clicked: int = 0  # Уникальных пользователей

    # Проблемные случаи
    lost_referrals: list[LostReferral] = field(default_factory=list)

    # Период анализа
    analysis_period_start: Optional[datetime] = None
    analysis_period_end: Optional[datetime] = None

    # Статистика парсинга
    total_lines_parsed: int = 0
    lines_in_period: int = 0

    def to_dict(self) -> dict:
        """Сериализация в dict для хранения в Redis."""
        return {
            'total_ref_clicks': self.total_ref_clicks,
            'unique_users_clicked': self.unique_users_clicked,
            'lost_referrals': [lr.to_dict() for lr in self.lost_referrals],
            'analysis_period_start': self.analysis_period_start.isoformat() if self.analysis_period_start else None,
            'analysis_period_end': self.analysis_period_end.isoformat() if self.analysis_period_end else None,
            'total_lines_parsed': self.total_lines_parsed,
            'lines_in_period': self.lines_in_period,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'DiagnosticReport':
        """Десериализация из dict."""
        start = data.get('analysis_period_start')
        end = data.get('analysis_period_end')
        if start and isinstance(start, str):
            start = datetime.fromisoformat(start)
        if end and isinstance(end, str):
            end = datetime.fromisoformat(end)

        lost_referrals = [
            LostReferral.from_dict(lr) for lr in data.get('lost_referrals', [])
        ]

        return cls(
            total_ref_clicks=data.get('total_ref_clicks', 0),
            unique_users_clicked=data.get('unique_users_clicked', 0),
            lost_referrals=lost_referrals,
            analysis_period_start=start,
            analysis_period_end=end,
            total_lines_parsed=data.get('total_lines_parsed', 0),
            lines_in_period=data.get('lines_in_period', 0),
        )


@dataclass
class FixDetail:
    """Детали исправления одного потерянного реферала."""

    telegram_id: int
    username: Optional[str]
    full_name: Optional[str]

    # Что сделано
    referred_by_set: bool  # Установлен referred_by_id
    referrer_id: Optional[int]  # ID реферера
    referrer_name: Optional[str]  # Имя реферера

    # Бонусы
    bonus_to_referral_kopeks: int = 0  # Бонус рефералу
    bonus_to_referrer_kopeks: int = 0  # Бонус рефереру

    # Статус
    had_first_topup: bool = False  # Было первое пополнение
    topup_amount_kopeks: int = 0  # Сумма пополнения

    # Ошибки
    error: Optional[str] = None


@dataclass
class FixReport:
    """Отчёт об исправлении потерянных рефералов."""

    users_fixed: int = 0  # Исправлено referred_by_id
    bonuses_to_referrals: int = 0  # Бонусов рефералам (копейки)
    bonuses_to_referrers: int = 0  # Бонусов рефереам (копейки)
    details: list[FixDetail] = field(default_factory=list)
    errors: int = 0  # Количество ошибок


@dataclass
class MissingBonus:
    """Информация о ненначисленном бонусе."""

    # Реферал (приглашённый)
    referral_id: int
    referral_telegram_id: int
    referral_username: Optional[str]
    referral_full_name: Optional[str]

    # Реферер (пригласивший)
    referrer_id: int
    referrer_telegram_id: int
    referrer_username: Optional[str]
    referrer_full_name: Optional[str]

    # Первое пополнение
    first_topup_amount_kopeks: int
    first_topup_date: Optional[datetime]

    # Какие бонусы не начислены
    missing_referral_bonus: bool = False  # Бонус рефералу
    missing_referrer_bonus: bool = False  # Бонус рефереру

    # Суммы для начисления
    referral_bonus_amount: int = 0
    referrer_bonus_amount: int = 0

    def to_dict(self) -> dict:
        """Сериализация для Redis."""
        return {
            'referral_id': self.referral_id,
            'referral_telegram_id': self.referral_telegram_id,
            'referral_username': self.referral_username,
            'referral_full_name': self.referral_full_name,
            'referrer_id': self.referrer_id,
            'referrer_telegram_id': self.referrer_telegram_id,
            'referrer_username': self.referrer_username,
            'referrer_full_name': self.referrer_full_name,
            'first_topup_amount_kopeks': self.first_topup_amount_kopeks,
            'first_topup_date': self.first_topup_date.isoformat() if self.first_topup_date else None,
            'missing_referral_bonus': self.missing_referral_bonus,
            'missing_referrer_bonus': self.missing_referrer_bonus,
            'referral_bonus_amount': self.referral_bonus_amount,
            'referrer_bonus_amount': self.referrer_bonus_amount,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'MissingBonus':
        """Десериализация из dict."""
        topup_date = data.get('first_topup_date')
        if topup_date and isinstance(topup_date, str):
            topup_date = datetime.fromisoformat(topup_date)
        return cls(
            referral_id=data['referral_id'],
            referral_telegram_id=data['referral_telegram_id'],
            referral_username=data.get('referral_username'),
            referral_full_name=data.get('referral_full_name'),
            referrer_id=data['referrer_id'],
            referrer_telegram_id=data['referrer_telegram_id'],
            referrer_username=data.get('referrer_username'),
            referrer_full_name=data.get('referrer_full_name'),
            first_topup_amount_kopeks=data.get('first_topup_amount_kopeks', 0),
            first_topup_date=topup_date,
            missing_referral_bonus=data.get('missing_referral_bonus', False),
            missing_referrer_bonus=data.get('missing_referrer_bonus', False),
            referral_bonus_amount=data.get('referral_bonus_amount', 0),
            referrer_bonus_amount=data.get('referrer_bonus_amount', 0),
        )


@dataclass
class MissingBonusReport:
    """Отчёт о ненначисленных бонусах."""

    total_referrals_checked: int = 0  # Всего проверено рефералов
    referrals_with_topup: int = 0  # Рефералов с первым пополнением
    missing_bonuses: list[MissingBonus] = field(default_factory=list)

    # Суммы
    total_missing_to_referrals: int = 0  # Всего не начислено рефералам
    total_missing_to_referrers: int = 0  # Всего не начислено рефереерам

    def to_dict(self) -> dict:
        """Сериализация для Redis."""
        return {
            'total_referrals_checked': self.total_referrals_checked,
            'referrals_with_topup': self.referrals_with_topup,
            'missing_bonuses': [mb.to_dict() for mb in self.missing_bonuses],
            'total_missing_to_referrals': self.total_missing_to_referrals,
            'total_missing_to_referrers': self.total_missing_to_referrers,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'MissingBonusReport':
        """Десериализация из dict."""
        missing_bonuses = [
            MissingBonus.from_dict(mb) for mb in data.get('missing_bonuses', [])
        ]
        return cls(
            total_referrals_checked=data.get('total_referrals_checked', 0),
            referrals_with_topup=data.get('referrals_with_topup', 0),
            missing_bonuses=missing_bonuses,
            total_missing_to_referrals=data.get('total_missing_to_referrals', 0),
            total_missing_to_referrers=data.get('total_missing_to_referrers', 0),
        )


class ReferralDiagnosticsService:
    """Сервис диагностики реферальной системы."""

    # Возможные пути к логам (приоритет: current > стандартный)
    LOG_PATHS = [
        'logs/current/bot.log',
        '/app/logs/current/bot.log',
        'logs/bot.log',
        '/app/logs/bot.log',
    ]

    def __init__(self, log_path: str | None = None):
        if log_path:
            self.log_path = Path(log_path)
        else:
            self.log_path = self._find_log_file()

    def _find_log_file(self) -> Path:
        """Ищет существующий лог-файл, предпочитая свежие."""
        today = datetime.now().date()
        candidates = []

        for path_str in self.LOG_PATHS:
            path = Path(path_str)
            if path.exists() and path.stat().st_size > 0:
                mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
                is_fresh = mtime >= today - timedelta(days=1)
                candidates.append((path, is_fresh, path.stat().st_mtime))
                logger.info(f'📁 Найден лог: {path} (свежий: {is_fresh})')

        candidates.sort(key=lambda x: (not x[1], -x[2]))

        if candidates:
            selected = candidates[0][0]
            logger.info(f'✅ Выбран лог-файл: {selected}')
            return selected

        return Path('logs/current/bot.log')

    @staticmethod
    def clean_referral_code(raw_code: str) -> str:
        """
        Очищает реферальный код от лишних префиксов.

        ref_refXXX -> refXXX (miniapp добавляет ref_)
        refXXX -> refXXX (без изменений)
        """
        if raw_code.startswith('ref_ref'):
            return raw_code[4:]  # Убираем "ref_"
        return raw_code

    async def analyze_today(self, db: AsyncSession) -> DiagnosticReport:
        """Анализирует реферальные события за сегодня."""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        return await self.analyze_period(db, today, tomorrow)

    async def analyze_period(
        self, db: AsyncSession, start_date: datetime, end_date: datetime
    ) -> DiagnosticReport:
        """Анализирует реферальные события за указанный период."""

        # 1. Парсим логи — находим все переходы по реф-ссылкам
        clicks, total_lines, lines_in_period = await self._parse_clicks(start_date, end_date)

        # 2. Группируем по telegram_id (берём последний клик)
        user_clicks: dict[int, ReferralClick] = {}
        for click in clicks:
            user_clicks[click.telegram_id] = click

        # 3. Сверяем с БД — находим потерянных рефералов
        lost_referrals = await self._find_lost_referrals(db, list(user_clicks.values()))

        return DiagnosticReport(
            total_ref_clicks=len(clicks),
            unique_users_clicked=len(user_clicks),
            lost_referrals=lost_referrals,
            analysis_period_start=start_date,
            analysis_period_end=end_date,
            total_lines_parsed=total_lines,
            lines_in_period=lines_in_period,
        )

    async def analyze_file(self, db: AsyncSession, file_path: str) -> DiagnosticReport:
        """
        Анализирует загруженный лог-файл на наличие потерянных рефералов.

        Args:
            db: Database session
            file_path: Путь к загруженному файлу

        Returns:
            DiagnosticReport с результатами анализа всего файла
        """
        logger.info(f'📂 Начинаю анализ файла: {file_path}')

        # Парсим весь файл без фильтра по дате
        # Используем широкий диапазон дат (все время)
        start_date = datetime(2000, 1, 1)
        end_date = datetime(2100, 1, 1)

        # Временно меняем путь к логу
        original_log_path = self.log_path
        self.log_path = Path(file_path)

        try:
            # skip_date_filter=True — парсим ВСЕ строки без фильтра по дате
            clicks, total_lines, lines_in_period = await self._parse_clicks(
                start_date, end_date, skip_date_filter=True
            )

            # Группируем по telegram_id (берём последний клик)
            user_clicks: dict[int, ReferralClick] = {}
            for click in clicks:
                user_clicks[click.telegram_id] = click

            # Сверяем с БД — находим потерянных рефералов
            lost_referrals = await self._find_lost_referrals(db, list(user_clicks.values()))

            logger.info(
                f'✅ Анализ файла завершён: строк={total_lines}, '
                f'реф-кликов={len(clicks)}, потерянных={len(lost_referrals)}'
            )

            return DiagnosticReport(
                total_ref_clicks=len(clicks),
                unique_users_clicked=len(user_clicks),
                lost_referrals=lost_referrals,
                analysis_period_start=None,
                analysis_period_end=None,
                total_lines_parsed=total_lines,
                lines_in_period=lines_in_period,
            )
        finally:
            # Восстанавливаем оригинальный путь
            self.log_path = original_log_path

    async def _parse_clicks(
        self, start_date: datetime, end_date: datetime, skip_date_filter: bool = False
    ) -> tuple[list[ReferralClick], int, int]:
        """Парсит логи и находит все переходы по реф-ссылкам."""

        clicks = []
        total_lines = 0
        lines_in_period = 0

        if not self.log_path.exists():
            logger.warning(f'❌ Лог-файл не найден: {self.log_path}')
            return clicks, 0, 0

        file_size = self.log_path.stat().st_size
        logger.info(f'📂 Читаю лог-файл: {self.log_path} ({file_size / 1024 / 1024:.2f} MB)')

        # Паттерн timestamp
        timestamp_pattern = re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - .+ - .+ - (.+)$'
        )

        # Паттерны для поиска реф-кликов
        # /start refXXX или /start ref_refXXX
        start_pattern = re.compile(r'📩 Сообщение от ID:(\d+).*?/start\s+(ref[\w_]+)')
        # Сохранение payload
        payload_pattern = re.compile(r"💾 Сохранен start payload '(ref[\w_]+)' для пользователя\s*(\d+)")

        # Для быстрой фильтрации по дате (только если не пропускаем фильтр)
        use_date_prefix = not skip_date_filter and (end_date - start_date).days <= 31
        date_prefix = start_date.strftime('%Y-%m-%d') if use_date_prefix else None

        try:
            with open(self.log_path, encoding='utf-8', errors='ignore') as f:
                for line in f:
                    total_lines += 1
                    line = line.strip()
                    if not line:
                        continue

                    # Убираем Docker-префикс
                    if ' | ' in line[:50]:
                        line = line.split(' | ', 1)[-1]

                    # Быстрая проверка по дате (только для коротких периодов)
                    if date_prefix and date_prefix not in line[:10]:
                        continue

                    # Парсим timestamp
                    match = timestamp_pattern.match(line)
                    if not match:
                        continue

                    timestamp_str, message = match.groups()
                    try:
                        timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        continue

                    if not (start_date <= timestamp < end_date):
                        continue

                    lines_in_period += 1

                    # Ищем реф-клики
                    for pattern in [start_pattern, payload_pattern]:
                        event_match = pattern.search(message)
                        if event_match:
                            if pattern == start_pattern:
                                telegram_id = int(event_match.group(1))
                                raw_code = event_match.group(2)
                            else:
                                raw_code = event_match.group(1)
                                telegram_id = int(event_match.group(2))

                            clean_code = self.clean_referral_code(raw_code)

                            clicks.append(ReferralClick(
                                timestamp=timestamp,
                                telegram_id=telegram_id,
                                raw_code=raw_code,
                                clean_code=clean_code,
                                log_line=line,
                            ))
                            break

        except Exception as e:
            logger.error(f'Ошибка парсинга логов: {e}', exc_info=True)

        logger.info(
            f'📊 Парсинг: строк={total_lines}, за период={lines_in_period}, '
            f'реф-кликов={len(clicks)}'
        )
        return clicks, total_lines, lines_in_period

    async def _find_lost_referrals(
        self, db: AsyncSession, clicks: list[ReferralClick]
    ) -> list[LostReferral]:
        """Находит потерянных рефералов — пришли по ссылке, но реферер не засчитался."""

        if not clicks:
            return []

        lost = []
        telegram_ids = [c.telegram_id for c in clicks]

        # Получаем пользователей из БД
        result = await db.execute(
            select(User).where(User.telegram_id.in_(telegram_ids))
        )
        users_map = {u.telegram_id: u for u in result.scalars().all()}

        # Получаем всех рефереров по кодам
        codes = list(set(c.clean_code for c in clicks))
        referrers_result = await db.execute(
            select(User).where(User.referral_code.in_(codes))
        )
        referrers_map = {u.referral_code: u for u in referrers_result.scalars().all()}

        for click in clicks:
            user = users_map.get(click.telegram_id)
            referrer = referrers_map.get(click.clean_code)

            # Проверяем — засчитался ли реферал?
            is_lost = False

            if user is None:
                # Пользователь не в БД — не завершил регистрацию
                is_lost = True
            elif user.created_at and user.created_at < click.timestamp:
                # Пользователь был создан ДО клика по реф-ссылке
                # Это старый пользователь, который просто зашёл по чужой ссылке
                is_lost = False
                logger.debug(
                    f'⏭️ Пропускаем {click.telegram_id}: создан {user.created_at} < клик {click.timestamp}'
                )
            elif user.referred_by_id is None:
                # Пользователь в БД, но без реферера (и создан после клика)
                is_lost = True
            elif referrer and user.referred_by_id != referrer.id:
                # Реферер другой (странный случай)
                is_lost = True

            if is_lost:
                lost.append(LostReferral(
                    telegram_id=click.telegram_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    referral_code=click.clean_code,
                    expected_referrer_code=click.clean_code,
                    expected_referrer_id=referrer.id if referrer else None,
                    expected_referrer_name=referrer.full_name if referrer else None,
                    click_time=click.timestamp,
                    registered=user is not None,
                    has_referrer=user.referred_by_id is not None if user else False,
                    current_referrer_id=user.referred_by_id if user else None,
                ))

        logger.info(f'🔍 Найдено потерянных рефералов: {len(lost)}')
        return lost

    async def _add_to_active_contests(
        self,
        db: AsyncSession,
        referral: User,
        referrer: User,
        amount_kopeks: int,
    ) -> None:
        """
        Добавляет восстановленного реферала в активные конкурсы.

        Проверяет все активные конкурсы и добавляет событие если:
        - Реферал зарегистрирован в период конкурса
        - Событие ещё не было добавлено
        """
        from datetime import datetime

        from app.database.crud.referral_contest import add_contest_event, get_contests_for_events

        if not settings.is_contests_enabled():
            return

        now_utc = datetime.utcnow()

        # Проверяем конкурсы по оплаченным рефералам
        contests = await get_contests_for_events(db, now_utc, contest_types=['referral_paid'])

        for contest in contests:
            try:
                # Проверяем что реферал зарегистрировался В ПЕРИОД конкурса
                user_created_at = (
                    referral.created_at
                    if referral.created_at.tzinfo is None
                    else referral.created_at.replace(tzinfo=None)
                )
                contest_start = (
                    contest.start_at if contest.start_at.tzinfo is None else contest.start_at.replace(tzinfo=None)
                )
                contest_end = contest.end_at if contest.end_at.tzinfo is None else contest.end_at.replace(tzinfo=None)

                if user_created_at < contest_start or user_created_at > contest_end:
                    logger.debug(
                        f'Реферал {referral.id} зарегистрирован вне периода конкурса {contest.id}'
                    )
                    continue

                event = await add_contest_event(
                    db,
                    contest_id=contest.id,
                    referrer_id=referrer.id,
                    referral_id=referral.id,
                    amount_kopeks=amount_kopeks,
                    event_type='restored_referral',
                )
                if event:
                    logger.info(
                        f'🏆 Восстановленный реферал добавлен в конкурс {contest.id}: '
                        f'реферер {referrer.id}, реферал {referral.id}'
                    )
            except Exception as exc:
                logger.error(f'Не удалось добавить в конкурс {contest.id}: {exc}')

        # Также проверяем конкурсы по регистрации (если есть)
        reg_contests = await get_contests_for_events(db, now_utc, contest_types=['referral_registered'])

        for contest in reg_contests:
            try:
                user_created_at = (
                    referral.created_at
                    if referral.created_at.tzinfo is None
                    else referral.created_at.replace(tzinfo=None)
                )
                contest_start = (
                    contest.start_at if contest.start_at.tzinfo is None else contest.start_at.replace(tzinfo=None)
                )
                contest_end = contest.end_at if contest.end_at.tzinfo is None else contest.end_at.replace(tzinfo=None)

                if user_created_at < contest_start or user_created_at > contest_end:
                    continue

                event = await add_contest_event(
                    db,
                    contest_id=contest.id,
                    referrer_id=referrer.id,
                    referral_id=referral.id,
                    amount_kopeks=0,
                    event_type='restored_referral_registration',
                )
                if event:
                    logger.info(
                        f'🏆 Восстановленный реферал (регистрация) добавлен в конкурс {contest.id}'
                    )
            except Exception as exc:
                logger.error(f'Не удалось добавить в конкурс регистрации {contest.id}: {exc}')

    async def fix_lost_referrals(
        self, db: AsyncSession, lost_referrals: list[LostReferral], apply: bool = False
    ) -> FixReport:
        """
        Исправляет потерянных рефералов.

        Args:
            db: Database session
            lost_referrals: Список потерянных рефералов
            apply: Если False — только предпросмотр, если True — применить изменения

        Returns:
            FixReport с деталями исправлений
        """
        report = FixReport()

        if not lost_referrals:
            logger.info('🔍 Нет потерянных рефералов для исправления')
            return report

        # Получаем всех пользователей и рефереров
        telegram_ids = [lr.telegram_id for lr in lost_referrals]
        result = await db.execute(select(User).where(User.telegram_id.in_(telegram_ids)))
        users_map = {u.telegram_id: u for u in result.scalars().all()}

        referrer_ids = list(set(lr.expected_referrer_id for lr in lost_referrals if lr.expected_referrer_id))
        referrers_result = await db.execute(select(User).where(User.id.in_(referrer_ids)))
        referrers_map = {u.id: u for u in referrers_result.scalars().all()}

        for lost in lost_referrals:
            detail = FixDetail(
                telegram_id=lost.telegram_id,
                username=lost.username,
                full_name=lost.full_name,
                referred_by_set=False,
                referrer_id=lost.expected_referrer_id,
                referrer_name=lost.expected_referrer_name,
            )

            try:
                user = users_map.get(lost.telegram_id)
                if not user:
                    detail.error = 'Пользователь не найден в БД'
                    report.errors += 1
                    report.details.append(detail)
                    continue

                referrer = referrers_map.get(lost.expected_referrer_id) if lost.expected_referrer_id else None
                if not referrer:
                    detail.error = 'Реферер не найден'
                    report.errors += 1
                    report.details.append(detail)
                    continue

                # 1. Устанавливаем referred_by_id
                if user.referred_by_id != referrer.id:
                    if apply:
                        user.referred_by_id = referrer.id
                        logger.info(f'✅ Установлен referred_by_id={referrer.id} для пользователя {user.telegram_id}')
                    detail.referred_by_set = True
                    report.users_fixed += 1

                # 2. Проверяем первое пополнение
                # Ищем первое пополнение пользователя
                from app.database.models import Transaction, TransactionType

                first_topup_result = await db.execute(
                    select(Transaction)
                    .where(Transaction.user_id == user.id, Transaction.type == TransactionType.DEPOSIT.value)
                    .order_by(Transaction.created_at.asc())
                    .limit(1)
                )
                first_topup = first_topup_result.scalar_one_or_none()

                if first_topup and first_topup.amount_kopeks >= settings.REFERRAL_MINIMUM_TOPUP_KOPEKS:
                    detail.had_first_topup = True
                    detail.topup_amount_kopeks = first_topup.amount_kopeks

                    # Проверяем, не начисляли ли уже бонусы
                    existing_bonus_result = await db.execute(
                        select(ReferralEarning)
                        .where(
                            ReferralEarning.user_id == referrer.id,
                            ReferralEarning.referral_id == user.id,
                            ReferralEarning.reason == 'referral_first_topup',
                        )
                        .limit(1)
                    )
                    existing_bonus = existing_bonus_result.scalar_one_or_none()

                    if not existing_bonus:
                        # 3. Начисляем бонус рефералу (приглашённому)
                        # Не проверяем has_made_first_topup — это восстановление потерянного реферала,
                        # он мог пополнить баланс, но бонус не получил т.к. не было referred_by_id
                        if settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0:
                            detail.bonus_to_referral_kopeks = settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS
                            report.bonuses_to_referrals += settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS

                            if apply:
                                await add_user_balance(
                                    db,
                                    user,
                                    settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
                                    'Восстановленный бонус за первое пополнение (потерянный реферал)',
                                    create_transaction=True,
                                    transaction_type=TransactionType.REFERRAL_REWARD,
                                )
                                user.has_made_first_topup = True
                                logger.info(
                                    f'💰 Начислен бонус рефералу {user.telegram_id}: '
                                    f'{settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS / 100}₽'
                                )

                        # 4. Начисляем бонус рефереру
                        from app.utils.user_utils import get_effective_referral_commission_percent

                        commission_percent = get_effective_referral_commission_percent(referrer)
                        commission_amount = int(first_topup.amount_kopeks * commission_percent / 100)
                        inviter_bonus = max(settings.REFERRAL_INVITER_BONUS_KOPEKS, commission_amount)

                        if inviter_bonus > 0:
                            detail.bonus_to_referrer_kopeks = inviter_bonus
                            report.bonuses_to_referrers += inviter_bonus

                            if apply:
                                await add_user_balance(
                                    db,
                                    referrer,
                                    inviter_bonus,
                                    f'Восстановленный бонус за реферала {user.full_name or user.username or user.telegram_id}',
                                    create_transaction=True,
                                    transaction_type=TransactionType.REFERRAL_REWARD,
                                )

                                # Создаём запись ReferralEarning
                                await create_referral_earning(
                                    db=db,
                                    user_id=referrer.id,
                                    referral_id=user.id,
                                    amount_kopeks=inviter_bonus,
                                    reason='referral_first_topup',
                                )

                                logger.info(
                                    f'💰 Начислен бонус рефереру {referrer.telegram_id or referrer.id}: '
                                    f'{inviter_bonus / 100}₽'
                                )

                                # Добавляем в активные конкурсы рефералов
                                await self._add_to_active_contests(
                                    db, user, referrer, first_topup.amount_kopeks
                                )
                    else:
                        detail.error = 'Бонусы уже начислены ранее'

                report.details.append(detail)

            except Exception as e:
                logger.error(f'❌ Ошибка исправления реферала {lost.telegram_id}: {e}', exc_info=True)
                detail.error = str(e)
                report.errors += 1
                report.details.append(detail)

        if apply:
            await db.commit()
            logger.info(
                f'✅ Исправлено рефералов: {report.users_fixed}, '
                f'начислено бонусов: {report.bonuses_to_referrals / 100}₽ + {report.bonuses_to_referrers / 100}₽'
            )
        else:
            logger.info(f'📋 Предпросмотр: {report.users_fixed} рефералов будут исправлены')

        return report

    async def check_missing_bonuses(self, db: AsyncSession) -> MissingBonusReport:
        """
        Проверяет по БД: всем ли рефералам и рефереерам начислены бонусы.

        Находит пользователей которые:
        1. Имеют referred_by_id (пришли по реф-ссылке)
        2. Сделали первое пополнение >= минимума
        3. Но бонусы не были начислены (нет ReferralEarning)

        Returns:
            MissingBonusReport со списком ненначисленных бонусов
        """
        from app.database.models import Transaction, TransactionType
        from app.utils.user_utils import get_effective_referral_commission_percent

        report = MissingBonusReport()

        # 1. Находим всех рефералов (у кого есть referred_by_id)
        referrals_result = await db.execute(
            select(User).where(User.referred_by_id.isnot(None))
        )
        referrals = referrals_result.scalars().all()
        report.total_referrals_checked = len(referrals)

        if not referrals:
            logger.info('📊 Нет рефералов для проверки')
            return report

        # 2. Собираем ID рефереров
        referrer_ids = list(set(r.referred_by_id for r in referrals))
        referrers_result = await db.execute(select(User).where(User.id.in_(referrer_ids)))
        referrers_map = {u.id: u for u in referrers_result.scalars().all()}

        # 3. Получаем все ReferralEarning для проверки
        referral_ids = [r.id for r in referrals]
        earnings_result = await db.execute(
            select(ReferralEarning).where(
                ReferralEarning.referral_id.in_(referral_ids),
                ReferralEarning.reason == 'referral_first_topup',
            )
        )
        # Множество пар (referrer_id, referral_id) где бонус уже начислен
        existing_earnings = {(e.user_id, e.referral_id) for e in earnings_result.scalars().all()}

        # 4. Проверяем каждого реферала
        for referral in referrals:
            referrer = referrers_map.get(referral.referred_by_id)
            if not referrer:
                continue

            # Ищем первое пополнение
            first_topup_result = await db.execute(
                select(Transaction)
                .where(
                    Transaction.user_id == referral.id,
                    Transaction.type == TransactionType.DEPOSIT.value,
                )
                .order_by(Transaction.created_at.asc())
                .limit(1)
            )
            first_topup = first_topup_result.scalar_one_or_none()

            # Если нет пополнения или меньше минимума — пропускаем
            if not first_topup or first_topup.amount_kopeks < settings.REFERRAL_MINIMUM_TOPUP_KOPEKS:
                continue

            report.referrals_with_topup += 1

            # Проверяем начислен ли бонус
            bonus_exists = (referrer.id, referral.id) in existing_earnings

            if bonus_exists:
                # Бонусы уже начислены
                continue

            # Бонусы НЕ начислены — добавляем в отчёт
            commission_percent = get_effective_referral_commission_percent(referrer)
            commission_amount = int(first_topup.amount_kopeks * commission_percent / 100)
            referrer_bonus = max(settings.REFERRAL_INVITER_BONUS_KOPEKS, commission_amount)

            missing = MissingBonus(
                referral_id=referral.id,
                referral_telegram_id=referral.telegram_id,
                referral_username=referral.username,
                referral_full_name=referral.full_name,
                referrer_id=referrer.id,
                referrer_telegram_id=referrer.telegram_id,
                referrer_username=referrer.username,
                referrer_full_name=referrer.full_name,
                first_topup_amount_kopeks=first_topup.amount_kopeks,
                first_topup_date=first_topup.created_at,
                missing_referral_bonus=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0,
                missing_referrer_bonus=referrer_bonus > 0,
                referral_bonus_amount=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
                referrer_bonus_amount=referrer_bonus,
            )

            report.missing_bonuses.append(missing)
            report.total_missing_to_referrals += missing.referral_bonus_amount
            report.total_missing_to_referrers += missing.referrer_bonus_amount

        logger.info(
            f'📊 Проверка бонусов: {report.total_referrals_checked} рефералов, '
            f'{report.referrals_with_topup} с пополнением, '
            f'{len(report.missing_bonuses)} без бонусов'
        )

        return report

    async def fix_missing_bonuses(
        self, db: AsyncSession, missing_bonuses: list[MissingBonus], apply: bool = False
    ) -> FixReport:
        """
        Начисляет пропущенные бонусы.

        Args:
            db: Database session
            missing_bonuses: Список пропущенных бонусов
            apply: Если False — только предпросмотр

        Returns:
            FixReport с деталями
        """
        from app.database.models import TransactionType

        report = FixReport()

        if not missing_bonuses:
            return report

        # Загружаем пользователей
        referral_ids = [mb.referral_id for mb in missing_bonuses]
        referrer_ids = [mb.referrer_id for mb in missing_bonuses]

        users_result = await db.execute(
            select(User).where(User.id.in_(referral_ids + referrer_ids))
        )
        users_map = {u.id: u for u in users_result.scalars().all()}

        for missing in missing_bonuses:
            referral = users_map.get(missing.referral_id)
            referrer = users_map.get(missing.referrer_id)

            detail = FixDetail(
                telegram_id=missing.referral_telegram_id,
                username=missing.referral_username,
                full_name=missing.referral_full_name,
                referred_by_set=False,  # referred_by уже установлен
                referrer_id=missing.referrer_id,
                referrer_name=missing.referrer_full_name,
                had_first_topup=True,
                topup_amount_kopeks=missing.first_topup_amount_kopeks,
            )

            if not referral or not referrer:
                detail.error = 'Пользователь не найден'
                report.errors += 1
                report.details.append(detail)
                continue

            try:
                # Начисляем бонус рефералу
                if missing.missing_referral_bonus and missing.referral_bonus_amount > 0:
                    detail.bonus_to_referral_kopeks = missing.referral_bonus_amount
                    report.bonuses_to_referrals += missing.referral_bonus_amount

                    if apply:
                        from app.database.models import TransactionType

                        await add_user_balance(
                            db,
                            referral,
                            missing.referral_bonus_amount,
                            'Восстановленный бонус за первое пополнение',
                            create_transaction=True,
                            transaction_type=TransactionType.REFERRAL_REWARD,
                        )
                        referral.has_made_first_topup = True
                        logger.info(
                            f'💰 Начислен бонус рефералу {referral.telegram_id}: '
                            f'{missing.referral_bonus_amount / 100}₽'
                        )

                # Начисляем бонус рефереру
                if missing.missing_referrer_bonus and missing.referrer_bonus_amount > 0:
                    detail.bonus_to_referrer_kopeks = missing.referrer_bonus_amount
                    report.bonuses_to_referrers += missing.referrer_bonus_amount

                    if apply:
                        await add_user_balance(
                            db,
                            referrer,
                            missing.referrer_bonus_amount,
                            f'Восстановленный бонус за реферала {referral.full_name or referral.username or referral.telegram_id}',
                            create_transaction=True,
                            transaction_type=TransactionType.REFERRAL_REWARD,
                        )

                        # Создаём ReferralEarning чтобы не начислять повторно
                        await create_referral_earning(
                            db=db,
                            user_id=referrer.id,
                            referral_id=referral.id,
                            amount_kopeks=missing.referrer_bonus_amount,
                            reason='referral_first_topup',
                        )
                        logger.info(
                            f'💰 Начислен бонус рефереру {referrer.telegram_id}: '
                            f'{missing.referrer_bonus_amount / 100}₽'
                        )

                        # Добавляем в активные конкурсы рефералов
                        await self._add_to_active_contests(
                            db, referral, referrer, missing.first_topup_amount_kopeks
                        )

                report.users_fixed += 1
                report.details.append(detail)

            except Exception as e:
                logger.error(f'❌ Ошибка начисления бонуса: {e}', exc_info=True)
                detail.error = str(e)
                report.errors += 1
                report.details.append(detail)

        if apply:
            await db.commit()
            logger.info(
                f'✅ Начислено бонусов: {report.bonuses_to_referrals / 100}₽ рефералам + '
                f'{report.bonuses_to_referrers / 100}₽ рефереерам'
            )

        return report


# Глобальный экземпляр сервиса
referral_diagnostics_service = ReferralDiagnosticsService()
