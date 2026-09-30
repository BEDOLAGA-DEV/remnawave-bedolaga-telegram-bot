"""Живое меню: фон перерисовывает последнее rich-меню, когда меняются видимые данные.

Ключ live_menu:{chat_id} ставит rich_menu.remember_live_menu, снимает нажатие на меню
(forget_live_menu_on_callback). Проход — только когда бот свободен: 15 мин, при нагрузке,
429 панели, лимите Telegram или ошибке интервал удваивается до 360.
"""

import asyncio
import json
import random
import time
from datetime import UTC, datetime, timedelta

import structlog
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import EditMessageText
from sqlalchemy import update
from sqlalchemy.orm.attributes import set_committed_value

from app.config import settings
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal, _pool_counters, engine
from app.database.models import Subscription
from app.external.remnawave_api import RemnaWaveAPI
from app.localization.texts import get_texts
from app.services.broadcast_service import broadcast_service
from app.services.subscription_service import SubscriptionService
from app.utils.cache import cache
from app.utils.rich_menu import (
    _apply_inline_buttons,
    _input_rich_message,
    _is_media_fetch_error,
    _is_rich_date_error,
    _looks_like_unsupported,
    _mark_logo_unavailable_once,
    _mark_rich_unavailable,
    _resolve_rich_logo_url,
    _strip_tg_time,
    build_main_menu_rich_html,
    is_rich_menu_enabled,
    live_menu_fingerprint,
)
from app.webserver.telegram import TelegramWebhookProcessor


logger = structlog.get_logger(__name__)

MIN_INTERVAL, MAX_INTERVAL = 15 * 60, 360 * 60
# ponytail: пороги на глаз под ~60 пользователей — ручки калибровки
PAUSE, LAG_BUSY, LAG_ABORT = 0.2, 0.05, 0.25
# Новый снимок пишется, только если ключ не менялся с начала обработки (нажатие, новое меню).
_CAS = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "return redis.call('SET', KEYS[1], ARGV[2], 'KEEPTTL') end return false"
)


async def live_menu_loop(monitoring) -> None:
    interval = MIN_INTERVAL
    # Свой клиент панели: RemnaWaveAPI мониторинга в async with подменяет и закрывает общую сессию,
    # параллельный проход рвал бы его запросы («Session is closed»).
    service = SubscriptionService()
    while monitoring.is_running:
        await asyncio.sleep(interval)
        if not (settings.MAIN_MENU_LIVE_ENABLED and is_rich_menu_enabled()) or monitoring.bot is None:
            interval = MIN_INTERVAL
            continue
        try:
            throttled = await refresh_live_menus(monitoring.bot, service)
        except Exception as error:
            logger.exception('Живое меню: ошибка прохода', error=str(error))
            throttled = True
        new_interval = min(interval * 2, MAX_INTERVAL) if throttled else MIN_INTERVAL
        if new_interval != interval:
            logger.info('Живое меню: интервал', minutes=new_interval // 60)
        interval = new_interval


async def _loop_lag() -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.sleep(PAUSE)
    return loop.time() - started - PAUSE


def _pressure() -> str | None:
    if not (settings.MAIN_MENU_LIVE_ENABLED and is_rich_menu_enabled()):
        return 'disabled'
    if RemnaWaveAPI._throttled_until > time.monotonic():
        return 'panel_429'
    if broadcast_service._tasks:  # идёт рассылка из кабинета: лимит Telegram отдаём ей
        return 'broadcast'
    processor = TelegramWebhookProcessor.active
    if processor and processor.is_running and processor._queue.qsize():  # воркеры вебхука заняты, апдейты ждут
        return 'webhook_queue'
    pool = _pool_counters(engine.pool)
    if pool and pool['checked_out'] >= max(1, settings.DATABASE_POOL_SIZE // 2):
        return 'db_pool'
    return None


async def _tracked_keys() -> list[str]:
    """Ключи живых меню через SCAN: KEYS блокирует Redis на обход всего пространства ключей,
    а в нём FSM-состояния, кэши и очереди всего бота."""
    if not cache._connected or cache.redis_client is None:
        return []
    keys = [
        key.decode() if isinstance(key, bytes) else key
        async for key in cache.redis_client.scan_iter(match='live_menu:*', count=500)
    ]
    return list(dict.fromkeys(keys))  # SCAN может вернуть ключ дважды


async def refresh_live_menus(bot, subscription_service) -> bool:
    """Один проход. True — бот/панель/Telegram заняты или была ошибка: следующий проход реже."""
    keys = await _tracked_keys()
    if not keys:
        return False
    random.shuffle(keys)  # проход, прерванный нагрузкой, не должен всякий раз обходить один и тот же хвост
    lag = sorted([await _loop_lag() for _ in range(5)])[2]  # медиана: один всплеск GC не решает
    reason = _pressure() or ('loop_lag' if lag > LAG_BUSY else None)
    if reason:
        logger.info('Живое меню: бот занят, проход пропущен', reason=reason, lag_ms=int(lag * 1000), tracked=len(keys))
        return reason != 'disabled'

    throttled = False
    fetched_at = datetime.now(UTC)
    try:  # ponytail: вся панель одним запросом; фильтр telegramId, если панель вырастет до тысяч
        async with subscription_service.get_api_client() as api:
            used_bytes = {u.id: u.used_traffic_bytes for u in await api.get_all_users_stream(size=1000)}
    except Exception as error:
        logger.warning('Живое меню: панель не ответила, трафик из БД', error=str(error))
        used_bytes, throttled = {}, True

    edited = dropped = 0
    started = time.monotonic()
    for index, key in enumerate(keys):
        if index:
            lag = await _loop_lag()  # пауза между пользователями (≤5 правок/с) и замер нагрузки
            reason = _pressure() or ('loop_lag' if lag > LAG_ABORT else None)
            if reason:
                logger.warning('Живое меню: проход прерван', reason=reason, done=index, tracked=len(keys))
                return reason != 'disabled'
        try:
            result = await _refresh_one(bot, key, used_bytes, fetched_at)
        except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError) as error:
            logger.warning(
                'Живое меню: проход прерван', reason='telegram', error=str(error), done=index, tracked=len(keys)
            )
            return True
        except Exception as error:
            logger.warning('Живое меню: не удалось обновить меню', key=key, error=str(error))
            throttled = True
            continue
        if result == 'unsupported':
            return False
        edited += result == 'edited'
        dropped += result == 'dropped'
    if edited or dropped:
        logger.info(
            'Живое меню: проход',
            tracked=len(keys),
            edited=edited,
            dropped=dropped,
            took_s=round(time.monotonic() - started, 1),
        )
    return throttled


async def _refresh_one(bot, key: str, used_bytes: dict[int, int], fetched_at: datetime) -> str | None:
    raw = await cache.redis_client.get(key)
    if not raw:
        return None
    state = json.loads(raw)
    chat_id = int(key.rsplit(':', 1)[1])
    multi = settings.is_multi_tariff_enabled()
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, chat_id)
        if user is None:
            await cache.delete(key)
            return 'dropped'
        changed = False
        for sub in filter(None, user.subscriptions if multi else [user.subscription]):
            panel_bytes = used_bytes.get(sub.remnawave_id if multi else user.remnawave_id)
            # ponytail: 5 мин запаса — сброс трафика коммитит БД до вызова панели, а now() в БД — начало транзакции
            if panel_bytes is None or (sub.updated_at and sub.updated_at > fetched_at - timedelta(minutes=5)):
                continue  # подписку меняли около снимка (продление, сброс трафика): пропускаем один проход
            used_gb = panel_bytes / 1024**3
            if abs((sub.traffic_used_gb or 0) - used_gb) > 0.01:  # допуск как _TRAFFIC_TOLERANCE_GB в panel_sync
                # updated_at не трогаем: по нему полная синхронизация решает, чьи поля новее (panel_sync/projection.py)
                await db.execute(
                    update(Subscription)
                    .where(Subscription.id == sub.id)
                    .values(traffic_used_gb=used_gb, updated_at=Subscription.updated_at)
                    .execution_options(synchronize_session=False)
                )
                set_committed_value(sub, 'traffic_used_gb', used_gb)
                changed = True
        if changed:
            await db.commit()
        texts = get_texts(user.language)
        fp = live_menu_fingerprint(user, texts)
        if fp == state.get('fp'):
            return None
        from app.handlers.menu import build_main_menu_keyboard  # menu.py сам импортирует rich_menu

        keyboard = await build_main_menu_keyboard(user, db)
        rich_html = await build_main_menu_rich_html(user, texts, db)
        language = user.language
    rich_html, keyboard = _apply_inline_buttons(rich_html, keyboard, for_edit=True)
    if await cache.redis_client.get(key) != raw:
        return None  # пока собирали, нажали кнопку на меню или пришло новое меню

    async def edit(html_: str) -> None:
        await bot(
            EditMessageText(
                chat_id=chat_id,
                message_id=state['m'],
                parse_mode=None,
                rich_message=_input_rich_message(html_, language),
                reply_markup=keyboard,
            )
        )

    try:
        try:
            await edit(rich_html)
        except TelegramBadRequest as error:
            if not _is_rich_date_error(error):
                raise
            await edit(_strip_tg_time(rich_html))
    except (TelegramBadRequest, TelegramNotFound, TelegramForbiddenError) as error:
        if 'message is not modified' not in str(error).lower():
            if _looks_like_unsupported(error):
                _mark_rich_unavailable(error)
                return 'unsupported'
            if _resolve_rich_logo_url() and _is_media_fetch_error(error):
                _mark_logo_unavailable_once(error)  # следующий проход уже без логотипа, ключ живёт
                return None
            await cache.delete(key)  # удалено, заблокирован, нельзя править
            logger.info('Живое меню: меню больше не отслеживается', chat_id=chat_id, error=str(error)[:200])
            return 'dropped'
    except ClientDecodeError:
        pass  # правка дошла, aiogram не разобрал rich-ответ (см. except Exception в try_send_rich_main_menu)
    await cache.redis_client.eval(_CAS, 1, key, raw, json.dumps({'m': state['m'], 'fp': fp}))
    return 'edited'
