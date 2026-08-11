"""Планировщик автобэкапов: одиночный цикл и таймзона BACKUP_TIME (#3030).

Две проблемы, которые пинят эти тесты:

1. Гонка рестартов. На холодном старте ``start_auto_backup`` вызывается
   конкурентно до 6 раз (по одному на каждую BACKUP_* настройку из
   ``_apply_to_settings`` + вызов из main.py). Без лока вызовы интерливились
   на ``await`` отмены старой таски: каждый создавал свой ``_auto_backup_loop``,
   ссылку ``_auto_backup_task`` получал только последний, остальные циклы
   осиротевали. В назначенный час 4-5 циклов одновременно писали один
   gzip-архив — на выходе битый файл (``zlib.error: invalid stored block
   lengths``) и 3-4 дублирующих уведомления в Telegram.

2. Таймзона. ``_calculate_next_backup_datetime`` подставлял BACKUP_TIME
   напрямую в UTC-«сейчас», игнорируя TZ контейнера: для Europe/Moscow бекап
   уезжал на 3 часа. Теперь BACKUP_TIME интерпретируется в settings.TIMEZONE
   (наследует env TZ), наружу возвращается UTC.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.config import settings
from app.services.backup_service import BackupService


@pytest.fixture
def service() -> BackupService:
    return BackupService()


# ============ Таймзона BACKUP_TIME ============


def test_backup_time_interpreted_in_configured_timezone(service, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow', raising=False)
    service._settings.backup_time = '21:00'

    reference = datetime(2026, 7, 2, 11, 36, tzinfo=UTC)  # 14:36 MSK
    next_run = service._calculate_next_backup_datetime(reference)

    # 21:00 MSK == 18:00 UTC того же дня
    assert next_run == datetime(2026, 7, 2, 18, 0, tzinfo=UTC)


def test_backup_time_rolls_to_next_day_by_local_clock(service, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow', raising=False)
    service._settings.backup_time = '21:00'

    reference = datetime(2026, 7, 2, 19, 0, tzinfo=UTC)  # 22:00 MSK — сегодняшний слот прошёл
    next_run = service._calculate_next_backup_datetime(reference)

    assert next_run == datetime(2026, 7, 3, 18, 0, tzinfo=UTC)


def test_utc_timezone_keeps_legacy_behavior(service, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC', raising=False)
    service._settings.backup_time = '03:00'

    reference = datetime(2026, 7, 2, 11, 0, tzinfo=UTC)
    next_run = service._calculate_next_backup_datetime(reference)

    assert next_run == datetime(2026, 7, 3, 3, 0, tzinfo=UTC)


def test_naive_reference_is_treated_as_utc(service, monkeypatch: pytest.MonkeyPatch) -> None:
    """Naive reference не должен интерпретироваться в системной зоне хоста —
    трактуем как UTC (конвенция кодбазы), иначе расчёт уехал бы на offset хоста."""
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow', raising=False)
    service._settings.backup_time = '21:00'

    naive_reference = datetime(2026, 7, 2, 11, 36)  # без tzinfo, подразумеваем UTC
    next_run = service._calculate_next_backup_datetime(naive_reference)

    # 11:36 UTC == 14:36 MSK, ближайшие 21:00 MSK == 18:00 UTC того же дня
    assert next_run == datetime(2026, 7, 2, 18, 0, tzinfo=UTC)


def test_format_local_renders_next_run_in_configured_timezone(service, monkeypatch: pytest.MonkeyPatch) -> None:
    """Лог-время запуска выводится в settings.TIMEZONE (с меткой зоны), чтобы
    оператор видел время, совпадающее с BACKUP_TIME, а не UTC (#3030)."""
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow', raising=False)

    rendered = service._format_local(datetime(2026, 7, 2, 18, 0, tzinfo=UTC))

    # 18:00 UTC == 21:00 MSK
    assert rendered.startswith('02.07.2026 21:00:00')
    assert 'MSK' in rendered


def test_invalid_timezone_falls_back_to_utc(service, monkeypatch: pytest.MonkeyPatch) -> None:
    # Валидатор конфига отсекает мусор при старте, но настройка может прийти
    # из БД/окружения мимо валидатора — расчёт не должен падать.
    monkeypatch.setattr(settings, 'TIMEZONE', 'Neverland/Nope', raising=False)
    service._settings.backup_time = '03:00'

    reference = datetime(2026, 7, 2, 11, 0, tzinfo=UTC)
    next_run = service._calculate_next_backup_datetime(reference)

    assert next_run == datetime(2026, 7, 3, 3, 0, tzinfo=UTC)


# ============ Гонка конкурентных рестартов ============


async def test_concurrent_starts_leave_exactly_one_loop(service, monkeypatch: pytest.MonkeyPatch) -> None:
    """РЕГРЕССИЯ #3030: 6 конкурентных start_auto_backup (холодный старт) не
    должны оставлять осиротевших циклов — выживает ровно один."""
    service._settings.auto_backup_enabled = True

    alive: set[object] = set()

    async def fake_loop(next_run=None):
        token = object()
        alive.add(token)
        try:
            await asyncio.Event().wait()  # живём до отмены
        finally:
            alive.discard(token)

    monkeypatch.setattr(service, '_auto_backup_loop', fake_loop)

    await asyncio.gather(*(service.start_auto_backup() for _ in range(6)))
    await asyncio.sleep(0)

    assert len(alive) == 1, f'осиротевшие циклы автобэкапа: живо {len(alive)}, должен быть ровно 1'
    assert service._auto_backup_task is not None and not service._auto_backup_task.done()

    await service.stop_auto_backup()
    assert len(alive) == 0, 'stop_auto_backup должен останавливать единственный цикл'


async def test_stop_without_running_task_is_noop(service) -> None:
    await service.stop_auto_backup()
    assert service._auto_backup_task is None
