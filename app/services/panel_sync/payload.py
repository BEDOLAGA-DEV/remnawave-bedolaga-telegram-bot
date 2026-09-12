"""Что бот отправляет в панель про одну подписку.

Один набор полей на все точки записи. До консолидации он собирался руками в
тринадцати местах, четыре из которых были буквально одинаковыми, и каждое место
расходилось по мелочи: пустые сквады, суффикс имени, перевод гигабайтов.

Поля возвращаются готовыми kwargs для ``RemnaWaveAPI.create_user`` и
``RemnaWaveAPI.update_user`` — чтобы у вызывающего не было соблазна что-то
дособрать по дороге.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import settings
from app.database.models import SubscriptionStatus, UserStatus as OwnerStatus
from app.external.remnawave_api import TrafficLimitStrategy, UserStatus
from app.services.panel_sync.expiry import panel_expire_at
from app.services.panel_sync.liveness import is_subscription_expired, is_subscription_live
from app.services.panel_sync.tags import resolve_panel_user_tag
from app.services.panel_sync.traffic_strategy import get_traffic_reset_strategy
from app.utils.subscription_utils import resolve_hwid_device_limit_for_payload


_BYTES_IN_GB = 1024**3

#: Что панель принимает в ``status`` при обновлении (контракт 3.4.3, ошибка A089:
#: «LIMITED и EXPIRED нельзя выставить вручную» — их панель выводит сама из даты
#: и расхода). При создании принимаются все четыре.
_PATCHABLE_STATUSES = frozenset({UserStatus.ACTIVE, UserStatus.DISABLED})


@dataclass(frozen=True)
class PanelPayload:
    """Готовые поля запроса. Имена совпадают с параметрами ``RemnaWaveAPI``."""

    username: str
    status: UserStatus
    traffic_limit_bytes: int
    traffic_limit_strategy: TrafficLimitStrategy | str
    telegram_id: int | None
    email: str | None
    description: str
    active_internal_squads: tuple[str, ...]
    hwid_device_limit: int | None
    external_squad_uuid: str | None
    tag: str | None
    end_date: datetime
    is_live: bool

    def _common(self) -> dict:
        kwargs: dict = {
            'traffic_limit_bytes': self.traffic_limit_bytes,
            'traffic_limit_strategy': self.traffic_limit_strategy,
            'telegram_id': self.telegram_id,
            'email': self.email,
            'description': self.description,
        }
        if self.hwid_device_limit is not None:
            kwargs['hwid_device_limit'] = self.hwid_device_limit
        # Панель отвечает ошибкой A039 на null в externalSquadUuid, поэтому
        # отсутствие внешнего сквада — это отсутствие поля, а не null.
        if self.external_squad_uuid is not None:
            kwargs['external_squad_uuid'] = self.external_squad_uuid
        if self.tag is not None:
            kwargs['tag'] = self.tag
        return kwargs

    def create_kwargs(self, *, now: datetime | None = None) -> dict:
        kwargs = self._common()
        kwargs['username'] = self.username
        kwargs['status'] = self.status
        # У нового аккаунта снимать нечего, поэтому пустой список безопасен, а
        # поле в POST обязательно.
        kwargs['active_internal_squads'] = list(self.active_internal_squads)
        kwargs['expire_at'] = panel_expire_at(self.end_date, is_active=self.is_live, creating=True, now=now)
        return kwargs

    def update_kwargs(
        self,
        *,
        user_id: int,
        panel_current: datetime | None = None,
        now: datetime | None = None,
        only_fields: set[str] | None = None,
    ) -> dict:
        """Поля для PATCH.

        ``only_fields`` — для узких правок (описание, сквады, лимит трафика):
        всё остальное в панель не уезжает. Без него в панель отправляется полное
        состояние подписки.
        """
        kwargs = self._common()
        # Истечение и исчерпанный трафик панель выводит сама и задать их не даёт;
        # DISABLED вместо них значил бы «отключена администратором». На уже
        # истёкшем или лимитированном аккаунте панель 3.4.3 такой PATCH молча
        # игнорирует (проверено на стенде), а вот аккаунт, который она ещё считает
        # живым (в боте истекла, в панели дата в будущем), превращала в
        # «отключённый админом» — и импорт нёс это «отключена» обратно в бота,
        # где такую подписку уже нельзя продлить из кабинета.
        if self.status in _PATCHABLE_STATUSES:
            kwargs['status'] = self.status
        # Пустой список в PATCH значит «снять все инбаунды». В базе пустота —
        # это дефолт колонки и результат сброса подписки, а не решение админа;
        # намеренный отзыв делается литеральным [] в других местах.
        if self.active_internal_squads:
            kwargs['active_internal_squads'] = list(self.active_internal_squads)
        expire_at = panel_expire_at(
            self.end_date,
            is_active=self.is_live,
            creating=False,
            now=now,
            panel_current=panel_current,
        )
        if expire_at is not None:
            kwargs['expire_at'] = expire_at
        if only_fields is not None:
            kwargs = {key: value for key, value in kwargs.items() if key in only_fields}
        kwargs['user_id'] = user_id
        return kwargs


def resolve_panel_status(user, subscription, *, is_live: bool, now: datetime) -> UserStatus:
    """Статус аккаунта в панели, каким его видит бот.

    ACTIVE — живая подписка; DISABLED — настоящее отключение: заблокированный
    владелец, выключенная в боте подписка (обнуление админом, PENDING) или
    противоречивое «истекла, но дата в будущем» — его гасим, как и раньше.
    EXPIRED и LIMITED — состояния, которые панель ведёт сама: при обновлении
    они не отправляются (``update_kwargs``), при создании — идут как есть
    (``POST /api/users`` принимает все четыре, проверено на 3.4.3). Истёкшей
    подписке, которую панель ещё считает живой, доступ закрывает погашенная дата
    (``expiry.py``): панель гасит аккаунт сама, как только дата пройдёт.
    """
    if is_live:
        return UserStatus.ACTIVE
    if getattr(user, 'status', OwnerStatus.ACTIVE.value) != OwnerStatus.ACTIVE.value:
        return UserStatus.DISABLED
    if getattr(subscription, 'status', None) == SubscriptionStatus.LIMITED.value:
        return UserStatus.LIMITED
    if is_subscription_expired(subscription, now=now) and not _end_date_ahead(subscription, now=now):
        return UserStatus.EXPIRED
    return UserStatus.DISABLED


def _end_date_ahead(subscription, *, now: datetime) -> bool:
    end_date = getattr(subscription, 'end_date', None)
    if end_date is None:
        return False
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    return end_date > now


def build_panel_payload(
    user,
    subscription,
    *,
    multi_tariff: bool,
    user_tag: str | None = None,
    now: datetime | None = None,
) -> PanelPayload:
    """Собрать поля запроса из пользователя, подписки и её тарифа."""
    moment = now or datetime.now(UTC)
    is_live = is_subscription_live(user, subscription, now=moment)
    tariff = getattr(subscription, 'tariff', None)

    if multi_tariff:
        # Суффикс ОБЯЗАН быть уникален на подписку: одинаковое имя вернёт из
        # панели одного и того же пользователя, и лимит устройств станет общим
        # на два тарифа. На пустом short_id (дефолт колонки у старых строк)
        # падаем на детерминированный суффикс по id подписки.
        suffix = getattr(subscription, 'remnawave_short_id', None) or f'sub{subscription.id}'
        username = settings.build_remnawave_subscription_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
            suffix=f'_{suffix}',
        )
    else:
        username = settings.format_remnawave_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )

    traffic_limit_gb = getattr(subscription, 'traffic_limit_gb', 0) or 0
    return PanelPayload(
        username=username,
        status=resolve_panel_status(user, subscription, is_live=is_live, now=moment),
        traffic_limit_bytes=traffic_limit_gb * _BYTES_IN_GB if traffic_limit_gb > 0 else 0,
        traffic_limit_strategy=get_traffic_reset_strategy(tariff),
        telegram_id=getattr(user, 'telegram_id', None),
        email=getattr(user, 'email', None),
        description=settings.format_remnawave_user_description(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        ),
        active_internal_squads=tuple(getattr(subscription, 'connected_squads', None) or ()),
        hwid_device_limit=resolve_hwid_device_limit_for_payload(subscription),
        external_squad_uuid=getattr(tariff, 'external_squad_uuid', None),
        # Вызывающий мог посчитать тег сам (тем же правилом); иначе берём его здесь —
        # так тег тарифа доходит до панели из любой точки записи.
        tag=user_tag if user_tag is not None else resolve_panel_user_tag(subscription),
        end_date=subscription.end_date,
        is_live=is_live,
    )
