import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import ParamSpec, TypeVar

import structlog
from sqlalchemy import bindparam, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from app.config import settings


logger = structlog.get_logger(__name__)

T = TypeVar('T')
P = ParamSpec('P')
R = TypeVar('R')

# ============================================================================
# PRODUCTION-GRADE CONNECTION POOLING
# ============================================================================


def _is_sqlite_url(url: str) -> bool:
    """Проверка на SQLite URL (поддерживает sqlite:// и sqlite+aiosqlite://)"""
    return url.startswith('sqlite') or ':memory:' in url


def _build_pool_kwargs(is_sqlite: bool) -> dict:
    """Собрать kwargs пула SQLAlchemy для engine.

    Для SQLite используется ``NullPool`` (без пулинга), поэтому kwargs пустые.
    Для PostgreSQL размер пула / overflow / timeout читаются из настроек
    (env-настраиваемые: масштабирование пула под нагрузку без пересборки образа,
    см. DATABASE_POOL_SIZE / DATABASE_MAX_OVERFLOW / DATABASE_POOL_TIMEOUT).
    recycle / pre_ping / reset_on_return остаются безопасными prod-дефолтами.
    """
    if is_sqlite:
        return {}
    return {
        'pool_size': settings.DATABASE_POOL_SIZE,  # держим ниже max_connections PostgreSQL
        'max_overflow': settings.DATABASE_MAX_OVERFLOW,  # доп. соединения сверх pool_size при всплесках
        'pool_timeout': settings.DATABASE_POOL_TIMEOUT,  # сек ожидания свободного соединения до TimeoutError
        'pool_recycle': 1800,  # 30 мин для более быстрого recycling
        'pool_pre_ping': True,
        # Агрессивная очистка мертвых соединений
        'pool_reset_on_return': 'rollback',
    }


DATABASE_URL = settings.get_database_url()
IS_SQLITE = _is_sqlite_url(DATABASE_URL)

poolclass = NullPool if IS_SQLITE else AsyncAdaptedQueuePool
pool_kwargs = _build_pool_kwargs(IS_SQLITE)

# ============================================================================
# ENGINE WITH ADVANCED OPTIMIZATIONS
# ============================================================================

# PostgreSQL-специфичные connect_args
_pg_connect_args = {
    'server_settings': {
        'application_name': 'remnawave_bot',
        'jit': 'on',
        'statement_timeout': '60000',  # 60 секунд
        'idle_in_transaction_session_timeout': '300000',  # 5 минут
    },
    'command_timeout': 30,  # Уменьшен с 60, быстрее обнаруживать зависшие запросы
    'timeout': 10,  # Уменьшен с 60, быстрый провал при недоступности PostgreSQL
}
_sqlite_connect_args = {
    # Grace-safe panel writes deliberately hold a SQLite writer transaction
    # across one HTTP request.  Let competing local work wait instead of
    # failing with a short default "database is locked" timeout.
    'timeout': 60,
}

engine = create_async_engine(
    DATABASE_URL,
    poolclass=poolclass,
    echo='debug' if settings.DEBUG else False,
    future=True,
    # Кеш скомпилированных запросов (правильное размещение)
    query_cache_size=500,
    connect_args=_pg_connect_args if not IS_SQLITE else _sqlite_connect_args,
    execution_options={
        'isolation_level': 'READ COMMITTED',
    },
    **pool_kwargs,
)

if IS_SQLITE:

    @event.listens_for(engine.sync_engine, 'connect')
    def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        """Enable concurrency settings on every SQLite connection.

        PRAGMA foreign_keys здесь сознательно НЕ включаем: у существующих
        SQLite-инсталляций могут быть orphan-строки из старых версий схемы,
        и глобальный флип enforcement ломал бы их DELETE/UPDATE. Защита
        grace-снимка держится на DB-триггере, а не на FK.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute('PRAGMA busy_timeout=60000')
            cursor.execute('PRAGMA journal_mode=WAL')
        finally:
            cursor.close()

# ============================================================================
# SESSION FACTORY WITH OPTIMIZATIONS
# ============================================================================

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,  # Критично для производительности
    autocommit=False,
)

# ============================================================================
# RETRY LOGIC FOR DATABASE OPERATIONS
# ============================================================================

RETRYABLE_EXCEPTIONS = (OperationalError, InterfaceError, ConnectionRefusedError, OSError, TimeoutError)
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 0.5  # секунды


def with_db_retry(
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    delay: float = DEFAULT_RETRY_DELAY,
    backoff: float = 2.0,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """
    Декоратор для автоматического retry при сбоях подключения к БД.

    Args:
        attempts: Количество попыток
        delay: Начальная задержка между попытками (секунды)
        backoff: Множитель задержки для каждой следующей попытки
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_exception: Exception | None = None
            current_delay = delay

            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except RETRYABLE_EXCEPTIONS as e:
                    last_exception = e
                    if attempt < attempts:
                        logger.warning(
                            'Ошибка БД (попытка /): . Повтор через сек...',
                            attempt=attempt,
                            attempts=attempts,
                            e=str(e)[:100],
                            current_delay=current_delay,
                        )
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error('Ошибка БД: все попыток исчерпаны. Последняя ошибка', attempts=attempts, e=str(e))

            raise last_exception  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


async def execute_with_retry(
    session: AsyncSession,
    statement,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
):
    """Выполнение SQL с retry логикой."""
    if attempts < 1:
        raise ValueError(f'attempts must be >= 1, got {attempts}')

    last_exception: Exception | None = None
    delay = DEFAULT_RETRY_DELAY

    for attempt in range(1, attempts + 1):
        try:
            return await session.execute(statement)
        except RETRYABLE_EXCEPTIONS as e:
            last_exception = e
            if attempt < attempts:
                logger.warning('SQL retry (попытка /)', attempt=attempt, attempts=attempts, e=str(e)[:100])
                await asyncio.sleep(delay)
                delay *= 2

    raise last_exception  # type: ignore[misc]


# ============================================================================
# QUERY PERFORMANCE MONITORING
# ============================================================================

if settings.DEBUG:

    @event.listens_for(Engine, 'before_cursor_execute')
    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault('query_start_time', []).append(time.time())
        logger.debug('🔍 Executing query: ...', statement=statement[:100])

    @event.listens_for(Engine, 'after_cursor_execute')
    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        total = time.time() - conn.info['query_start_time'].pop(-1)
        if total > 0.1:  # Логируем медленные запросы > 100ms
            logger.warning('🐌 Slow query (s): ...', total=round(total, 3), statement=statement[:100])
        else:
            logger.debug('⚡ Query executed in', total=round(total, 3))

# ============================================================================
# ADVANCED SESSION MANAGER WITH READ REPLICAS
# ============================================================================

HEALTH_CHECK_TIMEOUT = 5.0  # секунды


def _validate_database_url(url: str | None) -> str | None:
    """Валидация URL базы данных."""
    if not url:
        return None
    url = url.strip()
    if not url or url.isspace():
        return None
    # Простая проверка на валидный формат
    if not ('://' in url or url.startswith('sqlite')):
        logger.warning('Невалидный DATABASE_URL (не содержит ://)')
        return None
    return url


class DatabaseManager:
    """Продвинутый менеджер БД с поддержкой реплик и кеширования"""

    def __init__(self):
        self.engine = engine
        self.read_replica_engine: AsyncEngine | None = None
        self._read_replica_session_factory: async_sessionmaker | None = None

        # Валидация и создание read replica engine
        replica_url = _validate_database_url(getattr(settings, 'DATABASE_READ_REPLICA_URL', None))
        if replica_url:
            try:
                self.read_replica_engine = create_async_engine(
                    replica_url,
                    poolclass=poolclass,
                    pool_size=30,  # Больше для read операций
                    max_overflow=50,
                    pool_pre_ping=True,
                    pool_recycle=3600,
                    echo=False,
                )
                # Создаём sessionmaker один раз (не при каждом вызове)
                self._read_replica_session_factory = async_sessionmaker(
                    bind=self.read_replica_engine,
                    class_=AsyncSession,
                    expire_on_commit=False,
                    autoflush=False,
                )
                from sqlalchemy.engine import make_url

                safe_url = make_url(replica_url).render_as_string(hide_password=True)
                logger.info('Read replica настроена', replica_url=safe_url)
            except Exception as e:
                logger.error('Не удалось настроить read replica', e=e)
                self.read_replica_engine = None

    @asynccontextmanager
    async def session(self, read_only: bool = False):
        """Контекстный менеджер для работы с сессией БД."""
        # Используем предсозданный sessionmaker вместо создания нового
        if read_only and self._read_replica_session_factory:
            session_factory = self._read_replica_session_factory
        else:
            session_factory = AsyncSessionLocal

        async with session_factory() as session:
            try:
                yield session
                if not read_only:
                    await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def health_check(self, timeout: float = HEALTH_CHECK_TIMEOUT) -> dict:
        """
        Проверка здоровья БД с таймаутом.

        Args:
            timeout: Максимальное время ожидания (секунды)
        """
        pool = self.engine.pool
        status = 'unhealthy'
        latency = None

        try:
            async with asyncio.timeout(timeout):
                async with AsyncSessionLocal() as session:
                    start = time.time()
                    await session.execute(text('SELECT 1'))
                    latency = (time.time() - start) * 1000
            status = 'healthy'
        except TimeoutError:
            logger.error('Health check таймаут (сек)', timeout=timeout)
            status = 'timeout'
        except Exception as e:
            logger.error('Database health check failed', e=e)
            status = 'unhealthy'

        return {
            'status': status,
            'latency_ms': round(latency, 2) if latency else None,
            'pool': _collect_health_pool_metrics(pool),
        }

    async def health_check_replica(self, timeout: float = HEALTH_CHECK_TIMEOUT) -> dict | None:
        """Проверка здоровья read replica."""
        if not self.read_replica_engine:
            return None

        pool = self.read_replica_engine.pool
        status = 'unhealthy'
        latency = None

        try:
            async with asyncio.timeout(timeout):
                async with self._read_replica_session_factory() as session:
                    start = time.time()
                    await session.execute(text('SELECT 1'))
                    latency = (time.time() - start) * 1000
            status = 'healthy'
        except TimeoutError:
            status = 'timeout'
        except Exception as e:
            logger.error('Read replica health check failed', e=e)

        return {
            'status': status,
            'latency_ms': round(latency, 2) if latency else None,
            'pool': _collect_health_pool_metrics(pool),
        }


db_manager = DatabaseManager()

# ============================================================================
# SESSION DEPENDENCY FOR FASTAPI/AIOGRAM
# ============================================================================


async def get_db() -> AsyncGenerator[AsyncSession]:
    """Стандартная dependency для FastAPI"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db_read_only() -> AsyncGenerator[AsyncSession]:
    """Read-only dependency для тяжелых SELECT запросов"""
    async with db_manager.session(read_only=True) as session:
        yield session


# ============================================================================
# BATCH OPERATIONS FOR PERFORMANCE
# ============================================================================


class BatchOperations:
    """Утилиты для массовых операций"""

    @staticmethod
    async def bulk_insert(session: AsyncSession, model, data: list[dict], chunk_size: int = 1000):
        """Массовая вставка с чанками"""
        for i in range(0, len(data), chunk_size):
            chunk = data[i : i + chunk_size]
            session.add_all([model(**item) for item in chunk])
            await session.flush()
        await session.commit()

    @staticmethod
    async def bulk_update(session: AsyncSession, model, data: list[dict], chunk_size: int = 1000):
        """Массовое обновление с чанками"""
        if not data:
            return

        primary_keys = [column.name for column in model.__table__.primary_key.columns]
        if not primary_keys:
            raise ValueError('Model must have a primary key for bulk_update')

        updatable_columns = [column.name for column in model.__table__.columns if column.name not in primary_keys]

        if not updatable_columns:
            raise ValueError('No columns available for update in bulk_update')

        stmt = (
            model.__table__.update()
            .where(*[getattr(model.__table__.c, pk) == bindparam(pk) for pk in primary_keys])
            .values(**{column: bindparam(column, required=False) for column in updatable_columns})
        )

        for i in range(0, len(data), chunk_size):
            chunk = data[i : i + chunk_size]
            filtered_chunk = []
            for item in chunk:
                missing_keys = [pk for pk in primary_keys if pk not in item]
                if missing_keys:
                    raise ValueError(f'Missing primary key values {missing_keys} for bulk_update')

                filtered_item = {
                    key: value for key, value in item.items() if key in primary_keys or key in updatable_columns
                }
                filtered_chunk.append(filtered_item)

            await session.execute(stmt, filtered_chunk)
        await session.commit()


batch_ops = BatchOperations()

# ============================================================================
# INITIALIZATION AND CLEANUP
# ============================================================================


async def close_db() -> None:
    """Корректное закрытие всех соединений"""
    logger.info('Закрытие соединений с БД...')

    await engine.dispose()

    if db_manager.read_replica_engine:
        await db_manager.read_replica_engine.dispose()

    logger.info('Все подключения к базе данных закрыты')


# ============================================================================
# SEQUENCE SYNCHRONIZATION (after DB restores)
# ============================================================================


def _quote_ident(name: str) -> str:
    """Quote a PostgreSQL identifier to prevent SQL injection."""
    return '"' + name.replace('"', '""') + '"'


async def sync_postgres_sequences() -> bool:
    """Ensure PostgreSQL sequences match the current max values after restores."""
    if IS_SQLITE:
        logger.debug('Пропускаем синхронизацию последовательностей: SQLite')
        return True

    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        cols.table_schema,
                        cols.table_name,
                        cols.column_name,
                        pg_get_serial_sequence(
                            format('%I.%I', cols.table_schema, cols.table_name),
                            cols.column_name
                        ) AS sequence_path
                    FROM information_schema.columns AS cols
                    WHERE cols.column_default LIKE 'nextval(%'
                      AND cols.table_schema NOT IN ('pg_catalog', 'information_schema')
                    """
                )
            )

            sequences = result.fetchall()

            if not sequences:
                logger.info('Не найдено последовательностей PostgreSQL для синхронизации')
                return True

            for table_schema, table_name, column_name, sequence_path in sequences:
                if not sequence_path:
                    continue

                q_col = _quote_ident(column_name)
                q_schema = _quote_ident(table_schema)
                q_table = _quote_ident(table_name)

                max_result = await conn.execute(text(f'SELECT COALESCE(MAX({q_col}), 0) FROM {q_schema}.{q_table}'))
                max_value = max_result.scalar() or 0

                # pg_get_serial_sequence returns e.g. '"public"."users_id_seq"'.
                # Split on '"."' to handle quoted identifiers that may contain dots.
                if '"."' in sequence_path:
                    seq_schema, seq_name = sequence_path.split('"."', 1)
                    seq_schema = seq_schema.strip('"')
                    seq_name = seq_name.strip('"')
                else:
                    parts = sequence_path.split('.')
                    if len(parts) == 2:
                        seq_schema, seq_name = parts
                    else:
                        seq_schema, seq_name = 'public', parts[-1]
                q_seq_schema = _quote_ident(seq_schema)
                q_seq_name = _quote_ident(seq_name)
                current_result = await conn.execute(
                    text(f'SELECT last_value, is_called FROM {q_seq_schema}.{q_seq_name}')
                )
                current_row = current_result.fetchone()

                if current_row:
                    current_last, is_called = current_row
                    current_next = current_last + 1 if is_called else current_last
                    if current_next > max_value:
                        continue

                await conn.execute(
                    text(
                        """
                        SELECT setval(:sequence_name, :new_value, TRUE)
                        """
                    ),
                    {'sequence_name': sequence_path, 'new_value': max_value},
                )
                logger.info(
                    'Последовательность синхронизирована',
                    sequence_path=sequence_path,
                    max_value=max_value,
                    next_id=max_value + 1,
                )

        return True

    except Exception as error:
        logger.error('Ошибка синхронизации последовательностей PostgreSQL', error=error)
        return False


# ============================================================================
# CONNECTION POOL METRICS (для мониторинга)
# ============================================================================


def _pool_counters(pool):
    """Return basic pool counters or ``None`` when unsupported."""

    required_methods = ('size', 'checkedin', 'checkedout', 'overflow')

    for method_name in required_methods:
        method = getattr(pool, method_name, None)
        if method is None or not callable(method):
            return None

    size = pool.size()
    checked_in = pool.checkedin()
    checked_out = pool.checkedout()
    overflow = pool.overflow()

    total_connections = size + overflow

    return {
        'size': size,
        'checked_in': checked_in,
        'checked_out': checked_out,
        'overflow': overflow,
        'total_connections': total_connections,
        'utilization_percent': (checked_out / total_connections * 100) if total_connections else 0.0,
    }


def _collect_health_pool_metrics(pool) -> dict:
    counters = _pool_counters(pool)

    if counters is None:
        return {
            'metrics_available': False,
            'size': 0,
            'checked_in': 0,
            'checked_out': 0,
            'overflow': 0,
            'total_connections': 0,
            'utilization': '0.0%',
        }

    return {
        'metrics_available': True,
        'size': counters['size'],
        'checked_in': counters['checked_in'],
        'checked_out': counters['checked_out'],
        'overflow': counters['overflow'],
        'total_connections': counters['total_connections'],
        'utilization': f'{counters["utilization_percent"]:.1f}%',
    }


async def get_pool_metrics() -> dict:
    """Детальные метрики пула для Prometheus/Grafana"""
    pool = engine.pool

    counters = _pool_counters(pool)

    if counters is None:
        return {
            'metrics_available': False,
            'pool_size': 0,
            'checked_in_connections': 0,
            'checked_out_connections': 0,
            'overflow_connections': 0,
            'total_connections': 0,
            'max_possible_connections': 0,
            'pool_utilization_percent': 0.0,
        }

    return {
        'metrics_available': True,
        'pool_size': counters['size'],
        'checked_in_connections': counters['checked_in'],
        'checked_out_connections': counters['checked_out'],
        'overflow_connections': counters['overflow'],
        'total_connections': counters['total_connections'],
        'max_possible_connections': counters['total_connections'] + (getattr(pool, '_max_overflow', 0) or 0),
        'pool_utilization_percent': round(counters['utilization_percent'], 2),
    }
