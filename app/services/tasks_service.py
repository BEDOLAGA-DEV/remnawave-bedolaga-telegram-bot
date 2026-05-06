"""Сервис системы заданий с наградами.

Архитектура:
- Внешние действия (покупка тарифа, реферал, трафик и т.д.) триггерят
  ``record_event(...)`` через event_emitter или прямые вызовы.
- ``record_event`` для каждого активного задания пользователя считает прогресс
  под конкретный тип задания (см. ``_apply_event_to_progress``).
- При достижении ``target_value`` прогресс помечается ``completed_at``.
- Пользователь вызывает ``claim_reward(...)`` чтобы получить награду.

Многоуровневые задания: ``parent_task_id`` указывает на предыдущий уровень.
Уровень N+1 виден пользователю только после успешного ``claim`` уровня N.

Триал-юзеры (subscription.is_trial=True без платных подписок) не получают
прогресс — ``_user_eligible`` возвращает False.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud import tasks as tasks_crud
from app.database.models import (
    Subscription,
    Tariff,
    Task,
    TaskRewardType,
    TaskType,
    TaskUserAudience,
    User,
    UserTaskProgress,
)


logger = structlog.get_logger(__name__)


# ===========================================================================
# Eligibility helpers
# ===========================================================================


async def _user_eligible(db: AsyncSession, user: User) -> bool:
    """Eligibility пользователя для системы заданий.

    По требованию: триал-юзеры (есть только триал-подписки) НЕ получают доступ
    к выполнению заданий (`выполнение заданий триал юзерам недоступно`).

    Юзеры без подписок вообще — допускаем (могут выполнять реферал/гифт/spend задания).
    Юзеры с хотя бы одной платной подпиской — допускаем.

    Per-task-type фильтр (например, ``purchase_tariff`` payload помечается ``is_trial``)
    дополнительно блокирует засчитывание trial-конверсий ретроактивно.
    """
    if not user.subscriptions:
        return True
    has_paid = any(not getattr(sub, 'is_trial', False) for sub in user.subscriptions)
    return has_paid


def _user_audience_for(user: User) -> str:
    """Определяет аудиторию пользователя для фильтрации заданий."""
    has_telegram = user.telegram_id is not None
    has_email = bool(getattr(user, 'email', None))
    if has_telegram and has_email:
        return 'both'
    if has_telegram:
        return TaskUserAudience.TELEGRAM.value
    if has_email:
        return TaskUserAudience.EMAIL.value
    return 'both'


def _user_promo_group_id(user: User) -> int | None:
    pg = getattr(user, 'promo_group', None)
    if pg is None:
        return None
    return pg.id


def _is_within_period(task: Task, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if task.starts_at is not None and task.starts_at > now:
        return False
    if task.ends_at is not None and task.ends_at < now:
        return False
    return True


def _audience_matches(task: Task, user_audience: str) -> bool:
    if task.user_audience == TaskUserAudience.BOTH.value:
        return True
    return task.user_audience == user_audience


def _promo_group_matches(task: Task, user_promo_group_id: int | None) -> bool:
    if task.promo_group_id is None:
        return True
    return task.promo_group_id == user_promo_group_id


async def _parent_completed_and_claimed(db: AsyncSession, *, user_id: int, parent_task_id: int) -> bool:
    """Проверяет, что предыдущий уровень в цепочке выполнен и награда получена."""
    progress = await tasks_crud.get_progress(db, user_id=user_id, task_id=parent_task_id)
    if progress is None:
        return False
    return progress.claimed_at is not None


async def get_available_tasks_for_user(db: AsyncSession, user: User) -> list[tuple[Task, UserTaskProgress | None]]:
    """Возвращает список доступных заданий для пользователя с текущим прогрессом.

    Отфильтровывает скрытые (по audience/promo_group/period/parent).
    """
    if not await _user_eligible(db, user):
        return []

    audience = _user_audience_for(user)
    promo_id = _user_promo_group_id(user)
    candidates = await tasks_crud.list_active_tasks_for_user(db, user_audience=audience, promo_group_id=promo_id)

    progress_map: dict[int, UserTaskProgress] = {}
    if candidates:
        progress_records = await tasks_crud.list_user_progress(db, user_id=user.id, task_ids=[t.id for t in candidates])
        progress_map = {p.task_id: p for p in progress_records}

    visible: list[tuple[Task, UserTaskProgress | None]] = []
    for task in candidates:
        if not _is_within_period(task):
            continue
        if not _audience_matches(task, audience):
            continue
        if not _promo_group_matches(task, promo_id):
            continue
        if task.parent_task_id is not None:
            parent_done = await _parent_completed_and_claimed(db, user_id=user.id, parent_task_id=task.parent_task_id)
            if not parent_done:
                continue
        visible.append((task, progress_map.get(task.id)))

    return visible


# ===========================================================================
# Event recording (вызывается из бизнес-логики при событиях)
# ===========================================================================


async def record_event(
    db: AsyncSession,
    *,
    user_id: int,
    event_type: TaskType | str,
    payload: dict[str, Any] | None = None,
) -> list[UserTaskProgress]:
    """Регистрирует событие и обновляет прогресс по соответствующим заданиям.

    Возвращает список прогрессов, которые были изменены (для последующих
    уведомлений / триггеров).
    """
    payload = payload or {}
    event_value = event_type.value if isinstance(event_type, TaskType) else event_type

    # Загружаем юзера со связями
    user_q = await db.execute(
        select(User)
        .options(
            selectinload(User.subscriptions),
            selectinload(User.promo_group),
        )
        .where(User.id == user_id)
    )
    user = user_q.scalar_one_or_none()
    if user is None:
        return []

    if not await _user_eligible(db, user):
        return []

    audience = _user_audience_for(user)
    promo_id = _user_promo_group_id(user)

    # Берём только те задания, тип которых совпадает с событием.
    # Это важно для перфоманса — иначе пришлось бы пробегать все активные.
    candidates = await tasks_crud.list_active_tasks_for_user(db, user_audience=audience, promo_group_id=promo_id)
    matching = [t for t in candidates if t.task_type == event_value and _is_within_period(t)]
    if not matching:
        return []

    changed: list[UserTaskProgress] = []
    for task in matching:
        # Multi-level: пропустить, если родитель не зачищен
        if task.parent_task_id is not None:
            parent_done = await _parent_completed_and_claimed(db, user_id=user.id, parent_task_id=task.parent_task_id)
            if not parent_done:
                continue
        progress = await _apply_event_to_progress(db, user, task, payload)
        if progress is not None:
            changed.append(progress)

    # Пишем изменения в текущую транзакцию через flush; commit делает caller —
    # это сохраняет атомарность операции, в рамках которой триггерится событие
    # (покупка, реферал, etc).
    if changed:
        await db.flush()
    return changed


async def _apply_event_to_progress(
    db: AsyncSession, user: User, task: Task, payload: dict[str, Any]
) -> UserTaskProgress | None:
    """Применяет событие к прогрессу конкретного задания.

    Возвращает обновлённый ``UserTaskProgress`` либо None если событие не подошло
    (например, тариф в задании не совпал с купленным).

    Использует FOR UPDATE-lock на progress row для серилизации параллельных обновлений
    (защита от lost-update / double-credit на gонке).
    """
    progress, created = await tasks_crud.get_or_create_progress(db, user_id=user.id, task_id=task.id)
    if not created:
        # Берём lock на существующую строку, чтобы исключить race condition
        locked = await tasks_crud.get_progress_by_id_for_update(db, progress.id)
        if locked is not None:
            progress = locked

    if progress.completed_at is not None:
        # Уже выполнено, повторно не обновляем
        return None

    value, target_meta_match, mode = _compute_increment(task, payload)
    if not target_meta_match:
        return None
    if value == 0:
        return None

    if mode == 'absolute':
        # Установить абсолютное значение (но не больше target_value)
        new_value = min(value, task.target_value)
        # Не уменьшаем прогресс назад (если admin отозвал тариф — оставим что есть)
        if new_value <= progress.current_value:
            return None
    else:
        new_value = min(progress.current_value + value, task.target_value)

    if new_value == progress.current_value:
        return None
    progress.current_value = new_value
    progress.updated_at = datetime.now(UTC)

    if progress.current_value >= task.target_value:
        await tasks_crud.mark_progress_completed(db, progress)

    return progress


def _compute_increment(task: Task, payload: dict[str, Any]) -> tuple[int, bool, str]:
    """Возвращает ``(value, target_meta_match, mode)``.

    ``mode``:
    - ``'increment'`` — прибавить ``value`` к ``current_value`` (накопительные счётчики)
    - ``'absolute'``  — установить ``value`` как абсолютное (например, кол-во активных подписок)

    ``target_meta_match`` — соответствует ли событие требованиям задания
    (например, конкретный ``tariff_id`` для PURCHASE_TARIFF). Если False — событие игнорируется.

    Триал-юзер фильтруется отдельно через ``_user_eligible``; здесь дополнительно блокируем
    события, помеченные ``payload['is_trial']=True``, чтобы конверсия trial→paid не давала
    ретроактивно зачитанный прогресс.
    """
    ttype = task.task_type
    meta = task.target_meta or {}

    if ttype == TaskType.PURCHASE_TARIFF.value:
        if payload.get('is_trial'):
            return 0, False, 'increment'
        target_tariff_id = meta.get('tariff_id')
        if target_tariff_id is None:
            return 0, False, 'increment'
        if int(payload.get('tariff_id') or 0) != int(target_tariff_id):
            return 0, False, 'increment'
        return 1, True, 'increment'

    if ttype == TaskType.SUBSCRIBE_CHANNEL.value:
        target_channel_id = meta.get('channel_id')
        if target_channel_id is None:
            return 0, False, 'absolute'
        if str(payload.get('channel_id') or '') != str(target_channel_id):
            return 0, False, 'absolute'
        # Подписался — задание выполнено сразу (1 канал = 1 шаг к цели)
        return task.target_value, True, 'absolute'

    if ttype == TaskType.TRAFFIC_USED.value:
        # TRAFFIC_USED обрабатывается ТОЛЬКО через update_traffic_progress (calendar-month
        # windowing + per-user aggregate). Direct record_event(...TRAFFIC_USED) — no-op.
        return 0, False, 'absolute'

    if ttype == TaskType.REFERRALS_INVITED.value:
        return 1, True, 'increment'

    if ttype == TaskType.PURCHASE_PERIOD.value:
        if payload.get('is_trial'):
            return 0, False, 'increment'
        target_period = int(meta.get('period_days') or 0)
        period = int(payload.get('period_days') or 0)
        if period < target_period:
            return 0, False, 'increment'
        return 1, True, 'increment'

    if ttype == TaskType.SPEND_AMOUNT.value:
        if payload.get('is_trial'):
            return 0, False, 'increment'
        amount_kopeks = int(payload.get('amount_kopeks') or 0)
        if amount_kopeks <= 0:
            return 0, True, 'increment'
        return amount_kopeks, True, 'increment'

    if ttype == TaskType.MULTI_TARIFF.value:
        # Абсолютное число активных платных подписок пользователя в текущий момент.
        count = int(payload.get('active_paid_subscriptions') or 0)
        if count <= 0:
            return 0, True, 'absolute'
        return count, True, 'absolute'

    if ttype == TaskType.GIFT_PURCHASED.value:
        # Достаточно одного подарка — устанавливаем target_value сразу
        return task.target_value, True, 'absolute'

    if ttype == TaskType.GIFTS_COUNT.value:
        return 1, True, 'increment'

    return 0, False, 'increment'

    return 0, False


# ===========================================================================
# Claim reward
# ===========================================================================


async def claim_reward(
    db: AsyncSession,
    *,
    user_id: int,
    task_id: int,
    chosen_subscription_id: int | None = None,
    chosen_reward_type: TaskRewardType | str | None = None,
) -> dict[str, Any]:
    """Выдаёт награду пользователю за выполненное задание.

    ``chosen_subscription_id`` — для multi-tariff: какой подписке начислить дни.
    ``chosen_reward_type`` — если задание ``allow_user_choice=True``, юзер
    может выбрать тип награды.

    Возвращает meta-словарь о выданной награде. Бросает ValueError при
    некорректном вызове (не выполнено, уже выдано, и т.д.).
    """
    progress = await tasks_crud.get_progress_for_update(db, user_id=user_id, task_id=task_id)
    if progress is None:
        raise ValueError('progress_not_found')
    if progress.completed_at is None:
        raise ValueError('not_completed')
    if progress.claimed_at is not None:
        raise ValueError('already_claimed')

    task_q = await db.execute(select(Task).where(Task.id == task_id))
    task = task_q.scalar_one_or_none()
    if task is None:
        raise ValueError('task_not_found')

    user_q = await db.execute(select(User).options(selectinload(User.subscriptions)).where(User.id == user_id))
    user = user_q.scalar_one_or_none()
    if user is None:
        raise ValueError('user_not_found')

    # Триал-юзер не может клеймить
    if not await _user_eligible(db, user):
        raise ValueError('user_not_eligible')

    # chosen_reward_type разрешён только если task.allow_user_choice=True. Без этой проверки
    # юзер мог бы подменить balance-награду на subscription_days (или наоборот) и получить
    # значение, не предусмотренное админом. См. claim_reward в tasks_service.
    chosen_value = (
        chosen_reward_type.value if isinstance(chosen_reward_type, TaskRewardType) else chosen_reward_type
    )
    if chosen_value is not None and chosen_value != task.reward_type and not task.allow_user_choice:
        raise ValueError('user_choice_not_allowed')

    reward_type = chosen_value or task.reward_type

    granted_meta: dict[str, Any] = {'type': reward_type}

    if reward_type == TaskRewardType.BALANCE.value:
        granted_meta.update(await _grant_balance_reward(db, user=user, task=task))
    elif reward_type == TaskRewardType.SUBSCRIPTION_DAYS.value:
        granted_meta.update(
            await _grant_subscription_days_reward(
                db,
                user=user,
                task=task,
                chosen_subscription_id=chosen_subscription_id,
            )
        )
    else:
        raise ValueError(f'unknown_reward_type:{reward_type}')

    await tasks_crud.mark_progress_claimed(db, progress, reward_granted_meta=granted_meta)
    await db.commit()
    logger.info(
        'Награда за задание выдана',
        user_id=user_id,
        task_id=task_id,
        reward_type=reward_type,
        meta=granted_meta,
    )
    return granted_meta


async def _grant_balance_reward(db: AsyncSession, *, user: User, task: Task) -> dict[str, Any]:
    """Начисляет деньги на баланс пользователя через ``add_user_balance``.

    Не делает commit — caller (``claim_reward``) сам закрывает транзакцию.
    """
    from app.database.crud.user import add_user_balance
    from app.database.models import PaymentMethod, TransactionType

    amount_kopeks = int(task.reward_value or 0)
    if amount_kopeks <= 0:
        raise ValueError('invalid_reward_amount')

    old_balance = user.balance_kopeks

    # add_user_balance берёт FOR UPDATE на user, создаёт транзакцию.
    # transaction_type=DEPOSIT (единственный безопасный non-monetary тип в проекте — у
    # TransactionType нет MANUAL). payment_method=MANUAL исключает транзакцию из revenue
    # графиков (см. REAL_PAYMENT_METHODS).
    success = await add_user_balance(
        db,
        user,
        amount_kopeks,
        description=f'Награда за задание #{task.id}',
        create_transaction=True,
        transaction_type=TransactionType.DEPOSIT,
        payment_method=PaymentMethod.MANUAL,
        commit=False,
    )
    if not success:
        raise ValueError('balance_grant_failed')

    # После lock_for_update в add_user_balance объект user обновлён в session.
    await db.refresh(user)

    return {
        'value': amount_kopeks,
        'old_balance': old_balance,
        'new_balance': user.balance_kopeks,
    }


async def _grant_subscription_days_reward(
    db: AsyncSession,
    *,
    user: User,
    task: Task,
    chosen_subscription_id: int | None,
) -> dict[str, Any]:
    """Начисляет бонусные дни на платную подписку.

    Логика выбора подписки:
    1. Если у задания ``reward_meta['tariff_id']`` задан — ищем платную подписку
       пользователя с этим тарифом.
    2. Иначе если у юзера несколько платных подписок и ``chosen_subscription_id``
       не задан — ошибка ``need_choose_subscription``.
    3. Иначе — единственная платная подписка.

    Количество дней берётся из ``task.reward_value`` (приоритетно), либо из
    ``Tariff.bonus_days_per_purchase`` если ``reward_value=0`` и задан target tariff.
    """
    from datetime import timedelta

    from app.database.models import SubscriptionStatus

    # Только активные платные подписки — продлевать expired/disabled нельзя
    active_paid_subs = [
        s
        for s in user.subscriptions
        if not getattr(s, 'is_trial', False) and getattr(s, 'status', None) == SubscriptionStatus.ACTIVE.value
    ]
    if not active_paid_subs:
        raise ValueError('no_paid_subscription')

    target_tariff_id = (task.reward_meta or {}).get('tariff_id')
    candidate_subs: list[Subscription]
    if target_tariff_id is not None:
        candidate_subs = [s for s in active_paid_subs if s.tariff_id == int(target_tariff_id)]
        if not candidate_subs:
            raise ValueError('no_subscription_with_target_tariff')
    else:
        candidate_subs = active_paid_subs

    if chosen_subscription_id is not None:
        chosen = next((s for s in candidate_subs if s.id == int(chosen_subscription_id)), None)
        if chosen is None:
            raise ValueError('chosen_subscription_invalid')
        target_sub = chosen
    elif len(candidate_subs) == 1:
        target_sub = candidate_subs[0]
    else:
        raise ValueError('need_choose_subscription')

    # Берём FOR UPDATE на выбранную подписку — защита от lost-update между
    # параллельными claim'ами на одной подписке (например, два task с разными reward).
    locked_sub_q = await db.execute(
        select(Subscription)
        .where(Subscription.id == target_sub.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_sub = locked_sub_q.scalar_one_or_none()
    if locked_sub is None:
        raise ValueError('chosen_subscription_invalid')
    target_sub = locked_sub

    # Количество дней: task.reward_value > 0 имеет приоритет;
    # иначе fallback на Tariff.bonus_days_per_purchase (target → выбранной подписки).
    days = int(task.reward_value or 0)
    if days <= 0 and target_tariff_id is not None:
        tariff_q = await db.execute(select(Tariff).where(Tariff.id == int(target_tariff_id)))
        tariff = tariff_q.scalar_one_or_none()
        if tariff is not None:
            days = int(getattr(tariff, 'bonus_days_per_purchase', 0) or 0)
    if days <= 0 and target_sub.tariff_id is not None:
        tariff_q = await db.execute(select(Tariff).where(Tariff.id == target_sub.tariff_id))
        tariff = tariff_q.scalar_one_or_none()
        if tariff is not None:
            days = int(getattr(tariff, 'bonus_days_per_purchase', 0) or 0)
    if days <= 0:
        raise ValueError('invalid_reward_days')

    old_end_date = target_sub.end_date
    target_sub.end_date = (
        target_sub.end_date + timedelta(days=days)
        if target_sub.end_date is not None
        else datetime.now(UTC) + timedelta(days=days)
    )
    target_sub.updated_at = datetime.now(UTC)

    return {
        'value': days,
        'subscription_id': target_sub.id,
        'tariff_id': target_sub.tariff_id,
        'old_end_date': old_end_date.isoformat() if old_end_date else None,
        'new_end_date': target_sub.end_date.isoformat() if target_sub.end_date else None,
    }


# ===========================================================================
# Helpers (для интеграции из других сервисов)
# ===========================================================================


def _current_month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def update_traffic_progress(
    db: AsyncSession,
    *,
    user_id: int,
    traffic_used_gb_total: float | None = None,
) -> list[UserTaskProgress]:
    """Обновляет прогресс по заданиям типа TRAFFIC_USED (за календарный месяц на пользователя).

    ``traffic_used_gb_total`` — суммарный трафик по всем платным подпискам пользователя.
    Если ``None`` — считаем сами через ``user.subscriptions``.

    Логика per-user (НЕ per-subscription):
    - При пересечении календарного месяца (UTC): фиксируем ``baseline_value =
      текущий cumulative`` и ``current_value = 0`` (новый период начался с нуля).
    - На каждом обновлении: ``current_value = min(target, max(0, current - baseline))``.
    - Если cumulative уменьшился (panel reset / удаление подписки): сдвигаем
      ``baseline`` вниз, чтобы прогресс не уехал в минус и продолжал считаться корректно.
    """
    user_q = await db.execute(
        select(User).options(selectinload(User.subscriptions), selectinload(User.promo_group)).where(User.id == user_id)
    )
    user = user_q.scalar_one_or_none()
    if user is None:
        return []
    if not await _user_eligible(db, user):
        return []

    # Считаем суммарный использованный трафик по платным подпискам
    if traffic_used_gb_total is None:
        traffic_used_gb_total = sum(
            float(getattr(sub, 'traffic_used_gb', 0) or 0)
            for sub in user.subscriptions
            if not getattr(sub, 'is_trial', False)
        )

    audience = _user_audience_for(user)
    promo_id = _user_promo_group_id(user)

    candidates = await tasks_crud.list_active_tasks_for_user(db, user_audience=audience, promo_group_id=promo_id)
    matching = [t for t in candidates if t.task_type == TaskType.TRAFFIC_USED.value and _is_within_period(t)]
    if not matching:
        return []

    month_start = _current_month_start()
    used_gb_int = int(traffic_used_gb_total)  # GB округляем вниз
    changed: list[UserTaskProgress] = []

    for task in matching:
        if task.parent_task_id is not None:
            parent_done = await _parent_completed_and_claimed(db, user_id=user.id, parent_task_id=task.parent_task_id)
            if not parent_done:
                continue

        progress, created = await tasks_crud.get_or_create_progress(db, user_id=user.id, task_id=task.id)
        if not created:
            locked = await tasks_crud.get_progress_by_id_for_update(db, progress.id)
            if locked is not None:
                progress = locked

        if progress.completed_at is not None:
            continue

        # Сброс при пересечении календарного месяца
        if progress.period_started_at is None or progress.period_started_at < month_start:
            progress.period_started_at = month_start
            progress.baseline_value = used_gb_int
            progress.current_value = 0
            progress.updated_at = datetime.now(UTC)

        delta = used_gb_int - progress.baseline_value
        if delta < 0:
            # Cumulative ушёл вниз (удалили подписку, panel reset). Сдвигаем baseline,
            # сохраняя current_value (юзер уже потратил эти GB в этом месяце).
            progress.baseline_value = used_gb_int
            progress.updated_at = datetime.now(UTC)
            continue

        new_value = min(delta, task.target_value)
        if new_value != progress.current_value:
            progress.current_value = new_value
            progress.updated_at = datetime.now(UTC)
            changed.append(progress)

        if progress.current_value >= task.target_value:
            await tasks_crud.mark_progress_completed(db, progress)

    if changed:
        await db.flush()
    return changed


async def trigger_paid_purchase_tasks(
    db: AsyncSession,
    *,
    user_id: int,
    tariff_id: int | None,
    period_days: int,
    amount_kopeks: int,
    subscription_id: int | None,
    is_trial: bool = False,
) -> None:
    """Триггерит PURCHASE_TARIFF / PURCHASE_PERIOD / MULTI_TARIFF после платной покупки.

    Используется из bot handlers и cabinet submit_purchase. Делает явный db.commit() —
    record_event сам коммит не делает.

    Безопасно — все исключения логируются как warning, но не пробрасываются.
    """
    try:
        from app.database.crud.subscription import get_active_subscriptions_by_user_id
        from app.database.models import SubscriptionStatus

        common_payload = {
            'is_trial': is_trial,
            'tariff_id': tariff_id,
            'period_days': period_days,
            'amount_kopeks': amount_kopeks,
            'subscription_id': subscription_id,
        }

        await record_event(
            db, user_id=user_id, event_type=TaskType.PURCHASE_TARIFF, payload=common_payload
        )
        await record_event(
            db, user_id=user_id, event_type=TaskType.PURCHASE_PERIOD, payload=common_payload
        )

        try:
            user_subs = await get_active_subscriptions_by_user_id(db, user_id)
            active_paid = sum(
                1
                for s in user_subs
                if not getattr(s, 'is_trial', False)
                and getattr(s, 'status', None) == SubscriptionStatus.ACTIVE.value
            )
        except Exception:
            active_paid = 0

        if active_paid > 0:
            await record_event(
                db,
                user_id=user_id,
                event_type=TaskType.MULTI_TARIFF,
                payload={'active_paid_subscriptions': active_paid, **common_payload},
            )
        await db.commit()
    except Exception as exc:
        # Сессия может быть в poisoned state (например, IntegrityError race на uq_user_task) —
        # явно откатываем, чтобы caller мог продолжить работу с сессией.
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning(
            'Tasks: ошибка trigger_paid_purchase_tasks',
            user_id=user_id,
            error=exc,
        )


async def has_available_tasks(db: AsyncSession, user: User) -> bool:
    """Быстрый чек: есть ли у пользователя хотя бы одно доступное задание.

    Используется фронтом для условного показа вкладки «Задания».
    """
    visible = await get_available_tasks_for_user(db, user)
    return len(visible) > 0


async def count_completed_unclaimed(db: AsyncSession, *, user_id: int) -> int:
    """Количество выполненных, но не полученных наград (для бейджа на иконке)."""
    from sqlalchemy import func

    result = await db.execute(
        select(func.count())
        .select_from(UserTaskProgress)
        .where(
            UserTaskProgress.user_id == user_id,
            UserTaskProgress.completed_at.isnot(None),
            UserTaskProgress.claimed_at.is_(None),
        )
    )
    return int(result.scalar() or 0)
