"""Формы админки (партнёрка, тикеты) сохраняют настройки в базу, а не в .env.

Баг: «убрать галочку „Раздел партнёрки виден в кабинете“, перезагрузить бота —
значение возвращается в true». Роут менял settings в памяти и переписывал файл
.env; в контейнере это либо не тот файл, что читает docker при старте, либо его
нет вовсе. Все остальные настройки идут через system_settings — теперь и эти.
Ключ, закреплённый в .env, база перекрыть не может: роут сообщает о нём в
``env_locked``, чтобы кабинет показал подсказку и не давал крутить бесполезный
переключатель.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import SystemSetting
from app.services.system_settings_service import BotConfigurationService
from tests.fixtures.sqlite_memory import memory_session


ROUTES_DIR = Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes'
TABLES = (SystemSetting.__table__,)
ADMIN = SimpleNamespace(id=1, telegram_id=1001)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setattr(BotConfigurationService, '_overrides_raw', {})
    monkeypatch.setattr(BotConfigurationService, '_env_override_keys', set())
    monkeypatch.setattr(settings, 'REFERRAL_PARTNER_SECTION_VISIBLE', True)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS', 100000)
    monkeypatch.setattr(settings, 'SUPPORT_TICKET_SLA_MINUTES', 30)


async def _stored(db, key: str) -> str | None:
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == key))).scalar_one_or_none()
    return None if row is None else row.value


class TestPartnerSettings:
    @pytest.mark.asyncio
    async def test_unchecking_visibility_lands_in_db_and_applies(self, monkeypatch):
        from app.cabinet.routes.admin_partners import PartnerSettingsUpdateRequest, update_partner_settings

        async with memory_session(monkeypatch, TABLES) as db:
            response = await update_partner_settings(
                request=PartnerSettingsUpdateRequest(partner_section_visible=False, withdrawal_min_amount_kopeks=5000),
                admin=ADMIN,
                db=db,
            )
            assert await _stored(db, 'REFERRAL_PARTNER_SECTION_VISIBLE') == 'false'
            assert await _stored(db, 'REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS') == '5000'

        assert settings.REFERRAL_PARTNER_SECTION_VISIBLE is False, 'применяется сразу, без перезапуска'
        assert response.partner_section_visible is False
        assert response.env_locked == []

    @pytest.mark.asyncio
    async def test_env_locked_key_is_reported_and_not_applied(self, monkeypatch):
        from app.cabinet.routes.admin_partners import PartnerSettingsUpdateRequest, update_partner_settings

        monkeypatch.setattr(BotConfigurationService, '_env_override_keys', {'REFERRAL_PARTNER_SECTION_VISIBLE'})
        async with memory_session(monkeypatch, TABLES) as db:
            response = await update_partner_settings(
                request=PartnerSettingsUpdateRequest(partner_section_visible=False), admin=ADMIN, db=db
            )
            # в базу пишем (сработает, когда ключ уберут из .env), в память — нет
            assert await _stored(db, 'REFERRAL_PARTNER_SECTION_VISIBLE') == 'false'

        assert settings.REFERRAL_PARTNER_SECTION_VISIBLE is True
        assert response.partner_section_visible is True
        assert response.env_locked == ['partner_section_visible']

    @pytest.mark.asyncio
    async def test_get_reports_env_locked_fields(self, monkeypatch):
        from app.cabinet.routes.admin_partners import get_partner_settings

        monkeypatch.setattr(BotConfigurationService, '_env_override_keys', {'REFERRAL_WITHDRAWAL_ENABLED'})
        response = await get_partner_settings(admin=ADMIN)
        assert response.env_locked == ['withdrawal_enabled']


class TestTicketSettings:
    @pytest.mark.asyncio
    async def test_sla_minutes_land_in_db_and_apply(self, monkeypatch):
        from app.cabinet.routes.admin_tickets import TicketSettingsUpdateRequest, update_ticket_settings

        async with memory_session(monkeypatch, TABLES) as db:
            response = await update_ticket_settings(
                request=TicketSettingsUpdateRequest(sla_minutes=45), admin=ADMIN, db=db
            )
            assert await _stored(db, 'SUPPORT_TICKET_SLA_MINUTES') == '45'

        assert settings.SUPPORT_TICKET_SLA_MINUTES == 45
        assert response.sla_minutes == 45
        assert response.env_locked == []


def test_no_cabinet_route_rewrites_dotenv():
    """Сторож: .env — не хранилище настроек; в контейнере это не тот файл или его нет."""
    offenders = [
        str(path.relative_to(ROUTES_DIR.parent.parent.parent))
        for path in ROUTES_DIR.rglob('*.py')
        if "Path('.env')" in path.read_text(encoding='utf-8')
    ]
    assert offenders == []
