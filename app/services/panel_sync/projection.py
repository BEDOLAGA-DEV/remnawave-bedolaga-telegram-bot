"""Обратное направление: что бот забирает из панели в свою подписку.

Раньше это делали шесть независимых мапперов — массовая синхронизация, её
мультитарифная ветка, помощник обновления, кабинетная кнопка, вход по почте и
обработчики вебхуков. Каждый переносил свой набор полей по своим правилам:
``is_trial`` читали двое из шести, лимит устройств — четверо, а «когда доверять
дате панели» у каждого было своё.

Правила, собранные в одно место:

* **Дата окончания** обновляется, только когда панель считает пользователя
  ACTIVE, и только при расхождении больше минуты. У DISABLED и EXPIRED в панели
  может лежать искусственная дата, проставленная старыми версиями бота
  («сейчас плюс минута»), — ей нельзя перезаписывать настоящий срок.
* **Статус** выводится из статуса панели и даты, но живую подписку в боте
  никогда не гасит сама синхронизация: продление могло произойти между чтением
  и записью. Гасит её мидлвара с буфером.
* **Трафик** переносится, если разошёлся больше чем на 0.01 ГБ.
* **Сквады** — панель авторитетна, но пустой список игнорируется: он значит
  «панель ещё не знает», а не «отобрать все инбаунды».
* **Лимит трафика и лимит устройств из панели НЕ читаются**: их источник —
  тариф в боте. Иначе ручная правка в панели молча меняла бы оплаченный тариф.
* Пока открыт грейс-доступ, состояние подписки трогать нельзя вовсе.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from app.database.models import SubscriptionStatus
from app.utils.timezone import panel_datetime_to_utc


logger = structlog.get_logger(__name__)


#: Меньшую разницу дат считаем дрожанием часов, а не изменением.
_DATE_TOLERANCE_SECONDS = 60
#: Меньшую разницу трафика не переносим — она набегает на каждом запросе.
_TRAFFIC_TOLERANCE_GB = 0.01


@dataclass(frozen=True)
class PanelSnapshot:
    """Что панель говорит про аккаунт, в терминах бота."""

    status: str | None = None
    expire_at: datetime | None = None
    traffic_used_gb: float | None = None
    squads: tuple[str, ...] = ()
    short_uuid: str | None = None
    subscription_url: str | None = None
    crypto_link: str | None = None


def _field(panel_user, *names):
    """Достать поле и из словаря панели, и из разобранного объекта."""
    for name in names:
        if isinstance(panel_user, dict):
            if name in panel_user:
                return panel_user[name]
        elif hasattr(panel_user, name):
            return getattr(panel_user, name)
    return None


def _parse_date(value) -> datetime | None:
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return panel_datetime_to_utc(value)
    try:
        text = str(value).strip().replace('Z', '+00:00')
        return panel_datetime_to_utc(datetime.fromisoformat(text))
    except (TypeError, ValueError):
        logger.warning('Панель прислала дату, которую не разобрать', value=value)
        return None


def _squads(value) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    uuids = []
    for squad in value:
        if isinstance(squad, dict) and squad.get('uuid'):
            uuids.append(squad['uuid'])
        elif isinstance(squad, str) and squad:
            uuids.append(squad)
    return tuple(uuids)


def read_panel_user(panel_user) -> PanelSnapshot:
    """Разобрать ответ панели — словарь или объект клиента — в снимок."""
    used_bytes = _field(panel_user, 'usedTrafficBytes', 'used_traffic_bytes')
    crypto = _field(panel_user, 'subscriptionCryptoLink', 'happ_crypto_link')
    if crypto is None:
        happ = _field(panel_user, 'happ')
        if isinstance(happ, dict):
            crypto = happ.get('cryptoLink')

    status = _field(panel_user, 'status')
    return PanelSnapshot(
        status=str(status).upper() if status is not None else None,
        expire_at=_parse_date(_field(panel_user, 'expireAt', 'expire_at')),
        traffic_used_gb=(used_bytes / (1024**3)) if isinstance(used_bytes, int | float) else None,
        squads=_squads(_field(panel_user, 'activeInternalSquads', 'active_internal_squads')),
        short_uuid=_field(panel_user, 'shortUuid', 'short_uuid') or None,
        subscription_url=_field(panel_user, 'subscriptionUrl', 'subscription_url') or None,
        crypto_link=crypto or None,
    )


def _next_status(subscription, snapshot: PanelSnapshot, *, now: datetime) -> str:
    end_date = panel_datetime_to_utc(subscription.end_date) if subscription.end_date else None

    if snapshot.status == 'ACTIVE' and end_date is not None and end_date > now:
        return SubscriptionStatus.ACTIVE.value
    if snapshot.status == 'LIMITED':
        return SubscriptionStatus.LIMITED.value
    if snapshot.status == 'DISABLED':
        return SubscriptionStatus.DISABLED.value
    if end_date is not None and end_date <= now:
        # Живую подписку синхронизация не гасит: продление могло случиться между
        # чтением панели и записью, и мы бы отобрали только что оплаченный срок.
        # Истечение доводит мидлвара, у неё для этого есть буфер.
        if subscription.status == SubscriptionStatus.ACTIVE.value:
            return subscription.status
        return SubscriptionStatus.EXPIRED.value
    return subscription.status


def project_onto_subscription(
    subscription,
    snapshot: PanelSnapshot,
    *,
    now: datetime | None = None,
    grace_open: bool = False,
) -> set[str]:
    """Перенести состояние панели в подписку. Возвращает имена изменённых полей.

    Ссылки на подписку (``shortUuid``, url, крипто-ссылка) переносятся всегда:
    они описывают аккаунт панели, а не биллинговое состояние, и грейсу не мешают.
    """
    moment = now or datetime.now(UTC)
    changed: set[str] = set()

    if snapshot.short_uuid and subscription.remnawave_short_uuid != snapshot.short_uuid:
        subscription.remnawave_short_uuid = snapshot.short_uuid
        changed.add('remnawave_short_uuid')
    if snapshot.subscription_url and subscription.subscription_url != snapshot.subscription_url:
        subscription.subscription_url = snapshot.subscription_url
        changed.add('subscription_url')
    if snapshot.crypto_link and subscription.subscription_crypto_link != snapshot.crypto_link:
        subscription.subscription_crypto_link = snapshot.crypto_link
        changed.add('subscription_crypto_link')

    if grace_open:
        # Грейс — временное состояние, которое бот держит сам; панель в это время
        # не источник истины.
        return changed

    if snapshot.expire_at is not None and snapshot.status == 'ACTIVE' and subscription.end_date is not None:
        end_date = panel_datetime_to_utc(subscription.end_date)
        if abs((end_date - snapshot.expire_at).total_seconds()) > _DATE_TOLERANCE_SECONDS:
            subscription.end_date = snapshot.expire_at
            changed.add('end_date')

    new_status = _next_status(subscription, snapshot, now=moment)
    if new_status != subscription.status:
        subscription.status = new_status
        if new_status in (SubscriptionStatus.EXPIRED.value, SubscriptionStatus.LIMITED.value):
            subscription.grace_candidate_reason = new_status
            subscription.grace_candidate_at = moment
        changed.add('status')

    if snapshot.traffic_used_gb is not None:
        current = subscription.traffic_used_gb or 0.0
        if abs(current - snapshot.traffic_used_gb) > _TRAFFIC_TOLERANCE_GB:
            subscription.traffic_used_gb = snapshot.traffic_used_gb
            changed.add('traffic_used_gb')

    # Пустой список сквадов значит «панель ещё не знает», а не «отобрать все».
    if snapshot.squads and set(snapshot.squads) != set(subscription.connected_squads or []):
        subscription.connected_squads = list(snapshot.squads)
        changed.add('connected_squads')

    return changed
