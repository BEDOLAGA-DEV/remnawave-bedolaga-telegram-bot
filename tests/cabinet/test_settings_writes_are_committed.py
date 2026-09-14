"""Настройка, сохранённая из кабинета, должна пережить перезапуск.

Жалоба владельца: режим уровней реферальных наград переключается, кабинет
показывает новый режим — а после перезапуска бота он снова прежний, и так
сколько ни переключай.

Причина общая для нескольких настроек: запись в системные настройки делает
только ``flush``, а сессия кабинета закрывается БЕЗ коммита. Значение
применялось к живому процессу (поэтому кабинет и показывал новое), но в базу
не доезжало — перезапуск читал старое.

Проверка бьёт ровно в это: после записи делается ``rollback`` — то же, чем
заканчивается незакоммиченная сессия. Закоммиченное значение его переживает,
незакоммиченное исчезает.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.database.crud.system_setting import get_setting_value
from app.database.models import Base
from app.services.system_settings_service import bot_configuration_service
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)

ADMIN = SimpleNamespace(id=1, username='admin')


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Настройки — глобальный объект: возвращаем значения после теста."""
    monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')
    monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 3)
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    yield
    bot_configuration_service._overrides_raw.pop('REFERRAL_LEVELS_MODE', None)
    bot_configuration_service._overrides_raw.pop('REFERRAL_MAX_LEVEL_DEPTH', None)
    bot_configuration_service._overrides_raw.pop('REFERRAL_REWARD_SCHEME', None)
    bot_configuration_service._overrides_raw.pop('EMAIL_DISABLED_TYPES', None)


@pytest.mark.asyncio
async def test_set_value_survives_a_session_without_commit(monkeypatch):
    """Сама запись настройки обязана коммитить — на неё полагаются все вызовы."""
    async with memory_session(monkeypatch, TABLES) as db:
        await bot_configuration_service.set_value(db, 'REFERRAL_LEVELS_MODE', 'tiers')
        # Сессия кабинета закрывается без коммита — незакоммиченное пропадёт.
        await db.rollback()
        stored = await get_setting_value(db, 'REFERRAL_LEVELS_MODE')

    assert stored == 'tiers'


@pytest.mark.asyncio
async def test_levels_mode_route_persists(monkeypatch):
    """Переключение режима из кабинета доезжает до базы."""
    from app.cabinet.routes.admin_partners import update_referral_levels_mode
    from app.cabinet.schemas.referral import ReferralLevelsModeUpdateRequest

    async with memory_session(monkeypatch, TABLES) as db:
        await update_referral_levels_mode(
            request=ReferralLevelsModeUpdateRequest(levels_mode='tiers'),
            admin=ADMIN,
            db=db,
        )
        await db.rollback()
        stored = await get_setting_value(db, 'REFERRAL_LEVELS_MODE')

    assert stored == 'tiers'


@pytest.mark.asyncio
async def test_chain_depth_route_persists(monkeypatch):
    """Глубина цепочки — та же поверхность, тот же дефект."""
    from app.cabinet.routes.admin_partners import update_referral_depth
    from app.cabinet.schemas.referral import ReferralDepthUpdateRequest

    async with memory_session(monkeypatch, TABLES) as db:
        await update_referral_depth(
            request=ReferralDepthUpdateRequest(max_level_depth=5),
            admin=ADMIN,
            db=db,
        )
        await db.rollback()
        stored = await get_setting_value(db, 'REFERRAL_MAX_LEVEL_DEPTH')

    assert stored == '5'


@pytest.mark.asyncio
async def test_reward_scheme_route_persists(monkeypatch):
    """Схема наград — и она тоже."""
    from app.cabinet.routes.admin_partners import update_referral_scheme
    from app.cabinet.schemas.referral import ReferralSchemeUpdateRequest

    async with memory_session(monkeypatch, TABLES) as db:
        await update_referral_scheme(
            request=ReferralSchemeUpdateRequest(scheme='legacy'),
            admin=ADMIN,
            db=db,
        )
        await db.rollback()
        stored = await get_setting_value(db, 'REFERRAL_REWARD_SCHEME')

    assert stored == 'legacy'


@pytest.mark.asyncio
async def test_email_type_switch_persists(monkeypatch):
    """Выключатель писем по типу — четвёртое место с тем же дефектом."""
    from app.cabinet.services.email_type_switch import EMAIL_DISABLED_TYPES_KEY, set_email_type_enabled

    monkeypatch.setattr(settings, 'EMAIL_DISABLED_TYPES', '')

    async with memory_session(monkeypatch, TABLES) as db:
        disabled = await set_email_type_enabled(db, 'promo_offer', False)
        await db.rollback()
        stored = await get_setting_value(db, EMAIL_DISABLED_TYPES_KEY)

    assert 'promo_offer' in disabled
    assert stored is not None and 'promo_offer' in stored


# ── Сторож ────────────────────────────────────────────────────────────────


def test_batch_writers_commit_themselves():
    """Кто отказался от коммита внутри записи — обязан коммитить сам.

    ``commit=False`` нужен только для пакетной записи «всё или ничего». Если
    такой вызов останется без своего коммита, настройка снова будет теряться
    при перезапуске — ровно тот дефект, ради которого коммит переехал внутрь.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / 'app'
    offenders: list[str] = []

    for path in sorted(root.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue

            opted_out = False
            commits = False
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                called = inner.func.attr if isinstance(inner.func, ast.Attribute) else getattr(inner.func, 'id', None)
                if called in {'set_value', 'reset_value'}:
                    for keyword in inner.keywords:
                        if keyword.arg == 'commit' and isinstance(keyword.value, ast.Constant):
                            opted_out = opted_out or keyword.value.value is False
                if called == 'commit':
                    commits = True

            if opted_out and not commits:
                offenders.append(f'{path.relative_to(root.parent)}:{node.lineno} {node.name}')

    assert not offenders, 'запись настройки без коммита: ' + ', '.join(offenders)
