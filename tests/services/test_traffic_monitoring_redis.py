"""
Тесты для хранения snapshot трафика в Redis.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.traffic_monitoring_service import (
    TRAFFIC_NOTIFICATION_CACHE_KEY,
    TRAFFIC_SNAPSHOT_KEY,
    TRAFFIC_SNAPSHOT_TIME_KEY,
    TrafficMonitoringServiceV2,
)


@pytest.fixture
def service():
    """Создаёт экземпляр сервиса для тестов."""
    return TrafficMonitoringServiceV2()


@pytest.fixture
def mock_cache():
    """Мок для cache сервиса."""
    with patch('app.services.traffic_monitoring_service.cache') as mock:
        mock.set = AsyncMock(return_value=True)
        mock.get = AsyncMock(return_value=None)
        yield mock


@pytest.fixture
def sample_snapshot():
    """Пример snapshot данных: ключ — числовой id панельного юзера (Remnawave 3.0.0)."""
    return {
        101: 1073741824.0,  # 1 GB
        102: 2147483648.0,  # 2 GB
        103: 5368709120.0,  # 5 GB
    }


@pytest.fixture
def stored_snapshot(sample_snapshot):
    """Тот же snapshot в том виде, в котором лежит в Redis: JSON не умеет числовые ключи."""
    return {str(panel_user_id): value for panel_user_id, value in sample_snapshot.items()}


# ============== Тесты ключей Redis ==============


def test_redis_keys_are_versioned_for_panel_id_identity():
    """Префикс v3 — версия идентичности панельного юзера. До Remnawave 3.0.0 те же
    ключи хранили UUID-идентичность; без бампа старые записи выглядели бы валидными,
    но дельта считалась бы от чужого значения, а кулдауны — от чужих юзеров.
    """
    assert TRAFFIC_SNAPSHOT_KEY == 'traffic:v3:snapshot'
    assert TRAFFIC_SNAPSHOT_TIME_KEY == 'traffic:v3:snapshot:time'
    assert TRAFFIC_NOTIFICATION_CACHE_KEY == 'traffic:v3:notifications'


# ============== Тесты сохранения snapshot в Redis ==============


async def test_save_snapshot_to_redis_success(service, mock_cache, sample_snapshot, stored_snapshot):
    """Тест успешного сохранения snapshot в Redis."""
    mock_cache.set = AsyncMock(return_value=True)

    result = await service._save_snapshot_to_redis(sample_snapshot)

    assert result is True
    assert mock_cache.set.call_count == 2  # snapshot + time

    # Проверяем что сохранён snapshot: числовые id сериализуются в строковые ключи
    first_call = mock_cache.set.call_args_list[0]
    assert first_call[0][0] == TRAFFIC_SNAPSHOT_KEY
    assert first_call[0][1] == stored_snapshot


async def test_save_snapshot_to_redis_failure(service, mock_cache, sample_snapshot):
    """Тест неудачного сохранения snapshot в Redis."""
    mock_cache.set = AsyncMock(return_value=False)

    result = await service._save_snapshot_to_redis(sample_snapshot)

    assert result is False


async def test_save_snapshot_to_redis_exception(service, mock_cache, sample_snapshot):
    """Тест обработки исключения при сохранении."""
    mock_cache.set = AsyncMock(side_effect=Exception('Redis error'))

    result = await service._save_snapshot_to_redis(sample_snapshot)

    assert result is False


# ============== Тесты загрузки snapshot из Redis ==============


async def test_load_snapshot_from_redis_success(service, mock_cache, sample_snapshot, stored_snapshot):
    """Тест успешной загрузки snapshot из Redis: строковые ключи возвращаются к числовым id."""
    mock_cache.get = AsyncMock(return_value=stored_snapshot)

    result = await service._load_snapshot_from_redis()

    assert result == sample_snapshot
    assert all(isinstance(panel_user_id, int) for panel_user_id in result)
    mock_cache.get.assert_called_once_with(TRAFFIC_SNAPSHOT_KEY)


async def test_load_snapshot_from_redis_empty(service, mock_cache):
    """Тест загрузки когда snapshot отсутствует."""
    mock_cache.get = AsyncMock(return_value=None)

    result = await service._load_snapshot_from_redis()

    assert result is None


async def test_load_snapshot_from_redis_invalid_data(service, mock_cache):
    """Тест загрузки невалидных данных."""
    mock_cache.get = AsyncMock(return_value='not a dict')

    result = await service._load_snapshot_from_redis()

    assert result is None


async def test_load_snapshot_from_redis_skips_non_numeric_keys(service, mock_cache):
    """Непригодный ключ (например, протухший UUID) пропускается поштучно, а не роняет
    весь snapshot: иначе цикл счёл бы всех пользователей новыми и промолчал."""
    mock_cache.get = AsyncMock(return_value={'101': 100.0, 'uuid-legacy': 200.0, None: 300.0})

    result = await service._load_snapshot_from_redis()

    assert result == {101: 100.0}


async def test_load_snapshot_from_redis_exception(service, mock_cache):
    """Тест обработки исключения при загрузке."""
    mock_cache.get = AsyncMock(side_effect=Exception('Redis error'))

    result = await service._load_snapshot_from_redis()

    assert result is None


# ============== Тесты времени snapshot ==============


async def test_get_snapshot_time_from_redis_success(service, mock_cache):
    """Тест получения времени snapshot."""
    test_time = datetime(2024, 1, 15, 12, 30, 0, tzinfo=UTC)
    mock_cache.get = AsyncMock(return_value=test_time.isoformat())

    result = await service._get_snapshot_time_from_redis()

    assert result == test_time
    mock_cache.get.assert_called_once_with(TRAFFIC_SNAPSHOT_TIME_KEY)


async def test_get_snapshot_time_from_redis_empty(service, mock_cache):
    """Тест когда время отсутствует."""
    mock_cache.get = AsyncMock(return_value=None)

    result = await service._get_snapshot_time_from_redis()

    assert result is None


# ============== Тесты has_snapshot ==============


async def test_has_snapshot_redis_exists(service, mock_cache, sample_snapshot):
    """Тест has_snapshot когда snapshot есть в Redis."""
    mock_cache.get = AsyncMock(return_value=sample_snapshot)

    result = await service.has_snapshot()

    assert result is True


async def test_has_snapshot_memory_fallback(service, mock_cache):
    """Тест has_snapshot с fallback на память."""
    mock_cache.get = AsyncMock(return_value=None)

    # Устанавливаем данные в память
    service._memory_snapshot = {101: 1000.0}
    service._memory_snapshot_time = datetime.now(UTC)

    result = await service.has_snapshot()

    assert result is True


async def test_has_snapshot_none(service, mock_cache):
    """Тест has_snapshot когда snapshot нет нигде."""
    mock_cache.get = AsyncMock(return_value=None)
    service._memory_snapshot = {}
    service._memory_snapshot_time = None

    result = await service.has_snapshot()

    assert result is False


# ============== Тесты get_snapshot_age_minutes ==============


async def test_get_snapshot_age_minutes_from_redis(service, mock_cache):
    """Тест возраста snapshot из Redis."""
    # Snapshot создан 30 минут назад
    past_time = datetime.now(UTC) - timedelta(minutes=30)
    mock_cache.get = AsyncMock(return_value=past_time.isoformat())

    result = await service.get_snapshot_age_minutes()

    assert 29 <= result <= 31  # Допуск на время выполнения


async def test_get_snapshot_age_minutes_memory_fallback(service, mock_cache):
    """Тест возраста snapshot из памяти."""
    mock_cache.get = AsyncMock(return_value=None)
    service._memory_snapshot_time = datetime.now(UTC) - timedelta(minutes=15)

    result = await service.get_snapshot_age_minutes()

    assert 14 <= result <= 16


async def test_get_snapshot_age_minutes_no_snapshot(service, mock_cache):
    """Тест возраста когда snapshot нет."""
    mock_cache.get = AsyncMock(return_value=None)
    service._memory_snapshot_time = None

    result = await service.get_snapshot_age_minutes()

    assert result == float('inf')


# ============== Тесты _save_snapshot (с fallback) ==============


async def test_save_snapshot_redis_success(service, mock_cache, sample_snapshot):
    """Тест сохранения snapshot в Redis успешно."""
    mock_cache.set = AsyncMock(return_value=True)

    # Заполняем память чтобы проверить что она очистится
    service._memory_snapshot = {999: 123.0}
    service._memory_snapshot_time = datetime.now(UTC)

    result = await service._save_snapshot(sample_snapshot)

    assert result is True
    assert service._memory_snapshot == {}  # Память очищена
    assert service._memory_snapshot_time is None


async def test_save_snapshot_fallback_to_memory(service, mock_cache, sample_snapshot):
    """Тест fallback на память когда Redis недоступен."""
    mock_cache.set = AsyncMock(return_value=False)

    result = await service._save_snapshot(sample_snapshot)

    assert result is True
    assert service._memory_snapshot == sample_snapshot
    assert service._memory_snapshot_time is not None


# ============== Тесты _get_current_snapshot ==============


async def test_get_current_snapshot_from_redis(service, mock_cache, sample_snapshot, stored_snapshot):
    """Тест получения snapshot из Redis."""
    mock_cache.get = AsyncMock(return_value=stored_snapshot)

    result = await service._get_current_snapshot()

    assert result == sample_snapshot


async def test_get_current_snapshot_fallback_to_memory(service, mock_cache, sample_snapshot):
    """Тест fallback на память."""
    mock_cache.get = AsyncMock(return_value=None)
    service._memory_snapshot = sample_snapshot

    result = await service._get_current_snapshot()

    assert result == sample_snapshot


# ============== Тесты уведомлений ==============


async def test_save_notification_to_redis(service, mock_cache):
    """Тест сохранения времени уведомления. Кулдаун заключён на числовой id панели."""
    mock_cache.set = AsyncMock(return_value=True)

    result = await service._save_notification_to_redis(123)

    assert result is True
    mock_cache.set.assert_called_once()
    call_args = mock_cache.set.call_args
    assert call_args[0][0] == 'traffic:v3:notifications:123'


async def test_get_notification_time_from_redis(service, mock_cache):
    """Тест получения времени уведомления."""
    test_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
    mock_cache.get = AsyncMock(return_value=test_time.isoformat())

    result = await service._get_notification_time_from_redis(123)

    assert result == test_time
    mock_cache.get.assert_called_once_with('traffic:v3:notifications:123')


async def test_should_send_notification_no_previous(service, mock_cache):
    """Тест should_send_notification когда уведомлений не было."""
    mock_cache.get = AsyncMock(return_value=None)
    service._memory_notification_cache = {}

    result = await service.should_send_notification(123)

    assert result is True


async def test_should_send_notification_cooldown_active(service, mock_cache):
    """Тест should_send_notification когда кулдаун активен."""
    # Уведомление было 5 минут назад, кулдаун 60 минут
    recent_time = datetime.now(UTC) - timedelta(minutes=5)
    mock_cache.get = AsyncMock(return_value=recent_time.isoformat())

    result = await service.should_send_notification(123)

    assert result is False


async def test_should_send_notification_cooldown_expired(service, mock_cache):
    """Тест should_send_notification когда кулдаун истёк."""
    # Уведомление было 120 минут назад, кулдаун 60 минут
    old_time = datetime.now(UTC) - timedelta(minutes=120)
    mock_cache.get = AsyncMock(return_value=old_time.isoformat())

    result = await service.should_send_notification(123)

    assert result is True


async def test_should_send_notification_memory_fallback_keyed_by_panel_id(service, mock_cache):
    """Fallback на память тоже заключён на числовой id — ключ должен совпасть."""
    mock_cache.get = AsyncMock(return_value=None)
    service._memory_notification_cache = {123: datetime.now(UTC) - timedelta(minutes=5)}

    assert await service.should_send_notification(123) is False
    assert await service.should_send_notification(456) is True


async def test_record_notification_redis(service, mock_cache):
    """Тест record_notification сохраняет в Redis."""
    mock_cache.set = AsyncMock(return_value=True)

    await service.record_notification(123)

    mock_cache.set.assert_called_once()
    assert mock_cache.set.call_args[0][0] == 'traffic:v3:notifications:123'


async def test_record_notification_fallback_to_memory(service, mock_cache):
    """Тест record_notification с fallback на память."""
    mock_cache.set = AsyncMock(return_value=False)

    await service.record_notification(123)

    assert 123 in service._memory_notification_cache


# ============== Тесты create_initial_snapshot ==============


async def test_create_initial_snapshot_uses_existing_redis(service, mock_cache, sample_snapshot, stored_snapshot):
    """Тест что create_initial_snapshot использует существующий snapshot из Redis."""
    mock_cache.get = AsyncMock(
        side_effect=[
            stored_snapshot,  # _load_snapshot_from_redis
            (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),  # _get_snapshot_time_from_redis
        ]
    )

    with patch.object(service, 'get_all_users_with_traffic', new_callable=AsyncMock) as mock_get_users:
        result = await service.create_initial_snapshot()

        # Не должен вызывать API - используем существующий snapshot
        mock_get_users.assert_not_called()
        assert result == len(sample_snapshot)


async def test_create_initial_snapshot_creates_new(service, mock_cache):
    """Тест создания нового snapshot когда в Redis пусто."""
    mock_cache.get = AsyncMock(return_value=None)
    mock_cache.set = AsyncMock(return_value=True)

    # Мокаем пользователей из API: идентичность панельного юзера — числовой id
    mock_user = MagicMock()
    mock_user.id = 101
    mock_user.user_traffic = MagicMock()
    mock_user.user_traffic.used_traffic_bytes = 1073741824  # 1 GB

    with patch.object(service, 'get_all_users_with_traffic', new_callable=AsyncMock) as mock_get_users:
        mock_get_users.return_value = [mock_user]

        result = await service.create_initial_snapshot()

        mock_get_users.assert_called_once()
        assert result == 1
        # Snapshot должен быть заключён на id панели, а не на UUID
        assert mock_cache.set.call_args_list[0][0][1] == {'101': 1073741824}


async def test_create_initial_snapshot_skips_user_without_panel_id(service, mock_cache):
    """Юзер без числового id непригоден как ключ snapshot — пропускаем, а не падаем."""
    mock_cache.get = AsyncMock(return_value=None)
    mock_cache.set = AsyncMock(return_value=True)

    no_id_user = MagicMock()
    no_id_user.id = None
    no_id_user.user_traffic = MagicMock()
    no_id_user.user_traffic.used_traffic_bytes = 500

    ok_user = MagicMock()
    ok_user.id = 102
    ok_user.user_traffic = MagicMock()
    ok_user.user_traffic.used_traffic_bytes = 700

    with patch.object(service, 'get_all_users_with_traffic', new_callable=AsyncMock) as mock_get_users:
        mock_get_users.return_value = [no_id_user, ok_user]

        result = await service.create_initial_snapshot()

        assert result == 1
        assert mock_cache.set.call_args_list[0][0][1] == {'102': 700}


# ============== Тесты cleanup_notification_cache ==============


async def test_cleanup_notification_cache_removes_old(service, mock_cache):
    """Тест очистки старых записей из памяти."""
    old_time = datetime.now(UTC) - timedelta(hours=25)
    recent_time = datetime.now(UTC) - timedelta(hours=1)

    service._memory_notification_cache = {
        101: old_time,
        102: recent_time,
    }

    await service.cleanup_notification_cache()

    assert 101 not in service._memory_notification_cache
    assert 102 in service._memory_notification_cache
