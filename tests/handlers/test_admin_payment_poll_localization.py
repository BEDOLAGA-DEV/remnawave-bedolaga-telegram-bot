"""Early admin payment/poll errors and labels use the account locale."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PaymentMethod
from app.handlers.admin import payments, polls
from app.localization.texts import get_texts


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_payment_labels_and_test_status_are_localized(language):
    texts = get_texts(language)
    record = SimpleNamespace(method=PaymentMethod.CISPAY, status='paid', payment=SimpleNamespace(is_sandbox=True))
    assert payments._status_info(record, texts=texts) == ('🧪', texts.t('ADMIN_ANORE_SANDBOX'))
    assert payments._format_user_line(None, language) == texts.t('ADMIN_PAYMENT_GUEST_BUYER')
    assert (
        'SBP' in payments._method_display(PaymentMethod.PLATEGA_RECURRENT, language)
        if language in {'en', 'zh', 'fa'}
        else True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_invalid_payment_reference_never_queries_provider(monkeypatch, language):
    query = AsyncMock()
    monkeypatch.setattr(payments, 'get_payment_record', query)
    callback = SimpleNamespace(data='invalid', answer=AsyncMock())
    await inspect.unwrap(payments.manual_check_payment)(callback, SimpleNamespace(language=language), None)
    query.assert_not_awaited()
    callback.answer.assert_awaited_once_with(get_texts(language).t('ADMIN_PAYMENT_INVALID_REFERENCE'), show_alert=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_missing_poll_error_uses_requester_language(monkeypatch, language):
    monkeypatch.setattr(polls, 'get_poll_by_id', AsyncMock(return_value=None))
    callback = SimpleNamespace(data='poll:123', answer=AsyncMock())
    await inspect.unwrap(polls.show_poll_details)(callback, SimpleNamespace(language=language), None)
    callback.answer.assert_awaited_once_with(get_texts(language).t('POLL_NOT_FOUND'), show_alert=True)
