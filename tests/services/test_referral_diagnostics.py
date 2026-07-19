"""
Тесты для сервиса диагностики реферальной системы.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.referral_diagnostics_service import ReferralDiagnosticsService


@pytest.fixture
def temp_log_file():
    """Создаёт временный лог-файл для тестов."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
        yield Path(f.name)
    # Cleanup
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture
def sample_log_content():
    """Пример содержимого лог-файла с реферальными событиями."""
    today = datetime.now(UTC).strftime('%Y-%m-%d')
    return f"""
{today} 10:00:00,123 - app.handlers.start - INFO - 📩 Сообщение от ID:123456789 текст /start refABC123
{today} 11:00:00,345 - app.handlers.start - INFO - 📩 Сообщение от ID:987654321 текст /start ref987

{today} 12:00:00,901 - app.handlers.start - INFO - 💾 Сохранен start payload 'ref_refTEST777' для пользователя 555000111

{today} 13:00:00,234 - unrelated module - INFO - Some other log message
"""


@pytest.mark.asyncio
async def test_parse_logs_basic(temp_log_file, sample_log_content):
    """Тест базового парсинга логов."""
    # Записываем тестовые данные в файл
    temp_log_file.write_text(sample_log_content)

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    clicks, total_lines, lines_in_period = await service._parse_clicks(today, tomorrow)

    # Проверяем что нашлись все реф-клики (3 строки с /start ref... или payload)
    assert len(clicks) == 3, f'Expected 3 clicks, found {len(clicks)}'

    # Проверяем telegram_id из распарсенных кликов
    telegram_ids = {c.telegram_id for c in clicks}
    assert telegram_ids == {123456789, 987654321, 555000111}

    # Проверяем что ref_ref-префикс очищается до ref
    payload_click = next(c for c in clicks if c.telegram_id == 555000111)
    assert payload_click.raw_code == 'ref_refTEST777'
    assert payload_click.clean_code == 'refTEST777'


@pytest.mark.asyncio
async def test_analyze_period_with_issues(temp_log_file, sample_log_content):
    """Тест анализа с проблемными случаями."""
    temp_log_file.write_text(sample_log_content)

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    # Используем None вместо db для базового теста парсинга
    from unittest.mock import AsyncMock, MagicMock

    # _find_lost_referrals делает (await db.execute(...)).scalars().all();
    # пустой список => пользователи не в БД => считаются потерянными.
    # Результат должен быть синхронным MagicMock, иначе .scalars() вернёт корутину.
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=empty_result)

    report = await service.analyze_period(mock_db, today, tomorrow)

    # Проверяем статистику
    assert report.total_ref_clicks >= 1, 'Should have ref clicks'

    # Проверяем что нашлись проблемные случаи
    # (987654321 пришёл по ссылке, но его нет в БД => потерянный реферал)
    assert any(lr.telegram_id == 987654321 for lr in report.lost_referrals), (
        f'Expected 987654321 in lost_referrals, got: {[lr.telegram_id for lr in report.lost_referrals]}'
    )


@pytest.mark.asyncio
async def test_empty_log_file(temp_log_file):
    """Тест работы с пустым лог-файлом."""
    temp_log_file.write_text('')

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    from unittest.mock import AsyncMock

    mock_db = AsyncMock()

    report = await service.analyze_period(mock_db, today, tomorrow)

    # Проверяем что отчёт пустой
    assert report.total_ref_clicks == 0
    assert report.unique_users_clicked == 0
    assert report.lost_referrals == []
    assert report.total_lines_parsed == 0
    assert report.lines_in_period == 0


@pytest.mark.asyncio
async def test_nonexistent_log_file():
    """Тест работы с несуществующим лог-файлом."""
    service = ReferralDiagnosticsService(log_path='/nonexistent/path/to/log.log')

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    from unittest.mock import AsyncMock

    mock_db = AsyncMock()

    # Не должно быть исключений
    report = await service.analyze_period(mock_db, today, tomorrow)

    assert report.total_ref_clicks == 0
    assert report.unique_users_clicked == 0
    assert report.lost_referrals == []


@pytest.mark.asyncio
async def test_analyze_today(temp_log_file, sample_log_content):
    """Тест метода analyze_today."""
    temp_log_file.write_text(sample_log_content)

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    from unittest.mock import AsyncMock, MagicMock

    # Лог содержит реф-клики, поэтому _find_lost_referrals выполнит запросы к БД.
    # Возвращаем пустые наборы (синхронный результат) — конкретика БД здесь не проверяется.
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=empty_result)

    report = await service.analyze_today(mock_db)

    # Проверяем что период установлен корректно
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    assert report.analysis_period_start.date() == today.date()
