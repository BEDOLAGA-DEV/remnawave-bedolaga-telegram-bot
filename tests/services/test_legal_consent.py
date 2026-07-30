"""Согласие с офертой и политикой при первой авторизации в кабинете.

Главное, что здесь проверяется: гейт нельзя случайно превратить в кирпич. Требовать
галочку можно только с документом, который пользователь способен открыть, — иначе
установка без заполненной оферты заблокировала бы регистрацию вообще всем.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.database.models import LegalConsent, PromoGroup, Subscription, Tariff, User, UserPromoGroup, UserStatus
from app.services import legal_consent_service as lcs
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    LegalConsent.__table__,
)


@pytest.fixture(autouse=True)
def _documents_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """По умолчанию оба документа заполнены и видны в вебе."""
    monkeypatch.setattr(settings, 'CABINET_REQUIRE_LEGAL_CONSENT', True)
    monkeypatch.setattr(settings, 'CABINET_LEGAL_CONSENT_PRECHECKED', False)
    monkeypatch.setattr(settings, 'PUBLIC_OFFER_DISPLAY_MODE', 'both')
    monkeypatch.setattr(settings, 'PRIVACY_POLICY_DISPLAY_MODE', 'both')

    async def fake_offer(_db, _language, fallback=True):
        return SimpleNamespace(content='Оферта')

    async def fake_policy(_db, _language, fallback=True):
        return SimpleNamespace(content='Политика')

    monkeypatch.setattr(lcs.PublicOfferService, 'get_offer', staticmethod(fake_offer))
    monkeypatch.setattr(lcs.PrivacyPolicyService, 'get_policy', staticmethod(fake_policy))


async def test_both_documents_required_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        requirement = await lcs.get_requirement(db)

        assert requirement.required is True
        assert requirement.prechecked is False
        assert requirement.documents == [lcs.PUBLIC_OFFER, lcs.PRIVACY_POLICY]


async def test_setting_disables_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CABINET_REQUIRE_LEGAL_CONSENT', False)

    async with memory_session(monkeypatch, TABLES) as db:
        requirement = await lcs.get_requirement(db)

        assert requirement.required is False
        assert requirement.documents == []


async def test_prechecked_flag_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CABINET_LEGAL_CONSENT_PRECHECKED', True)

    async with memory_session(monkeypatch, TABLES) as db:
        requirement = await lcs.get_requirement(db)

        assert requirement.required is True
        assert requirement.prechecked is True


async def test_document_hidden_from_web_is_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Документ только для бота нельзя прочитать в кабинете — галочки по нему нет."""
    monkeypatch.setattr(settings, 'PUBLIC_OFFER_DISPLAY_MODE', 'bot')

    async with memory_session(monkeypatch, TABLES) as db:
        requirement = await lcs.get_requirement(db)

        assert requirement.documents == [lcs.PRIVACY_POLICY]


@pytest.mark.parametrize('empty', ['', '   ', None])
async def test_empty_document_is_not_required(monkeypatch: pytest.MonkeyPatch, empty) -> None:
    async def fake_offer(_db, _language, fallback=True):
        return SimpleNamespace(content=empty)

    monkeypatch.setattr(lcs.PublicOfferService, 'get_offer', staticmethod(fake_offer))

    async with memory_session(monkeypatch, TABLES) as db:
        requirement = await lcs.get_requirement(db)

        assert requirement.documents == [lcs.PRIVACY_POLICY]


async def test_no_documents_at_all_disables_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Иначе установка без юр. документов заблокировала бы регистрацию всем."""
    monkeypatch.setattr(settings, 'PUBLIC_OFFER_DISPLAY_MODE', 'bot')
    monkeypatch.setattr(settings, 'PRIVACY_POLICY_DISPLAY_MODE', 'bot')

    async with memory_session(monkeypatch, TABLES) as db:
        requirement = await lcs.get_requirement(db)

        assert requirement.required is False
        assert requirement.documents == []


async def test_broken_document_read_does_not_block_login(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(_db, _language, fallback=True):
        raise RuntimeError('db is down')

    monkeypatch.setattr(lcs.PublicOfferService, 'get_offer', staticmethod(boom))

    async with memory_session(monkeypatch, TABLES) as db:
        requirement = await lcs.get_requirement(db)

        assert requirement.documents == [lcs.PRIVACY_POLICY]


def test_missing_documents_reports_unchecked_boxes() -> None:
    required = [lcs.PUBLIC_OFFER, lcs.PRIVACY_POLICY]

    assert lcs.missing_documents(required, None) == required
    assert lcs.missing_documents(required, []) == required
    assert lcs.missing_documents(required, [lcs.PUBLIC_OFFER]) == [lcs.PRIVACY_POLICY]
    assert lcs.missing_documents(required, required) == []
    # Лишние ключи от клиента ничего не ломают и ничего не подтверждают
    assert lcs.missing_documents(required, ['whatever']) == required


async def test_record_consent_writes_a_row_per_document(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import select

    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            telegram_id=770001,
            username='newbie',
            first_name='Newbie',
            status=UserStatus.ACTIVE.value,
            language='ru',
            balance_kopeks=0,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        await lcs.record_consent(
            db,
            user,
            [lcs.PUBLIC_OFFER, lcs.PRIVACY_POLICY],
            source='cabinet_telegram',
            ip_address='203.0.113.7',
        )

        rows = (await db.execute(select(LegalConsent).order_by(LegalConsent.id))).scalars().all()
        assert [row.document for row in rows] == [lcs.PUBLIC_OFFER, lcs.PRIVACY_POLICY]
        assert {row.user_id for row in rows} == {user.id}
        assert rows[0].source == 'cabinet_telegram'
        assert rows[0].ip_address == '203.0.113.7'
        assert rows[0].accepted_at is not None


async def test_record_consent_with_no_documents_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import select

    async with memory_session(monkeypatch, TABLES) as db:
        await lcs.record_consent(db, SimpleNamespace(id=1), [], source='cabinet_email')

        assert (await db.execute(select(LegalConsent))).scalars().all() == []


# ── Гейт в auth-роутах ────────────────────────────────────────────────────────


async def test_gate_rejects_missing_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cabinet.routes.auth import _require_legal_consent

    async with memory_session(monkeypatch, TABLES) as db:
        with pytest.raises(HTTPException) as exc:
            await _require_legal_consent(db, accepted=[lcs.PUBLIC_OFFER], language='ru')

        assert exc.value.status_code == 428
        assert exc.value.detail['code'] == 'legal_consent_required'
        assert exc.value.detail['missing'] == [lcs.PRIVACY_POLICY]
        assert exc.value.detail['documents'] == [lcs.PUBLIC_OFFER, lcs.PRIVACY_POLICY]


async def test_gate_passes_with_full_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cabinet.routes.auth import _require_legal_consent

    async with memory_session(monkeypatch, TABLES) as db:
        documents = await _require_legal_consent(db, accepted=[lcs.PUBLIC_OFFER, lcs.PRIVACY_POLICY], language='ru')

        assert documents == [lcs.PUBLIC_OFFER, lcs.PRIVACY_POLICY]


async def test_gate_is_transparent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Выключенная настройка не должна ломать регистрацию без чекбоксов."""
    from app.cabinet.routes.auth import _require_legal_consent

    monkeypatch.setattr(settings, 'CABINET_REQUIRE_LEGAL_CONSENT', False)

    async with memory_session(monkeypatch, TABLES) as db:
        assert await _require_legal_consent(db, accepted=None, language='ru') == []
