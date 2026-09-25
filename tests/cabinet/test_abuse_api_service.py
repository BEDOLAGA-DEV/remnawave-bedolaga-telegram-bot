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
