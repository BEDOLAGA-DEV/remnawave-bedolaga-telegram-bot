"""Выбор OAuth redirect_uri по домену запроса.

Кабинет может стоять на нескольких доменах (зеркало, CDN). Раньше redirect_uri
жёстко указывал на CABINET_URL, поэтому логин с зеркала возвращал человека на
канонический домен, а зеркало оставалось разлогиненным.

Тут проверяется главное: адрес возврата берётся ИЗ СПИСКА разрешённых, а не из
заголовка как есть. Origin присылает клиент, и совпадение обязано быть точным —
иначе чужой домен получил бы authorization code, то есть чужой аккаунт.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings


CANONICAL = 'https://cab.example.com'
MIRROR = 'https://mirror.example.com'


@pytest.fixture
def origins(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_URL', CANONICAL, raising=False)
    monkeypatch.setattr(
        settings,
        'CABINET_ALLOWED_ORIGINS',
        f'{MIRROR},https://cdn.example.net/',
        raising=False,
    )


def _resolve(origin):
    from app.cabinet.auth.oauth_providers import resolve_oauth_redirect_uri

    return resolve_oauth_redirect_uri(origin)


def test_allowed_mirror_returns_to_itself(origins):
    """Разрешённое зеркало завершает OAuth на своём же домене."""
    assert _resolve(MIRROR) == f'{MIRROR}/auth/oauth/callback'


def test_trailing_slash_matches_on_both_sides(origins):
    """Слэш в конце — у заголовка или в настройке — не должен ломать совпадение."""
    assert _resolve(f'{MIRROR}/') == f'{MIRROR}/auth/oauth/callback'
    # в настройке этот домен записан со слэшем
    assert _resolve('https://cdn.example.net') == 'https://cdn.example.net/auth/oauth/callback'


def test_unknown_origin_falls_back_to_canonical(origins):
    """Чужой Origin не должен получать authorization code.

    Заголовок присылает клиент, поэтому список разрешённых — единственная
    защита: всё, чего в нём нет, уходит на канонический домен.
    """
    assert _resolve('https://evil.example') == f'{CANONICAL}/auth/oauth/callback'


def test_lookalike_origin_is_not_accepted(origins):
    """Совпадение точное: домен-двойник с суффиксом не проходит."""
    assert _resolve(f'{MIRROR}.evil.example') == f'{CANONICAL}/auth/oauth/callback'
    assert _resolve('https://evil.example?x=https://mirror.example.com') == (f'{CANONICAL}/auth/oauth/callback')


def test_missing_origin_falls_back_to_canonical(origins):
    """Запрос без Origin — прежнее поведение канонического домена."""
    assert _resolve(None) == f'{CANONICAL}/auth/oauth/callback'
    assert _resolve('') == f'{CANONICAL}/auth/oauth/callback'


def test_canonical_origin_allowed_even_without_the_list(monkeypatch):
    """Свой домен работает, даже если список разрешённых пуст."""
    monkeypatch.setattr(settings, 'CABINET_URL', CANONICAL, raising=False)
    monkeypatch.setattr(settings, 'CABINET_ALLOWED_ORIGINS', '', raising=False)

    assert _resolve(CANONICAL) == f'{CANONICAL}/auth/oauth/callback'
    assert _resolve(MIRROR) == f'{CANONICAL}/auth/oauth/callback'


def test_wildcard_in_the_list_does_not_open_everything(monkeypatch):
    """CABINET_ALLOWED_ORIGINS='*' не должен пускать произвольный домен.

    Звёздочку туда пишут ради CORS; сравнение здесь строковое, поэтому '*'
    остаётся просто невалидным элементом списка — фиксируем это тестом, чтобы
    случайный переход на сопоставление по маске не прошёл незамеченным.
    """
    monkeypatch.setattr(settings, 'CABINET_URL', CANONICAL, raising=False)
    monkeypatch.setattr(settings, 'CABINET_ALLOWED_ORIGINS', '*', raising=False)

    assert _resolve('https://evil.example') == f'{CANONICAL}/auth/oauth/callback'


class _FakeRequest:
    def __init__(self, origin: str | None):
        self.headers = {'origin': origin} if origin else {}


@pytest.mark.asyncio
async def test_authorize_stores_redirect_uri_in_state_and_keeps_it_out_of_the_url(origins, monkeypatch):
    """Выбранный адрес возврата уезжает в state, но не в ссылку авторизации.

    В state он нужен, чтобы обмен кода прошёл с ТЕМ ЖЕ redirect_uri (провайдер
    их сверяет). В query-параметрах ему делать нечего.
    """
    from app.cabinet.routes import oauth as oauth_routes

    captured: dict = {}

    async def fake_generate_state(provider, extra_data=None):
        captured['extra_data'] = extra_data
        return 'state-token'

    class FakeProvider:
        def __init__(self, redirect_uri):
            self.redirect_uri = redirect_uri

        def prepare_auth_state(self):
            return None

        def get_authorization_url(self, state, **params):
            captured['url_params'] = params
            return f'https://provider.example/auth?state={state}&redirect_uri={self.redirect_uri}'

    monkeypatch.setattr(oauth_routes, 'generate_oauth_state', fake_generate_state)
    monkeypatch.setattr(oauth_routes, 'get_provider', lambda name, redirect_uri=None: FakeProvider(redirect_uri))

    response = await oauth_routes.get_oauth_authorize_url('yandex', _FakeRequest(MIRROR))

    assert captured['extra_data']['oauth_redirect_uri'] == f'{MIRROR}/auth/oauth/callback'
    # в ссылку уходят только параметры с префиксом _; адреса возврата там быть
    # не должно ни под каким именем — иначе он поедет в query authorize-ссылки
    assert not [k for k in captured['url_params'] if 'redirect' in k]
    assert f'{MIRROR}/auth/oauth/callback' in response.authorize_url


@pytest.mark.asyncio
async def test_authorize_from_unknown_origin_stores_canonical(origins, monkeypatch):
    """С чужого домена в state попадает канонический адрес, а не присланный."""
    from app.cabinet.routes import oauth as oauth_routes

    captured: dict = {}

    async def fake_generate_state(provider, extra_data=None):
        captured['extra_data'] = extra_data
        return 'state-token'

    class FakeProvider:
        def __init__(self, redirect_uri):
            self.redirect_uri = redirect_uri

        def prepare_auth_state(self):
            return None

        def get_authorization_url(self, state, **params):
            return 'https://provider.example/auth'

    monkeypatch.setattr(oauth_routes, 'generate_oauth_state', fake_generate_state)
    monkeypatch.setattr(oauth_routes, 'get_provider', lambda name, redirect_uri=None: FakeProvider(redirect_uri))

    await oauth_routes.get_oauth_authorize_url('yandex', _FakeRequest('https://evil.example'))

    assert captured['extra_data']['oauth_redirect_uri'] == f'{CANONICAL}/auth/oauth/callback'


@pytest.mark.asyncio
async def test_linking_init_uses_the_request_origin(origins, monkeypatch):
    """Привязка провайдера с зеркала тоже возвращается на зеркало.

    Логин и привязка — два независимых входа в OAuth; исправленный только
    логин оставил бы «Подключить Яндекс» с прежним разрывом домена.
    """
    from app.cabinet.routes import account_linking as linking

    captured: dict = {}

    async def fake_generate_state(provider, extra_data=None):
        captured['extra_data'] = extra_data
        return 'state-token'

    class FakeProvider:
        def __init__(self, redirect_uri):
            captured['redirect_uri'] = redirect_uri

        def prepare_auth_state(self):
            return None

        def get_authorization_url(self, state, **params):
            return 'https://provider.example/auth'

    monkeypatch.setattr(linking, 'generate_oauth_state', fake_generate_state)
    monkeypatch.setattr(linking, 'get_provider', lambda name, redirect_uri=None: FakeProvider(redirect_uri))

    user = SimpleNamespace(id=1, yandex_id=None)
    await linking.link_provider_init('yandex', _FakeRequest(MIRROR), user=user)

    assert captured['redirect_uri'] == f'{MIRROR}/auth/oauth/callback'
    assert captured['extra_data']['oauth_redirect_uri'] == f'{MIRROR}/auth/oauth/callback'


@pytest.mark.asyncio
async def test_linking_exchange_reuses_the_redirect_uri_from_state(origins, monkeypatch):
    """Обмен кода при привязке идёт с тем же адресом, что и на init.

    Провайдер сверяет redirect_uri между авторизацией и обменом; разошедшиеся
    значения дают invalid_grant, и привязка молча не срабатывает.
    """
    from fastapi import HTTPException

    from app.cabinet.routes import account_linking as linking

    captured: dict = {}

    def fake_get_provider(name, redirect_uri=None):
        captured['redirect_uri'] = redirect_uri

    monkeypatch.setattr(linking, 'get_provider', fake_get_provider)

    with pytest.raises(HTTPException):
        await linking._exchange_and_link_oauth(
            db=None,
            user=SimpleNamespace(id=1),
            provider='yandex',
            code='code',
            state='state-token',
            state_data={'oauth_redirect_uri': f'{MIRROR}/auth/oauth/callback'},
            device_id=None,
            log_context='test',
        )

    assert captured['redirect_uri'] == f'{MIRROR}/auth/oauth/callback'
