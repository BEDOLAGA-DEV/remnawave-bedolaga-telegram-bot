"""Issue #3000: параметры пула подключений к БД должны настраиваться через env.

Раньше pool_size / max_overflow / pool_timeout были захардкожены в
app/database/database.py, из-за чего под нагрузкой (несколько воркеров, всплески
вебхуков Remnawave) бот ловил ``QueuePool limit of size 20 overflow 20 reached``
и не масштабировался без пересборки образа. Теперь они читаются из настроек.
"""

import app.database.database as db
from app.config import Settings, settings


def test_sqlite_uses_nullpool_without_kwargs():
    """Для SQLite пул не применяется — kwargs пустые (NullPool без пулинга)."""
    assert db._build_pool_kwargs(True) == {}


def test_postgres_pool_kwargs_read_from_settings(monkeypatch):
    """Настраиваемые знобы берутся из settings, безопасные дефолты — фиксированы."""
    monkeypatch.setattr(settings, 'DATABASE_POOL_SIZE', 50)
    monkeypatch.setattr(settings, 'DATABASE_MAX_OVERFLOW', 100)
    monkeypatch.setattr(settings, 'DATABASE_POOL_TIMEOUT', 45)

    kwargs = db._build_pool_kwargs(False)

    assert kwargs['pool_size'] == 50
    assert kwargs['max_overflow'] == 100
    assert kwargs['pool_timeout'] == 45
    # Эти остаются prod-дефолтами и не зависят от env.
    assert kwargs['pool_recycle'] == 1800
    assert kwargs['pool_pre_ping'] is True
    assert kwargs['pool_reset_on_return'] == 'rollback'


def test_pool_defaults_preserve_legacy_values():
    """Дефолты совпадают с прежними захардкоженными значениями (без регрессии)."""
    s = Settings()
    assert s.DATABASE_POOL_SIZE == 20
    assert s.DATABASE_MAX_OVERFLOW == 20
    assert s.DATABASE_POOL_TIMEOUT == 30


def test_pool_size_clamped_to_at_least_one():
    """pool_size=0 у QueuePool означает «без лимита» — клампим к >= 1."""
    assert Settings(DATABASE_POOL_SIZE=0).DATABASE_POOL_SIZE == 1
    assert Settings(DATABASE_POOL_SIZE=-5).DATABASE_POOL_SIZE == 1


def test_max_overflow_clamped_to_nonnegative():
    assert Settings(DATABASE_MAX_OVERFLOW=-1).DATABASE_MAX_OVERFLOW == 0
    # Явный 0 = «без overflow» — легитимная настройка, её нельзя спутать с
    # None/'' (которые откатываются к дефолту 20).
    assert Settings(DATABASE_MAX_OVERFLOW=0).DATABASE_MAX_OVERFLOW == 0
    assert Settings(DATABASE_MAX_OVERFLOW='0').DATABASE_MAX_OVERFLOW == 0
    assert Settings(DATABASE_MAX_OVERFLOW=200).DATABASE_MAX_OVERFLOW == 200  # верхнего клампа нет


def test_pool_timeout_clamped_to_at_least_one():
    assert Settings(DATABASE_POOL_TIMEOUT=0).DATABASE_POOL_TIMEOUT == 1


def test_invalid_values_fall_back_to_defaults():
    """Мусор в env не должен ронять старт — откатываемся к дефолтам."""
    assert Settings(DATABASE_POOL_SIZE='abc').DATABASE_POOL_SIZE == 20
    assert Settings(DATABASE_MAX_OVERFLOW='').DATABASE_MAX_OVERFLOW == 20
    assert Settings(DATABASE_POOL_TIMEOUT=None).DATABASE_POOL_TIMEOUT == 30


def test_custom_env_values_are_applied():
    """Числа из env (как строки) корректно парсятся."""
    s = Settings(DATABASE_POOL_SIZE='40', DATABASE_MAX_OVERFLOW='80', DATABASE_POOL_TIMEOUT='60')
    assert s.DATABASE_POOL_SIZE == 40
    assert s.DATABASE_MAX_OVERFLOW == 80
    assert s.DATABASE_POOL_TIMEOUT == 60


def test_live_engine_is_wired_with_pool_kwargs():
    """Боевой engine реально получает kwargs из хелпера — это и есть фикс #3000.

    Защищает от рефакторинга, который выкинет ``**pool_kwargs`` из
    create_async_engine: такая регрессия вернула бы баг "QueuePool limit reached",
    но все dict-уровневые тесты выше остались бы зелёными. conftest форсит
    DATABASE_MODE=postgresql, поэтому боевой пул — настоящий QueuePool.
    """
    from sqlalchemy.pool import AsyncAdaptedQueuePool

    assert db.pool_kwargs == db._build_pool_kwargs(db.IS_SQLITE)
    assert isinstance(db.engine.pool, AsyncAdaptedQueuePool)
    # engine создан на импорте с дефолтными настройками (20/20/30).
    assert db.engine.pool.size() == 20
    assert db.engine.pool._max_overflow == 20
    assert db.engine.pool._timeout == 30


def test_custom_pool_values_reach_a_real_engine(monkeypatch):
    """End-to-end: настроенные значения долетают через create_async_engine в пул."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import AsyncAdaptedQueuePool

    monkeypatch.setattr(settings, 'DATABASE_POOL_SIZE', 50)
    monkeypatch.setattr(settings, 'DATABASE_MAX_OVERFLOW', 100)
    monkeypatch.setattr(settings, 'DATABASE_POOL_TIMEOUT', 45)

    eng = create_async_engine(
        'postgresql+asyncpg://u:p@localhost/test_db',
        poolclass=AsyncAdaptedQueuePool,
        **db._build_pool_kwargs(False),
    )
    try:
        assert eng.pool.size() == 50
        assert eng.pool._max_overflow == 100
        assert eng.pool._timeout == 45
    finally:
        eng.sync_engine.dispose()
