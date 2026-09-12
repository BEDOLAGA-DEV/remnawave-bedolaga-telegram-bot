"""Сервис статистики кликов по кнопкам меню."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, and_, case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.local_date import local_date_expr
from app.database.models import ButtonClickLog
from app.utils.timezone import local_day_start


class MenuLayoutStatsService:
    """Сервис для сбора и анализа статистики кликов по кнопкам."""

    @classmethod
    def _is_sqlite(cls) -> bool:
        """Проверить, используется ли SQLite."""
        return settings.is_sqlite()

    @classmethod
    def _get_hour_expr(cls, column):
        """Получить выражение для извлечения часа (совместимо с SQLite и PostgreSQL)."""
        if cls._is_sqlite():
            # SQLite: strftime('%H', column) возвращает строку
            return func.cast(func.strftime('%H', column), Integer)
        # PostgreSQL: EXTRACT(hour FROM column)
        return func.extract('hour', column)

    @classmethod
    def _get_weekday_expr(cls, column):
        """Получить выражение для дня недели (0=Пн, 6=Вс) совместимо с SQLite и PostgreSQL."""
        if cls._is_sqlite():
            # SQLite: strftime('%w', column) возвращает 0=воскресенье, 1-6=пн-сб
            # Преобразуем: 0->6, 1->0, 2->1, ..., 6->5
            dow = func.cast(func.strftime('%w', column), Integer)
            return case((dow == 0, 6), else_=dow - 1)
        # PostgreSQL: EXTRACT(dow FROM column) возвращает 0=воскресенье, 1-6=пн-сб
        dow = func.extract('dow', column)
        return case((dow == 0, 6), else_=dow - 1)

    @classmethod
    async def log_button_click(
        cls,
        db: AsyncSession,
        button_id: str,
        user_id: int | None = None,
        callback_data: str | None = None,
        button_type: str | None = None,
        button_text: str | None = None,
        telegram_id: int | None = None,
    ) -> ButtonClickLog | None:
        """Записать клик по кнопке.

        Args:
            user_id: внутренний User.id (кабинет, Mini App, внешний API)
            telegram_id: Telegram ID (middleware бота знает только его)

        Порядок поиска детерминирован: telegram_id ищется только в своей
        колонке, user_id — сначала в своей, и лишь для внешнего API (где
        исторически присылали что угодно) — по telegram_id. Раньше одно число
        искали в обеих колонках без порядка, и при совпадении внутреннего id
        одного человека с Telegram ID другого клик уходил чужому.
        """
        try:
            from sqlalchemy import select

            from app.database.models import User

            # Проверяем существование пользователя перед вставкой,
            # чтобы избежать ошибки foreign key в логах БД.
            actual_user_id = None
            if telegram_id is not None:
                result = await db.execute(select(User.id).where(User.telegram_id == telegram_id).limit(1))
                actual_user_id = result.scalar_one_or_none()
            elif user_id is not None:
                result = await db.execute(select(User.id).where(User.id == user_id).limit(1))
                actual_user_id = result.scalar_one_or_none()
                if actual_user_id is None:
                    result = await db.execute(select(User.id).where(User.telegram_id == user_id).limit(1))
                    actual_user_id = result.scalar_one_or_none()

            click_log = ButtonClickLog(
                button_id=button_id,
                user_id=actual_user_id,
                callback_data=callback_data,
                button_type=button_type,
                button_text=button_text,
            )
            db.add(click_log)
            await db.commit()
            return click_log
        except Exception:
            await db.rollback()
            return None

    @classmethod
    async def get_button_stats(
        cls,
        db: AsyncSession,
        button_id: str,
        days: int = 30,
    ) -> dict[str, Any]:
        """Получить статистику кликов по конкретной кнопке."""
        now = datetime.now(UTC)
        today_start = local_day_start(now)
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=days)

        # Общее количество кликов
        total_result = await db.execute(
            select(func.count(ButtonClickLog.id)).where(ButtonClickLog.button_id == button_id)
        )
        clicks_total = total_result.scalar() or 0

        # Клики сегодня
        today_result = await db.execute(
            select(func.count(ButtonClickLog.id)).where(
                and_(ButtonClickLog.button_id == button_id, ButtonClickLog.clicked_at >= today_start)
            )
        )
        clicks_today = today_result.scalar() or 0

        # Клики за неделю
        week_result = await db.execute(
            select(func.count(ButtonClickLog.id)).where(
                and_(ButtonClickLog.button_id == button_id, ButtonClickLog.clicked_at >= week_ago)
            )
        )
        clicks_week = week_result.scalar() or 0

        # Клики за месяц
        month_result = await db.execute(
            select(func.count(ButtonClickLog.id)).where(
                and_(ButtonClickLog.button_id == button_id, ButtonClickLog.clicked_at >= month_ago)
            )
        )
        clicks_month = month_result.scalar() or 0

        # Уникальные пользователи
        unique_result = await db.execute(
            select(func.count(func.distinct(ButtonClickLog.user_id))).where(ButtonClickLog.button_id == button_id)
        )
        unique_users = unique_result.scalar() or 0

        # Последний клик
        last_click_result = await db.execute(
            select(ButtonClickLog.clicked_at)
            .where(ButtonClickLog.button_id == button_id)
            .order_by(desc(ButtonClickLog.clicked_at))
            .limit(1)
        )
        last_click = last_click_result.scalar_one_or_none()

        return {
            'button_id': button_id,
            'clicks_total': clicks_total,
            'clicks_today': clicks_today,
            'clicks_week': clicks_week,
            'clicks_month': clicks_month,
            'unique_users': unique_users,
            'last_click_at': last_click,
        }

    @classmethod
    async def get_button_clicks_by_day(
        cls,
        db: AsyncSession,
        button_id: str,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику кликов по дням."""
        start_date = datetime.now(UTC) - timedelta(days=days)

        # Группировка по дате
        result = await db.execute(
            select(
                local_date_expr(ButtonClickLog.clicked_at, db).label('date'),
                func.count(ButtonClickLog.id).label('count'),
            )
            .where(and_(ButtonClickLog.button_id == button_id, ButtonClickLog.clicked_at >= start_date))
            .group_by(local_date_expr(ButtonClickLog.clicked_at, db))
            .order_by(local_date_expr(ButtonClickLog.clicked_at, db))
        )

        return [{'date': str(row.date), 'count': row.count} for row in result.all()]

    @classmethod
    async def get_all_buttons_stats(
        cls,
        db: AsyncSession,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику по всем кнопкам."""
        now = datetime.now(UTC)
        today_start = local_day_start(now)
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=days)

        # Для производительности используем один запрос с подзапросами через CASE
        result = await db.execute(
            select(
                ButtonClickLog.button_id,
                # Общее количество кликов (все клики без фильтра по датам)
                func.count(ButtonClickLog.id).label('clicks_total'),
                # Уникальные пользователи (все время)
                func.count(func.distinct(ButtonClickLog.user_id)).label('unique_users'),
                # Последний клик (все время)
                func.max(ButtonClickLog.clicked_at).label('last_click_at'),
                # Подсчет кликов за сегодня
                func.sum(case((ButtonClickLog.clicked_at >= today_start, 1), else_=0)).label('clicks_today'),
                # Подсчет кликов за неделю
                func.sum(case((ButtonClickLog.clicked_at >= week_ago, 1), else_=0)).label('clicks_week'),
                # Подсчет кликов за месяц
                func.sum(case((ButtonClickLog.clicked_at >= month_ago, 1), else_=0)).label('clicks_month'),
            )
            .group_by(ButtonClickLog.button_id)
            .order_by(desc(func.count(ButtonClickLog.id)))
        )

        return [
            {
                'button_id': row.button_id,
                'clicks_total': row.clicks_total,
                'clicks_today': row.clicks_today or 0,
                'clicks_week': row.clicks_week or 0,
                'clicks_month': row.clicks_month or 0,
                'unique_users': row.unique_users,
                'last_click_at': row.last_click_at,
            }
            for row in result.all()
        ]

    @classmethod
    async def get_total_clicks(
        cls,
        db: AsyncSession,
        days: int = 30,
    ) -> int:
        """Получить общее количество кликов за период."""
        start_date = datetime.now(UTC) - timedelta(days=days)

        result = await db.execute(select(func.count(ButtonClickLog.id)).where(ButtonClickLog.clicked_at >= start_date))
        return result.scalar() or 0

    @classmethod
    async def get_stats_by_button_type(
        cls,
        db: AsyncSession,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику кликов по типам кнопок."""
        start_date = datetime.now(UTC) - timedelta(days=days)

        result = await db.execute(
            select(
                ButtonClickLog.button_type,
                func.count(ButtonClickLog.id).label('clicks_total'),
                func.count(func.distinct(ButtonClickLog.user_id)).label('unique_users'),
            )
            .where(and_(ButtonClickLog.clicked_at >= start_date, ButtonClickLog.button_type.isnot(None)))
            .group_by(ButtonClickLog.button_type)
            .order_by(desc(func.count(ButtonClickLog.id)))
        )

        return [
            {
                'button_type': row.button_type or 'unknown',
                'clicks_total': row.clicks_total,
                'unique_users': row.unique_users,
            }
            for row in result.all()
        ]

    @classmethod
    async def get_clicks_by_hour(
        cls,
        db: AsyncSession,
        button_id: str | None = None,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику кликов по часам дня."""
        start_date = datetime.now(UTC) - timedelta(days=days)

        # Используем helper-метод для совместимости с SQLite и PostgreSQL
        hour_expr = cls._get_hour_expr(ButtonClickLog.clicked_at).label('hour')

        query = select(hour_expr, func.count(ButtonClickLog.id).label('count')).where(
            ButtonClickLog.clicked_at >= start_date
        )

        if button_id:
            query = query.where(ButtonClickLog.button_id == button_id)

        result = await db.execute(query.group_by(hour_expr).order_by(hour_expr))

        # Создаем словарь для быстрого доступа по часу
        stats_dict = {int(row.hour): row.count for row in result.all()}

        # Возвращаем все 24 часа, даже если count = 0
        return [{'hour': hour, 'count': stats_dict.get(hour, 0)} for hour in range(24)]

    @classmethod
    async def get_clicks_by_weekday(
        cls,
        db: AsyncSession,
        button_id: str | None = None,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику кликов по дням недели.

        Возвращает 0=понедельник, 6=воскресенье.
        Поддерживает как PostgreSQL, так и SQLite.
        """
        start_date = datetime.now(UTC) - timedelta(days=days)

        # Используем helper-метод для совместимости с SQLite и PostgreSQL
        weekday_expr = cls._get_weekday_expr(ButtonClickLog.clicked_at).label('weekday')

        query = select(weekday_expr, func.count(ButtonClickLog.id).label('count')).where(
            ButtonClickLog.clicked_at >= start_date
        )

        if button_id:
            query = query.where(ButtonClickLog.button_id == button_id)

        result = await db.execute(query.group_by(weekday_expr).order_by(weekday_expr))

        weekday_names = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']

        # Создаем словарь для быстрого доступа по weekday
        stats_dict = {int(row.weekday): row.count for row in result.all()}

        # Возвращаем все дни недели, даже если count = 0
        return [
            {'weekday': weekday, 'weekday_name': weekday_names[weekday], 'count': stats_dict.get(weekday, 0)}
            for weekday in range(7)
        ]

    @classmethod
    async def get_top_users(
        cls,
        db: AsyncSession,
        button_id: str | None = None,
        limit: int = 10,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить топ пользователей по количеству кликов."""
        start_date = datetime.now(UTC) - timedelta(days=days)

        query = select(
            ButtonClickLog.user_id,
            func.count(ButtonClickLog.id).label('clicks_count'),
            func.max(ButtonClickLog.clicked_at).label('last_click_at'),
        ).where(and_(ButtonClickLog.clicked_at >= start_date, ButtonClickLog.user_id.isnot(None)))

        if button_id:
            query = query.where(ButtonClickLog.button_id == button_id)

        result = await db.execute(
            query.group_by(ButtonClickLog.user_id).order_by(desc(func.count(ButtonClickLog.id))).limit(limit)
        )

        return [
            {
                'user_id': row.user_id,
                'clicks_count': row.clicks_count,
                'last_click_at': row.last_click_at,
            }
            for row in result.all()
        ]

    @classmethod
    async def get_period_comparison(
        cls,
        db: AsyncSession,
        button_id: str | None = None,
        current_days: int = 7,
        previous_days: int = 7,
    ) -> dict[str, Any]:
        """Сравнить статистику текущего и предыдущего периода."""
        now = datetime.now(UTC)
        current_start = now - timedelta(days=current_days)
        previous_start = current_start - timedelta(days=previous_days)
        previous_end = current_start

        query_current = select(func.count(ButtonClickLog.id))
        query_previous = select(func.count(ButtonClickLog.id))

        if button_id:
            query_current = query_current.where(ButtonClickLog.button_id == button_id)
            query_previous = query_previous.where(ButtonClickLog.button_id == button_id)

        query_current = query_current.where(ButtonClickLog.clicked_at >= current_start)
        query_previous = query_previous.where(
            and_(ButtonClickLog.clicked_at >= previous_start, ButtonClickLog.clicked_at < previous_end)
        )

        current_result = await db.execute(query_current)
        previous_result = await db.execute(query_previous)

        current_count = current_result.scalar() or 0
        previous_count = previous_result.scalar() or 0

        change_percent = 0
        if previous_count > 0:
            change_percent = ((current_count - previous_count) / previous_count) * 100

        return {
            'current_period': {
                'clicks': current_count,
                'days': current_days,
                'start': current_start,
                'end': now,
            },
            'previous_period': {
                'clicks': previous_count,
                'days': previous_days,
                'start': previous_start,
                'end': previous_end,
            },
            'change': {
                'absolute': current_count - previous_count,
                'percent': round(change_percent, 2),
                'trend': 'up' if change_percent > 0 else 'down' if change_percent < 0 else 'stable',
            },
        }

    @classmethod
    async def get_click_sequences(
        cls,
        db: AsyncSession,
        user_id: int,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Получить последовательности кликов пользователя."""
        result = await db.execute(
            select(
                ButtonClickLog.button_id,
                ButtonClickLog.button_text,
                ButtonClickLog.clicked_at,
            )
            .where(ButtonClickLog.user_id == user_id)
            .order_by(desc(ButtonClickLog.clicked_at))
            .limit(limit)
        )

        return [
            {
                'button_id': row.button_id,
                'button_text': row.button_text,
                'clicked_at': row.clicked_at,
            }
            for row in result.all()
        ]
