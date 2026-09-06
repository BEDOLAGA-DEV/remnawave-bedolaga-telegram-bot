"""Границы периода премиум-лимита.

Премиум сбрасывается по той же логике, что задана в тарифе полем
``traffic_reset_mode`` — отдельной настройки нет. Панель по этому же режиму
сбрасывает общий трафик, так что оба счётчика обнуляются согласованно.

**Почему считаем сами, а не берём `lastTrafficResetAt` панели.** Изначально
план был обратным, но снятые с боевой панели данные это опровергли: поле пустое
у большинства пользователей (панель проставляет его только после первого
фактического сброса), а там, где заполнено, смешивает периодический сброс с
ручным — в выборке три пользователя со сбросом в пределах двух минут, то есть
массовая операция бота, а не расписание. Строить границу периода на поле,
которое пустое в большинстве случаев и не отличает свои сбросы от чужих, нельзя.

`lastTrafficResetAt` при этом не выбрасывается: если панель сбросила **раньше**
расчётного срока — например, бот дёрнул сброс при смене тарифа, — период едет
за ней. См. ``resolve_period_start``.

Всё считается в UTC: колонки времени в проекте aware (``AwareDateTime``), а
панель отдаёт даты с ``Z``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


# Длина окна скользящего месяца. В админке подписано как «через 30 дней от
# первого подключения» (handlers/admin/tariffs.py), панель считает так же.
ROLLING_WINDOW_DAYS = 30

DAY = 'DAY'
WEEK = 'WEEK'
MONTH = 'MONTH'
MONTH_ROLLING = 'MONTH_ROLLING'
NO_RESET = 'NO_RESET'

RESET_MODES = frozenset({DAY, WEEK, MONTH, MONTH_ROLLING, NO_RESET})


def _as_utc(value: datetime) -> datetime:
    """Привести к UTC. Наивное время трактуем как UTC — так же, как модели."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _start_of_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def normalize_mode(mode: object) -> str:
    """Привести режим к строке из ``RESET_MODES``.

    Принимает и строку, и ``TrafficLimitStrategy`` — вызывающие получают режим
    из разных источников. Неизвестное значение трактуем как ``NO_RESET``: не
    сбросить период безопаснее, чем сбросить его невпопад и подарить лимит.
    """
    raw = getattr(mode, 'value', mode)
    if not isinstance(raw, str):
        return NO_RESET
    upper = raw.strip().upper()
    return upper if upper in RESET_MODES else NO_RESET


def rolling_period_start(anchor: datetime, now: datetime, window_days: int = ROLLING_WINDOW_DAYS) -> datetime:
    """Начало текущего окна скользящего месяца.

    Окна идут подряд от ``anchor``; берём последнее, которое уже началось. До
    первого окончания окна периодом остаётся сам ``anchor``.
    """
    anchor = _as_utc(anchor)
    now = _as_utc(now)
    if now <= anchor or window_days <= 0:
        return anchor
    elapsed_days = (now - anchor).days
    windows_passed = elapsed_days // window_days
    return anchor + timedelta(days=windows_passed * window_days)


def period_start_for_mode(mode: object, *, anchor: datetime, now: datetime) -> datetime:
    """Начало текущего периода по режиму сброса тарифа.

    ``anchor`` — точка отсчёта подписки: первое подключение, иначе начало
    подписки. Для календарных режимов он ограничивает результат снизу, чтобы
    период не начинался раньше самой подписки: иначе расход считался бы с даты,
    когда подписки ещё не существовало.
    """
    anchor = _as_utc(anchor)
    now = _as_utc(now)
    resolved = normalize_mode(mode)

    if resolved == NO_RESET:
        # Панель не сбрасывает — не сбрасываем и мы. Лимит становится
        # пожизненным, и это согласованное поведение, а не упущение.
        return anchor

    if resolved == MONTH_ROLLING:
        return rolling_period_start(anchor, now)

    if resolved == DAY:
        start = _start_of_day(now)
    elif resolved == WEEK:
        start = _start_of_day(now) - timedelta(days=now.weekday())
    else:  # MONTH
        start = _start_of_day(now).replace(day=1)

    return max(start, anchor)


def resolve_period_start(
    mode: object,
    *,
    anchor: datetime,
    now: datetime,
    panel_reset_at: datetime | None = None,
    acknowledged_panel_reset_at: datetime | None = None,
) -> datetime:
    """Начало периода с поправкой на досрочный сброс панели.

    Если панель обнулила трафик раньше расчётного срока — при смене тарифа, при
    продлении с включённым ``RESET_TRAFFIC_ON_PAYMENT``, — премиум едет следом,
    чтобы оба счётчика жили одним периодом.

    ``acknowledged_panel_reset_at`` — сброс, который мы уже учли и решили не
    переносить на премиум. Так разводится ручной сброс админа: «сбросить только
    обычный трафик» отмечает значение здесь, и премиум-период за ним не идёт.
    """
    computed = period_start_for_mode(mode, anchor=anchor, now=now)
    if panel_reset_at is None:
        return computed

    panel_reset = _as_utc(panel_reset_at)
    if acknowledged_panel_reset_at is not None and panel_reset <= _as_utc(acknowledged_panel_reset_at):
        return computed
    if panel_reset > _as_utc(now):
        # Время из будущего — рассинхрон часов панели и бота. Доверяем расчёту.
        return computed
    return max(computed, panel_reset)


def period_anchor(
    first_connected_at: datetime | None,
    subscription_start_at: datetime | None,
    *,
    fallback: datetime,
) -> datetime:
    """Точка отсчёта периодов для подписки.

    Скользящий месяц панель считает от первого подключения, поэтому оно в
    приоритете. Пока подключения не было, отсчёт ведём от начала подписки.
    """
    for candidate in (first_connected_at, subscription_start_at):
        if candidate is not None:
            return _as_utc(candidate)
    return _as_utc(fallback)
