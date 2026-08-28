"""Заглушка ``RemnaWaveConfigurationError`` при неудачном импорте сервиса панели.

Оба роутера (кабинет и webapi) импортируют сервис в ``try/except`` и на случай
провала подставляют заглушку. Заглушка обязана быть классом-исключением: в
модулях есть ``except RemnaWaveConfigurationError``, и с ``None`` этот блок
падал бы с ``TypeError`` прямо в обработчике ошибки, подменяя честный 503
пятисоткой без объяснений.
"""

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

MODULES = [
    ('app/cabinet/routes/admin_remnawave.py', 'app.cabinet.routes'),
    ('app/webapi/routes/remnawave.py', 'app.webapi.routes'),
]


def _load_with_broken_service_import(relative_path: str, package: str):
    """Грузит копию модуля так, будто ``remnawave_service`` не импортируется."""
    # Имя внутри настоящего пакета: иначе относительные импорты модуля не
    # разрешатся. В sys.modules копию не кладём, чтобы не подменить оригинал.
    alias = f'{package}._fallback_copy'
    spec = importlib.util.spec_from_file_location(alias, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)

    saved = sys.modules.get('app.services.remnawave_service')
    # None в sys.modules заставляет `from ... import ...` поднять ImportError.
    sys.modules['app.services.remnawave_service'] = None
    try:
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop('app.services.remnawave_service', None)
        else:
            sys.modules['app.services.remnawave_service'] = saved
        sys.modules.pop(alias, None)

    return module


@pytest.mark.parametrize(('relative_path', 'package'), MODULES)
def test_fallback_is_usable_in_except_clause(relative_path, package):
    module = _load_with_broken_service_import(relative_path, package)

    assert module.RemnaWaveService is None
    assert isinstance(module.RemnaWaveConfigurationError, type)
    assert issubclass(module.RemnaWaveConfigurationError, BaseException)

    # Главное: блок `except` с заглушкой не падает и не глотает чужие ошибки.
    with pytest.raises(ValueError):
        try:
            raise ValueError('boom')
        except module.RemnaWaveConfigurationError:  # pragma: no cover - не должно сработать
            pytest.fail('заглушка не должна ловить посторонние исключения')
