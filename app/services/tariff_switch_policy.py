"""Правила смены тарифа, общие для кабинета, Mini App и бота.

Три флоу смены тарифа написаны независимо и одинаково ошибались:

* остаток считался как ``(end_date - now).days`` — целая часть. У подписки,
  которой осталось меньше суток, остаток выходил нулевым, доплата за
  переключение обнулялась, и в последний день можно было бесплатно прыгать
  между тарифами. Заодно ломался запрет повышения тарифа: при нулевой
  доплате переключение всегда выглядело понижением.
* использованный трафик обнулялся при ЛЮБОМ переключении. В паре с
  бесплатным прыжком это давало неограниченный трафик: A → B → A, и счётчик
  каждый раз с нуля.

* после переключения подписка оставалась в статусе ``limited``, даже если
  новый тариф давал трафик с запасом. При открытом грейсе (Telegram-only)
  обычное обновление панели такой статус бережёт, и человек, оплативший
  тариф больше, оставался в грейсе до его конца.

Здесь эти правила в одном месте, чтобы четвёртый флоу не завёл их заново.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import settings
from app.database.models import SubscriptionStatus


def remaining_days_for_switch(end_date: datetime | None, now: datetime | None = None) -> int:
    """Сколько дней остатка оплачивать при переключении тарифа.

    Живая подписка стоит минимум один день: остаток в несколько часов — это не
    ноль, иначе переключение достаётся бесплатно. Полные сутки округляются вниз,
    как и раньше, — существующие цены не меняются.

    Истёкшая подписка (или без даты окончания) даёт 0: её путь — покупка нового
    тарифа, а не переключение, и сами флоу это проверяют отдельно.
    """
    if end_date is None:
        return 0

    moment = now or datetime.now(UTC)
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    delta = end_date - moment
    if delta.total_seconds() <= 0:
        return 0
    return max(1, delta.days)


def should_reset_used_traffic(upgrade_cost_kopeks: int) -> bool:
    """Обнулять ли счётчик трафика при переключении тарифа.

    Только если переключение оплачено. Бесплатное переключение (понижение
    тарифа, суточный → суточный) новую квоту не покупает, а обнуление счётчика
    при нём превращалось в бесконечный трафик: понизил тариф — счётчик с нуля,
    вернулся обратно — ещё раз.

    Общий выключатель ``RESET_TRAFFIC_ON_TARIFF_SWITCH`` по-прежнему главнее:
    выключен — не обнуляем никогда.
    """
    if not settings.RESET_TRAFFIC_ON_TARIFF_SWITCH:
        return False
    return upgrade_cost_kopeks > 0


def lift_traffic_limit_after_switch(subscription, now: datetime | None = None) -> bool:
    """Снять статус «трафик кончился», если новый тариф даёт трафик.

    Вызывать после того, как переключение выставило новый лимит, счётчик
    и дату окончания. Трогает только ``limited``: ``disabled`` ставит
    администратор или другая политика, ``expired`` лечится покупкой, а не
    переключением. Подписки в режиме учёта трафика ``managed``/``migrating``
    статус квоты получают от учёта трафика, их не трогаем.

    Возвращает True, если статус изменён.
    """
    if subscription.status != SubscriptionStatus.LIMITED.value:
        return False
    if getattr(subscription, 'traffic_accounting_mode', None) in ('managed', 'migrating'):
        return False

    moment = now or datetime.now(UTC)
    end_date = subscription.end_date
    if end_date is None:
        return False
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    if end_date <= moment:
        return False

    limit_gb = subscription.traffic_limit_gb or 0
    used_gb = subscription.traffic_used_gb or 0.0
    if limit_gb > 0 and used_gb >= limit_gb:
        return False

    subscription.status = SubscriptionStatus.ACTIVE.value
    return True
