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
from app.external.remnawave_api import (
    RemnaWaveAPIError,
    RemnaWaveUser,
    is_expire_in_past_error,
    is_user_not_found_error,
)
from app.services.panel_sync.expiry import SKEW_RETRY_MARGIN, stale_panel_expire_at
from app.services.panel_sync.identity import PanelIdentity, resolve_panel_identity
from app.services.panel_sync.payload import PanelPayload, build_panel_payload


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PanelWriteResult:
    """Чем закончилась запись."""

    #: ``None`` — писать было некуда и создавать не разрешили.
    panel_user: RemnaWaveUser | None
    #: 'updated' — аккаунт нашли и обновили, 'created' — завели новый,
    #: 'no_changes' — аккаунта нет, а создавать его вызывающий запретил.
    action: str
    #: Пришлось ли гасить дату, которую панель держала в будущем.
    expiry_extinguished: bool = False
    #: Адрес аккаунта в панели. Отдельным полем, потому что обёртки грейс-доступа
    #: возвращают урезанный объект без id.
    panel_user_id: int | None = None


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
    verify_recorded_id: bool = True,
    create_if_missing: bool = True,
    recreate_on_missing: bool = True,
    update_call=None,
    create_call=None,
    now: datetime | None = None,
) -> PanelWriteResult:
    """Отправить состояние подписки в панель.

    ``db`` нужен только чтобы записать связь: без него аккаунт обновится, но
    ``subscriptions.remnawave_id`` не проставится (колонка частично уникальна, и
    проверить занятость id без базы нельзя).

    ``only_fields`` — узкая правка: в панель уедут лишь перечисленные поля.

    ``recreate_on_missing=False`` — не пересоздавать аккаунт, если панель на PATCH
    ответила «такого пользователя нет»: у мониторинга для этого свой путь со
    своей проверкой, что подписку вообще стоит воскрешать.

    ``create_if_missing=False`` — если аккаунта в панели нет, не заводить новый
    и вернуть ``action='no_changes'``: так админ в кабинете может починить
    существующий аккаунт, не создавая его случайно.

    ``update_call``/``create_call`` — чем именно писать. По умолчанию это методы
    клиента панели, а кабинет и админка бота подставляют обёртки грейс-доступа:
    у них своя блокировка и свои переходы, но собирать запрос они должны так же,
    как все остальные.
    """
    moment = now or datetime.now(UTC)
    if multi_tariff is None:
        multi_tariff = settings.is_multi_tariff_enabled()
    if identity is None:
        identity = await resolve_panel_identity(
            api,
            user,
            subscription,
            multi_tariff=multi_tariff,
            pinned=pinned,
            verify_recorded_id=verify_recorded_id,
        )
    if payload is None:
        payload = build_panel_payload(user, subscription, multi_tariff=multi_tariff, user_tag=user_tag, now=moment)

    if reset_devices is None:
        reset_devices = settings.RESET_DEVICES_ON_RENEWAL
    update = update_call or api.update_user
    create = create_call or api.create_user

    panel_user_id = identity.user_id
    if panel_user_id is not None:
        if reset_devices and not await api.reset_user_devices(panel_user_id):
            logger.error('⚠️ Не удалось сбросить HWID', panel_user_id=panel_user_id)
        update_kwargs = payload.update_kwargs(
            user_id=panel_user_id,
            panel_current=identity.expire_at,
            now=moment,
            only_fields=only_fields,
        )
        already_sent = identity.expire_at
        try:
            try:
                panel_user = await update(**update_kwargs)
            except RemnaWaveAPIError as error:
                if not is_expire_in_past_error(error) or 'expire_at' not in update_kwargs:
                    raise
                # Гашение уехало в одном запросе со статусом, и панель отвергла
                # весь запрос: по её часам дата уже прошла. Статус важнее даты —
                # шлём без неё, а дату гасим отдельно, с запасом на разъезд.
                logger.warning(
                    'Панель сочла дату гашения прошедшей — часы бота и панели разошлись; шлём статус без даты',
                    subscription_id=getattr(subscription, 'id', None),
                    panel_user_id=panel_user_id,
                    expire_at=update_kwargs['expire_at'],
                )
                panel_user = await update(**{key: value for key, value in update_kwargs.items() if key != 'expire_at'})
                already_sent = None
        except RemnaWaveAPIError as error:
            # «Пользователя нет» — только явный признак этого (404/A018/A063).
            # Битый локальный идентификатор и транзиентная ошибка сюда намеренно
            # не попадают: уход в создание плодил бы дубли.
            if not is_user_not_found_error(error) or not recreate_on_missing:
                raise
            logger.warning(
                'Панельный аккаунт исчез — создаём заново',
                subscription_id=getattr(subscription, 'id', None),
                panel_user_id=panel_user_id,
            )
            panel_user = await create(**payload.create_kwargs(now=moment))
            # Связь ОБЯЗАНА перезаписаться: в колонке лежит id аккаунта, которого
            # в панели больше нет. Оставить его — значит на каждом следующем
            # проходе снова не находить аккаунт и заводить ещё один дубль.
            await _record_identity(db, user, subscription, panel_user, multi_tariff=multi_tariff, replace_stale=True)
            return PanelWriteResult(
                panel_user=panel_user, action='created', panel_user_id=getattr(panel_user, 'id', None)
            )

        extinguished = await _extinguish_stale_date(
            update,
            subscription,
            panel_user,
            panel_user_id=panel_user_id,
            already_sent=already_sent,
            now=moment,
        )
        await _record_identity(
            db, user, subscription, panel_user, multi_tariff=multi_tariff, panel_user_id=panel_user_id
        )
        return PanelWriteResult(
            panel_user=panel_user,
            action='updated',
            expiry_extinguished=extinguished,
            panel_user_id=getattr(panel_user, 'id', None) or panel_user_id,
        )

    panel_user = await create(**payload.create_kwargs(now=moment))
    await _record_identity(db, user, subscription, panel_user, multi_tariff=multi_tariff)
    return PanelWriteResult(panel_user=panel_user, action='created', panel_user_id=getattr(panel_user, 'id', None))


async def _extinguish_stale_date(
    update,
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
    try:
        await update(user_id=panel_user_id, expire_at=extinguish_at)
    except RemnaWaveAPIError as error:
        if not is_expire_in_past_error(error):
            raise
        # Панель сравнивает дату со своими часами: наш запас она уже съела.
        # Вторая попытка — с большим; если и её отвергнет, это уже не разъезд
        # часов, а что-то, о чём должен узнать оператор.
        retry_at = now + SKEW_RETRY_MARGIN
        logger.warning(
            'Панель отвергла дату гашения как прошедшую — часы бота отстают от панели; повтор с запасом',
            subscription_id=getattr(subscription, 'id', None),
            panel_user_id=panel_user_id,
            rejected=extinguish_at,
            retry_at=retry_at,
        )
        await update(user_id=panel_user_id, expire_at=retry_at)
    return True


async def _record_identity(
    db,
    user,
    subscription,
    panel_user,
    *,
    multi_tariff: bool,
    panel_user_id: int | None = None,
    replace_stale: bool = False,
) -> None:
    """Записать в базу, каким аккаунтом панели закрыта эта подписка.

    Без этого следующий проход не найдёт аккаунт точным ключом и заведёт дубль.

    ``panel_user_id`` — адрес, по которому мы только что писали. Он нужен,
    потому что обёртки грейс-доступа возвращают урезанный объект без id: сам
    аккаунт от этого не меняется, а связь потерять нельзя.

    ``replace_stale`` — в колонке лежит id аккаунта, которого в панели уже нет:
    его надо затереть, иначе следующий проход снова не найдёт аккаунт и заведёт
    ещё один дубль.
    """
    from app.services.subscription_service import link_subscription_panel_identity

    panel_user_id = getattr(panel_user, 'id', None) or panel_user_id
    if panel_user_id is None:
        return

    if replace_stale:
        subscription.remnawave_id = None
        if not multi_tariff:
            user.remnawave_id = None

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


async def patch_panel_account(
    api,
    *,
    user_id: int,
    description: str | None = None,
    telegram_id: int | None = None,
    email: str | None = None,
    hwid_device_limit: int | None = None,
    tag: str | None = None,
    update_call=None,
) -> RemnaWaveUser:
    """Обновить карточку аккаунта в панели, не трогая состояние подписки.

    Отдельный вход, потому что это другая задача: описание, телеграм и почта
    описывают человека, а не его подписку. Здесь нет ни статуса, ни даты, ни
    сквадов — значит, нечему и разъезжаться с остальными писателями. Нужен он
    там, где подписки под рукой нет вовсе: обновление описания в мидлваре и
    перенос аккаунтов при слиянии.
    """
    update = update_call or api.update_user
    kwargs: dict = {'user_id': user_id}
    if description is not None:
        kwargs['description'] = description
    if telegram_id is not None:
        kwargs['telegram_id'] = telegram_id
    if email is not None:
        kwargs['email'] = email
    if hwid_device_limit is not None:
        kwargs['hwid_device_limit'] = hwid_device_limit
    if tag is not None:
        kwargs['tag'] = tag
    return await update(**kwargs)


async def patch_panel_squads(
    api,
    *,
    user_id: int,
    squads: list[str],
    external_squad_uuid: str | None,
    update_call=None,
) -> RemnaWaveUser:
    """Переназначить аккаунту сквады тарифа.

    Отдельный вход, потому что источник здесь не подписка, а тариф: сквады
    приходят из его новой конфигурации, а строка подписки узнаёт о них только
    после успешного ответа панели. Собирать ради этого состояние подписки нельзя
    — уехали бы старые сквады.

    ``external_squad_uuid=None`` отправляется как null намеренно: у тарифа сняли
    внешний сквад, и в панели он тоже должен исчезнуть.
    """
    update = update_call or api.update_user
    return await update(
        user_id=user_id,
        active_internal_squads=squads,
        external_squad_uuid=external_squad_uuid,
    )
