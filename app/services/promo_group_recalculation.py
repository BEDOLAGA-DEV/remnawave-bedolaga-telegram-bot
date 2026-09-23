"""Пересчёт промогрупп по тратам для всех людей сразу.

Автоназначение промогруппы (``promo_group_assignment``) срабатывает в момент
оплаты подписки. Когда оператор создаёт, правит или удаляет группу с порогом,
у людей ничего не происходит до следующей оплаты — и тысячи человек висят в
базовой группе, хотя по тратам давно заслужили другую. Здесь то же правило
прогоняется по всем, у кого есть траты: один проход, одна итоговая сводка
админам вместо уведомления на каждого человека.

Запуск в фоне (``promo_group_recalculation.schedule``) — из CRUD групп и из
кнопки в кабинете. Проход идёт в собственной сессии; повторный запрос во
время прохода не плодит задач, а просит ещё один проход после текущего.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.promo_group import has_auto_assign_promo_groups
from app.database.models import Transaction, TransactionType, User, UserPromoGroup


logger = structlog.get_logger(__name__)

MembershipSnapshot = tuple[int | None, frozenset[tuple[int, str]]]


@dataclass(frozen=True, slots=True)
class RecalculationResult:
    """Итог одного прохода: сколько людей проверено и у скольких изменилась группа."""

    reason: str
    checked: int = 0
    changed: int = 0
    failed: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data['started_at'] = self.started_at.isoformat() if self.started_at else None
        data['finished_at'] = self.finished_at.isoformat() if self.finished_at else None
        return data


async def _paying_user_ids(db: AsyncSession) -> list[int]:
    """Только те, у кого есть личные траты: остальным группу по тратам не выдать."""
    result = await db.execute(
        select(Transaction.user_id)
        .where(Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value)
        .where(Transaction.is_completed.is_(True))
        .distinct()
        .order_by(Transaction.user_id)
    )
    return [int(user_id) for user_id in result.scalars().all()]


async def _membership_snapshot(db: AsyncSession, user_id: int) -> MembershipSnapshot:
    """Основная группа и все связи человека — чтобы честно посчитать «изменилось»."""
    primary = await db.scalar(select(User.promo_group_id).where(User.id == user_id))
    links = await db.execute(
        select(UserPromoGroup.promo_group_id, UserPromoGroup.assigned_by).where(UserPromoGroup.user_id == user_id)
    )
    return primary, frozenset((int(group_id), str(assigned_by)) for group_id, assigned_by in links.all())


async def recalculate_promo_groups(db: AsyncSession, *, reason: str = 'вручную') -> RecalculationResult:
    """Прогоняет правило автоназначения по всем платившим. Уведомлений на каждого — нет."""
    from app.services.promo_group_assignment import maybe_assign_promo_group_by_total_spent

    started_at = datetime.now(UTC)
    if not await has_auto_assign_promo_groups(db):
        return RecalculationResult(reason=reason, started_at=started_at, finished_at=datetime.now(UTC))

    user_ids = await _paying_user_ids(db)
    changed = 0
    failed = 0
    for user_id in user_ids:
        try:
            before = await _membership_snapshot(db, user_id)
            await maybe_assign_promo_group_by_total_spent(db, user_id, notify_admins=False, notify_user=False)
            after = await _membership_snapshot(db, user_id)
        except Exception as exc:
            failed += 1
            logger.error('Пересчёт промогруппы для пользователя не удался', user_id=user_id, exc=exc)
            await db.rollback()
            continue
        if before != after:
            changed += 1

    result = RecalculationResult(
        reason=reason,
        checked=len(user_ids),
        changed=changed,
        failed=failed,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
    logger.info('Пересчёт промогрупп завершён', **result.to_dict())
    return result


async def _run_with_own_session(reason: str) -> RecalculationResult:
    from app.database.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        return await recalculate_promo_groups(db, reason=reason)


def build_summary_text(result: RecalculationResult) -> str:
    """Одна сводка админам за весь проход."""
    lines = [
        '🏷 <b>ПЕРЕСЧЁТ ПРОМОГРУПП</b>',
        '',
        f'📝 Причина: {html.escape(result.reason)}',
        f'👥 Проверено: {result.checked}',
        f'🔁 Переназначено: {result.changed}',
    ]
    if result.failed:
        lines.append(f'⚠️ Не удалось: {result.failed}')
    if result.error:
        lines.append(f'❌ Ошибка: {html.escape(result.error)}')
    if result.started_at and result.finished_at:
        lines.append(f'⏱ {(result.finished_at - result.started_at).total_seconds():.1f} с')
    return '\n'.join(lines)


async def notify_admins_about_recalculation(result: RecalculationResult) -> None:
    """Сводка уходит только когда есть что сказать: изменения или сбой."""
    if not result.changed and not result.failed and not result.error:
        return

    from app.bot_factory import create_bot
    from app.config import settings
    from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

    if not getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False):
        return
    bot_token = getattr(settings, 'BOT_TOKEN', None)
    if not bot_token:
        return

    bot = create_bot(token=bot_token)
    try:
        await AdminNotificationService(bot).send_admin_notification(
            build_summary_text(result), category=NotificationCategory.PROMO
        )
    except Exception as exc:
        logger.error('Не удалось отправить сводку пересчёта промогрупп', exc=exc)
    finally:
        try:
            await bot.session.close()
        except Exception as exc:
            # Сводка уже отправлена (или нет) — незакрытая HTTP-сессия бота на это не влияет.
            logger.debug('Не удалось закрыть сессию бота после сводки пересчёта', exc=exc)


Runner = Callable[[str], Awaitable[RecalculationResult]]
Announcer = Callable[[RecalculationResult], Awaitable[None]]


class PromoGroupRecalculation:
    """Один фоновый проход за раз; запрос во время прохода = ещё один проход после."""

    def __init__(
        self, *, runner: Runner = _run_with_own_session, announcer: Announcer = notify_admins_about_recalculation
    ):
        self._runner = runner
        self._announcer = announcer
        self._task: asyncio.Task[None] | None = None
        self._current_reason: str | None = None
        self._rerun_reason: str | None = None
        self._last: RecalculationResult | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_result(self) -> RecalculationResult | None:
        return self._last

    def schedule(self, reason: str) -> bool:
        """Ставит проход в фон. ``True`` — запущен сейчас, ``False`` — встал в очередь или запускать негде."""
        if self.is_running:
            self._rerun_reason = reason
            logger.info('Пересчёт промогрупп уже идёт — повтор после текущего', reason=reason)
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning('Пересчёт промогрупп не запущен: нет цикла событий', reason=reason)
            return False
        self._current_reason = reason
        self._task = loop.create_task(self._run(reason), name='promo-group-recalculation')
        return True

    async def wait(self) -> None:
        """Дождаться конца текущего прохода (для тестов и корректной остановки)."""
        if self._task is not None:
            # gather, а не голый await атрибута: тот же результат и те же исключения,
            # но CodeQL не принимает его за выражение без эффекта.
            await asyncio.gather(self._task)

    def snapshot(self) -> dict[str, object]:
        return {
            'running': self.is_running,
            'reason': self._current_reason,
            'queued': self._rerun_reason is not None,
            'last': self._last.to_dict() if self._last else None,
        }

    async def _run(self, reason: str) -> None:
        while True:
            self._current_reason = reason
            self._last = await self._run_once(reason)
            try:
                await self._announcer(self._last)
            except Exception as exc:
                logger.error('Сводка пересчёта промогрупп не отправлена', exc=exc)
            if self._rerun_reason is None:
                break
            reason, self._rerun_reason = self._rerun_reason, None
        self._current_reason = None

    async def _run_once(self, reason: str) -> RecalculationResult:
        started_at = datetime.now(UTC)
        try:
            return await self._runner(reason)
        except Exception as exc:
            logger.error('Пересчёт промогрупп упал', reason=reason, exc=exc)
            return RecalculationResult(
                reason=reason, started_at=started_at, finished_at=datetime.now(UTC), error=str(exc)
            )


promo_group_recalculation = PromoGroupRecalculation()
