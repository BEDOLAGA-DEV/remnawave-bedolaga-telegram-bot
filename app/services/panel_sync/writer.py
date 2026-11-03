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
from functools import partial

import structlog

from app.config import settings
from app.external.remnawave_api import (
    RemnaWaveAPIError,
    RemnaWaveUser,
    is_expire_in_past_error,
    is_user_not_found_error,
)
from app.services.panel_sync.expiry import SKEW_RETRY_MARGIN, stale_panel_expire_at
from app.services.panel_sync.expiry_intent import acknowledge_pending_expiry
from app.services.panel_sync.identity import (
    PanelIdentity,
    find_foreign_panel_owner,
    link_subscription_panel_identity,
    resolve_panel_identity,
    user_panel_id_is_free_for,
)
from app.services.panel_sync.payload import PanelPayload, build_panel_payload


logger = structlog.get_logger(__name__)


class DeviceLimitReconciliationError(RemnaWaveAPIError):
    """The panel cap was written, but registered devices still need reconciliation."""


async def _enforce_registered_device_limit(api, panel_user_id: int, limit: int | None) -> None:
    """A panel cap only rejects NEW HWIDs; reconcile old registrations as well.

    Always recheck finite caps, even when PATCH repeats the same value: an earlier
    PATCH may have succeeded while device deletion failed. Keep the most recently
    active registrations; never touch unlimited accounts or another panel user.

    This is a point-in-time reconciliation, not an atomic admission guarantee:
    Remnawave 3.4.3 can recreate deleted HWIDs in delayed metadata upsert jobs.
    See docs/hwid-limit-reconciliation.md for the reproduced upstream race.
    """
    if type(limit) is not int or limit <= 0:
        return

    async def devices():
        result = await api.get_user_devices_all(panel_user_id)
        rows = result.get('devices') if isinstance(result, dict) else None
        if (
            not isinstance(rows, list)
            or result.get('total') != len(rows)
            or any(
                not isinstance(row, dict)
                or row.get('userId') != panel_user_id
                or not isinstance(row.get('hwid'), str)
                or not row['hwid']
                for row in rows
            )
            or len({row['hwid'] for row in rows}) != len(rows)
        ):
            raise DeviceLimitReconciliationError('Cannot safely reconcile HWID limit: invalid device list')
        return rows

    registered = await devices()
    if len(registered) <= limit:
        return

    def last_activity(row):
        for field in ('updatedAt', 'createdAt'):
            try:
                moment = datetime.fromisoformat(str(row.get(field)).replace('Z', '+00:00'))
                return (moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment, row['hwid'])
            except (ValueError, TypeError):
                continue
        return datetime.min.replace(tzinfo=UTC), row['hwid']

    excess = sorted(registered, key=last_activity, reverse=True)[limit:]
    for row in excess:
        if not await api.remove_device(panel_user_id, row['hwid']):
            raise DeviceLimitReconciliationError('HWID limit reconciliation failed: device removal not confirmed')
    if len(await devices()) > limit:
        raise DeviceLimitReconciliationError('HWID limit reconciliation incomplete; retry required')
    logger.info(
        'Reconciled registered HWID device limit', panel_user_id=panel_user_id, limit=limit, removed=len(excess)
    )


async def _write_with_current_squads(api, write, **kwargs):
    updated, _surviving = await write_panel_user_with_squad_recovery(api, write, **kwargs)
    return updated


async def write_panel_user_with_squad_recovery(api, write, **kwargs):
    """Recover a rejected write only when a fresh catalog proves stale squads.

    A018/A039 are generic failures, not evidence of a deleted squad. Never retry
    unchanged, substitute unrelated servers, or turn an explicit set into an
    empty one. Keep paid local intent intact; normal sync reconciles the catalog.
    Return the panel response and the surviving selection only when narrowed;
    otherwise the second element is None and the original target is unchanged.
    """
    try:
        return await write(**kwargs), None
    except RemnaWaveAPIError as error:
        data = error.response_data if isinstance(error.response_data, dict) else {}
        requested = kwargs.get('active_internal_squads')
        if data.get('errorCode') not in {'A018', 'A039', 'A118'} or not requested:
            raise
        try:
            catalog = await api.get_internal_squads()
            if not isinstance(catalog, list) or any(
                not isinstance(getattr(squad, 'uuid', None), str) or not squad.uuid for squad in catalog
            ):
                raise ValueError('Invalid internal squad catalog')
            available = {squad.uuid for squad in catalog}
        except Exception:
            # A failed/invalid catalog must not be interpreted as mass deletion.
            raise error from None
        surviving = [squad for squad in requested if squad in available]
        if not surviving or surviving == requested:
            raise
        logger.warning(
            'Retrying panel write with surviving assigned internal squads',
            removed_count=len(requested) - len(surviving),
        )
        updated = await write(**{**kwargs, 'active_internal_squads': surviving})
        return updated, tuple(surviving)


async def _update_with_device_limit(api, update, **kwargs):
    panel_user = await _write_with_current_squads(api, update, **kwargs)
    if panel_user is not None:
        try:
            await _enforce_registered_device_limit(api, kwargs['user_id'], kwargs.get('hwid_device_limit'))
        except DeviceLimitReconciliationError:
            raise
        except Exception as error:
            raise DeviceLimitReconciliationError('HWID limit reconciliation failed; retry required') from error
    return panel_user


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
    mode = getattr(subscription, 'traffic_accounting_mode', None) or 'legacy'
    if db is not None and getattr(subscription, 'id', None) is not None:
        from sqlalchemy import select

        from app.database.models import Subscription

        # Read the fence even if the supplied ORM object predates migration.
        persisted_mode = await db.scalar(
            select(Subscription.traffic_accounting_mode).where(Subscription.id == subscription.id)
        )
        if isinstance(persisted_mode, str):
            mode = persisted_mode
    if mode != 'legacy':
        return await _push_managed_subscription(
            api,
            subscription,
            db=db,
            now=moment,
            only_fields=only_fields,
            reset_devices=reset_devices,
            user_tag=user_tag if user_tag is not None else getattr(payload, 'tag', None),
            create_if_missing=create_if_missing,
            recreate_on_missing=recreate_on_missing,
        )
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
            db=db,
        )
    elif db is not None and identity.user_id is not None:
        # Адрес передали готовым (продление берёт ``users.remnawave_id``) — он мог
        # прилипнуть от прошлой записи по почте в чужой аккаунт; проверяем так же.
        owner = await find_foreign_panel_owner(db, user, subscription, identity.user_id, multi_tariff=multi_tariff)
        if owner is not None:
            identity = PanelIdentity(foreign_owner=owner, foreign_panel_id=identity.user_id)
    # Нашёлся только аккаунт другого человека (#3245): писать туда — гасить его
    # оплату, создавать новый — плодить двойника с той же почтой. Решает оператор.
    identity.raise_if_foreign(subscription)
    if payload is None:
        payload = build_panel_payload(user, subscription, multi_tariff=multi_tariff, user_tag=user_tag, now=moment)

    if reset_devices is None:
        reset_devices = settings.RESET_DEVICES_ON_RENEWAL
    update = partial(_update_with_device_limit, api, update_call or api.update_user)
    create = partial(_write_with_current_squads, api, create_call or api.create_user)

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
            # «Пользователя нет» — только явный признак этого (A025/A063, см. is_user_not_found_error).
            # Битый локальный идентификатор и транзиентная ошибка сюда намеренно
            # не попадают: уход в создание плодил бы дубли.
            if not is_user_not_found_error(error) or not recreate_on_missing:
                raise
            # Прежде чем создавать: аккаунт подписки мог уже быть заведён прошлой
            # попыткой, которая не успела записать связь. Создание тогда упирается
            # в занятое имя — и так на каждом проходе, без выхода.
            survivor = await resolve_panel_identity(
                api,
                user,
                subscription,
                multi_tariff=multi_tariff,
                pinned=pinned,
                ignore_recorded_ids=True,
                db=db,
            )
            survivor.raise_if_foreign(subscription)
            if survivor.user_id is not None and survivor.user_id != panel_user_id:
                logger.warning(
                    'Записанный панельный id мёртв, аккаунт подписки нашёлся другим ключом — перепривязываем',
                    subscription_id=getattr(subscription, 'id', None),
                    dead_panel_user_id=panel_user_id,
                    panel_user_id=survivor.user_id,
                    source=survivor.source,
                )
                panel_user = await update(
                    **payload.update_kwargs(
                        user_id=survivor.user_id,
                        panel_current=survivor.expire_at,
                        now=moment,
                        only_fields=only_fields,
                    )
                )
                await _record_identity(
                    db,
                    user,
                    subscription,
                    panel_user,
                    multi_tariff=multi_tariff,
                    panel_user_id=survivor.user_id,
                    replace_stale=True,
                    dead_panel_user_id=panel_user_id,
                )
                return PanelWriteResult(
                    panel_user=panel_user,
                    action='updated',
                    panel_user_id=getattr(panel_user, 'id', None) or survivor.user_id,
                )
            logger.warning(
                'Панельный аккаунт исчез — создаём заново',
                subscription_id=getattr(subscription, 'id', None),
                panel_user_id=panel_user_id,
            )
            panel_user = await create(**payload.create_kwargs(now=moment))
            # Связь ОБЯЗАНА перезаписаться: в колонке лежит id аккаунта, которого
            # в панели больше нет. Оставить его — значит на каждом следующем
            # проходе снова не находить аккаунт и заводить ещё один дубль.
            await _record_identity(
                db,
                user,
                subscription,
                panel_user,
                multi_tariff=multi_tariff,
                replace_stale=True,
                dead_panel_user_id=panel_user_id,
            )
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
            db,
            user,
            subscription,
            panel_user,
            multi_tariff=multi_tariff,
            panel_user_id=panel_user_id,
            replace_stale=bool(identity.dead_recorded_ids),
            dead_panel_user_id=_dead_user_link(identity, user),
        )
        return PanelWriteResult(
            panel_user=panel_user,
            action='updated',
            expiry_extinguished=extinguished,
            panel_user_id=getattr(panel_user, 'id', None) or panel_user_id,
        )

    panel_user = await create(**payload.create_kwargs(now=moment))
    # Аккаунт не нашёлся ни одним ключом, и записанные id проверкой признаны
    # мёртвыми — связь ОБЯЗАНА перезаписаться. Иначе колонка остаётся занятой
    # адресом удалённой учётки: следующее нажатие снова её не найдёт и заведёт
    # ещё одну (issue #3277).
    await _record_identity(
        db,
        user,
        subscription,
        panel_user,
        multi_tariff=multi_tariff,
        replace_stale=bool(identity.dead_recorded_ids),
        dead_panel_user_id=_dead_user_link(identity, user),
    )
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


def _dead_user_link(identity, user) -> int | None:
    """Мёртвый ли адрес записан у человека.

    ``_record_identity`` стирает ``users.remnawave_id`` в мультитарифе только
    при совпадении с этим значением: там аккаунты принадлежат подпискам, и
    чужой живой адрес человека трогать нельзя.
    """
    recorded = getattr(user, 'remnawave_id', None)
    if recorded and recorded in identity.dead_recorded_ids:
        return int(recorded)
    return None


async def _record_identity(
    db,
    user,
    subscription,
    panel_user,
    *,
    multi_tariff: bool,
    panel_user_id: int | None = None,
    replace_stale: bool = False,
    dead_panel_user_id: int | None = None,
) -> None:
    """Записать в базу, каким аккаунтом панели закрыта эта подписка.

    Без этого следующий проход не найдёт аккаунт точным ключом и заведёт дубль.

    ``panel_user_id`` — адрес, по которому мы только что писали. Он нужен,
    потому что обёртки грейс-доступа возвращают урезанный объект без id: сам
    аккаунт от этого не меняется, а связь потерять нельзя.

    ``replace_stale`` — в колонке лежит id аккаунта, которого в панели уже нет:
    его надо затереть, иначе следующий проход снова не найдёт аккаунт и заведёт
    ещё один дубль.

    ``dead_panel_user_id`` — тот самый исчезнувший id. В мультитарифе первый
    аккаунт записан и человеку; мёртвый адрес там достался бы следующей покупке
    (``should_create_panel_account``), поэтому затираем его и у человека.
    """
    panel_user_id = getattr(panel_user, 'id', None) or panel_user_id
    if panel_user_id is None:
        return

    if replace_stale:
        subscription.remnawave_id = None
        if not multi_tariff or (dead_panel_user_id and getattr(user, 'remnawave_id', None) == dead_panel_user_id):
            user.remnawave_id = None

    short_uuid = getattr(panel_user, 'short_uuid', None)
    if short_uuid:
        subscription.remnawave_short_uuid = short_uuid
    subscription_url = getattr(panel_user, 'subscription_url', None)
    url_changed = bool(subscription_url and subscription.subscription_url != subscription_url)
    if subscription_url:
        subscription.subscription_url = subscription_url
    crypto_link = getattr(panel_user, 'happ_crypto_link', None)
    if crypto_link is not None:
        subscription.subscription_crypto_link = crypto_link
    elif url_changed:
        subscription.subscription_crypto_link = None

    # Одиночный режим адресует панель через человека. В мультитарифе аккаунты у
    # подписок, но первый из них записываем и человеку: иначе после возврата
    # оператора в одиночный режим у него «нет аккаунта» — 0 устройств, второй
    # аккаунт при покупке. Записанный аккаунт не перезаписываем.
    if not getattr(user, 'remnawave_id', None) and (
        db is None or await user_panel_id_is_free_for(db, user, panel_user_id)
    ):
        user.remnawave_id = panel_user_id

    if db is None:
        # Без сессии занятость id не проверить, но и промолчать нельзя: без
        # записанной связи следующий проход не найдёт аккаунт точным ключом и
        # заведёт рядом с ним дубль. Пишем в пустую колонку.
        if not getattr(subscription, 'remnawave_id', None):
            subscription.remnawave_id = panel_user_id
        return
    await link_subscription_panel_identity(db, subscription, panel_user_id)
    await acknowledge_pending_expiry(db, subscription, panel_user)


async def patch_panel_account(
    api,
    *,
    user_id: int,
    description: str | None = None,
    telegram_id: int | None = None,
    email: str | None = None,
    hwid_device_limit: int | None = None,
    tag: str | type(...) | None = ...,
    update_call=None,
) -> RemnaWaveUser:
    """Обновить карточку аккаунта в панели, не трогая состояние подписки.

    ``tag`` не передан — поле не трогается; ``tag=None`` — снять тег: вызывающий
    посчитал его по правилу ``resolve_panel_user_tag`` и получил «тега нет».

    Отдельный вход, потому что это другая задача: описание, телеграм и почта
    описывают человека, а не его подписку. Здесь нет ни статуса, ни даты, ни
    сквадов — значит, нечему и разъезжаться с остальными писателями. Нужен он
    там, где подписки под рукой нет вовсе: обновление описания в мидлваре и
    перенос аккаунтов при слиянии.
    """
    update = partial(_update_with_device_limit, api, update_call or api.update_user)
    kwargs: dict = {'user_id': user_id}
    if description is not None:
        kwargs['description'] = description
    if telegram_id is not None:
        kwargs['telegram_id'] = telegram_id
    if email is not None:
        kwargs['email'] = email
    if hwid_device_limit is not None:
        kwargs['hwid_device_limit'] = hwid_device_limit
    if tag is not ...:
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
    update = partial(_update_with_device_limit, api, update_call or api.update_user)
    return await update(
        user_id=user_id,
        active_internal_squads=squads,
        external_squad_uuid=external_squad_uuid,
    )


async def patch_managed_traffic(api, *, user_id: int, limit_bytes: int | None = None, status=None):
    """Accounting coordinator's narrow write, under its shared database lease.

    Never reset usage. NO_RESET is mandatory even for an otherwise narrow quota
    update. A zero limit would mean unlimited in Remnawave and is rejected.
    """
    from app.external.remnawave_api import TrafficLimitStrategy, UserStatus
    from app.services.traffic_accounting import AccountingConflict

    kwargs = {'user_id': user_id, 'traffic_limit_strategy': TrafficLimitStrategy.NO_RESET}
    if limit_bytes is not None:
        if type(limit_bytes) is not int or limit_bytes <= 0:
            raise AccountingConflict('Managed panel quota must be positive')
        kwargs['traffic_limit_bytes'] = limit_bytes
    if status is not None:
        if status not in {UserStatus.ACTIVE, UserStatus.DISABLED}:
            raise AccountingConflict('Unsupported managed panel status')
        kwargs['status'] = status
    return await api.update_user(**kwargs)


async def _push_managed_subscription(
    api, subscription, *, db, now, only_fields, reset_devices, user_tag, create_if_missing, recreate_on_missing
):
    from app.services.traffic_accounting import AccountingConflict
    from app.services.traffic_accounting_service import (
        AccountingAccountMissing,
        _fresh_panel,
        _require_lease,
        lock_accounting_subscription,
        reconcile_account_locked,
        settle_snapshot,
    )
    from app.services.traffic_accounting_store import load_account, save_account

    if db is None:
        raise AccountingConflict('Managed panel writes require a database transaction')
    try:
        _require_lease(db, subscription.id)
    except AccountingConflict:
        if db.dirty or db.new:
            raise AccountingConflict('Acquire the accounting lease before changing billing state') from None
        subscription, _ = await lock_accounting_subscription(db, subscription.id)
    loaded = await load_account(db, subscription.id)
    if loaded is None or loaded[1].phase not in {'ready', 'unlimited'}:
        raise AccountingConflict('Accounting migration is incomplete')
    try:
        panel = await _fresh_panel(db, api, subscription)
    except AccountingAccountMissing:
        if not create_if_missing or not recreate_on_missing:
            return PanelWriteResult(panel_user=None, action='no_changes')
        # The worker recreates only after confirmed absence. Keep the explicit
        # sync request durable; never commit a caller's transaction here.
        from app.services.traffic_accounting_service import schedule_projection_after_commit
        from app.services.traffic_accounting_sync import stage_managed_sync

        await stage_managed_sync(db, subscription, only_fields=only_fields, user_tag=user_tag)
        schedule_projection_after_commit(db, subscription.id)
        return PanelWriteResult(panel_user=None, action='queued')
    unlimited = loaded[1].phase == 'unlimited'
    if unlimited:
        if subscription.traffic_limit_gb != 0:
            raise AccountingConflict('A finite plan needs an explicit accounting grant')
        account = settle_snapshot(loaded[0], panel, now=now, exclude_usage=True)
        await save_account(db, subscription.id, account, expected_revision=loaded[1].revision)
    else:
        account = await reconcile_account_locked(db, subscription, panel, now=now)
    from app.services.traffic_accounting_service import schedule_projection_after_commit
    from app.services.traffic_accounting_sync import stage_managed_sync

    await stage_managed_sync(db, subscription, only_fields=only_fields, user_tag=user_tag)
    await reset_managed_devices_once(api, panel.id, enabled=reset_devices)
    schedule_projection_after_commit(db, subscription.id)
    return PanelWriteResult(panel_user=panel, action='queued', panel_user_id=panel.id)


async def reset_managed_devices_once(api, panel_id, *, enabled):
    """Explicit action only; retrying quota delivery must not evict new devices."""
    if enabled is None:
        enabled = settings.RESET_DEVICES_ON_RENEWAL
    if not enabled:
        return
    try:
        if not await api.reset_user_devices(panel_id):
            logger.warning('Managed device reset was not confirmed', panel_user_id=panel_id)
    except Exception as error:
        logger.warning('Managed device reset failed', panel_user_id=panel_id, error_type=type(error).__name__)


async def write_managed_payload_locked(api, panel_user_id, payload, *, now, panel_current=None, only_fields=None):
    """Delivery-only write after the coordinator committed its quota intent."""
    return await _update_with_device_limit(
        api,
        api.update_user,
        **payload.update_kwargs(user_id=panel_user_id, now=now, panel_current=panel_current, only_fields=only_fields),
    )


async def create_managed_replacement(api, user, subscription, account, *, username, now):
    from dataclasses import replace

    from app.external.remnawave_api import UserStatus

    payload = build_panel_payload(
        user,
        subscription,
        multi_tariff=settings.is_multi_tariff_enabled(),
        now=now,
        accounting_account=account,
        accounting_recreation=True,
    )
    payload = replace(payload, username=username, status=UserStatus.DISABLED)
    return await api.create_user(**payload.create_kwargs(now=now))
