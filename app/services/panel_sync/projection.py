"""Обратное направление: что бот забирает из панели в свою подписку.

Раньше это делали шесть независимых мапперов — массовая синхронизация, её
мультитарифная ветка, помощник обновления, кабинетная кнопка, вход по почте и
обработчики вебхуков. Каждый переносил свой набор полей по своим правилам:
``is_trial`` читали двое из шести, лимит устройств — четверо, а «когда доверять
дате панели» у каждого было своё.

Правило одно, решение владельца (2026-09-11): **панель — истина**. Панель сама
считает, когда кончится подписка; бот — касса, он пишет в панель только при
покупке, продлении и явных действиях админа, а синхронизация панель не трогает —
«в бота пишется истина панели».

Что это значит для маппера:

* **Дата окончания, статус, трафик, сквады, лимиты трафика и устройств** берутся
  из панели при любом статусе аккаунта. Дата — при расхождении больше минуты,
  трафик — больше 0.01 ГБ.
* **Статус**: ACTIVE с датой в будущем — живая (триал в боте остаётся триалом,
  панель их не различает), LIMITED, DISABLED и EXPIRED переносятся как есть,
  ACTIVE с прошедшей датой — истекла. Панель не назвала статус — статус не
  трогаем, а по своей дате истечение доводит мониторинг.
* **Сквады** — пустой список игнорируется: он значит «панель ещё не знает», а не
  «отобрать все инбаунды».
* От устаревшего снимка полного прохода защищает его возраст
  (``snapshot_taken_at``): подписку, изменённую в боте после снимка (оплата,
  продление), снимок не трогает — иначе он откатывал бы только что оплаченный
  срок. Расход трафика и ссылки переносятся всё равно.
* Пока открыт грейс-доступ, биллинговое состояние — собственность бота.

Политики ``ROUTINE`` (фоновое чтение), ``BULK_SNAPSHOT`` (полный проход) и
``ADMIN_PULL`` (кнопка «из панели в бота») теперь одно и то же — ``PANEL_TRUTH``;
имена оставлены, чтобы точки вызова говорили, откуда пришли. ``WEBHOOK`` —
событие панели: свежее любого снимка, но подписку, намеренно отключённую в боте
(обнуление админом), не воскрешает, и истёкшей её не делает — это работа
мониторинга с его уведомлениями.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

import structlog

from app.database.models import SubscriptionStatus
from app.utils.subscription_utils import coerce_panel_device_limit
from app.utils.timezone import panel_datetime_to_utc


logger = structlog.get_logger(__name__)


#: Меньшую разницу дат считаем дрожанием часов, а не изменением.
_DATE_TOLERANCE_SECONDS = 60
#: Меньшую разницу трафика не переносим — она набегает на каждом запросе.
_TRAFFIC_TOLERANCE_GB = 0.01
#: Статусы, из которых подписка ещё может уйти в «исчерпана» или «истекла».
_RENEWABLE_STATUSES = (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)


@dataclass(frozen=True)
class ProjectionPolicy:
    """Насколько доверять панели. Готовые политики — ниже."""

    name: str
    #: Переносить ли дату окончания вообще.
    takes_date: bool = True
    #: Брать дату только у ACTIVE (у остальных там бывает искусственная дата).
    date_only_from_active: bool = True
    #: Как выводить статус: 'panel_truth' | 'webhook' | 'routine' (только для полей по умолчанию).
    status_mode: str = 'routine'
    #: Брать из панели лимит трафика (обычно его задаёт тариф).
    takes_traffic_limit: bool = False
    #: Брать из панели лимит устройств.
    takes_device_limit: bool = False
    #: Не переносить дату, пока подписка намеренно отключена в боте.
    respects_local_disable: bool = False


#: Панель — истина: дата, статус и лимиты при любом статусе аккаунта.
PANEL_TRUTH = ProjectionPolicy(
    'panel_truth',
    date_only_from_active=False,
    status_mode='panel_truth',
    takes_traffic_limit=True,
    takes_device_limit=True,
)
#: Фоновое чтение, полный проход и кнопка «из панели в бота» — одна и та же истина.
ROUTINE = replace(PANEL_TRUTH, name='routine')
BULK_SNAPSHOT = replace(PANEL_TRUTH, name='bulk_snapshot')
ADMIN_PULL = replace(PANEL_TRUTH, name='admin_pull')
#: Событие от панели: свежее любого снимка, но отключённую подписку не воскрешает.
WEBHOOK = ProjectionPolicy(
    'webhook',
    date_only_from_active=False,
    status_mode='webhook',
    takes_traffic_limit=True,
    respects_local_disable=True,
)


@dataclass(frozen=True)
class PanelSnapshot:
    """Что панель говорит про аккаунт, в терминах бота."""

    status: str | None = None
    expire_at: datetime | None = None
    traffic_used_gb: float | None = None
    traffic_limit_gb: int | None = None
    device_limit: int | None = None
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
    if used_bytes is None:
        # Расширенная схема панели прячет расход в userTraffic; плоского поля там нет.
        nested = _field(panel_user, 'userTraffic')
        if isinstance(nested, dict):
            used_bytes = nested.get('usedTrafficBytes')
    limit_bytes = _field(panel_user, 'trafficLimitBytes', 'traffic_limit_bytes')
    device_limit = _field(panel_user, 'hwidDeviceLimit', 'hwid_device_limit')
    crypto = _field(panel_user, 'subscriptionCryptoLink', 'happ_crypto_link')
    if crypto is None:
        happ = _field(panel_user, 'happ')
        if isinstance(happ, dict):
            crypto = happ.get('cryptoLink')

    status = _field(panel_user, 'status')
    # Клиент отдаёт статус перечислением, сырой ответ панели — строкой.
    status = getattr(status, 'value', status)
    return PanelSnapshot(
        status=str(status).upper() if status is not None else None,
        expire_at=_parse_date(_field(panel_user, 'expireAt', 'expire_at')),
        traffic_used_gb=(used_bytes / (1024**3)) if isinstance(used_bytes, int | float) else None,
        traffic_limit_gb=int(limit_bytes / (1024**3)) if isinstance(limit_bytes, int | float) else None,
        device_limit=coerce_panel_device_limit(device_limit) if device_limit is not None else None,
        squads=_squads(_field(panel_user, 'activeInternalSquads', 'active_internal_squads')),
        short_uuid=_field(panel_user, 'shortUuid', 'short_uuid') or None,
        subscription_url=_field(panel_user, 'subscriptionUrl', 'subscription_url') or None,
        crypto_link=crypto or None,
    )


def _next_status_from_webhook(subscription, snapshot: PanelSnapshot, *, now: datetime) -> str:
    """Статус по событию панели.

    Вебхук умеет включить подписку (панель сказала ACTIVE, срок ещё не вышел) и
    отключить (панель сказала DISABLED). Истечение он не объявляет: это делает
    мониторинг, у которого есть буфер и уведомления.
    """
    if snapshot.status == 'ACTIVE':
        end_date = panel_datetime_to_utc(subscription.end_date) if subscription.end_date else None
        if end_date is not None and end_date > now:
            return SubscriptionStatus.ACTIVE.value
    elif snapshot.status == 'DISABLED':
        return SubscriptionStatus.DISABLED.value
    return subscription.status


def _next_status_panel_truth(subscription, snapshot: PanelSnapshot, *, now: datetime) -> str:
    """Статус, как его видит панель — она истина.

    ACTIVE с датой в будущем — живая; триал в боте остаётся триалом, панель их не
    различает. ACTIVE с прошедшей датой — истекла: панель погасит аккаунт сама
    через минуту, бот не ждёт. LIMITED, DISABLED, EXPIRED — как есть. Панель не
    назвала статус (или дату не разобрать у ACTIVE) — не гадаем: по своей дате
    истечение доводит мониторинг, сверившись с панелью.
    """
    if snapshot.status == 'ACTIVE':
        if snapshot.expire_at is None:
            return subscription.status
        if snapshot.expire_at <= now:
            return SubscriptionStatus.EXPIRED.value
        if subscription.status == SubscriptionStatus.TRIAL.value:
            return subscription.status
        return SubscriptionStatus.ACTIVE.value
    if snapshot.status == 'LIMITED':
        return SubscriptionStatus.LIMITED.value
    if snapshot.status == 'DISABLED':
        return SubscriptionStatus.DISABLED.value
    if snapshot.status == 'EXPIRED':
        return SubscriptionStatus.EXPIRED.value
    return _next_status(subscription, snapshot, now=now)


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
    policy: ProjectionPolicy = ROUTINE,
    grace_open: bool = False,
    trust_status: bool = True,
    snapshot_taken_at: datetime | None = None,
) -> set[str]:
    """Перенести состояние панели в подписку. Возвращает имена изменённых полей.

    ``policy`` — насколько доверять панели (см. ROUTINE / BULK_SNAPSHOT /
    ADMIN_PULL в начале модуля).

    ``trust_status=False`` — статус не трогать вовсе. Так помечают подписку,
    только что обновлённую вебхуком: свежая оплата важнее любого снимка.

    ``snapshot_taken_at`` — когда снимок был снят. Полный проход выгружает весь
    список панели и применяет его минутами позже; если подписку за это время
    изменили (оплатили, продлили, обнулили), снимок про неё уже врёт — тогда
    биллинговые поля не трогаем вовсе, а расход и ссылки переносим.

    Ссылки на подписку (``shortUuid``, url, крипто-ссылка) переносятся всегда:
    они описывают аккаунт панели, а не биллинговое состояние, и грейсу не мешают.
    """
    moment = now or datetime.now(UTC)
    changed: set[str] = set()

    if snapshot_taken_at is not None:
        touched_at = max(
            (
                panel_datetime_to_utc(value)
                for value in (
                    getattr(subscription, 'updated_at', None),
                    getattr(subscription, 'last_webhook_update_at', None),
                )
                if value is not None
            ),
            default=None,
        )
        if touched_at is not None and touched_at > snapshot_taken_at:
            # Подписку изменили уже после того, как снимок был снят: применять
            # его поверх свежей правки — значит откатывать оплату.
            trust_status = False
            policy = replace(policy, takes_date=False, takes_traffic_limit=False, takes_device_limit=False)

    if snapshot.short_uuid and subscription.remnawave_short_uuid != snapshot.short_uuid:
        subscription.remnawave_short_uuid = snapshot.short_uuid
        changed.add('remnawave_short_uuid')
    if snapshot.subscription_url and subscription.subscription_url != snapshot.subscription_url:
        subscription.subscription_url = snapshot.subscription_url
        changed.add('subscription_url')
    if snapshot.crypto_link and subscription.subscription_crypto_link != snapshot.crypto_link:
        subscription.subscription_crypto_link = snapshot.crypto_link
        changed.add('subscription_crypto_link')

    if snapshot.traffic_used_gb is not None:
        current = subscription.traffic_used_gb or 0.0
        if abs(current - snapshot.traffic_used_gb) > _TRAFFIC_TOLERANCE_GB:
            subscription.traffic_used_gb = snapshot.traffic_used_gb
            changed.add('traffic_used_gb')

    if grace_open:
        # Грейс — временное состояние, которое бот держит сам: дату, статус и
        # сквады панель в это время не переписывает.
        return changed

    locally_disabled = subscription.status == SubscriptionStatus.DISABLED.value
    if (
        policy.takes_date
        and snapshot.expire_at is not None
        and (snapshot.status == 'ACTIVE' or not policy.date_only_from_active)
        and subscription.end_date is not None
        # Подписку обнулили в боте намеренно: старая дата из панели вернула бы
        # списанные дни.
        and not (policy.respects_local_disable and locally_disabled)
    ):
        end_date = panel_datetime_to_utc(subscription.end_date)
        if abs((end_date - snapshot.expire_at).total_seconds()) > _DATE_TOLERANCE_SECONDS:
            subscription.end_date = snapshot.expire_at
            changed.add('end_date')

    status_rules = {
        'panel_truth': _next_status_panel_truth,
        'webhook': _next_status_from_webhook,
        'routine': _next_status,
    }
    if not trust_status:
        new_status = subscription.status
    else:
        new_status = status_rules[policy.status_mode](subscription, snapshot, now=moment)
    if new_status != subscription.status:
        subscription.status = new_status
        if new_status in (SubscriptionStatus.EXPIRED.value, SubscriptionStatus.LIMITED.value):
            subscription.grace_candidate_reason = new_status
            subscription.grace_candidate_at = moment
        changed.add('status')

    # Лимиты — тоже истина панели (правка там приезжает в бота); вебхук берёт
    # только лимит трафика, как и раньше.
    if (
        policy.takes_traffic_limit
        and snapshot.traffic_limit_gb is not None
        and subscription.traffic_limit_gb != snapshot.traffic_limit_gb
    ):
        subscription.traffic_limit_gb = snapshot.traffic_limit_gb
        changed.add('traffic_limit_gb')
    if (
        policy.takes_device_limit
        and snapshot.device_limit is not None
        and subscription.device_limit != snapshot.device_limit
    ):
        subscription.device_limit = snapshot.device_limit
        changed.add('device_limit')

    # Пустой список сквадов значит «панель ещё не знает», а не «отобрать все».
    if snapshot.squads and set(snapshot.squads) != set(subscription.connected_squads or []):
        subscription.connected_squads = list(snapshot.squads)
        changed.add('connected_squads')

    return changed


def panel_status_for_new_subscription(snapshot: PanelSnapshot, *, now: datetime | None = None) -> str:
    """Статус подписки, которую бот заводит по уже существующему аккаунту панели.

    Отдельная функция, потому что подписки ещё нет — сравнивать не с чем, и всё
    решает панель: живая с будущей датой активна, с прошедшей истекла, остальное
    отключено.
    """
    moment = now or datetime.now(UTC)
    if snapshot.status == 'ACTIVE' and snapshot.expire_at is not None and snapshot.expire_at > moment:
        return SubscriptionStatus.ACTIVE.value
    if snapshot.expire_at is not None and snapshot.expire_at <= moment:
        return SubscriptionStatus.EXPIRED.value
    return SubscriptionStatus.DISABLED.value
