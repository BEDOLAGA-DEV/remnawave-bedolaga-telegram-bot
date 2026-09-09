"""Запись подписки в панель — единственный путь для всех кнопок и фоновых задач.

Собирает вместе то, что раньше было раскопировано по тринадцати местам: найти
аккаунт, отправить полное состояние подписки, пересоздать аккаунт, если панель
говорит «такого нет», погасить дату, которая противоречит боту, и записать связь
обратно в базу.

Грейс-доступ остаётся снаружи: у него своя блокировка и своя проверка совпадения
с панелью, поэтому вызывающий сам решает, оборачивать ли вызов в
``grace_sensitive_panel_update``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from app.config import settings
from app.external.remnawave_api import RemnaWaveAPIError, RemnaWaveUser, is_user_not_found_error
from app.services.panel_sync.expiry import stale_panel_expire_at
from app.services.panel_sync.identity import PanelIdentity, resolve_panel_identity
from app.services.panel_sync.payload import PanelPayload, build_panel_payload


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PanelWriteResult:
    """Чем закончилась запись."""

    panel_user: RemnaWaveUser
    #: 'updated' — аккаунт нашли и обновили, 'created' — завели новый.
    action: str
    #: Пришлось ли гасить дату, которую панель держала в будущем.
    expiry_extinguished: bool = False


async def push_subscription(
    api,
    user,
    subscription,
    *,
    db=None,
    multi_tariff: bool | None = None,
    pinned: bool = False,
    identity: PanelIdentity | None = None,
    payload: PanelPayload | None = None,
    user_tag: str | None = None,
    only_fields: set[str] | None = None,
    reset_devices: bool | None = None,
    now: datetime | None = None,
) -> PanelWriteResult:
    """Отправить состояние подписки в панель.

    ``db`` нужен только чтобы записать связь: без него аккаунт обновится, но
    ``subscriptions.remnawave_id`` не проставится (колонка частично уникальна, и
    проверить занятость id без базы нельзя).

    ``only_fields`` — узкая правка: в панель уедут лишь перечисленные поля.
    """
    moment = now or datetime.now(UTC)
    if multi_tariff is None:
        multi_tariff = settings.is_multi_tariff_enabled()
    if identity is None:
        identity = await resolve_panel_identity(api, user, subscription, multi_tariff=multi_tariff, pinned=pinned)
    if payload is None:
        payload = build_panel_payload(user, subscription, multi_tariff=multi_tariff, user_tag=user_tag, now=moment)

    if reset_devices is None:
        reset_devices = settings.RESET_DEVICES_ON_RENEWAL

    panel_user_id = identity.user_id
    if panel_user_id is not None:
        if reset_devices and not await api.reset_user_devices(panel_user_id):
            logger.error('⚠️ Не удалось сбросить HWID', panel_user_id=panel_user_id)
        try:
            panel_user = await api.update_user(
                **payload.update_kwargs(
                    user_id=panel_user_id,
                    panel_current=identity.expire_at,
                    now=moment,
                    only_fields=only_fields,
                )
            )
        except RemnaWaveAPIError as error:
            # «Пользователя нет» — только явный признак этого (404/A018/A063).
            # Битый локальный идентификатор и транзиентная ошибка сюда намеренно
            # не попадают: уход в создание плодил бы дубли.
            if not is_user_not_found_error(error):
                raise
            logger.warning(
                'Панельный аккаунт исчез — создаём заново',
                subscription_id=getattr(subscription, 'id', None),
                panel_user_id=panel_user_id,
            )
            panel_user = await api.create_user(**payload.create_kwargs(now=moment))
            await _record_identity(db, user, subscription, panel_user, multi_tariff=multi_tariff)
            return PanelWriteResult(panel_user=panel_user, action='created')

        extinguished = await _extinguish_stale_date(
            api,
            subscription,
            panel_user,
            panel_user_id=panel_user_id,
            already_sent=identity.expire_at,
            now=moment,
        )
        await _record_identity(db, user, subscription, panel_user, multi_tariff=multi_tariff)
        return PanelWriteResult(panel_user=panel_user, action='updated', expiry_extinguished=extinguished)

    panel_user = await api.create_user(**payload.create_kwargs(now=moment))
    await _record_identity(db, user, subscription, panel_user, multi_tariff=multi_tariff)
    return PanelWriteResult(panel_user=panel_user, action='created')


async def _extinguish_stale_date(
    api,
    subscription,
    panel_user,
    *,
    panel_user_id: int,
    already_sent: datetime | None,
    now: datetime,
) -> bool:
    """Погасить дату, если панель после обновления всё ещё держит будущее.

    Дату панели не всегда знают заранее: массовая синхронизация узнаёт её только
    из ответа на PATCH. Если там будущее у подписки, которая в боте истекла,
    панель до этой даты показывает живую подписку — гасим вторым запросом.
    Прошедшую дату панель при обновлении не принимает, поэтому ставим ближайший
    допустимый момент; следующий проход увидит там прошлое и уже ничего не тронет.
    """
    end_date = getattr(subscription, 'end_date', None)
    if end_date is None:
        return False
    extinguish_at = stale_panel_expire_at(getattr(panel_user, 'expire_at', None), end_date=end_date, now=now)
    if extinguish_at is None:
        return False
    if already_sent is not None:
        # Дата была известна до запроса — гашение уже уехало тем же PATCH.
        return True
    await api.update_user(user_id=panel_user_id, expire_at=extinguish_at)
    return True


async def _record_identity(db, user, subscription, panel_user, *, multi_tariff: bool) -> None:
    """Записать в базу, каким аккаунтом панели закрыта эта подписка.

    Без этого следующий проход не найдёт аккаунт точным ключом и заведёт дубль.
    """
    from app.services.subscription_service import link_subscription_panel_identity

    panel_user_id = getattr(panel_user, 'id', None)
    if panel_user_id is None:
        return

    short_uuid = getattr(panel_user, 'short_uuid', None)
    if short_uuid:
        subscription.remnawave_short_uuid = short_uuid
    subscription_url = getattr(panel_user, 'subscription_url', None)
    if subscription_url:
        subscription.subscription_url = subscription_url
    crypto_link = getattr(panel_user, 'happ_crypto_link', None)
    if crypto_link is not None:
        subscription.subscription_crypto_link = crypto_link

    if not multi_tariff and not getattr(user, 'remnawave_id', None):
        user.remnawave_id = panel_user_id

    if db is None:
        # Без сессии занятость id не проверить, но и промолчать нельзя: без
        # записанной связи следующий проход не найдёт аккаунт точным ключом и
        # заведёт рядом с ним дубль. Пишем в пустую колонку.
        if not getattr(subscription, 'remnawave_id', None):
            subscription.remnawave_id = panel_user_id
        return
    await link_subscription_panel_identity(db, subscription, panel_user_id)
