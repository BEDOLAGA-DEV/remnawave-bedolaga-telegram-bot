"""Поиск панельного аккаунта: один порядок ключей на все точки записи.

До консолидации полный порядок был только в сервисе подписок; массовая
синхронизация и кабинет искали короче и в тех же ситуациях заводили в панели
дубль рядом с живым оплаченным аккаунтом.

Порядок — по убыванию точности: числовой id → shortUuid → telegram → email.
Неточные ключи отдают список, из которого без дополнительной проверки берётся
первый попавшийся, поэтому точные обязаны идти раньше.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import RemnaWaveInvalidUserIdError, RemnaWaveTransientError
from app.services.panel_sync import resolve_panel_identity


def _api(**overrides):
    api = AsyncMock()
    api.get_user_by_id.return_value = None
    api.get_user_by_short_uuid.return_value = None
    api.find_users_by_telegram_id.return_value = []
    api.find_users_by_email.return_value = []
    for key, value in overrides.items():
        getattr(api, key).return_value = value
    return api


def _user(**kw):
    base = dict(id=1, telegram_id=555, email='u@example.com', remnawave_id=None, status='active')
    base.update(kw)
    return SimpleNamespace(**base)


def _sub(**kw):
    base = dict(id=101, remnawave_id=None, remnawave_short_uuid='abc123', remnawave_short_id='ab12cd')
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_exact_subscription_id_wins_in_multi_tariff():
    api = _api(get_user_by_id=SimpleNamespace(id=42, username='u_ab12cd'))

    identity = await resolve_panel_identity(api, _user(), _sub(remnawave_id=42), multi_tariff=True)

    assert identity.user_id == 42
    assert identity.source == 'subscription'
    api.find_users_by_telegram_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_tariff_uses_the_user_level_id():
    api = _api(get_user_by_id=SimpleNamespace(id=7, username='u'))

    identity = await resolve_panel_identity(api, _user(remnawave_id=7), _sub(), multi_tariff=False)

    assert identity.user_id == 7
    assert identity.source == 'user'


@pytest.mark.asyncio
async def test_stale_id_is_not_trusted_and_search_continues():
    """Панель ответила 404 на записанный id — связь протухла, ищем дальше."""
    api = _api(get_user_by_short_uuid=SimpleNamespace(id=55, username='u_ab12cd'))

    identity = await resolve_panel_identity(api, _user(), _sub(remnawave_id=42), multi_tariff=True)

    assert identity.user_id == 55
    assert identity.source == 'short_uuid'


@pytest.mark.asyncio
async def test_short_uuid_is_checked_before_telegram():
    """У человека может быть несколько аккаунтов — неточный ключ берёт первый попавшийся."""
    api = _api(
        get_user_by_short_uuid=SimpleNamespace(id=77, username='u_ab12cd'),
        find_users_by_telegram_id=[SimpleNamespace(id=99, username='u_other')],
    )

    identity = await resolve_panel_identity(api, _user(), _sub(), multi_tariff=False)

    assert identity.user_id == 77


@pytest.mark.asyncio
async def test_email_is_the_last_resort():
    api = _api(find_users_by_email=[SimpleNamespace(id=13, username='u')])

    identity = await resolve_panel_identity(api, _user(), _sub(), multi_tariff=False)

    assert identity.user_id == 13
    assert identity.source == 'email'


@pytest.mark.asyncio
async def test_transient_error_on_short_uuid_forbids_guessing():
    """Точный ключ остался непроверенным: создавать нового нельзя, честно падаем."""
    api = _api()
    api.get_user_by_short_uuid.side_effect = RemnaWaveTransientError('panel is down')

    with pytest.raises(RemnaWaveTransientError):
        await resolve_panel_identity(api, _user(), _sub(), multi_tariff=False)


@pytest.mark.asyncio
async def test_transient_error_on_short_uuid_is_forgiven_when_another_key_identifies():
    """Если опознали по telegram — падать не из-за чего."""
    api = _api(find_users_by_telegram_id=[SimpleNamespace(id=21, username='u')])
    api.get_user_by_short_uuid.side_effect = RemnaWaveTransientError('panel is down')

    identity = await resolve_panel_identity(api, _user(), _sub(), multi_tariff=False)

    assert identity.user_id == 21


@pytest.mark.asyncio
async def test_invalid_local_id_is_a_data_bug_and_is_raised():
    """Непригодный идентификатор бота — не «аккаунта нет»; уход в создание плодил бы дубли."""
    api = _api()
    api.get_user_by_id.side_effect = RemnaWaveInvalidUserIdError('bad id')

    with pytest.raises(RemnaWaveInvalidUserIdError):
        await resolve_panel_identity(api, _user(), _sub(remnawave_id='мусор'), multi_tariff=True)


@pytest.mark.asyncio
async def test_multi_tariff_filters_inexact_matches_by_subscription_suffix():
    api = _api(
        find_users_by_telegram_id=[
            SimpleNamespace(id=1, username='u_other'),
            SimpleNamespace(id=2, username='u_ab12cd'),
        ]
    )

    identity = await resolve_panel_identity(api, _user(), _sub(), multi_tariff=True)

    assert identity.user_id == 2


@pytest.mark.asyncio
async def test_multi_tariff_without_short_id_does_not_guess():
    """Без суффикса неточный ключ выбрал бы чужую подписку того же человека."""
    api = _api(find_users_by_telegram_id=[SimpleNamespace(id=1, username='u_other')])

    identity = await resolve_panel_identity(api, _user(), _sub(remnawave_short_id=''), multi_tariff=True)

    assert identity.panel_user is None
    assert identity.source is None


@pytest.mark.asyncio
async def test_pinned_identity_never_falls_back_to_the_user_account():
    """Подменять личность выбранной подписки пользовательским id нельзя."""
    api = _api(get_user_by_id=SimpleNamespace(id=7, username='u'))

    identity = await resolve_panel_identity(api, _user(remnawave_id=7), _sub(), multi_tariff=False, pinned=True)

    assert identity.panel_user is None


@pytest.mark.asyncio
async def test_nothing_found_returns_an_empty_identity():
    identity = await resolve_panel_identity(_api(), _user(), _sub(), multi_tariff=False)

    assert identity.panel_user is None
    assert identity.user_id is None
    assert identity.source is None
