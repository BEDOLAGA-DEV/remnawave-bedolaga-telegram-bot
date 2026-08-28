"""Каждое имя из ``__all__`` должно реально резолвиться.

``app/webserver`` и ``app/webapi`` объявляют публичный интерфейс в ``__all__``,
а отдают его модульным ``__getattr__`` (PEP 562): импорт пакета не должен
тянуть за собой FastAPI-приложение целиком. Опечатка в ``__all__`` или
переименование в ``__getattr__`` при таком устройстве не ловится ничем —
пакет импортируется нормально, а падает только тот, кто обратится к имени.

Запрос CodeQL ``py/undefined-export`` про ``__getattr__`` не знает и помечал
все эти имена как неопределённые (пять error-алертов), поэтому он выключен в
.github/codeql/codeql-config.yml — а инвариант проверяется здесь, честным
обращением к атрибуту.
"""

from __future__ import annotations

import importlib

import pytest


LAZY_PACKAGES = ['app.webserver', 'app.webapi']


@pytest.mark.parametrize('package_name', LAZY_PACKAGES)
def test_package_declares_exports(package_name: str) -> None:
    package = importlib.import_module(package_name)

    assert getattr(package, '__all__', None), f'{package_name}: пустой или отсутствующий __all__'


@pytest.mark.parametrize('package_name', LAZY_PACKAGES)
def test_every_exported_name_resolves(package_name: str) -> None:
    package = importlib.import_module(package_name)

    unresolved = []
    for name in package.__all__:
        try:
            getattr(package, name)
        except AttributeError:
            unresolved.append(name)

    assert unresolved == [], f'{package_name}: имена из __all__ не резолвятся: {unresolved}'


@pytest.mark.parametrize('package_name', LAZY_PACKAGES)
def test_unknown_name_still_raises(package_name: str) -> None:
    """__getattr__ не должен выдавать что попало вместо AttributeError."""
    package = importlib.import_module(package_name)

    missing = 'definitely_not_exported'  # через переменную: B009 запрещает getattr с литералом
    with pytest.raises(AttributeError):
        getattr(package, missing)
