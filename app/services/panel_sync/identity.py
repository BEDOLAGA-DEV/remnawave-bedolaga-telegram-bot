"""Поиск панельного аккаунта подписки.

Ключи перебираются по убыванию точности:

1. числовой id, записанный в базе (подписки — в мультитарифе, пользователя — в
   одиночном режиме);
2. ``shortUuid`` — тоже точный ключ, он переживает апгрейд панели до 3.0.0, где
   исчез старый ``uuid``;
3. ``telegramId``;
4. ``email``.

Первые два адресуют аккаунт однозначно, последние два отдают список, из которого
без дополнительной проверки берётся первый попавшийся. Поэтому порядок здесь не
украшение: у человека бывает несколько аккаунтов в панели, и «найти по телеграму»
раньше «найти по shortUuid» — это выбрать чужую подписку.

Ошибки панели различаются по смыслу. ``None`` от клиента означает ровно 404
(«аккаунта нет»), а вот таймаут, 5xx на рестарте панели и битый локальный
идентификатор — это «не знаем». Проглотить их нельзя: вызывающий воспримет
пустой результат как «создавать нового» и заведёт дубль рядом с живым
оплаченным аккаунтом.

Найденный аккаунт ещё надо проверить на хозяина (GitHub #3245): почта или
Telegram бывают у двух записей одного человека, и неточный ключ приводит в
аккаунт, который база бота уже закрепила за другой подпиской. Писать туда —
значит гасить или переписывать чужую оплату, брать оттуда срок — дарить себе
чужую. Такие аккаунты при поиске пропускаются.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import or_, select

from app.database.models import Subscription, User, UserStatus
from app.external.remnawave_api import RemnaWaveUser


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PanelOwner:
    """Кому в базе бота принадлежит панельный аккаунт."""

    user_id: int
    #: ``None`` — аккаунт закреплён только за пользователем (старые строки одиночного режима).
    subscription_id: int | None = None


class PanelAccountOwnedByAnotherUser(Exception):
    """Единственный найденный аккаунт панели закреплён за другим человеком.

    Не ошибка панели и не «аккаунта нет»: создавать новый нельзя (почта и имя
    аккаунта те же — панель либо откажет, либо заведёт двойника), писать в
    найденный — тем более. Две записи бота на одного человека разбирает оператор.
    """

    def __init__(self, *, subscription_id: int | None, panel_user_id: int | None, owner: PanelOwner):
        self.subscription_id = subscription_id
        self.panel_user_id = panel_user_id
        self.owner_user_id = owner.user_id
        self.owner_subscription_id = owner.subscription_id
        super().__init__(
            f'Аккаунт панели {panel_user_id} закреплён за пользователем бота {owner.user_id}'
            f' (подписка {owner.subscription_id}), а не за подпиской {subscription_id}'
        )


@dataclass(frozen=True)
class PanelIdentity:
    """Что панель знает про эту подписку прямо сейчас."""

    panel_user: RemnaWaveUser | None = None
    #: Каким ключом опознали: subscription | user | short_uuid | telegram | email.
    source: str | None = None
    #: Записанный в базе id, который решили не проверять запросом (массовый проход).
    known_id: int | None = None
    #: Своего аккаунта нет, но нашёлся чужой — чей он (иначе ``None``).
    foreign_owner: PanelOwner | None = None
    #: Id того чужого аккаунта — для сообщения оператору.
    foreign_panel_id: int | None = None

    @property
    def user_id(self) -> int | None:
        return getattr(self.panel_user, 'id', None) or self.known_id

    @property
    def expire_at(self):
        return getattr(self.panel_user, 'expire_at', None)

    def raise_if_foreign(self, subscription) -> None:
        """Своего аккаунта нет, а чужой есть — ни писать, ни создавать нельзя."""
        if self.user_id is None and self.foreign_owner is not None:
            raise PanelAccountOwnedByAnotherUser(
                subscription_id=getattr(subscription, 'id', None),
                panel_user_id=self.foreign_panel_id,
                owner=self.foreign_owner,
            )


def _not_deleted():
    # Удалённый человек ничем не владеет: его записи не должны блокировать живого.
    return or_(User.status.is_(None), User.status != UserStatus.DELETED.value)


async def find_foreign_panel_owner(db, user, subscription, panel_id, *, multi_tariff: bool) -> PanelOwner | None:
    """Чей это аккаунт панели, если не этой подписки; ``None`` — наш или ничей.

    Хозяин определяется по базе бота, а не по полям аккаунта: почта и Telegram
    в панели бывают от разных записей одного человека (так и было в #3245).

    1. Строка подписки, держащая этот id, — самый точный признак (колонка
       частично уникальна). Своя строка — наш. Строка того же человека в
       одиночном режиме — тоже наш: там все подписки адресуют один аккаунт. В
       мультитарифе у каждой подписки свой аккаунт, и соседняя — уже хозяин.
    2. Только в одиночном режиме: ``users.remnawave_id`` (уникален) — у старых
       строк адрес лежит лишь там. В мультитарифе это мусор из прошлого.
    """
    if panel_id is None:
        return None
    try:
        panel_id = int(panel_id)
    except (TypeError, ValueError):
        return None
    if getattr(subscription, 'remnawave_id', None) == panel_id:
        return None

    row = (
        await db.execute(
            select(Subscription.id, Subscription.user_id)
            .join(User, User.id == Subscription.user_id)
            .where(Subscription.remnawave_id == panel_id, _not_deleted())
            .limit(1)
        )
    ).first()
    if row is not None:
        holder_id, holder_user_id = row
        if holder_id == getattr(subscription, 'id', None):
            return None
        if not multi_tariff and holder_user_id == getattr(user, 'id', None):
            return None
        return PanelOwner(user_id=holder_user_id, subscription_id=holder_id)

    if multi_tariff:
        return None
    holder = (await db.execute(select(User.id).where(User.remnawave_id == panel_id, _not_deleted()).limit(1))).first()
    if holder is None or holder[0] == getattr(user, 'id', None):
        return None
    return PanelOwner(user_id=holder[0])


def _subscription_suffix(subscription) -> str | None:
    short_id = (getattr(subscription, 'remnawave_short_id', None) or '').strip()
    return f'_{short_id}' if short_id else None


def _pick_from_list(candidates, *, multi_tariff: bool, subscription) -> RemnaWaveUser | None:
    """Выбрать аккаунт из неточного поиска.

    В мультитарифе у каждой подписки свой аккаунт, поэтому список фильтруется по
    суффиксу имени. Без суффикса угадывать нельзя: выберем чужую подписку того же
    человека и сольём две в одну.
    """
    if not candidates:
        return None
    if not multi_tariff:
        if len(candidates) > 1:
            logger.warning(
                '⚠️ У пользователя несколько панельных аккаунтов, точного ключа нет — берём первый',
                candidates=[getattr(candidate, 'id', None) for candidate in candidates],
            )
        return candidates[0]

    suffix = _subscription_suffix(subscription)
    if not suffix:
        return None
    return next(
        (candidate for candidate in candidates if (getattr(candidate, 'username', None) or '').endswith(suffix)),
        None,
    )


async def resolve_panel_identity(
    api,
    user,
    subscription,
    *,
    multi_tariff: bool,
    pinned: bool = False,
    verify_recorded_id: bool = True,
    db=None,
) -> PanelIdentity:
    """Найти в панели аккаунт этой подписки.

    ``pinned`` — синхронизация конкретной выбранной подписки: подменять её
    личность пользовательским аккаунтом нельзя даже в одиночном режиме тарифов.

    ``verify_recorded_id=False`` — не проверять записанный id запросом. Так ходит
    массовый проход: на большой базе лишний GET к панели на каждую подписку
    удваивает нагрузку, а протухший id всё равно обнаружится по ответу на PATCH
    («такого пользователя нет») и приведёт к пересозданию.

    ``db`` — проверить хозяина найденного аккаунта (см. ``find_foreign_panel_owner``):
    чужие пропускаются, поиск идёт дальше; если нашлись только чужие, в ответе
    ``foreign_owner``. Без базы проверить некому — так ходят лишь тесты и чтение.
    """
    foreign: list[tuple[int, PanelOwner]] = []

    async def is_foreign(panel_id) -> bool:
        if db is None:
            return False
        owner = await find_foreign_panel_owner(db, user, subscription, panel_id, multi_tariff=multi_tariff)
        if owner is None:
            return False
        logger.warning(
            '⚠️ Аккаунт панели закреплён за другим пользователем бота — пропускаем (две записи одного человека?)',
            subscription_id=getattr(subscription, 'id', None),
            user_id=getattr(user, 'id', None),
            panel_user_id=panel_id,
            owner_user_id=owner.user_id,
            owner_subscription_id=owner.subscription_id,
        )
        foreign.append((panel_id, owner))
        return True

    # В одиночном режиме тарифов панель адресуется через пользователя, и его id
    # заполнен у всех старых строк — поэтому он первый. В мультитарифе у каждой
    # подписки свой аккаунт, и пользовательский id там не адрес, а мусор из
    # прошлого: подставив его, мы бы переписали чужую подписку.
    exact_ids: list[tuple[str, int | None]] = []
    if not pinned and not multi_tariff:
        exact_ids.append(('user', getattr(user, 'remnawave_id', None)))
    exact_ids.append(('subscription', getattr(subscription, 'remnawave_id', None)))

    for source, panel_user_id in exact_ids:
        if not panel_user_id:
            continue
        # Свою строку не проверяем: колонка уникальна, она и есть хозяин. А вот
        # id пользователя мог прилипнуть от прошлой записи по почте в чужой аккаунт.
        if source != 'subscription' and await is_foreign(panel_user_id):
            continue
        if not verify_recorded_id:
            return PanelIdentity(source=source, known_id=panel_user_id)
        # RemnaWaveInvalidUserIdError пробрасываем: это баг в данных бота, а не
        # отсутствие аккаунта, и уход в создание плодил бы дубли.
        panel_user = await api.get_user_by_id(panel_user_id)
        if panel_user is not None:
            return PanelIdentity(panel_user=panel_user, source=source)
        logger.warning(
            '⚠️ Записанный панельный id не найден в панели — ищем другими ключами',
            subscription_id=getattr(subscription, 'id', None),
            panel_user_id=panel_user_id,
            source=source,
        )

    short_uuid = (getattr(subscription, 'remnawave_short_uuid', None) or '').strip()
    adoption_error: Exception | None = None
    if short_uuid:
        try:
            panel_user = await api.get_user_by_short_uuid(short_uuid)
        except Exception as error:
            # Отсутствие аккаунта доказывает ТОЛЬКО 404 (клиент отдаёт его как
            # None). Любой другой ответ — 5xx на рестарте панели, 429, таймаут —
            # значит «не знаем». Падать сразу нельзя: дальше аккаунт может
            # опознаться по телеграму. Но если не опознается, создавать нового
            # тоже нельзя — поднимем эту ошибку в конце.
            adoption_error = error
            panel_user = None
            logger.warning(
                '⚠️ Панель не ответила по short_uuid — пробуем другие ключи',
                subscription_id=getattr(subscription, 'id', None),
                error=str(error),
            )
        # shortUuid прилипает от любой прошлой записи — в том числе в чужой аккаунт.
        if panel_user is not None and not await is_foreign(getattr(panel_user, 'id', None)):
            return PanelIdentity(panel_user=panel_user, source='short_uuid')

    async def pick_own(candidates) -> RemnaWaveUser | None:
        # Хозяина проверяем у выбранного, а не у всего списка: в мультитарифе
        # список по Telegram — это аккаунты соседних подписок, и проверка каждого
        # засыпала бы лог предупреждениями о штатной ситуации.
        remaining = list(candidates or [])
        while True:
            chosen = _pick_from_list(remaining, multi_tariff=multi_tariff, subscription=subscription)
            if chosen is None or not await is_foreign(getattr(chosen, 'id', None)):
                return chosen
            remaining = [candidate for candidate in remaining if candidate is not chosen]

    telegram_id = getattr(user, 'telegram_id', None)
    if telegram_id:
        chosen = await pick_own(await api.find_users_by_telegram_id(telegram_id))
        if chosen is not None:
            return PanelIdentity(panel_user=chosen, source='telegram')

    email = getattr(user, 'email', None)
    if email:
        chosen = await pick_own(await api.find_users_by_email(email))
        if chosen is not None:
            return PanelIdentity(panel_user=chosen, source='email')

    if adoption_error is not None:
        raise adoption_error

    if foreign:
        panel_id, owner = foreign[0]
        return PanelIdentity(foreign_owner=owner, foreign_panel_id=panel_id)
    return PanelIdentity()


async def panel_id_is_free_for(db, subscription, panel_id: int | None) -> bool:
    """Не держит ли этот панельный id уже ДРУГАЯ строка подписок.

    Колонка частично уникальна, и в single-tariff все подписки одного человека
    адресуют один и тот же панельный аккаунт, поэтому конфликт — штатная
    ситуация, а не аномалия. Единственная проверка перед записью
    ``subscriptions.remnawave_id`` — и для сервиса, и для админских роутов.
    """
    if panel_id is None:
        return False
    other = (
        await db.execute(
            select(Subscription.id)
            .where(
                Subscription.remnawave_id == int(panel_id),
                Subscription.id != getattr(subscription, 'id', None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return other is None


async def link_subscription_panel_identity(db, subscription, panel_id: int | None) -> bool:
    """Проставить строке id панельного аккаунта, который только что обновили.

    В single-tariff панель адресуется через ``users.remnawave_id``, и свежая строка
    подписки (создана после удаления старой или повторной покупкой) оставалась с
    пустым ``subscriptions.remnawave_id`` — а админские экраны по выбранной подписке
    (panel-info, устройства, трафик) читают строго его: «пользователь не найден в
    панели». Пишем только в пустую строку и только если id не держит соседняя —
    колонка частично уникальна, и IntegrityError после успешного PATCH откатил бы
    всё сделанное. True — привязали.
    """
    if getattr(subscription, 'remnawave_id', None) or panel_id is None:
        return False
    if not await panel_id_is_free_for(db, subscription, panel_id):
        return False
    subscription.remnawave_id = int(panel_id)
    return True
