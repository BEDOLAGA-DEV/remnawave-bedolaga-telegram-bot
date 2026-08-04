"""Regression tests for the Remnawave 3.0.0 API migration.

3.0.0 removed ``uuid`` from ``UsersSchema``: a panel user is identified by the
numeric ``id`` only. Everything that used to carry a user UUID was renamed —
including the HWID delete body, which is now ``{'userId': int, 'hwid': str}``.
The predecessor of this file (``test_remnawave_2_8_0.py``) asserted in its
docstring that the delete payload "is unchanged (still ``userUuid``)"; that
claim was already wrong for 3.0.0 and is precisely why the rename went
unnoticed. It is now covered by an explicit test.

Also covered here:
  * ``PATCH /api/users`` keyed on ``id`` (the body has no ``uuid`` field at all);
  * ``POST /api/users/resolve`` — exactly one of id/shortUuid/username;
  * ``POST /api/users/{id}/actions/extend`` with ``{'days': N}``;
  * empty-body 202/204 responses (delete / bulk squad actions / node restart)
    — success is "no exception", there is no ``isDeleted`` / ``eventSent`` field
    left to read;
  * the ``coerce_panel_user_id`` boundary and why ``is_user_not_found_error``
    must NOT treat 400 (or a bad local id) as "user does not exist";
  * cursor pagination of ``/api/users/stream`` incl. the 1..1000 size clamp and
    the 3.0.0 query filters that replaced ``by-telegram-id`` / ``by-email``;
  * single-node restart still requires ``forceRestart`` in the request body;
  * the happ crypt-link chain (local RSA → panel → external Happ API).
"""

from __future__ import annotations

import base64
from typing import Any, Self
from unittest.mock import AsyncMock

import pytest
from yarl import URL

from app.external.remnawave_api import (
    RemnaWaveAPI,
    RemnaWaveAPIError,
    RemnaWaveInvalidUserIdError,
    coerce_panel_user_id,
    is_user_not_found_error,
)


def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


def _user_payload(**overrides: Any) -> dict[str, Any]:
    """Пользователь в форме 3.0.0: числовой ``id``, поля ``uuid`` больше нет."""
    payload: dict[str, Any] = {
        'id': 42,
        'shortUuid': 'short-42',
        'username': 'user42',
        'status': 'ACTIVE',
        'trafficLimitBytes': 0,
        'trafficLimitStrategy': 'NO_RESET',
        'expireAt': '2030-01-01T00:00:00.000Z',
        'createdAt': '2026-01-01T00:00:00.000Z',
        'updatedAt': '2026-01-02T00:00:00.000Z',
        'telegramId': 555,
        'vlessUuid': '11111111-1111-1111-1111-111111111111',
    }
    payload.update(overrides)
    return payload


class _FakeResponse:
    """Минимальный дубль ``aiohttp.ClientResponse`` (тот же приём, что в tests/services)."""

    def __init__(self, status: int = 200, body: str = '') -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    """Дубль ``aiohttp.ClientSession``: записывает вызовы и отдаёт заготовленные ответы.

    Нужен там, где важна именно проводка до сети (query string, тело запроса,
    ответ без тела), а не поведение поверх ``_make_request``.
    """

    def __init__(self, *responses: _FakeResponse) -> None:
        self._responses = list(responses) or [_FakeResponse(204)]
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({'method': method, **kwargs})
        # Последний ответ переиспользуется — удобно для одностраничных обходов.
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]

    def query_string(self, index: int = -1) -> str:
        call = self.calls[index]
        return URL(call['url']).with_query(call['params'] or {}).query_string


def _api_with_session(*responses: _FakeResponse) -> tuple[RemnaWaveAPI, _FakeSession]:
    api = _api()
    session = _FakeSession(*responses)
    api.session = session
    return api, session


@pytest.fixture(autouse=True)
def _local_happ_encryption_off(monkeypatch):
    """Тесты fallback-цепочки ниже проверяют путь панель -> внешний Happ API;
    локальное RSA-шифрование (основной путь по умолчанию) закоротило бы их,
    поэтому здесь оно выключено и включается явно в тестах локального шифрования."""
    from app.config import settings

    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', False)
    # Кэш ключуется по URL и не знает о подменах ключа в тестах — чистим между тестами.
    RemnaWaveAPI._happ_local_cache.clear()


# ============== Граница идентификатора панельного пользователя ==============


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (42, 42),
        (1, 1),
        ('42', 42),  # JSON/FSM могут донести число строкой
        ('  42  ', 42),
        (10**12, 10**12),  # BigInteger из БД
    ],
)
def test_coerce_panel_user_id_accepts_ints_and_digit_strings(value: Any, expected: int):
    assert coerce_panel_user_id(value) == expected
    assert isinstance(coerce_panel_user_id(value), int)


@pytest.mark.parametrize(
    'value',
    [
        '11111111-1111-1111-1111-111111111111',  # протухший panel uuid из старой колонки
        'abc',
        '4.2',
        '-7',
        '',
        '   ',
        None,
        0,
        -1,
        True,  # bool — подкласс int, но осмысленным id не является
        False,
        1.0,
        4.2,
        b'42',
        [42],
    ],
)
def test_coerce_panel_user_id_rejects_everything_else(value: Any):
    """Мусорный идентификатор обязан падать на границе клиента, а не уходить в панель:
    маршруты 3.0.0 параметризованы ``z.coerce.number().positive()`` и ответят 400,
    который ``is_user_not_found_error`` (намеренно) не считает «пользователя нет»."""
    with pytest.raises(RemnaWaveInvalidUserIdError):
        coerce_panel_user_id(value)


def test_invalid_user_id_error_is_a_remnawave_api_error():
    """Вызывающий код ловит RemnaWaveAPIError — новый тип не должен пролетать мимо."""
    assert issubclass(RemnaWaveInvalidUserIdError, RemnaWaveAPIError)
    # Небезопасные формы, которые int() принял бы молча, подменив id
    # ДРУГИМ пользователем, и не-ASCII цифры, проходящие str.isdigit().
    for bad in ('4_2', '+42', '\u00b2', '\u0665', '\uff11\uff12'):
        with pytest.raises(RemnaWaveInvalidUserIdError):
            coerce_panel_user_id(bad)


@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (RemnaWaveAPIError('not found', 404, {}), True),
        (RemnaWaveAPIError('user not found', 500, {'errorCode': 'A018'}), True),
        (RemnaWaveAPIError('user not found', 500, {'errorCode': 'A063'}), True),
        (RemnaWaveAPIError('user not found', 404, {'errorCode': 'A063'}), True),
        # 400 = панель отвергла сам запрос (например, id не коерсится в число).
        (RemnaWaveAPIError('Validation failed', 400, {}), False),
        (RemnaWaveAPIError('Validation failed', 400, {'errorCode': 'A001'}), False),
        (RemnaWaveAPIError('boom', 500, {}), False),
        (RemnaWaveAPIError('boom', None, None), False),
    ],
)
def test_is_user_not_found_error_recognises_only_real_absence(error: RemnaWaveAPIError, expected: bool):
    assert is_user_not_found_error(error) is expected


def test_is_user_not_found_error_never_true_for_invalid_local_id():
    """Битая ссылка в БД бота — это баг данных, а не «в панели нет пользователя».
    Иначе каждый промах идентификатора уходил бы в ветку «создать нового» и плодил
    дубли в панели — поэтому тип проверяется раньше статуса и errorCode."""
    error = RemnaWaveInvalidUserIdError('Invalid panel user id', 404, {'errorCode': 'A018'})

    assert is_user_not_found_error(error) is False


# ============== Пользователь идентифицируется числовым id ==============


def test_parsed_user_has_numeric_id_and_no_uuid_field():
    """3.0.0 удалил ``uuid`` из UsersSchema — датакласс не должен его воскрешать."""
    user = _api()._parse_user(_user_payload())

    assert user.id == 42
    assert isinstance(user.id, int)
    assert not hasattr(user, 'uuid')
    # short_uuid / vless_uuid сохранились, но идентификаторами записи не являются.
    assert user.short_uuid == 'short-42'
    assert user.vless_uuid == '11111111-1111-1111-1111-111111111111'


async def test_get_user_by_id_uses_numeric_path():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': _user_payload()})

    user = await api.get_user_by_id(42)

    assert api._make_request.call_args.args[:2] == ('GET', '/api/users/42')
    assert user.id == 42


async def test_get_user_by_id_rejects_uuid_before_any_request():
    api = _api()
    api._make_request = AsyncMock()

    with pytest.raises(RemnaWaveInvalidUserIdError):
        await api.get_user_by_id('11111111-1111-1111-1111-111111111111')

    api._make_request.assert_not_called()


async def test_update_user_body_is_keyed_on_id_not_uuid():
    """UpdateUserCommand.RequestBodySchema в 3.0.0 не имеет поля ``uuid``: zod срежет
    неизвестный ключ молча, и запрос упадёт в 400 из-за .refine(username ?? id)."""
    api = _api()
    api._make_request = AsyncMock(return_value={'response': _user_payload()})

    await api.update_user(42, telegram_id=555)

    method, endpoint, body = api._make_request.call_args.args[:3]
    assert (method, endpoint) == ('PATCH', '/api/users')
    assert body['id'] == 42
    assert 'uuid' not in body
    assert body['telegramId'] == 555


async def test_update_user_coerces_digit_string_id_to_number():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': _user_payload()})

    await api.update_user('42')

    body = api._make_request.call_args.args[2]
    assert body['id'] == 42
    assert isinstance(body['id'], int)


async def test_update_user_rejects_uuid_before_any_request():
    api = _api()
    api._make_request = AsyncMock()

    with pytest.raises(RemnaWaveInvalidUserIdError):
        await api.update_user('11111111-1111-1111-1111-111111111111', telegram_id=555)

    api._make_request.assert_not_called()


@pytest.mark.parametrize(
    ('method_name', 'endpoint'),
    [
        ('enable_user', '/api/users/42/actions/enable'),
        ('disable_user', '/api/users/42/actions/disable'),
        ('reset_user_traffic', '/api/users/42/actions/reset-traffic'),
    ],
)
async def test_user_actions_are_addressed_by_numeric_id(method_name: str, endpoint: str):
    api = _api()
    api._make_request = AsyncMock(return_value={'response': _user_payload()})

    await getattr(api, method_name)(42)

    assert api._make_request.call_args.args[:2] == ('POST', endpoint)


# ============== POST /api/users/resolve ==============


@pytest.mark.parametrize(
    ('kwargs', 'expected_body'),
    [
        ({'user_id': 42}, {'id': 42}),
        ({'user_id': '42'}, {'id': 42}),  # строка коерсится в число
        ({'short_uuid': 'short-42'}, {'shortUuid': 'short-42'}),
        ({'username': 'user42'}, {'username': 'user42'}),
    ],
)
async def test_resolve_user_sends_exactly_one_identifier(kwargs: dict[str, Any], expected_body: dict[str, Any]):
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'id': 42, 'username': 'user42', 'shortUuid': 'short-42'}})

    resolved = await api.resolve_user(**kwargs)

    method, endpoint, body = api._make_request.call_args.args[:3]
    assert (method, endpoint) == ('POST', '/api/users/resolve')
    assert body == expected_body
    assert resolved == {'id': 42, 'username': 'user42', 'shortUuid': 'short-42'}


@pytest.mark.parametrize(
    'kwargs',
    [
        {},  # ноль идентификаторов
        {'user_id': 42, 'username': 'user42'},  # два
        {'short_uuid': 'short-42', 'username': 'user42'},
        {'user_id': 42, 'short_uuid': 'short-42', 'username': 'user42'},  # три
    ],
)
async def test_resolve_user_rejects_zero_or_multiple_identifiers(kwargs: dict[str, Any]):
    """Панель требует ровно одно поле — отсекаем локально, не тратя запрос на 400."""
    api = _api()
    api._make_request = AsyncMock()

    with pytest.raises(RemnaWaveAPIError):
        await api.resolve_user(**kwargs)

    api._make_request.assert_not_called()


@pytest.mark.parametrize(
    'error',
    [
        RemnaWaveAPIError('not found', 404, {}),
        RemnaWaveAPIError('user not found', 500, {'errorCode': 'A018'}),
        RemnaWaveAPIError('user not found', 500, {'errorCode': 'A063'}),
    ],
)
async def test_resolve_user_returns_none_when_panel_has_no_such_user(error: RemnaWaveAPIError):
    api = _api()
    api._make_request = AsyncMock(side_effect=error)

    assert await api.resolve_user(username='ghost') is None


async def test_resolve_user_propagates_non_not_found_errors():
    """400 — это отказ панели обработать запрос, а не «пользователя нет»:
    проглотив его как None, вызывающий код создал бы дубль."""
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('Validation failed', 400, {}))

    with pytest.raises(RemnaWaveAPIError):
        await api.resolve_user(username='user42')


async def test_resolve_user_returns_none_on_empty_response_envelope():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': None})

    assert await api.resolve_user(user_id=42) is None


# ============== POST /api/users/{id}/actions/extend ==============


@pytest.mark.parametrize('days', [1, 7, 30, 365])
async def test_extend_user_expiration_sends_days_body(days: int):
    api = _api()
    api._make_request = AsyncMock(return_value={'response': _user_payload()})

    user = await api.extend_user_expiration(42, days)

    method, endpoint, body = api._make_request.call_args.args[:3]
    assert (method, endpoint) == ('POST', '/api/users/42/actions/extend')
    assert body == {'days': days}
    assert user.id == 42


@pytest.mark.parametrize('days', [0, -1, -30])
async def test_extend_user_expiration_requires_at_least_one_day(days: int):
    """days < 1 панель отвергнет валидацией — запрос не отправляем вовсе."""
    api = _api()
    api._make_request = AsyncMock()

    with pytest.raises(RemnaWaveAPIError):
        await api.extend_user_expiration(42, days)

    api._make_request.assert_not_called()


async def test_extend_user_expiration_rejects_uuid_before_any_request():
    api = _api()
    api._make_request = AsyncMock()

    with pytest.raises(RemnaWaveInvalidUserIdError):
        await api.extend_user_expiration('11111111-1111-1111-1111-111111111111', 30)

    api._make_request.assert_not_called()


# ============== HWID: тело запроса на числовом userId ==============


async def test_remove_device_body_uses_numeric_user_id_not_user_uuid():
    """2.8.0 переименовал ``userUuid`` -> ``userId`` в HWID-командах, 3.0.0 сделал его
    числом. Со старым ключом панель отвечает 400, и устройство молча не удаляется."""
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    assert await api.remove_device(42, 'TARGET-HWID') is True

    method, endpoint = api._make_request.call_args.args[:2]
    body = api._make_request.call_args.kwargs['data']
    assert (method, endpoint) == ('POST', '/api/hwid/devices/delete')
    assert body == {'userId': 42, 'hwid': 'TARGET-HWID'}
    assert isinstance(body['userId'], int)
    assert 'userUuid' not in body


async def test_remove_device_coerces_digit_string_user_id():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    assert await api.remove_device('42', 'TARGET-HWID') is True

    assert api._make_request.call_args.kwargs['data'] == {'userId': 42, 'hwid': 'TARGET-HWID'}


async def test_remove_device_with_uuid_fails_without_touching_panel():
    """Протухший uuid в БД бота: сообщаем о неудаче, но не шлём заведомо битый запрос."""
    api = _api()
    api._make_request = AsyncMock()

    assert await api.remove_device('11111111-1111-1111-1111-111111111111', 'TARGET-HWID') is False

    api._make_request.assert_not_called()


async def test_reset_user_devices_uses_single_delete_all_call():
    """Раньше это был цикл из N удалений с эвристикой «успех, если упало меньше
    половины»; 3.0.0 делает то же атомарно одним запросом."""
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {}})

    assert await api.reset_user_devices(42) is True

    assert api._make_request.call_count == 1
    method, endpoint = api._make_request.call_args.args[:2]
    assert (method, endpoint) == ('POST', '/api/hwid/devices/delete-all')
    assert api._make_request.call_args.kwargs['data'] == {'userId': 42}


async def test_reset_user_devices_with_uuid_fails_without_touching_panel():
    api = _api()
    api._make_request = AsyncMock()

    assert await api.reset_user_devices('11111111-1111-1111-1111-111111111111') is False

    api._make_request.assert_not_called()


async def test_get_user_devices_is_addressed_by_numeric_id():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'total': 1, 'devices': [{'hwid': 'A', 'userId': 42}]}})

    devices = await api.get_user_devices(42)

    assert api._make_request.call_args.args[:2] == ('GET', '/api/hwid/devices/42')
    assert devices['devices'][0]['userId'] == 42


# ============== Ответы без тела: 202 / 204 ==============


@pytest.mark.parametrize('status', [202, 204])
async def test_delete_user_returns_true_on_empty_body(status: int):
    """3.0.0: DELETE отвечает 204 (синхронно) либо 202 (в очередь) — тела нет,
    поля ``isDeleted`` больше не существует. Чтение ответа дало бы KeyError."""
    api, session = _api_with_session(_FakeResponse(status))

    assert await api.delete_user(42) is True

    assert session.last['method'] == 'DELETE'
    assert session.last['url'] == 'http://panel.local/api/users/42'


@pytest.mark.parametrize('status', [202, 204])
async def test_empty_body_actions_return_true_without_reading_response(status: int):
    """Bulk-операции сквадов, удаление сквада и рестарт ноды выполняются в фоне:
    ``eventSent`` / ``isDeleted`` вычищены из контракта, успех = нет исключения."""
    squad = 'squad-uuid'
    cases = [
        (lambda api: api.delete_internal_squad(squad), 'DELETE', f'http://panel.local/api/internal-squads/{squad}'),
        (
            lambda api: api.add_users_to_internal_squad(squad),
            'POST',
            f'http://panel.local/api/internal-squads/{squad}/bulk-actions/add-users',
        ),
        (
            lambda api: api.remove_users_from_internal_squad(squad),
            'DELETE',
            f'http://panel.local/api/internal-squads/{squad}/bulk-actions/remove-users',
        ),
        (
            lambda api: api.add_many_users_to_internal_squad(squad, [1, 2]),
            'POST',
            f'http://panel.local/api/internal-squads/{squad}/bulk-actions/add-many-users',
        ),
        (
            lambda api: api.remove_many_users_from_internal_squad(squad, [1, 2]),
            'DELETE',
            f'http://panel.local/api/internal-squads/{squad}/bulk-actions/remove-many-users',
        ),
        (lambda api: api.restart_node('node-uuid'), 'POST', 'http://panel.local/api/nodes/node-uuid/actions/restart'),
        (lambda api: api.restart_all_nodes(), 'POST', 'http://panel.local/api/nodes/actions/restart-all'),
    ]

    for call, method, url in cases:
        api, session = _api_with_session(_FakeResponse(status))

        assert await call(api) is True, url

        assert (session.last['method'], session.last['url']) == (method, url)


async def test_add_many_users_sends_numeric_ids():
    api, session = _api_with_session(_FakeResponse(202))

    assert await api.add_many_users_to_internal_squad('squad-uuid', [1, '2', 3]) is True

    assert session.last['json'] == {'userIds': [1, 2, 3]}


async def test_add_many_users_rejects_invalid_ids_before_request():
    api, session = _api_with_session(_FakeResponse(202))

    with pytest.raises(RemnaWaveInvalidUserIdError):
        await api.add_many_users_to_internal_squad('squad-uuid', [1, '11111111-1111-1111-1111-111111111111'])

    assert session.calls == []


async def test_bulk_squad_actions_skip_request_for_empty_id_list():
    api, session = _api_with_session(_FakeResponse(202))

    assert await api.add_many_users_to_internal_squad('squad-uuid', []) is True
    assert await api.remove_many_users_from_internal_squad('squad-uuid', []) is True

    assert session.calls == []


# ============== Рестарт ноды: forceRestart в теле ==============


async def test_restart_node_sends_force_restart_body_default_false():
    api = _api()
    api._make_request = AsyncMock(return_value={})

    assert await api.restart_node('node-uuid') is True

    method, endpoint = api._make_request.call_args.args[:2]
    body = api._make_request.call_args.args[2]
    assert (method, endpoint) == ('POST', '/api/nodes/node-uuid/actions/restart')
    assert body == {'forceRestart': False}


async def test_restart_node_forwards_force_restart_true():
    api = _api()
    api._make_request = AsyncMock(return_value={})

    await api.restart_node('node-uuid', force_restart=True)

    assert api._make_request.call_args.args[2] == {'forceRestart': True}


async def test_restart_all_nodes_sends_force_restart_body():
    api = _api()
    api._make_request = AsyncMock(return_value={})

    assert await api.restart_all_nodes(force_restart=True) is True

    method, endpoint = api._make_request.call_args.args[:2]
    assert (method, endpoint) == ('POST', '/api/nodes/actions/restart-all')
    assert api._make_request.call_args.args[2] == {'forceRestart': True}


# ============== /api/users/stream: курсор, клэмп size, фильтры ==============


async def test_users_page_stream_omits_cursor_on_first_page():
    api = _api()
    api._parse_user = lambda u: u  # bypass heavy user parsing
    api._make_request = AsyncMock(return_value={'response': {'users': [{'id': 1}], 'nextCursor': '5', 'hasMore': True}})

    page = await api.get_all_users_page_stream()

    assert api._make_request.call_args.args[:2] == ('GET', '/api/users/stream')
    # First page: no cursor in params, only size.
    assert api._make_request.call_args.kwargs['params'] == {'size': 500}
    assert page == {'users': [{'id': 1}], 'nextCursor': '5', 'hasMore': True}


async def test_users_page_stream_passes_cursor_when_given():
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(return_value={'response': {'users': [], 'nextCursor': None, 'hasMore': False}})

    await api.get_all_users_page_stream(cursor='42', size=100)

    assert api._make_request.call_args.kwargs['params'] == {'size': 100, 'cursor': '42'}


async def test_users_page_stream_keeps_cursor_as_string():
    """Запрос коерсит курсор в число (z.coerce.number), а ответ отдаёт его строкой —
    клиент обязан прокидывать значение как есть, не приводя тип."""
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(
        return_value={'response': {'users': [{'id': 1}], 'nextCursor': '9007199254740993', 'hasMore': True}}
    )

    page = await api.get_all_users_page_stream()

    assert page['nextCursor'] == '9007199254740993'
    assert isinstance(page['nextCursor'], str)


@pytest.mark.parametrize(
    ('requested', 'sent'),
    [
        (5000, 1000),  # старые конфиги с батчем больше панельного максимума
        (1000, 1000),
        (500, 500),
        (1, 1),
        (0, 1),
        (-5, 1),
    ],
)
async def test_users_page_stream_clamps_size_to_panel_contract(requested: int, sent: int):
    """Контракт панели (zod): size строго 1..1000, иначе 400 «Validation failed»
    и обход пользователей падает на первой же странице."""
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(return_value={'response': {'users': [], 'nextCursor': None, 'hasMore': False}})

    await api.get_all_users_page_stream(size=requested)

    assert api._make_request.call_args.kwargs['params']['size'] == sent


async def test_users_stream_follows_cursor_until_exhausted():
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(
        side_effect=[
            {'response': {'users': [{'id': 1}, {'id': 2}], 'nextCursor': '2', 'hasMore': True}},
            {'response': {'users': [{'id': 3}], 'nextCursor': None, 'hasMore': False}},
        ]
    )

    users = await api.get_all_users_stream(size=2)

    assert [u['id'] for u in users] == [1, 2, 3]
    assert api._make_request.call_count == 2
    # Second call must carry the nextCursor from the first page.
    assert api._make_request.call_args_list[1].kwargs['params'] == {'size': 2, 'cursor': '2'}


async def test_users_stream_stops_when_next_cursor_is_null_even_if_has_more_true():
    """Defensive: a null nextCursor terminates the scan regardless of hasMore."""
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(
        return_value={'response': {'users': [{'id': 1}], 'nextCursor': None, 'hasMore': True}}
    )

    users = await api.get_all_users_stream()

    assert [u['id'] for u in users] == [1]
    assert api._make_request.call_count == 1


async def test_find_users_by_telegram_id_filters_in_query_string():
    """``GET /api/users/by-telegram-id/{id}`` удалён — поиск живёт в query-фильтре
    стрима. Фильтр обязан дойти до строки запроса, иначе бот обойдёт всю панель
    и «найдёт» первого попавшегося пользователя."""
    body = '{"response": {"users": [], "nextCursor": null, "hasMore": false}}'
    api, session = _api_with_session(_FakeResponse(200, body))

    assert await api.find_users_by_telegram_id(555) == []

    assert session.last['url'] == 'http://panel.local/api/users/stream'
    assert session.last['params'] == {'size': 1000, 'telegramId': 555}
    assert session.query_string() == 'size=1000&telegramId=555'


async def test_find_users_by_email_filters_in_query_string():
    """``GET /api/users/by-email/{email}`` удалён — тот же query-фильтр стрима."""
    body = '{"response": {"users": [], "nextCursor": null, "hasMore": false}}'
    api, session = _api_with_session(_FakeResponse(200, body))

    assert await api.find_users_by_email('user@example.com') == []

    assert session.last['params'] == {'size': 1000, 'email': 'user@example.com'}
    assert session.query_string() == 'size=1000&email=user@example.com'


async def test_find_users_passes_all_supported_filters():
    from app.external.remnawave_api import TrafficLimitStrategy, UserStatus

    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(return_value={'response': {'users': [], 'nextCursor': None, 'hasMore': False}})

    await api.find_users(
        telegram_id=555,
        email='user@example.com',
        tag='VIP',
        status=UserStatus.ACTIVE,
        traffic_limit_strategy=TrafficLimitStrategy.MONTH,
        external_squad_uuid='squad-uuid',
    )

    assert api._make_request.call_args.kwargs['params'] == {
        'size': 1000,
        'telegramId': 555,
        'email': 'user@example.com',
        'tag': 'VIP',
        'status': 'ACTIVE',
        'trafficLimitStrategy': 'MONTH',
        'externalSquadUuid': 'squad-uuid',
    }


async def test_find_users_sends_no_filters_when_none_given():
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(return_value={'response': {'users': [], 'nextCursor': None, 'hasMore': False}})

    await api.find_users()

    assert api._make_request.call_args.kwargs['params'] == {'size': 1000}


async def test_find_users_follows_cursor_and_honours_max_results():
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(
        side_effect=[
            {'response': {'users': [{'id': 1}, {'id': 2}], 'nextCursor': '2', 'hasMore': True}},
            {'response': {'users': [{'id': 3}], 'nextCursor': None, 'hasMore': False}},
        ]
    )

    assert [u['id'] for u in await api.find_users(telegram_id=555, max_results=3)] == [1, 2, 3]
    assert api._make_request.call_count == 2
    assert api._make_request.call_args_list[1].kwargs['params'] == {'size': 1000, 'cursor': '2', 'telegramId': 555}


async def test_find_users_stops_early_once_max_results_reached():
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(
        return_value={'response': {'users': [{'id': 1}, {'id': 2}], 'nextCursor': '2', 'hasMore': True}}
    )

    assert [u['id'] for u in await api.find_users(telegram_id=555, max_results=1)] == [1]
    assert api._make_request.call_count == 1


# ============== Happ crypt-ссылки ==============


def _reset_happ_state() -> None:
    RemnaWaveAPI._happ_encrypt_unavailable = False
    RemnaWaveAPI._happ_api_disabled_until = 0.0
    RemnaWaveAPI._happ_api_cache.clear()
    RemnaWaveAPI._happ_api_failed_urls.clear()
    RemnaWaveAPI._happ_local_cache.clear()


async def test_happ_encrypt_404_disables_panel_endpoint_and_falls_back():
    """2.8.0 removed POST /api/system/tools/happ/encrypt → 404 must disable further
    panel calls, but the official Happ API fallback must still produce a crypt5 link."""
    _reset_happ_state()
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('Not Found', 404, {}))
    api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/encrypted-x')
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') == 'happ://crypt5/encrypted-x'
        assert RemnaWaveAPI._happ_encrypt_unavailable is True

        # Subsequent calls short-circuit without touching the removed endpoint.
        api._make_request.reset_mock()
        api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/encrypted-y')
        assert await api.encrypt_happ_crypto_link('https://sub.example/y') == 'happ://crypt5/encrypted-y'
        api._make_request.assert_not_called()
    finally:
        _reset_happ_state()


async def test_happ_encrypt_non_404_error_keeps_endpoint_enabled():
    """A transient 5xx must NOT permanently disable happ-encrypt (only a 404 = removed)."""
    _reset_happ_state()
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('boom', 500, {}))
    api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/encrypted')
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') == 'happ://crypt5/encrypted'
        assert RemnaWaveAPI._happ_encrypt_unavailable is False
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_caches_by_subscription_url():
    """The client is recreated per request — the crypt5 cache must live on the class
    so the external Happ API is hit once per subscription URL until the DB save."""
    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/encrypted')
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') == 'happ://crypt5/encrypted'

        other = _api()
        other._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/should-not-be-called')
        assert await other.encrypt_happ_crypto_link('https://sub.example/x') == 'happ://crypt5/encrypted'
        other._call_happ_crypto_api.assert_not_called()
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_cooldown_after_failure():
    """A Happ API outage must not stall hot paths — one failure pauses further calls."""
    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(side_effect=TimeoutError('slow'))
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') is None
        assert RemnaWaveAPI._happ_api_disabled_until > 0

        api._call_happ_crypto_api.reset_mock()
        assert await api.encrypt_happ_crypto_link('https://sub.example/y') is None
        api._call_happ_crypto_api.assert_not_called()
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_rejects_unexpected_payload_per_url():
    """A non-happ:// body is a per-URL problem: never cached as a link, never retried,
    but it must NOT arm the global cooldown (other URLs keep working)."""
    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(return_value='<html>rate limited</html>')
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') is None
        assert not RemnaWaveAPI._happ_api_cache
        assert RemnaWaveAPI._happ_api_disabled_until == 0.0

        # Same URL is not retried, a different URL still goes through.
        api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/ok')
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') is None
        api._call_happ_crypto_api.assert_not_called()
        assert await api.encrypt_happ_crypto_link('https://sub.example/y') == 'happ://crypt5/ok'
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_4xx_does_not_poison_global_cooldown():
    """A 4xx rejection of one URL must not disable the fallback for everyone."""
    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(side_effect=RemnaWaveAPIError('bad url', 422, {}))
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/bad') is None
        assert RemnaWaveAPI._happ_api_disabled_until == 0.0

        api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/ok')
        assert await api.encrypt_happ_crypto_link('https://sub.example/good') == 'happ://crypt5/ok'
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_429_arms_cooldown_not_per_url_ban():
    """429 is service throttling: pause globally, but the URL must stay retryable."""
    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(side_effect=RemnaWaveAPIError('slow down', 429, {}))
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') is None
        assert RemnaWaveAPI._happ_api_disabled_until > 0
        assert 'https://sub.example/x' not in RemnaWaveAPI._happ_api_failed_urls
    finally:
        _reset_happ_state()


async def test_enrich_uses_external_fallback_only_in_cryptolink_mode(monkeypatch):
    """enrich runs on every get_user_by_*: subscription URLs must not go to the
    external Happ API unless the bot actually needs crypt links (happ_cryptolink
    mode); the cabinet generates missing links on demand in the app-config flow."""
    from types import SimpleNamespace

    from app.config import settings

    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/enriched')
    user = SimpleNamespace(happ_crypto_link=None, subscription_url='https://sub.example/x')
    try:
        monkeypatch.setattr(type(settings), 'is_happ_cryptolink_mode', lambda self: False)
        assert (await api.enrich_user_with_happ_link(user)).happ_crypto_link is None
        api._call_happ_crypto_api.assert_not_called()

        monkeypatch.setattr(type(settings), 'is_happ_cryptolink_mode', lambda self: True)
        assert (await api.enrich_user_with_happ_link(user)).happ_crypto_link == 'happ://crypt5/enriched'
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_disabled_by_setting(monkeypatch):
    """HAPP_CRYPTOLINK_API_FALLBACK_ENABLED=false must skip the external service."""
    from app.config import settings

    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_API_FALLBACK_ENABLED', False)
    api = _api()
    api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/encrypted')
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') is None
        api._call_happ_crypto_api.assert_not_called()
    finally:
        _reset_happ_state()


async def test_happ_local_encryption_roundtrip(monkeypatch):
    """Локальное шифрование должно давать happ://crypt4/<base64>, расшифровываемый
    приватным ключом (та же схема PKCS#1 v1.5, что у subpage панели). Настоящий
    приватный ключ есть только у Happ, поэтому roundtrip — на тестовой паре."""
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA

    from app.config import settings

    keypair = RSA.generate(2048)
    monkeypatch.setattr(
        'app.external.remnawave_api.HAPP_CRYPTO_V4_PUBLIC_KEY',
        keypair.publickey().export_key().decode(),
    )
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', True)

    api = _api()
    api._make_request = AsyncMock()
    api._call_happ_crypto_api = AsyncMock()

    link = await api.encrypt_happ_crypto_link('https://sub.example/x')

    assert link is not None and link.startswith('happ://crypt4/')
    blob = base64.b64decode(link.removeprefix('happ://crypt4/'))
    assert PKCS1_v1_5.new(keypair).decrypt(blob, None) == b'https://sub.example/x'
    # Локальный путь не должен трогать ни панель, ни внешний сервис.
    api._make_request.assert_not_called()
    api._call_happ_crypto_api.assert_not_called()


async def test_happ_local_encryption_real_key_single_rsa4096_block(monkeypatch):
    """Со вшитым ключом Happ v4 (RSA-4096) шифртекст — один блок в 512 байт,
    как у ссылок, которые генерирует официальная страница подписки."""
    from app.config import settings

    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', True)

    link = RemnaWaveAPI._encrypt_locally('https://sub.example/x')

    assert link is not None and link.startswith('happ://crypt4/')
    assert len(base64.b64decode(link.removeprefix('happ://crypt4/'))) == 512


async def test_happ_local_encryption_rejects_oversized_payload(monkeypatch):
    """PKCS#1 v1.5 вмещает size_in_bytes()-11: слишком длинная ссылка -> None,
    а не исключение (дальше цепочка уйдёт в панель/внешний API)."""
    from app.config import settings

    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', True)

    assert RemnaWaveAPI._encrypt_locally('https://sub.example/' + 'x' * 600) is None


async def test_happ_local_encryption_disabled_by_setting():
    """HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED=false должен пропустить локальный
    путь (fixture уже выключила флаг) — цепочка идёт в панель/внешний API."""
    assert RemnaWaveAPI._encrypt_locally('https://sub.example/x') is None


async def test_happ_local_encryption_stable_for_same_url(monkeypatch):
    """Паддинг PKCS#1 v1.5 случайный, поэтому без кэша каждый вызов давал бы новую
    ссылку для того же URL — и синки (сравнение с сохранённой subscription_crypto_link)
    записывали бы ложное «изменение» на каждом проходе. Кэш держит ссылку стабильной
    в рамках процесса."""
    from app.config import settings

    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', True)

    first = RemnaWaveAPI._encrypt_locally('https://sub.example/x')
    second = RemnaWaveAPI._encrypt_locally('https://sub.example/x')

    assert first is not None
    assert first == second
    # Другой URL по-прежнему шифруется независимо.
    assert RemnaWaveAPI._encrypt_locally('https://sub.example/y') != first


async def test_enrich_uses_local_encryption_without_network(monkeypatch):
    """С локальным шифрованием enrich заполняет crypt-ссылку в любом режиме бота,
    не делая ни одного сетевого вызова (ни в панель, ни во внешний Happ API)."""
    from types import SimpleNamespace

    from app.config import settings

    _reset_happ_state()
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', True)
    monkeypatch.setattr(type(settings), 'is_happ_cryptolink_mode', lambda self: False)
    api = _api()
    api._make_request = AsyncMock()
    api._call_happ_crypto_api = AsyncMock()
    user = SimpleNamespace(happ_crypto_link=None, subscription_url='https://sub.example/x')
    try:
        enriched = await api.enrich_user_with_happ_link(user)
        assert enriched.happ_crypto_link is not None
        assert enriched.happ_crypto_link.startswith('happ://crypt4/')
        api._make_request.assert_not_called()
        api._call_happ_crypto_api.assert_not_called()
    finally:
        _reset_happ_state()


@pytest.mark.asyncio
async def test_delete_all_devices_reports_failure_when_devices_remain(monkeypatch):
    """Панель может ответить 200, оставив устройства — это не успех.

    Ответ `delete-all` несёт состояние ПОСЛЕ удаления (`{total, devices}`).
    Пока его игнорировали, кабинет рапортовал пользователю «готово», а
    устройства оставались на месте.
    """
    from app.external.remnawave_api import RemnaWaveAPI

    api = RemnaWaveAPI(base_url='http://x', api_key='k')
    calls = []

    async def fake(method, path, data=None, **kw):
        calls.append((method, path))
        return {'response': {'total': 3, 'devices': [{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}]}}

    monkeypatch.setattr(api, '_make_request', fake)
    assert await api.reset_user_devices(42) is False, 'остались устройства — успехом это не считается'
    assert calls == [('POST', '/api/hwid/devices/delete-all')]


@pytest.mark.asyncio
async def test_delete_all_devices_reports_success_when_panel_is_empty(monkeypatch):
    from app.external.remnawave_api import RemnaWaveAPI

    api = RemnaWaveAPI(base_url='http://x', api_key='k')

    async def fake(method, path, data=None, **kw):
        return {'response': {'total': 0, 'devices': []}}

    monkeypatch.setattr(api, '_make_request', fake)
    assert await api.reset_user_devices(42) is True
