"""Персистентное хранилище ошибок приложения со статусом доставки.

Зачем: провал доставки уведомления в админ-чат намеренно логируется как
``warning``, а не ``error`` — иначе ``TelegramNotifierProcessor`` попробует
переслать эту ошибку в тот же недоступный чат и получится петля усиления.
Следствие: когда канал до Telegram лежит, ошибки не всплывают нигде. Этот
сервис пишет их в БД ДО попытки отправки, поэтому запись переживает отказ
любого числа путей доставки.

Все ошибки самого сервиса логируются через ``logger.warning`` — уровень
``error`` отсюда снова ушёл бы в тот же конвейер и вызвал рекурсию.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, insert, update

from app.database.database import AsyncSessionLocal
from app.database.models import SystemErrorEvent


logger = structlog.get_logger(__name__)

# Очередь ограничена намеренно: при шторме ошибок лучше потерять хвост, чем
# съесть память процесса. Потери считаются и видны в логе.
QUEUE_MAX_SIZE = 1000

# Ограничения на размер полей, чтобы одна ошибка с гигантским трейсбеком
# не раздула таблицу.
MAX_EVENT_LEN = 4000
MAX_TRACEBACK_LEN = 20000
MAX_ERROR_TYPE_LEN = 255
MAX_LOGGER_LEN = 255

RETENTION_DAYS = 30
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60

# Статусы доставки
STATUS_PENDING = 'pending'
STATUS_SENT = 'sent'
STATUS_FAILED = 'failed'
STATUS_SUPPRESSED = 'suppressed'
STATUS_SKIPPED = 'skipped'


def _redact(text: str) -> str:
    """Вырезать Telegram-токены перед записью в БД.

    Тот же фильтр, что применяется на пути в админ-чат: aiohttp кладёт в
    сообщение и трейсбек полный URL вида ``.../bot<TOKEN>/sendMessage``. Без
    него журнал складывал бы токен бота в открытом виде в таблицу и отдавал
    его наружу через ``GET /admin/system-errors/{id}``.
    """
    try:
        from app.services.admin_notification_service import _redact_telegram_secrets

        return _redact_telegram_secrets(text)
    except Exception:
        # Молча: error-уровень отсюда вернулся бы в тот же конвейер. Лучше
        # потерять запись, чем записать секрет.
        return ''


def _truncate(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return _redact(text)[:limit]


class SystemErrorLogService:
    """Фоновый писатель событий об ошибках в БД."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._worker: asyncio.Task | None = None
        self._cleanup_worker: asyncio.Task | None = None
        self._dropped = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._worker and not self._worker.done():
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._worker = asyncio.create_task(self._run())
        self._cleanup_worker = asyncio.create_task(self._run_cleanup())
        logger.info('SystemErrorLogService запущен', queue_max_size=QUEUE_MAX_SIZE)

    async def stop(self, drain_timeout: float = 5.0) -> None:
        """Дописать то, что уже в очереди, и остановить воркеров.

        Слив обязателен: при аварийном завершении в очереди лежат ровно те
        ошибки, которые к этому завершению и привели. Отмена воркера без слива
        выбросила бы их — то есть самый ценный случай журнал бы и потерял.
        """
        queue = self._queue
        if queue is not None and self._worker and not self._worker.done():
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(queue.join(), timeout=drain_timeout)

        for task in (self._worker, self._cleanup_worker):
            if task and not task.done():
                task.cancel()
                await asyncio.wait([task])
        self._worker = None
        self._cleanup_worker = None

    @property
    def is_running(self) -> bool:
        return bool(self._worker and not self._worker.done())

    # ------------------------------------------------------------------
    # Публичный API — вызывается из синхронного лог-конвейера
    # ------------------------------------------------------------------

    def record(self, event_dict: dict[str, Any], dedup_hash: str | None = None) -> str | None:
        """Поставить событие в очередь на запись. Возвращает event_uid.

        Безопасно для вызова из синхронного кода: при переполнении очереди или
        отсутствии живого воркера просто возвращает None, ничего не бросая.
        """
        try:
            event_uid = uuid.uuid4().hex
            payload = self._build_payload(event_dict, event_uid, dedup_hash)
            if not self._enqueue('insert', payload):
                return None
            return event_uid
        except Exception as e:  # никогда не мешаем работе приложения
            self._warn('Не удалось поставить ошибку в очередь записи', e)
            return None

    def mark(self, event_uid: str | None, status: str, error: Any = None) -> None:
        """Обновить статус доставки ранее записанного события."""
        if not event_uid:
            return
        try:
            self._enqueue(
                'status',
                {
                    'event_uid': event_uid,
                    'status': status,
                    'error': _truncate(error, 1000),
                },
            )
        except Exception as e:
            self._warn('Не удалось поставить обновление статуса в очередь', e)

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    @staticmethod
    def _warn(message: str, error: Any = None) -> None:
        """ВАЖНО: только warning. logger.error отсюда уйдёт в
        TelegramNotifierProcessor, тот снова позовёт этот сервис — рекурсия.
        """
        try:
            logger.warning(message, error=str(error)[:200] if error else None)
        except Exception:
            # Намеренно молча: это последний рубеж жалобы самого журнала ошибок.
            # Пожаловаться на сбой предупреждения больше некому и нечем — любая
            # попытка снова придёт сюда же.
            pass

    def _put_nowait(self, op: str, payload: dict[str, Any]) -> bool:
        queue = self._queue
        if queue is None:
            return False
        try:
            queue.put_nowait((op, payload))
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                self._warn(f'Очередь записи ошибок переполнена, потеряно событий: {self._dropped}')
            return False

    def _enqueue(self, op: str, payload: dict[str, Any]) -> bool:
        """Поставить операцию в очередь из любого потока.

        ``asyncio.Queue`` не потокобезопасна, а ошибки логируются в том числе
        из пула потоков (``run_in_executor``). Поэтому из чужого потока кладём
        через ``call_soon_threadsafe``, а не напрямую.
        """
        loop = self._loop
        if self._queue is None or loop is None:
            return False

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            return self._put_nowait(op, payload)

        try:
            loop.call_soon_threadsafe(self._put_nowait, op, payload)
            return True
        except RuntimeError:
            # Цикл уже закрыт — записывать некуда, но падать нельзя.
            return False

    @staticmethod
    def _build_payload(event_dict: dict[str, Any], event_uid: str, dedup_hash: str | None) -> dict[str, Any]:
        exc_info = event_dict.get('exc_info')
        error_type: str | None = None
        traceback_text: str | None = None

        if isinstance(exc_info, tuple) and len(exc_info) == 3:
            if exc_info[0] is not None:
                error_type = exc_info[0].__name__
            if exc_info[2] is not None:
                import traceback as tb_module

                traceback_text = ''.join(tb_module.format_exception(*exc_info))

        if error_type is None:
            candidate = event_dict.get('error')
            if isinstance(candidate, BaseException):
                error_type = type(candidate).__name__

        # Контекст: всё, что не служебное и сериализуемо в строку.
        skip_keys = {'exc_info', 'event', 'level', 'logger', 'timestamp', '_admin_notified'}
        context: dict[str, str] = {}
        for key, value in event_dict.items():
            if key in skip_keys:
                continue
            try:
                context[str(key)[:64]] = _redact(str(value))[:500]
            except Exception:
                continue

        raw_user_id = event_dict.get('user_id')
        try:
            user_id = int(raw_user_id) if raw_user_id is not None else None
        except (TypeError, ValueError):
            user_id = None

        return {
            'event_uid': event_uid,
            'level': _truncate(event_dict.get('level', 'error'), 16),
            'logger_name': _truncate(event_dict.get('logger'), MAX_LOGGER_LEN),
            'event': _truncate(event_dict.get('event', ''), MAX_EVENT_LEN) or '',
            'error_type': _truncate(error_type, MAX_ERROR_TYPE_LEN),
            'traceback': _truncate(traceback_text, MAX_TRACEBACK_LEN),
            'context': context or None,
            'user_id': user_id,
            'dedup_hash': _truncate(dedup_hash, 32),
            'delivery_status': STATUS_PENDING,
            'delivery_attempts': 0,
        }

    async def _run(self) -> None:
        queue = self._queue
        if queue is None:
            return
        while True:
            try:
                op, payload = await queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._apply(op, payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._warn('Сбой записи события об ошибке в БД', e)
            finally:
                queue.task_done()

    async def _apply(self, op: str, payload: dict[str, Any]) -> None:
        async with AsyncSessionLocal() as session:
            if op == 'insert':
                await session.execute(insert(SystemErrorEvent).values(**payload))
            elif op == 'status':
                values: dict[str, Any] = {'delivery_status': payload['status']}
                # Попыткой считается только реальная отправка. suppressed
                # (дубликат в окне TTL) и skipped (бот не поднят, канал выключен)
                # до Telegram не доходят, и счётчик «сколько раз пытались» на них
                # врал бы — админ читает его, чтобы понять, пробовали ли вообще.
                if payload['status'] in (STATUS_SENT, STATUS_FAILED):
                    values['last_attempt_at'] = datetime.now(tz=UTC)
                    values['delivery_attempts'] = SystemErrorEvent.delivery_attempts + 1
                if payload['status'] == STATUS_SENT:
                    values['delivered_at'] = datetime.now(tz=UTC)
                    values['delivery_error'] = None
                elif payload.get('error'):
                    values['delivery_error'] = payload['error']
                await session.execute(
                    update(SystemErrorEvent).where(SystemErrorEvent.event_uid == payload['event_uid']).values(**values)
                )
            await session.commit()

    async def _run_cleanup(self) -> None:
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                cutoff = datetime.now(tz=UTC) - timedelta(days=RETENTION_DAYS)
                async with AsyncSessionLocal() as session:
                    result = await session.execute(delete(SystemErrorEvent).where(SystemErrorEvent.created_at < cutoff))
                    await session.commit()
                if result.rowcount:
                    logger.info('Очищены старые записи об ошибках', deleted=result.rowcount, days=RETENTION_DAYS)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._warn('Сбой очистки старых записей об ошибках', e)


system_error_log_service = SystemErrorLogService()
