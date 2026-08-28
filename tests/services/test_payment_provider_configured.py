"""`is_X_configured()` — это ровно кредовая часть `is_X_enabled()`.

Маршруты вебхуков монтируются по `is_X_configured()`, а меню и создание
платежа — по `is_X_enabled()`. Разъедься эти два предиката, и получится либо
неотвечающий вебхук у включённого провайдера, либо открытый эндпоинт у
ненастроенного. Проверяется инвариант

    is_X_enabled() == X_ENABLED and is_X_configured()

на обоих значениях флага, а для провайдеров, чьи учётные данные можно
подделать присваиванием, — ещё и с заполненными кредами.
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest

from app.config import Settings, settings


# Ровно те провайдеры, чьи вебхуки монтирует create_payment_router.
PROVIDERS = [
    'antilopay',
    'apple_iap',
    'aurapay',
    'cispay',
    'cloudpayments',
    'cryptobot',
    'donut',
    'etoplatezhi',
    'freekassa',
    'heleket',
    'jupiter',
    'kassa_ai',
    'lava',
    'mulenpay',
    'overpay',
    'pal24',
    'paypear',
    'platega',
    'riopay',
    'rollypay',
    'severpay',
    'wata',
    'yookassa',
]
# У Tribute нет is_tribute_enabled — код читает флаг напрямую, инвариант ниже
# к нему неприменим, но сам предикат должен существовать (см. отдельный тест).
FLAG_OVERRIDES = {'kassa_ai': 'KASSA_AI_ENABLED', 'apple_iap': 'APPLE_IAP_ENABLED'}


def _flag_name(provider: str) -> str:
    return FLAG_OVERRIDES.get(provider, f'{provider.upper()}_ENABLED')


def _credential_names(provider: str) -> list[str]:
    """Поля настроек, которые читает is_X_configured (по исходнику)."""
    source = inspect.getsource(getattr(Settings, f'is_{provider}_configured'))
    tree = ast.parse(source.lstrip())
    names = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and re.fullmatch(r'[A-Z][A-Z0-9_]*', node.attr)
    }
    return sorted(names)


def test_every_listed_provider_has_both_predicates() -> None:
    """Список ниже — контракт с create_payment_router, а не украшение."""
    missing = [
        p for p in PROVIDERS if not (hasattr(settings, f'is_{p}_configured') and hasattr(settings, f'is_{p}_enabled'))
    ]
    assert missing == [], missing


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('flag', [True, False], ids=['on', 'off'])
def test_enabled_is_flag_and_configured(monkeypatch, provider: str, flag: bool) -> None:
    monkeypatch.setattr(settings, _flag_name(provider), flag, raising=False)

    configured = getattr(settings, f'is_{provider}_configured')()
    enabled = getattr(settings, f'is_{provider}_enabled')()

    assert enabled == (flag and configured)


@pytest.mark.parametrize('provider', PROVIDERS)
def test_enabled_is_flag_when_credentials_are_present(monkeypatch, provider: str) -> None:
    """С заполненными кредами включение решает только флаг."""
    for name in _credential_names(provider):
        monkeypatch.setattr(settings, name, 'x', raising=False)

    if not getattr(settings, f'is_{provider}_configured')():
        # apple_iap дополнительно требует файлы сертификатов — подделать
        # присваиванием нельзя, и ветка configured=True тут недостижима.
        pytest.skip(f'{provider}: учётные данные не подделываются присваиванием')

    monkeypatch.setattr(settings, _flag_name(provider), True, raising=False)
    assert getattr(settings, f'is_{provider}_enabled')() is True

    monkeypatch.setattr(settings, _flag_name(provider), False, raising=False)
    assert getattr(settings, f'is_{provider}_enabled')() is False


@pytest.mark.parametrize('provider', PROVIDERS)
def test_missing_credential_disables_the_provider(monkeypatch, provider: str) -> None:
    """Убрали любую креду — провайдер не настроен и не включён."""
    credentials = _credential_names(provider)
    for name in credentials:
        monkeypatch.setattr(settings, name, 'x', raising=False)
    if not getattr(settings, f'is_{provider}_configured')():
        pytest.skip(f'{provider}: учётные данные не подделываются присваиванием')

    for missing in credentials:
        monkeypatch.setattr(settings, missing, None, raising=False)
        monkeypatch.setattr(settings, _flag_name(provider), True, raising=False)

        assert getattr(settings, f'is_{provider}_configured')() is False, missing
        assert getattr(settings, f'is_{provider}_enabled')() is False, missing

        monkeypatch.setattr(settings, missing, 'x', raising=False)


def test_tribute_has_a_configured_predicate() -> None:
    """У Tribute нет is_*_enabled, но маршруту нужен тот же признак."""
    assert hasattr(settings, 'is_tribute_configured')
