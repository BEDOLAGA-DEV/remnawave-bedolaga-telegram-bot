import pytest

from app.services.user_cart_service import UserCartService
from tests.fixtures.redis_pipeline import SequentialPipelineMixin


# Мок для Redis клиента
class MockRedis(SequentialPipelineMixin):
    def __init__(self):
        self.storage = {}

    async def setex(self, key, ttl, value):
        self.storage[key] = value
        return True

    async def get(self, key):
        return self.storage.get(key)

    async def delete(self, key):
        if key in self.storage:
            del self.storage[key]
            return 1
        return 0

    async def exists(self, key):
        return 1 if key in self.storage else 0


@pytest.fixture
def mock_redis():
    return MockRedis()


@pytest.fixture
def user_cart_service(mock_redis):
    service = UserCartService()
    service._redis_client = mock_redis
    service._initialized = True
    return service


async def test_save_user_cart(user_cart_service, mock_redis):
    """Тест сохранения корзины пользователя"""
    user_id = 12345
    cart_data = {'period_days': 30, 'countries': ['ru', 'us'], 'devices': 3, 'traffic_gb': 10, 'total_price': 50000}

    result = await user_cart_service.save_user_cart(user_id, cart_data)

    assert result is True
    assert f'user_cart:{user_id}' in mock_redis.storage
    assert cart_data == eval(mock_redis.storage[f'user_cart:{user_id}'])


async def test_get_user_cart(user_cart_service, mock_redis):
    """Тест получения корзины пользователя"""
    user_id = 12345
    cart_data = {'period_days': 30, 'countries': ['ru', 'us'], 'devices': 3, 'traffic_gb': 10, 'total_price': 50000}

    # Сохраняем данные
    await user_cart_service.save_user_cart(user_id, cart_data)

    # Получаем данные
    result = await user_cart_service.get_user_cart(user_id)

    assert result == cart_data


async def test_get_user_cart_not_found(user_cart_service):
    """Тест получения несуществующей корзины пользователя"""
    user_id = 99999

    result = await user_cart_service.get_user_cart(user_id)

    assert result is None


async def test_delete_user_cart(user_cart_service, mock_redis):
    """Тест удаления корзины пользователя"""
    user_id = 12345
    cart_data = {'period_days': 30, 'countries': ['ru', 'us'], 'devices': 3, 'traffic_gb': 10, 'total_price': 50000}

    # Сохраняем данные
    await user_cart_service.save_user_cart(user_id, cart_data)
    assert f'user_cart:{user_id}' in mock_redis.storage

    # Удаляем данные
    result = await user_cart_service.delete_user_cart(user_id)

    assert result is True
    assert f'user_cart:{user_id}' not in mock_redis.storage


async def test_delete_user_cart_not_found(user_cart_service):
    """Тест удаления несуществующей корзины пользователя"""
    user_id = 99999

    result = await user_cart_service.delete_user_cart(user_id)

    assert result is False


async def test_has_user_cart(user_cart_service, mock_redis):
    """Тест проверки наличия корзины пользователя"""
    user_id = 12345
    cart_data = {'period_days': 30, 'countries': ['ru', 'us'], 'devices': 3, 'traffic_gb': 10, 'total_price': 50000}

    # Проверяем, что корзины нет
    result = await user_cart_service.has_user_cart(user_id)
    assert result is False

    # Сохраняем данные
    await user_cart_service.save_user_cart(user_id, cart_data)

    # Проверяем, что корзина есть
    result = await user_cart_service.has_user_cart(user_id)
    assert result is True


async def test_has_user_cart_not_found(user_cart_service):
    """Тест проверки отсутствия корзины пользователя"""
    user_id = 99999

    result = await user_cart_service.has_user_cart(user_id)

    assert result is False


# ---- «Свежее намерение» пополнить ради корзины (cart_topup_intent) ----


def _intent_key(user_id: int) -> str:
    return f'cart_topup_intent:{user_id}'


async def test_save_cart_with_return_to_cart_sets_intent(user_cart_service, mock_redis):
    """return_to_cart=True ставит метку намерения пополнить ради корзины."""
    user_id = 12345
    cart_data = {'period_days': 30, 'total_price': 50000, 'return_to_cart': True}

    await user_cart_service.save_user_cart(user_id, cart_data)

    assert _intent_key(user_id) in mock_redis.storage
    assert await user_cart_service.has_topup_intent(user_id) is True


async def test_save_cart_without_return_to_cart_no_intent(user_cart_service, mock_redis):
    """Обычное сохранение корзины (без return_to_cart) метку НЕ ставит.

    Это и есть защита: пополнение ради подарка/просто денег не должно молча
    тратиться на подписку из корзины.
    """
    user_id = 12345
    cart_data = {'period_days': 30, 'total_price': 50000}

    await user_cart_service.save_user_cart(user_id, cart_data)

    assert _intent_key(user_id) not in mock_redis.storage
    assert await user_cart_service.has_topup_intent(user_id) is False


async def test_has_topup_intent_is_non_destructive(user_cart_service):
    """Проверка наличия метки не гасит её — частичное пополнение может до-сработать."""
    user_id = 12345
    await user_cart_service.save_user_cart(user_id, {'total_price': 100, 'return_to_cart': True})

    assert await user_cart_service.has_topup_intent(user_id) is True
    # Повторная проверка всё ещё True (peek, не consume)
    assert await user_cart_service.has_topup_intent(user_id) is True


async def test_clear_topup_intent(user_cart_service, mock_redis):
    """clear_topup_intent гасит метку (вызывается после успешной авто-покупки)."""
    user_id = 12345
    await user_cart_service.save_user_cart(user_id, {'total_price': 100, 'return_to_cart': True})
    assert _intent_key(user_id) in mock_redis.storage

    await user_cart_service.clear_topup_intent(user_id)

    assert _intent_key(user_id) not in mock_redis.storage
    assert await user_cart_service.has_topup_intent(user_id) is False


async def test_delete_user_cart_clears_intent(user_cart_service, mock_redis):
    """Очистка корзины снимает и метку намерения, чтобы она не «висела»."""
    user_id = 12345
    await user_cart_service.save_user_cart(user_id, {'total_price': 100, 'return_to_cart': True})
    assert _intent_key(user_id) in mock_redis.storage

    await user_cart_service.delete_user_cart(user_id)

    assert _intent_key(user_id) not in mock_redis.storage


async def test_has_topup_intent_false_when_redis_down():
    """Redis недоступен → намерение считается отсутствующим (не списываем молча)."""
    service = UserCartService()
    service._redis_client = None
    service._initialized = True

    assert await service.has_topup_intent(777) is False


async def test_tariff_checkout_identity_survives_repricing_but_not_new_checkout(user_cart_service):
    cart = {'cart_mode': 'tariff_purchase', 'tariff_id': 1, 'subscription_id': 10, 'total_price': 100}
    assert await user_cart_service.save_user_cart(12345, cart)
    first = await user_cart_service.get_user_cart(12345)
    assert first['tariff_checkout_id']
    first['total_price'] = 200
    assert await user_cart_service.save_user_cart(12345, first)
    restored = await user_cart_service.get_user_cart(12345)
    assert restored['tariff_checkout_id'] == first['tariff_checkout_id']
    per_sub = await user_cart_service.get_subscription_cart(12345, 10)
    assert per_sub['tariff_checkout_id'] == first['tariff_checkout_id']
    assert await user_cart_service.save_user_cart(12345, dict(cart, tariff_checkout_id='new-explicit-checkout'))
    assert (await user_cart_service.get_user_cart(12345))['tariff_checkout_id'] != first['tariff_checkout_id']
    fresh = {key: value for key, value in cart.items() if key != 'tariff_checkout_id'}
    assert await user_cart_service.save_user_cart(12345, fresh)
    assert (await user_cart_service.get_user_cart(12345))['tariff_checkout_id'] != first['tariff_checkout_id']
