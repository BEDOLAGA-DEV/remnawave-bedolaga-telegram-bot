"""Запись подписки в панель: один путь для всех кнопок и фоновых задач."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import SubscriptionStatus
from app.external.remnawave_api import RemnaWaveAPIError, RemnaWaveTransientError
from app.services.panel_sync import push_subscription
from app.services.panel_sync.expiry import _MINIMUM_FUTURE as MARGIN, SKEW_RETRY_MARGIN


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _user(**kw):
    base = dict(
        id=10,
        telegram_id=555,
        username='tg',
        full_name='Иван',
        email=None,
        status='active',
        remnawave_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _sub(**kw):
    base = dict(
        id=101,
        user_id=10,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=NOW + timedelta(days=30),
        traffic_limit_gb=10,
        connected_squads=['s1'],
        tariff=None,
        remnawave_id=None,
        remnawave_short_id='ab12cd',
        remnawave_short_uuid='abc123',
        device_limit=2,
        subscription_url='',
        subscription_crypto_link='',
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _panel_user(user_id=42, expire_at=None):
    return SimpleNamespace(
        id=user_id,
        short_uuid='abc123',
        subscription_url='https://p/s',
        happ_crypto_link='crypto',
        expire_at=expire_at or NOW + timedelta(days=30),
    )


def _api(**overrides):
    api = AsyncMock()
    api.get_user_by_id.return_value = None
    api.get_user_by_short_uuid.return_value = None
    api.find_users_by_telegram_id.return_value = []
    api.find_users_by_email.return_value = []
    api.update_user.return_value = _panel_user()
    api.create_user.return_value = _panel_user(user_id=77)
    for key, value in overrides.items():
        getattr(api, key).return_value = value
    return api


def _db(*, panel_id_holder=None):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: panel_id_holder))
    return db


@pytest.mark.asyncio
async def test_known_account_is_updated_not_created():
    api = _api(get_user_by_id=_panel_user())

    result = await push_subscription(api, _user(), _sub(remnawave_id=42), multi_tariff=True, now=NOW)

    assert result.action == 'updated'
    api.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_account_is_created():
    api = _api()

    result = await push_subscription(api, _user(), _sub(), multi_tariff=True, now=NOW)

    assert result.action == 'created'
    assert result.panel_user.id == 77
    assert 'username' in api.create_user.await_args.kwargs


@pytest.mark.asyncio
async def test_panel_says_user_is_gone_so_it_is_recreated():
    """Протухший id в базе не должен ронять синхронизацию."""
    api = _api(get_user_by_id=_panel_user())
    api.update_user.side_effect = RemnaWaveAPIError('User not found', 404, {'errorCode': 'A025'})

    result = await push_subscription(api, _user(), _sub(remnawave_id=42), multi_tariff=True, now=NOW)

    assert result.action == 'created'


@pytest.mark.asyncio
async def test_transient_panel_error_is_not_a_reason_to_create_a_duplicate():
    api = _api(get_user_by_id=_panel_user())
    api.update_user.side_effect = RemnaWaveTransientError('timeout')

    with pytest.raises(RemnaWaveTransientError):
        await push_subscription(api, _user(), _sub(remnawave_id=42), multi_tariff=True, now=NOW)

    api.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_subscription_extinguishes_a_future_date_known_in_advance():
    """Дата панели уже на руках — гасим тем же запросом, без второго."""
    stale = _panel_user(expire_at=NOW + timedelta(days=100))
    api = _api(get_user_by_id=stale, update_user=stale)
    sub = _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5), remnawave_id=42)

    result = await push_subscription(api, _user(), sub, multi_tariff=True, now=NOW)

    assert result.expiry_extinguished is True
    assert api.update_user.await_count == 1
    assert api.update_user.await_args.kwargs['expire_at'] == NOW + MARGIN


@pytest.mark.asyncio
async def test_expired_subscription_extinguishes_a_future_date_learned_from_the_answer():
    """Дату панели узнали только из ответа — гасим вторым запросом."""
    api = _api(
        get_user_by_id=_panel_user(expire_at=None),
        update_user=_panel_user(expire_at=NOW + timedelta(days=100)),
    )
    api.get_user_by_id.return_value = SimpleNamespace(id=42, short_uuid='abc123', subscription_url='u')
    sub = _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5), remnawave_id=42)

    result = await push_subscription(api, _user(), sub, multi_tariff=True, now=NOW)

    assert result.expiry_extinguished is True
    assert api.update_user.await_count == 2
    assert api.update_user.await_args.kwargs['expire_at'] == NOW + MARGIN


@pytest.mark.asyncio
async def test_live_subscription_is_written_once():
    api = _api(get_user_by_id=_panel_user())

    result = await push_subscription(api, _user(), _sub(remnawave_id=42), multi_tariff=True, now=NOW)

    assert api.update_user.await_count == 1
    assert result.expiry_extinguished is False


@pytest.mark.asyncio
async def test_identity_is_written_onto_the_subscription():
    api = _api(get_user_by_short_uuid=_panel_user(user_id=55), update_user=_panel_user(user_id=55))
    sub = _sub()

    await push_subscription(api, _user(), sub, db=_db(), multi_tariff=True, now=NOW)

    assert sub.remnawave_id == 55, 'без записи связи следующий проход заведёт дубль'
    assert sub.subscription_url == 'https://p/s'
    assert sub.subscription_crypto_link == 'crypto'


@pytest.mark.asyncio
async def test_panel_id_taken_by_a_sibling_row_is_not_written():
    """Колонка частично уникальна: IntegrityError откатил бы уже сделанный PATCH."""
    api = _api(get_user_by_short_uuid=_panel_user(user_id=55), update_user=_panel_user(user_id=55))
    sub = _sub()

    await push_subscription(api, _user(), sub, db=_db(panel_id_holder=999), multi_tariff=True, now=NOW)

    assert sub.remnawave_id is None


@pytest.mark.asyncio
async def test_single_tariff_records_the_account_on_the_user_too():
    api = _api(get_user_by_short_uuid=_panel_user(user_id=55), update_user=_panel_user(user_id=55))
    user = _user()

    await push_subscription(api, user, _sub(), db=_db(), multi_tariff=False, now=NOW)

    assert user.remnawave_id == 55


@pytest.mark.asyncio
async def test_only_fields_narrows_the_patch():
    """Узкая правка описания не должна тащить в панель дату и сквады."""
    api = _api(get_user_by_id=_panel_user())

    await push_subscription(
        api,
        _user(),
        _sub(remnawave_id=42),
        multi_tariff=True,
        only_fields={'description'},
        now=NOW,
    )

    assert set(api.update_user.await_args.kwargs) == {'user_id', 'description'}


@pytest.mark.asyncio
async def test_recreated_account_replaces_the_stale_link():
    """Иначе следующий проход снова не найдёт аккаунт и заведёт ещё один дубль.

    Живой прогон против панели: аккаунт удалили из панели, бот пересоздал его —
    но в колонке остался старый id, и каждый следующий проход плодил новый
    аккаунт заново.
    """
    api = _api(get_user_by_id=_panel_user(user_id=42))
    api.update_user.side_effect = RemnaWaveAPIError('User not found', 404, {'errorCode': 'A025'})
    api.create_user.return_value = _panel_user(user_id=99)
    subscription = _sub(remnawave_id=42)
    user = _user(remnawave_id=42)

    result = await push_subscription(api, user, subscription, db=_db(), multi_tariff=False, now=NOW)

    assert result.action == 'created'
    assert subscription.remnawave_id == 99, 'в колонке остался id удалённого аккаунта'
    assert user.remnawave_id == 99


# ---------------------------------------------------------------------------
# Разъезд часов: панель сравнивает дату со своими часами
# ---------------------------------------------------------------------------


def _past_date_error() -> RemnaWaveAPIError:
    """Ответ панели 3.4.3, снятый живьём: PATCH с датой, которую она считает прошедшей."""
    return RemnaWaveAPIError(
        'Validation failed',
        400,
        {
            'statusCode': 400,
            'message': 'Validation failed',
            'errors': [{'code': 'custom', 'path': ['expireAt'], 'message': 'Expiration date cannot be in the past'}],
        },
    )


@pytest.mark.asyncio
async def test_extinguish_learned_from_the_answer_retries_with_a_bigger_margin():
    """Второй PATCH отвергнут как «прошлое» — повтор с большим запасом, а не ошибка прохода."""
    api = _api(get_user_by_id=_panel_user(expire_at=None))
    api.get_user_by_id.return_value = SimpleNamespace(id=42, short_uuid='abc123', subscription_url='u')
    api.update_user.side_effect = [
        _panel_user(expire_at=NOW + timedelta(days=100)),  # статус уехал, панель показала будущее
        _past_date_error(),  # гашение «сейчас + запас» для панели уже прошлое
        _panel_user(expire_at=NOW + SKEW_RETRY_MARGIN),  # повтор с большим запасом принят
    ]
    sub = _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5), remnawave_id=42)

    result = await push_subscription(api, _user(), sub, multi_tariff=True, now=NOW)

    assert result.expiry_extinguished is True
    assert api.update_user.await_count == 3
    sent = [call.kwargs.get('expire_at') for call in api.update_user.await_args_list]
    assert sent == [None, NOW + MARGIN, NOW + SKEW_RETRY_MARGIN]


@pytest.mark.asyncio
async def test_extinguish_known_in_advance_falls_back_to_status_first():
    """Дата уехала одним PATCH с остальными полями, и панель отвергла всё: поля важнее — шлём без даты, дату гасим отдельно."""
    stale = _panel_user(expire_at=NOW + timedelta(days=100))
    api = _api(get_user_by_id=stale)
    api.update_user.side_effect = [
        _past_date_error(),  # статус + дата: отвергнуто целиком
        stale,  # статус без даты: принят, панель всё ещё показывает будущее
        _past_date_error(),  # гашение с обычным запасом: снова «прошлое»
        _panel_user(expire_at=NOW + SKEW_RETRY_MARGIN),  # повтор с большим запасом
    ]
    sub = _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5), remnawave_id=42)

    result = await push_subscription(api, _user(), sub, multi_tariff=True, now=NOW)

    assert result.expiry_extinguished is True
    calls = api.update_user.await_args_list
    # Истёкшей подписке статус в панель не уезжает вовсе: истечение панель выводит
    # сама, а DISABLED значил бы «отключена администратором».
    assert 'expire_at' in calls[0].kwargs and 'status' not in calls[0].kwargs
    assert 'expire_at' not in calls[1].kwargs and 'status' not in calls[1].kwargs
    assert calls[2].kwargs == {'user_id': 42, 'expire_at': NOW + MARGIN}
    assert calls[3].kwargs == {'user_id': 42, 'expire_at': NOW + SKEW_RETRY_MARGIN}


@pytest.mark.asyncio
async def test_a_second_rejection_is_a_real_error():
    """Если и большой запас панель считает прошлым, это не разъезд часов — ошибку не глотаем."""
    api = _api(get_user_by_id=_panel_user(expire_at=None))
    api.get_user_by_id.return_value = SimpleNamespace(id=42, short_uuid='abc123', subscription_url='u')
    api.update_user.side_effect = [
        _panel_user(expire_at=NOW + timedelta(days=100)),
        _past_date_error(),
        _past_date_error(),
    ]
    sub = _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5), remnawave_id=42)

    with pytest.raises(RemnaWaveAPIError):
        await push_subscription(api, _user(), sub, multi_tariff=True, now=NOW)


@pytest.mark.asyncio
async def test_other_validation_errors_are_not_mistaken_for_clock_skew():
    api = _api(get_user_by_id=_panel_user(expire_at=None))
    api.get_user_by_id.return_value = SimpleNamespace(id=42, short_uuid='abc123', subscription_url='u')
    other = RemnaWaveAPIError('Validation failed', 400, {'errors': [{'path': ['tag'], 'message': 'Invalid tag'}]})
    api.update_user.side_effect = [_panel_user(expire_at=NOW + timedelta(days=100)), other]
    sub = _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5), remnawave_id=42)

    with pytest.raises(RemnaWaveAPIError):
        await push_subscription(api, _user(), sub, multi_tariff=True, now=NOW)
    assert api.update_user.await_count == 2
