"""Учёт премиум-трафика по сквадам и снятие доступа при исчерпании.

Панель не умеет ограничивать трафик отдельным сквадом — у пользователя одно поле
``trafficLimitBytes`` на всю учётную запись. Этот сервис считает расход сам и сам
снимает доступ, убирая сквад из ``activeInternalSquads``.

**Как считается расход.** ``POST /api/bandwidth-stats/nodes/usage`` принимает
список нод и диапазон дат, а отдаёт расход каждого пользователя за этот
диапазон. Один запрос покрывает весь сквад и всех его подписчиков сразу, поэтому
стоимость прохода растёт от числа сквадов, а не от числа пользователей.

Диапазон у эндпоинта задаётся датами ``YYYY-MM-DD`` — время панель отвергает.
Отсюда согласованное огрубление: в первые сутки периода в расход попадает и то,
что потрачено до сброса. Раз в период это незаметно, а точности до суток хватает.

**Границы периода** считаются отдельно (``utils/premium_traffic_period``) по
режиму сброса тарифа, чтобы премиум обнулялся синхронно с общим трафиком.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

import structlog
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.premium_traffic import (
    get_or_create_state,
    record_usage,
    start_new_period,
)
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionPremiumTraffic, SubscriptionStatus, Tariff
from app.services.remnawave_service import RemnaWaveService
from app.utils.panel_node_usage import normalize_node_usage
from app.utils.premium_traffic import (
    BYTES_IN_GB,
    PremiumSquadConfig,
    get_premium_squads_for_tariff,
    parse_premium_squads,
)
from app.utils.premium_traffic_period import period_anchor, resolve_period_start


logger = structlog.get_logger(__name__)


DEFAULT_INTERVAL_SECONDS = 300
WARNING_THRESHOLD = 0.8
# Ноды сквада меняются редко, а спрашивают их на каждом проходе по каждому
# скваду. Кеш живёт дольше интервала воркера, чтобы не дёргать панель впустую.
NODES_CACHE_TTL_SECONDS = 3600
# Карточка панельного пользователя нужна ради `lastTrafficResetAt` (досрочный
# сброс) и фактического набора сквадов (сверка). Обе величины меняются редко, а
# запрашивать их на каждого подписчика каждый проход — самая дорогая часть
# работы: при пяти тысячах подписок это больше десятка запросов в секунду
# непрерывно. Держим час, разброс не даёт всем протухнуть одновременно.
PANEL_USER_CACHE_TTL_SECONDS = 3600
PANEL_USER_CACHE_JITTER = 0.25


def panel_user_id_for_subscription(subscription: Any) -> int | None:
    """Аккаунт в панели, к которому относятся расход и сквады подписки.

    В мультиподписках у каждой подписки свой аккаунт, и подстановка аккаунта
    пользователя увела бы расход, снятие сквада или сброс трафика на соседний
    тариф. Поэтому там — только аккаунт самой подписки, даже если его ещё нет.
    """
    if settings.is_multi_tariff_enabled():
        raw = getattr(subscription, 'remnawave_id', None)
    else:
        user = getattr(subscription, 'user', None)
        raw = getattr(subscription, 'remnawave_id', None) or (user.remnawave_id if user else None)
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


@dataclass
class _Target:
    """Подписка, которую проверяем по одному конкретному премиум-скваду."""

    subscription: Subscription
    config: PremiumSquadConfig
    panel_user_id: int
    # Как назвать сервер в уведомлении: своё название из тарифа, иначе имя
    # сервера. Без него человек с несколькими премиум-серверами не поймёт, на
    # каком именно кончился лимит.
    display_name: str = ''


class PremiumTrafficService:
    """Периодический учёт премиум-трафика."""

    def __init__(self) -> None:
        self._running = False
        self._bot: Bot | None = None
        self._nodes_cache: dict[str, tuple[float, list[str]]] = {}
        # Живёт между проходами, а не внутри одного: см. PANEL_USER_CACHE_TTL_SECONDS.
        self._panel_users: dict[int, tuple[float, Any]] = {}

    def set_bot(self, bot: Bot) -> None:
        self._bot = bot

    def is_enabled(self) -> bool:
        return bool(getattr(settings, 'PREMIUM_TRAFFIC_ENABLED', True))

    def get_check_interval_seconds(self) -> int:
        raw = getattr(settings, 'PREMIUM_TRAFFIC_CHECK_INTERVAL_SECONDS', DEFAULT_INTERVAL_SECONDS)
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL_SECONDS
        # Чаще минуты смысла нет: панель агрегирует статистику с задержкой, а
        # запросов станет больше без выигрыша в точности.
        return max(60, interval)

    # ---------------------------------------------------------------- цикл

    async def start_monitoring(self) -> None:
        self._running = True
        interval = self.get_check_interval_seconds()
        logger.info('🔄 Запуск учёта премиум-трафика', interval_seconds=interval)

        while self._running:
            try:
                stats = await self.process_once()
                if stats['limited'] or stats['restored'] or stats['errors']:
                    logger.info('📊 Проход по премиум-трафику', **stats)
            except Exception as error:
                logger.error('Ошибка в цикле учёта премиум-трафика', error=error, exc_info=True)
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------- главное

    async def process_once(self) -> dict[str, int]:
        """Один проход: посчитать расход, снять и вернуть сквады."""
        stats = {'checked': 0, 'limited': 0, 'restored': 0, 'warned': 0, 'errors': 0}

        service = RemnaWaveService()
        if not service.is_configured:
            logger.debug('Панель не настроена, проход по премиум-трафику пропущен')
            return stats

        async with AsyncSessionLocal() as db:
            # До сбора целей: если премиум убрали из всех тарифов, целей не будет,
            # а снятые сквады всё равно надо вернуть.
            stats['restored'] += await self._release_orphaned_limits(db, service)

            targets = await self._collect_targets(db)
            if not targets:
                return stats

            now = datetime.now(UTC)
            async with service.get_api_client() as api:
                # Группируем по скваду и дате начала периода: у эндпоинта один
                # диапазон на запрос, а у подписок с одинаковым режимом сброса
                # он совпадает. При календарных режимах это один запрос на сквад.
                groups: dict[tuple[str, str], list[_Target]] = defaultdict(list)
                # Начало периода — своё у каждой пары «подписка + сквад»:
                # состояния одной подписки могут разойтись, если один сквад уже
                # перевалил границу, а другой ещё нет.
                period_starts: dict[tuple[int, str], datetime] = {}
                for target in targets:
                    try:
                        period_start = await self._resolve_period(db, api, target, now)
                    except Exception as error:
                        stats['errors'] += 1
                        logger.warning(
                            'Не удалось определить период премиум-лимита',
                            subscription_id=target.subscription.id,
                            squad_uuid=target.config.squad_uuid,
                            error=error,
                        )
                        continue
                    period_starts[(target.subscription.id, target.config.squad_uuid)] = period_start
                    groups[(target.config.squad_uuid, period_start.date().isoformat())].append(target)

                await db.commit()

                for (squad_uuid, start_date), group in groups.items():
                    try:
                        usage = await self._fetch_usage(api, squad_uuid, start_date, now)
                    except Exception as error:
                        # Сбой панели ничего не снимает: состояние не трогаем,
                        # следующий проход посчитает заново.
                        stats['errors'] += len(group)
                        logger.warning(
                            'Не удалось получить расход по скваду',
                            squad_uuid=squad_uuid,
                            start_date=start_date,
                            error=error,
                        )
                        continue

                    for target in group:
                        stats['checked'] += 1
                        try:
                            outcome = await self._apply_usage(
                                db,
                                api,
                                target,
                                used_bytes=usage.get(target.panel_user_id, 0),
                                period_start=period_starts[(target.subscription.id, target.config.squad_uuid)],
                                now=now,
                                panel_user=self._cached_panel_user(target.panel_user_id),
                            )
                        except Exception as error:
                            stats['errors'] += 1
                            logger.error(
                                'Ошибка обработки премиум-лимита',
                                subscription_id=target.subscription.id,
                                squad_uuid=squad_uuid,
                                error=error,
                                exc_info=True,
                            )
                            continue
                        if outcome:
                            stats[outcome] += 1

            await db.commit()

        return stats

    # ------------------------------------------------------------- сборка

    async def _collect_targets(self, db: AsyncSession) -> list[_Target]:
        """Активные подписки с премиум-сквадами в тарифе.

        Сначала отбираем тарифы, потом подписки по ним. Тарифов десятки, а
        подписок могут быть десятки тысяч — тянуть их все и отсеивать в Python
        значило бы вычитывать таблицу целиком каждые пять минут.

        Фильтровать по JSON средствами БД нельзя переносимо: `server_traffic_limits`
        хранится как JSON и в PostgreSQL, и в SQLite, а условия к нему у них разные.
        """
        tariffs = await db.execute(select(Tariff.id, Tariff.server_traffic_limits))
        premium_tariff_ids = [tariff_id for tariff_id, limits in tariffs.all() if parse_premium_squads(limits)]
        if not premium_tariff_ids:
            return []

        result = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.tariff), selectinload(Subscription.user))
            .where(
                Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
                Subscription.tariff_id.in_(premium_tariff_ids),
            )
        )
        subscriptions = list(result.scalars().all())
        squad_names = await self._squad_display_names(db, subscriptions)

        targets: list[_Target] = []
        for subscription in subscriptions:
            premium = get_premium_squads_for_tariff(subscription.tariff)
            if not premium:
                continue
            panel_user_id = self._panel_user_id(subscription)
            if not panel_user_id:
                continue
            connected = set(subscription.connected_squads or [])
            for squad_uuid, config in premium.items():
                # Сквад, на который подписка не даёт права, не наш случай даже
                # если лимит на него в тарифе задан.
                if squad_uuid in connected:
                    targets.append(
                        _Target(
                            subscription,
                            config,
                            panel_user_id,
                            config.name or squad_names.get(squad_uuid, ''),
                        )
                    )
        return targets

    @staticmethod
    async def _squad_display_names(db: AsyncSession, subscriptions: list[Subscription]) -> dict[str, str]:
        """Имена премиум-серверов одним запросом на весь проход."""
        from app.database.crud.server_squad import get_squad_display_names

        uuids: set[str] = set()
        for subscription in subscriptions:
            uuids.update(get_premium_squads_for_tariff(subscription.tariff))
        return await get_squad_display_names(db, sorted(uuids))

    @staticmethod
    def _panel_user_id(subscription: Subscription) -> int | None:
        return panel_user_id_for_subscription(subscription)

    # ------------------------------------------------- отменённые лимиты

    async def _release_orphaned_limits(self, db: AsyncSession, service: Any) -> int:
        """Вернуть сквады, снятые за лимит, который больше не действует.

        Фильтр отправки вычитает сквад по отметке ``is_limited``, не заглядывая в
        тариф. Если клиент перешёл на тариф, где у сервера нет премиум-лимита, или
        лимит с сервера сняли в админке, запись выпадает из обхода — воркер берёт
        только премиум-сквады текущего тарифа, — и сквад остался бы вырезанным
        навсегда.

        Запись не удаляем, только снимаем отметку: докупленное в ней должно
        пережить обратное включение лимита в том же периоде.
        """
        orphaned = [
            (state, subscription)
            for state, subscription in await self._limited_states(db)
            if self._limit_no_longer_applies(state, subscription)
        ]
        if not orphaned:
            return 0

        active = {SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value}
        released = 0
        async with service.get_api_client() as api:
            for state, subscription in orphaned:
                # Сначала коммит: фильтр отправки читает отметку из базы.
                state.is_limited = False
                await db.commit()

                panel_user_id = self._panel_user_id(subscription)
                if subscription.status in active and panel_user_id:
                    try:
                        await self._push_subscription_squads(api, subscription, panel_user_id)
                    except Exception as error:
                        # Вернём отметку, чтобы следующий проход попробовал снова:
                        # иначе запись уже не попала бы в выборку, а сквада в
                        # панели так и не было бы.
                        state.is_limited = True
                        await db.commit()
                        logger.warning(
                            'Не удалось вернуть сквад с отменённым премиум-лимитом',
                            subscription_id=subscription.id,
                            squad_uuid=state.squad_uuid,
                            error=error,
                        )
                        continue
                # Неактивной подписке сквад вернёт ближайшая синхронизация при
                # продлении: отметки больше нет, фильтр его пропустит.
                released += 1
                logger.info(
                    'Премиум-лимит больше не действует, сквад возвращён',
                    subscription_id=subscription.id,
                    squad_uuid=state.squad_uuid,
                )
        return released

    @staticmethod
    async def _limited_states(db: AsyncSession) -> list[tuple[Any, Subscription]]:
        """Снятые записи вместе с подписками. Их единицы, выборка по флагу дешёвая."""
        result = await db.execute(
            select(SubscriptionPremiumTraffic, Subscription)
            .join(Subscription, Subscription.id == SubscriptionPremiumTraffic.subscription_id)
            .options(selectinload(Subscription.tariff), selectinload(Subscription.user))
            .where(SubscriptionPremiumTraffic.is_limited.is_(True))
        )
        return list(result.tuples().all())

    @staticmethod
    def _limit_no_longer_applies(state: Any, subscription: Any) -> bool:
        return state.squad_uuid not in get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))

    # ------------------------------------------------------------- период

    async def _resolve_period(
        self,
        db: AsyncSession,
        api: Any,
        target: _Target,
        now: datetime,
    ) -> datetime:
        """Определить период и, если он сменился, начать новый."""
        subscription = target.subscription
        state = await get_or_create_state(
            db,
            subscription.id,
            target.config.squad_uuid,
            limit_bytes=target.config.limit_bytes,
            period_start_at=now,
        )

        panel_user = await self._panel_user(api, target.panel_user_id)
        panel_reset_at = getattr(panel_user, 'last_traffic_reset_at', None)
        first_connected_at = getattr(panel_user, 'first_connected_at', None)
        anchor = period_anchor(first_connected_at, subscription.start_date, fallback=now)
        mode = getattr(subscription.tariff, 'traffic_reset_mode', None) or settings.DEFAULT_TRAFFIC_RESET_STRATEGY

        resolved = resolve_period_start(
            mode,
            anchor=anchor,
            now=now,
            panel_reset_at=panel_reset_at,
            acknowledged_panel_reset_at=state.panel_reset_ack_at,
        )

        if state.last_checked_at is None:
            # Запись ни разу не замерялась — её начало периода временное. Создают
            # её «с этой секунды» и воркер, и докупка, и выдача админом: верное
            # начало без карточки из панели не посчитать. Проверка на смену
            # периода ниже его не подхватит — верное начало всегда раньше
            # «сейчас», — и расход с начала периода до включения лимита терялся бы.
            # Счётчики не обнуляем: докупленное до первого замера должно остаться.
            state.period_start_at = resolved
            state.baseline_bytes = None
            if panel_reset_at is not None:
                state.panel_reset_ack_at = panel_reset_at
        elif resolved > _as_utc(state.period_start_at):
            start_new_period(
                state,
                period_start_at=resolved,
                limit_bytes=target.config.limit_bytes,
                panel_reset_ack_at=panel_reset_at,
            )
            logger.info(
                'Новый период премиум-лимита',
                subscription_id=subscription.id,
                squad_uuid=target.config.squad_uuid,
                period_start=resolved.isoformat(),
            )
        elif state.limit_bytes != target.config.limit_bytes and not state.is_limited:
            # Лимит в тарифе поменяли посреди периода. Поднять потолок можно
            # сразу; понижение до уже снятого сквада не трогаем, чтобы правка
            # тарифа не возвращала доступ задним числом.
            state.limit_bytes = target.config.limit_bytes

        return _as_utc(state.period_start_at)

    async def _panel_user(self, api: Any, panel_user_id: int) -> Any:
        """Пользователь из панели: отметки времени и фактический набор сквадов.

        Кеш переживает проходы: запрашивать карточку на каждого подписчика раз в
        пять минут — самая дорогая часть работы, и обе нужные величины меняются
        куда реже. Немедленные действия — снятие и возврат сквада — от кеша не
        зависят, они идут по свежему замеру расхода.
        """
        cached = self._cached_panel_user(panel_user_id)
        if cached is not None:
            return cached

        panel_user = await api.get_user_by_id(panel_user_id)
        # Разброс срока жизни: иначе через час все записи протухли бы разом и
        # один проход дал бы залп на всю базу подписчиков.
        ttl = PANEL_USER_CACHE_TTL_SECONDS * (1 + random.uniform(-PANEL_USER_CACHE_JITTER, PANEL_USER_CACHE_JITTER))
        self._panel_users[panel_user_id] = (asyncio.get_running_loop().time() + ttl, panel_user)
        return panel_user

    def _cached_panel_user(self, panel_user_id: int) -> Any:
        """Непротухшая карточка из кеша либо None."""
        entry = self._panel_users.get(panel_user_id)
        if entry is None:
            return None
        expires_at, panel_user = entry
        if asyncio.get_running_loop().time() >= expires_at:
            del self._panel_users[panel_user_id]
            return None
        return panel_user

    def invalidate_panel_user(self, panel_user_id: int) -> None:
        """Сбросить кеш после того, как мы сами изменили набор сквадов."""
        self._panel_users.pop(panel_user_id, None)

    # -------------------------------------------------------------- расход

    async def _squad_nodes(self, api: Any, squad_uuid: str) -> list[str]:
        cached = self._nodes_cache.get(squad_uuid)
        now = asyncio.get_running_loop().time()
        if cached and now - cached[0] < NODES_CACHE_TTL_SECONDS:
            return cached[1]

        nodes = await api.get_internal_squad_accessible_nodes(squad_uuid)
        uuids = [node.uuid for node in nodes if getattr(node, 'uuid', None)]
        self._nodes_cache[squad_uuid] = (now, uuids)
        return uuids

    async def _fetch_usage(self, api: Any, squad_uuid: str, start_date: str, now: datetime) -> dict[int, int]:
        """Расход всех пользователей сквада за период: {panel_user_id: байты}."""
        node_uuids = await self._squad_nodes(api, squad_uuid)
        if not node_uuids:
            return {}

        # Верхняя граница — завтра: панель включает конечную дату не всегда
        # предсказуемо, а лишние сутки в диапазоне не могут занизить расход.
        end_date = (now + timedelta(days=1)).date().isoformat()
        response = await api.get_bandwidth_stats_nodes_usage(node_uuids, start_date, end_date)

        totals: dict[int, int] = defaultdict(int)
        for node_entry in (response or {}).get('nodes') or []:
            node_uuid = node_entry.get('uuid') or ''
            for item in normalize_node_usage(node_entry.get('users'), node_uuid):
                user_id = item['user_id']
                if user_id is not None:
                    totals[user_id] += item['total_bytes']
        return dict(totals)

    # ------------------------------------------------------------ решение

    async def _apply_usage(
        self,
        db: AsyncSession,
        api: Any,
        target: _Target,
        *,
        used_bytes: int,
        period_start: datetime,
        now: datetime,
        panel_user: Any = None,
    ) -> str | None:
        from app.database.crud.premium_traffic import get_state

        state = await get_state(db, target.subscription.id, target.config.squad_uuid)
        if state is None:
            return None

        record_usage(state, self._net_usage(state, used_bytes, period_start, now), checked_at=now)

        if state.is_exhausted and not state.is_limited:
            await self._limit_squad(db, api, target, state)
            return 'limited'

        if state.is_limited and not state.is_exhausted:
            await self._restore_squad(db, api, target, state)
            return 'restored'

        if not state.is_limited and not state.notified_80 and self._crossed_warning(state):
            state.notified_80 = True
            await self._notify_warning(target, state)
            return 'warned'

        # Сверка с панелью — в обе стороны. Флаг `is_limited` пишется в базу до
        # отправки (иначе фильтр её не увидит), и если отправка не дошла —
        # панель недоступна, отказала по лимиту запросов, оборвалась сессия, —
        # ветки выше сюда уже не попадут: флаг-то стоит как надо.
        #
        # * Флаг снят, а сквада в панели нет — докупка через кабинет вернула
        #   флаг, но не сквад. Без сверки доступ не вернулся бы никогда.
        # * Флаг стоит, а сквад в панели есть — снятие не дошло. Без сверки
        #   исчерпанный лимит продолжал бы работать до конца периода.
        #
        # Отправка — тот же штатный набор через фильтр: он сам уберёт снятое и
        # оставит положенное, так что одна ветка чинит оба расхождения.
        has_squad = self._panel_has_squad(target, panel_user)
        if has_squad is not None and has_squad == state.is_limited:
            await self._push_squads(db, api, target)
            logger.info(
                'Премиум-сквад досинхронизирован с панелью',
                subscription_id=target.subscription.id,
                squad_uuid=target.config.squad_uuid,
                direction='снят' if state.is_limited else 'возвращён',
            )
            return 'limited' if state.is_limited else 'restored'

        return None

    @staticmethod
    def _panel_has_squad(target: _Target, panel_user: Any) -> bool | None:
        """Есть ли сквад в панели; None — если панель об этом не сказала.

        Панель отдаёт сквады объектами `{uuid, name}`, а не строками. Разбор
        берём общий с grace-механизмом, чтобы обе части читали одно и то же.
        """
        if panel_user is None:
            return None
        raw = getattr(panel_user, 'active_internal_squads', None)
        # `None` — панель не сказала, сверять не с чем. Пустой список — сказала,
        # что сквадов нет, и это ровно тот случай, ради которого сверка нужна.
        if raw is None:
            return None

        from app.services.grace_access_runtime import _extract_panel_squads

        return target.config.squad_uuid in set(_extract_panel_squads(raw))

    @staticmethod
    def _net_usage(state: Any, raw_bytes: int, period_start: datetime, now: datetime) -> int:
        """Расход за период с поправкой на первые сутки.

        Диапазон в статистике панели задаётся датами без времени, поэтому запрос
        за день начала периода приносит и то, что потрачено до сброса. Если
        период начался не в полночь, первый замер целиком относится к прошлому
        периоду — запоминаем его и дальше вычитаем.

        Поправку снимаем, только пока идут те же сутки, что и начало периода.
        Если воркер простоял дольше, вычитать уже нечего: первый замер включал
        бы законный расход нового периода, и мы подарили бы пользователю лимит.
        """
        if state.baseline_bytes is None:
            starts_midday = period_start.timetz().replace(tzinfo=None) != time(0, 0)
            same_day = now.date() == period_start.date()
            state.baseline_bytes = raw_bytes if (starts_midday and same_day) else 0
        return max(0, raw_bytes - (state.baseline_bytes or 0))

    @staticmethod
    def _crossed_warning(state: Any) -> bool:
        total = state.total_limit_bytes
        if total <= 0:
            return False
        return (state.used_bytes or 0) >= total * WARNING_THRESHOLD

    async def _limit_squad(self, db: AsyncSession, api: Any, target: _Target, state: Any) -> None:
        """Снять сквад: сперва отметить в базе, потом отправить в панель.

        Порядок важен. ``effective_panel_squads`` вычитает снятые сквады из
        набора, читая базу, — если отправить раньше коммита, фильтр ещё не
        увидит отметку и вернёт сквад обратно.
        """
        state.is_limited = True
        await db.commit()

        await self._push_squads(db, api, target)
        logger.info(
            'Премиум-сквад снят за перерасход',
            subscription_id=target.subscription.id,
            squad_uuid=target.config.squad_uuid,
            used_gb=round((state.used_bytes or 0) / BYTES_IN_GB, 2),
            limit_gb=round(state.total_limit_bytes / BYTES_IN_GB, 2),
        )
        await self._notify_exhausted(target, state)

    async def _restore_squad(self, db: AsyncSession, api: Any, target: _Target, state: Any) -> None:
        state.is_limited = False
        await db.commit()

        await self._push_squads(db, api, target)
        logger.info(
            'Премиум-сквад возвращён',
            subscription_id=target.subscription.id,
            squad_uuid=target.config.squad_uuid,
        )

    async def _push_squads(self, db: AsyncSession, api: Any, target: _Target) -> None:
        """Переотправить набор сквадов в панель.

        Через штатный вход сервиса синхронизации, а не своим запросом: там же
        живёт фильтр снятых премиум-сквадов, и собирать набор здесь означало бы
        завести вторую копию правила. Полное состояние подписки не шлём —
        меняются только сквады.

        Обёртка грейс-доступа обязательна: у него свои двухфазные переходы, и
        запись мимо неё разъехалась бы с открытой сессией.
        """
        await self._push_subscription_squads(api, target.subscription, target.panel_user_id)

    async def _push_subscription_squads(self, api: Any, subscription: Any, panel_user_id: int) -> None:
        from app.services.grace_access_runtime import update_panel_user_grace_safe
        from app.services.panel_sync import patch_panel_squads

        tariff = getattr(subscription, 'tariff', None)
        await patch_panel_squads(
            api,
            user_id=panel_user_id,
            squads=list(subscription.connected_squads or []),
            external_squad_uuid=getattr(tariff, 'external_squad_uuid', None),
            subscription_id=subscription.id,
            update_call=lambda **kwargs: update_panel_user_grace_safe(api, subscription.id, **kwargs),
        )
        # Набор сквадов в панели только что изменился — иначе сверка на
        # следующем проходе увидела бы протухший снимок и отправила бы всё заново.
        self.invalidate_panel_user(panel_user_id)

    # ------------------------------------------------------- уведомления

    async def _notify_warning(self, target: _Target, state: Any) -> None:
        await self._notify(
            target,
            'PREMIUM_TRAFFIC_WARNING',
            (
                '⚠️ <b>Премиум-трафик заканчивается</b>\n\n'
                'Использовано {used} ГБ из {limit} ГБ.\n'
                'При исчерпании доступ к этим серверам будет приостановлен до конца периода.'
            ),
            state,
        )

    async def _notify_exhausted(self, target: _Target, state: Any) -> None:
        await self._notify(
            target,
            'PREMIUM_TRAFFIC_EXHAUSTED',
            (
                '🚫 <b>Премиум-трафик исчерпан</b>\n\n'
                'Израсходовано {limit} ГБ. Доступ к этим серверам приостановлен.\n'
                'Остальные серверы продолжают работать.'
            ),
            state,
        )

    async def _notify(self, target: _Target, key: str, default_text: str, state: Any) -> None:
        if self._bot is None:
            return
        user = target.subscription.user
        if user is None or not getattr(user, 'telegram_id', None):
            return

        from app.localization.texts import get_texts

        texts = get_texts(getattr(user, 'language', 'ru'))
        # Без названия человек с несколькими премиум-серверами не поймёт, на
        # каком именно кончился лимит.
        name = target.display_name or texts.t('PREMIUM_TRAFFIC_LABEL', 'Премиум-трафик')
        message = texts.t(key, default_text).format(
            name=name,
            used=round((state.used_bytes or 0) / BYTES_IN_GB, 1),
            limit=round(state.total_limit_bytes / BYTES_IN_GB, 1),
        )
        try:
            await self._bot.send_message(user.telegram_id, message, parse_mode='HTML')
        except Exception as error:
            logger.warning(
                'Не удалось отправить уведомление о премиум-трафике',
                telegram_id=user.telegram_id,
                error=error,
            )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


premium_traffic_service = PremiumTrafficService()
