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
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.external.remnawave_api import RemnaWaveUser


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PanelIdentity:
    """Что панель знает про эту подписку прямо сейчас."""

    panel_user: RemnaWaveUser | None = None
    #: Каким ключом опознали: subscription | user | short_uuid | telegram | email.
    source: str | None = None
    #: Записанный в базе id, который решили не проверять запросом (массовый проход).
    known_id: int | None = None

    @property
    def user_id(self) -> int | None:
        return getattr(self.panel_user, 'id', None) or self.known_id

    @property
    def expire_at(self):
        return getattr(self.panel_user, 'expire_at', None)


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
) -> PanelIdentity:
    """Найти в панели аккаунт этой подписки.

    ``pinned`` — синхронизация конкретной выбранной подписки: подменять её
    личность пользовательским аккаунтом нельзя даже в одиночном режиме тарифов.

    ``verify_recorded_id=False`` — не проверять записанный id запросом. Так ходит
    массовый проход: на большой базе лишний GET к панели на каждую подписку
    удваивает нагрузку, а протухший id всё равно обнаружится по ответу на PATCH
    («такого пользователя нет») и приведёт к пересозданию.
    """
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
        if panel_user is not None:
            return PanelIdentity(panel_user=panel_user, source='short_uuid')

    telegram_id = getattr(user, 'telegram_id', None)
    if telegram_id:
        chosen = _pick_from_list(
            await api.find_users_by_telegram_id(telegram_id),
            multi_tariff=multi_tariff,
            subscription=subscription,
        )
        if chosen is not None:
            return PanelIdentity(panel_user=chosen, source='telegram')

    email = getattr(user, 'email', None)
    if email:
        chosen = _pick_from_list(
            await api.find_users_by_email(email),
            multi_tariff=multi_tariff,
            subscription=subscription,
        )
        if chosen is not None:
            return PanelIdentity(panel_user=chosen, source='email')

    if adoption_error is not None:
        raise adoption_error

    return PanelIdentity()
