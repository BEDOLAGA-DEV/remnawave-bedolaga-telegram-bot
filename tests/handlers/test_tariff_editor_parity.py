"""Сторож: всё, что правится в кабинете, правится и в телеграм-редакторе тарифов.

Владелец: «в боте много чего нет, как и в кабинете — настройки разнятся, нужно
исправлять, и не допустить ошибок как в прошлый раз». Поля тарифа заводятся в кабинете
(TariffUpdateRequest), а редактор бота отставал: не знал внешний сквад, продукт Lava,
подарки, произвольные дни, лимиты по серверам — и писал дни триала в колонку, которой
не было в модели.

Покрытие бота снимается с кода: kwargs вызовов update_tariff/create_tariff и присваивания
tariff.<поле> во всех модулях редактора. Поля, которые бот меняет отдельными CRUD-вызовами,
перечислены явно с указанием этих вызовов.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cabinet.schemas.tariffs import TariffUpdateRequest
from app.database.models import Tariff


ROOT = Path(__file__).resolve().parents[2]
EDITOR_MODULES = sorted((ROOT / 'app' / 'handlers' / 'admin').glob('tariff*.py'))

# Поля кабинета, которые бот пишет не через update_tariff, а отдельными CRUD-вызовами.
COVERED_BY_CALL = {
    'promo_group_ids': ('add_promo_group_to_tariff', 'remove_promo_group_from_tariff'),
    'is_trial_available': ('set_trial_tariff', 'clear_trial_tariff'),
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding='utf-8'))


def _written_fields() -> set[str]:
    written: set[str] = set()
    for path in EDITOR_MODULES:
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
                if name in ('update_tariff', 'create_tariff'):
                    written.update(kw.arg for kw in node.keywords if kw.arg)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == 'tariff'
                    ):
                        written.add(target.attr)
    return written


def _called_functions() -> set[str]:
    called: set[str] = set()
    for path in EDITOR_MODULES:
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call):
                called.add(node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', ''))
    return called


def test_editor_modules_are_found() -> None:
    names = {path.name for path in EDITOR_MODULES}
    assert 'tariffs.py' in names and 'tariff_custom_traffic.py' in names


def test_every_cabinet_field_is_editable_from_telegram() -> None:
    written = _written_fields()
    called = _called_functions()

    missing = []
    for field in TariffUpdateRequest.model_fields:
        if field in written:
            continue
        if field in COVERED_BY_CALL and any(fn in called for fn in COVERED_BY_CALL[field]):
            continue
        missing.append(field)

    assert not missing, 'кабинет правит эти поля тарифа, а телеграм-редактор — нет: ' + ', '.join(sorted(missing))


def test_telegram_editor_writes_only_real_columns() -> None:
    """Дни триала писались в атрибут, которого не было в модели — молча терялись."""
    columns = set(Tariff.__table__.c.keys())
    phantom = sorted(_written_fields() - columns - set(TariffUpdateRequest.model_fields))
    assert not phantom, f'редактор пишет поля, которых нет ни в модели, ни в кабинете: {phantom}'
    assert 'trial_duration_days' in columns


def test_new_editor_modules_are_registered_from_tariff_router() -> None:
    source = (ROOT / 'app' / 'handlers' / 'admin' / 'tariffs.py').read_text(encoding='utf-8')
    for register in (
        'register_custom_traffic_handlers(dp)',
        'register_custom_days_handlers(dp)',
        'register_panel_settings_handlers(dp)',
        'register_server_limits_handlers(dp)',
    ):
        assert register in source, register


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


@pytest.mark.asyncio
async def test_telegram_family_editor_sets_and_clears_metadata(monkeypatch) -> None:
    from app.handlers.admin import tariffs as mod

    tariff = SimpleNamespace(id=7, name='VPN', entitlement_family=None)
    message = MagicMock()
    message.answer = AsyncMock()
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'tariff_id': 7})
    state.clear = AsyncMock()
    updates = []

    async def update(_db, target, **kwargs):
        updates.append(kwargs)
        target.entitlement_family = kwargs['entitlement_family']
        return target

    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(mod, 'get_tariff_subscriptions_count', AsyncMock(return_value=0))
    monkeypatch.setattr(mod, 'update_tariff', update)
    monkeypatch.setattr(mod, 'format_tariff_info', lambda *_args: 'Тариф')
    monkeypatch.setattr(mod, 'get_tariff_view_keyboard', lambda *_args: None)

    for text_value, expected in (('main', 'main'), (' - ', None)):
        message.text = text_value
        await _unwrap(mod.process_edit_tariff_entitlement_family)(
            message, SimpleNamespace(language='ru'), MagicMock(), state
        )
        assert tariff.entitlement_family == expected

    assert updates == [{'entitlement_family': 'main'}, {'entitlement_family': None}]
    assert state.clear.await_count == 2


@pytest.mark.asyncio
async def test_telegram_family_editor_rejects_invalid_key_without_write(monkeypatch) -> None:
    from app.handlers.admin import tariffs as mod

    message = MagicMock()
    message.text = 'Main Family'
    message.answer = AsyncMock()
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'tariff_id': 7})
    state.clear = AsyncMock()
    update = AsyncMock()

    monkeypatch.setattr(
        mod,
        'get_tariff_by_id',
        AsyncMock(return_value=SimpleNamespace(id=7, name='VPN', entitlement_family=None)),
    )
    monkeypatch.setattr(mod, 'update_tariff', update)

    await _unwrap(mod.process_edit_tariff_entitlement_family)(
        message, SimpleNamespace(language='ru'), MagicMock(), state
    )

    update.assert_not_awaited()
    state.clear.assert_not_awaited()
    message.answer.assert_awaited_once()
