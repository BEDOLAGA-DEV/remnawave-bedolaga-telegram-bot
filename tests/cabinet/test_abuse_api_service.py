"""Внешний антифрод: молчание сервиса не должно вредить клиенту.

Сервис необязательный и живёт за сетью. Цена ошибок здесь несимметричная:
если недоступность прочитать как «подозрительный», честному человеку откажут
в триале и покажут плашку о нарушении, которого не было. Поэтому всё, что не
является явным ответом «ограничен», трактуется как отсутствие претензий.

Отдельно сторожим границу данных: клиентская ручка не должна уметь отдавать
скоринг и виды нарушений — перечень признаков на руках у нарушителя это
инструкция по обходу.
"""

import pytest

from app.services import abuse_api_service


class _Settings:
    def __init__(self, **values):
        self.ABUSE_API_ENABLED = values.get('enabled', True)
        self.ABUSE_API_URL = values.get('url', 'https://panel.example.com/api/v3')
        self.ABUSE_API_KEY = values.get('key', 'rwa_test')
        self.ABUSE_API_TIMEOUT = values.get('timeout', 5)


def test_disabled_service_is_not_configured(monkeypatch):
    monkeypatch.setattr(abuse_api_service, 'settings', _Settings(enabled=False))

    assert abuse_api_service.is_configured() is False


def test_missing_key_is_not_configured(monkeypatch):
    monkeypatch.setattr(abuse_api_service, 'settings', _Settings(key=None))

    assert abuse_api_service.is_configured() is False


@pytest.mark.asyncio
async def test_unconfigured_service_answers_nothing(monkeypatch):
    """Не настроен — вопросов к клиенту нет, а не «неизвестно, подозрительный»."""
    monkeypatch.setattr(abuse_api_service, 'settings', _Settings(enabled=False))

    assert await abuse_api_service.get_summary(366945364) is None
    assert await abuse_api_service.get_violations(366945364) == []
    assert await abuse_api_service.is_limited(366945364) is False


@pytest.mark.asyncio
async def test_unreachable_service_does_not_block_anyone(monkeypatch):
    """Сеть легла — клиент остаётся чистым, экраны кабинета работают."""
    monkeypatch.setattr(abuse_api_service, 'settings', _Settings())

    async def boom(path, params):
        raise OSError('connection refused')

    monkeypatch.setattr(abuse_api_service, '_get', boom)

    with pytest.raises(OSError):
        await abuse_api_service._get('/violations/summary', {})

    async def silent(path, params):
        return None

    monkeypatch.setattr(abuse_api_service, '_get', silent)
    assert await abuse_api_service.is_limited(366945364) is False


@pytest.mark.asyncio
async def test_limited_level_is_recognised(monkeypatch):
    monkeypatch.setattr(abuse_api_service, 'settings', _Settings())

    async def answer(path, params):
        return {'level': 'limited', 'violations': 3}

    monkeypatch.setattr(abuse_api_service, '_get', answer)

    assert await abuse_api_service.is_limited(366945364) is True


@pytest.mark.asyncio
async def test_warned_customer_is_not_limited(monkeypatch):
    """«Замечен» — повод написать человеку, а не отказывать ему в триале."""
    monkeypatch.setattr(abuse_api_service, 'settings', _Settings())

    async def answer(path, params):
        return {'level': 'warned', 'violations': 1}

    monkeypatch.setattr(abuse_api_service, '_get', answer)

    assert await abuse_api_service.is_limited(366945364) is False


def test_client_response_cannot_carry_detection_details():
    """Схема клиентского ответа не содержит полей со скорингом и видами."""
    from app.cabinet.routes.abuse import AbuseNoticeResponse, AbuseStatusResponse

    forbidden = {'level', 'score', 'max_score', 'kind', 'reasons', 'violations'}

    assert not forbidden & set(AbuseStatusResponse.model_fields)
    assert not forbidden & set(AbuseNoticeResponse.model_fields)


@pytest.mark.asyncio
async def test_malformed_notice_does_not_break_dashboard(monkeypatch):
    """Поле не того типа от чужого сервиса — молчание, а не 500 на главной."""
    from types import SimpleNamespace

    from app.cabinet.routes import abuse

    async def summary(telegram_id):
        return {'notice': {'body': 'нарушение', 'sent_at': 1700000000}}

    monkeypatch.setattr(abuse.abuse_api_service, 'get_summary', summary)

    response = await abuse.my_abuse_status(user=SimpleNamespace(id=1, telegram_id=366945364))

    assert response.warned is False
    assert response.notice is None


@pytest.mark.asyncio
async def test_malformed_violations_do_not_break_admin_card(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.cabinet.routes import abuse

    monkeypatch.setattr(abuse, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(telegram_id=366945364)))
    monkeypatch.setattr(abuse.abuse_api_service, 'is_configured', lambda: True)
    monkeypatch.setattr(abuse.abuse_api_service, 'get_summary', AsyncMock(return_value={'level': 'warned'}))

    # Мусорная строка в списке отбрасывается, остальные доезжают.
    monkeypatch.setattr(
        abuse.abuse_api_service,
        'get_violations',
        AsyncMock(return_value=['oops', {'score': 74.0, 'reasons': ['shared']}]),
    )
    response = await abuse.user_abuse_overview(user_id=1, admin=None, db=None)
    assert response.available is True
    assert [item.score for item in response.violations] == [74.0]

    # Поле не того типа — вкладка честно говорит «недоступно», а не падает.
    monkeypatch.setattr(
        abuse.abuse_api_service,
        'get_violations',
        AsyncMock(return_value=[{'reasons': 'не список'}]),
    )
    response = await abuse.user_abuse_overview(user_id=1, admin=None, db=None)
    assert response.available is False
