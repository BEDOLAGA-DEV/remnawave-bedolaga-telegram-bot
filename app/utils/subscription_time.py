"""Сколько осталось до конца подписки — одно определение для текстов и автопродления.

Раньше каждый экран считал остаток как ``(end_date - now).days``. Это целая
часть суток: у подписки, которой осталось 1 д 23 ч, выходило «1», и тексты
читали единицу как «истекает завтра», хотя истекала она послезавтра. Порог
автопродления «за N дней» по той же причине срабатывал почти на сутки раньше.

Здесь три разных вопроса, у каждого свой ответ:

* «истекает сегодня / завтра / через N дн.», «до 18.09 (N дн.)» — календарные
  дни в зоне ``settings.TIMEZONE``, та же дата, что показана рядом;
* «Осталось: …» — точный остаток, дни и часы без отбрасывания;
* «пора ли продлевать за N дней» — сравнение длительностей, не целых суток.

Цены (доплаты за остаток периода) сюда не относятся: там округление — часть
тарифной политики и считается в своих местах.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.utils.timezone import local_date


_SECONDS_IN_HOUR = 3600


class _Texts(Protocol):
    def t(self, key: str, default: str) -> str:
        """Текст по ключу локализации; ``default`` — если ключа нет."""


class _DefaultTexts:
    """Русские тексты по умолчанию — для админских экранов без локализации."""

    def t(self, key: str, default: str) -> str:
        return default


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _now(now: datetime | None) -> datetime:
    return datetime.now(UTC) if now is None else _aware(now)


def time_left(end_date: datetime, now: datetime | None = None) -> timedelta:
    """Остаток до ``end_date``; у истёкшей подписки — ноль, не отрицательное."""
    return max(timedelta(0), _aware(end_date) - _now(now))


def local_days_until(end_date: datetime, now: datetime | None = None, tz: ZoneInfo | None = None) -> int:
    """Через сколько календарных дней наступит дата окончания: 0 — сегодня, 1 — завтра.

    Истёкшая подписка даёт 0. Дни считаются в зоне оператора, поэтому число
    всегда согласовано с датой, которую человек видит рядом.
    """
    moment = _now(now)
    end = _aware(end_date)
    if end <= moment:
        return 0
    return (local_date(end, tz) - local_date(moment, tz)).days


def days_left_rounded_up(end_date: datetime, now: datetime | None = None) -> int:
    """Начатые сутки остатка: 20 ч → 1, 1 д 23 ч → 2; у истёкшего — 0.

    Для счётчика «N дн.» без даты рядом: ненулевой остаток никогда не
    превращается в 0, который экраны читают как «истекло».
    """
    remaining = time_left(end_date, now)
    return -(-remaining // timedelta(days=1))


def ends_within_days(end_date: datetime, days: int, now: datetime | None = None) -> bool:
    """Окончание не дальше чем через ``days`` суток (уже истёкшая — тоже да).

    Порог автопродления «за N дней»: сравниваются длительности. Целые сутки
    (``.days``) при N = 3 пускали продление уже при 3 д 23 ч остатка.
    Истёкшие сюда попадают намеренно — автоплатёж подбирает только что
    истёкшие подписки; кому они не нужны, отсекает их сам.
    """
    return _aware(end_date) - _now(now) <= timedelta(days=days)


def format_time_left(texts: _Texts | None, end_date: datetime, now: datetime | None = None) -> str:
    """«1 дн. 23 ч.», «5 ч.», «42 мин.» или «истёк» — без отбрасывания неполных суток."""
    texts = texts or _DefaultTexts()
    remaining = time_left(end_date, now)
    if remaining <= timedelta(0):
        return texts.t('SUBSCRIPTION_TIME_LEFT_EXPIRED', 'истёк')

    hours = remaining.seconds // _SECONDS_IN_HOUR
    if remaining.days > 0:
        if hours == 0:
            return texts.t('SUBSCRIPTION_TIME_LEFT_DAYS', '{days} дн.').format(days=remaining.days)
        return texts.t('SUBSCRIPTION_TIME_LEFT_DAYS_HOURS', '{days} дн. {hours} ч.').format(
            days=remaining.days, hours=hours
        )
    if hours > 0:
        return texts.t('SUBSCRIPTION_TIME_LEFT_HOURS', '{hours} ч.').format(hours=hours)
    minutes = remaining.seconds // 60
    return texts.t('SUBSCRIPTION_TIME_LEFT_MINUTES', '{minutes} мин.').format(minutes=minutes)


def format_expiry_warning(texts: _Texts, end_date: datetime, now: datetime | None = None) -> str:
    """Предупреждение под сроком: «истекает сегодня/завтра» по календарю, «через минуты» в последний час."""
    remaining = time_left(end_date, now)
    if remaining <= timedelta(0):
        return ''
    if remaining < timedelta(hours=1):
        return texts.t('SUBSCRIPTION_WARNING_MINUTES', '\n🔴 истекает через несколько минут!')

    days = local_days_until(end_date, now)
    if days == 0:
        return texts.t('SUBSCRIPTION_WARNING_TODAY', '\n⚠️ истекает сегодня!')
    if days == 1:
        return texts.t('SUBSCRIPTION_WARNING_TOMORROW', '\n⚠️ истекает завтра!')
    return ''
