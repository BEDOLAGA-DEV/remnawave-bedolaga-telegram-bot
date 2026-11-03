"""Reports preserve financial values, escaping and transport while selecting a locale."""

import inspect
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from app.config import settings
from app.handlers.admin import reports
from app.localization.texts import get_texts
from app.services import reporting_service as module
from app.services.reporting_service import ReportingService, ReportingServiceError, ReportPeriod


@pytest.fixture
def wired_report(monkeypatch):
    service = ReportingService()
    monkeypatch.setattr(module, 'AsyncSessionLocal', lambda: AsyncMock())
    monkeypatch.setattr(
        service,
        '_collect_current_totals',
        AsyncMock(return_value={'active_trials': 7, 'active_paid': 8, 'open_tickets': 9}),
    )
    stats = {
        'new_users': 10,
        'new_trials': 3,
        'trial_to_paid_conversions': 1,
        'new_paid_subscriptions': 2,
        'deposits_amount': 123456,
        'subscription_payments_count': 4,
        'subscription_payments_amount': 98765,
        'deposits_count': 5,
        'new_tickets': 6,
    }
    monkeypatch.setattr(service, '_collect_period_stats', AsyncMock(return_value=stats))
    monkeypatch.setattr(
        service, '_get_top_referrers', AsyncMock(return_value=[{'referrer_label': '<name>&', 'count': 11}])
    )
    monkeypatch.setattr(
        service, '_get_user_usage_stats', AsyncMock(return_value={'active_paid_users': 12, 'never_connected_users': 13})
    )
    return service, stats


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('period', [ReportPeriod.DAILY, ReportPeriod.WEEKLY])
@pytest.mark.asyncio
async def test_report_keeps_money_counts_conversion_and_name_escaping(wired_report, language, period):
    service, _ = wired_report
    text = await service.send_report(period, report_date=date(2030, 1, 8), language=language)
    texts = get_texts(language)
    assert texts.t('ADMIN_REPORT_UI_009').format(value1=1, value2=100 / 3) in text
    assert texts.t('ADMIN_REPORT_UI_016').format(value1=4, value2='987.65 ₽') in text
    assert texts.t('ADMIN_REPORT_UI_017').format(value1=5, value2='1 234.56 ₽') in text
    assert texts.t('ADMIN_REPORT_UI_011').format(value1='1 234.56 ₽') in text
    assert texts.t('ADMIN_REPORT_UI_031').format(value1=1, value2='&lt;name&gt;&amp;', value3=11) in text
    assert '<name>' not in text
    assert '&amp;lt;' not in text
    label = '08.01.2030' if period == ReportPeriod.DAILY else '01.01.2030 - 07.01.2030'
    key = 'ADMIN_REPORT_UI_004' if period == ReportPeriod.DAILY else 'ADMIN_REPORT_UI_005'
    assert text.startswith(texts.t(key).format(value1=label))


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_automatic_report_uses_default_language_and_same_delivered_payload(monkeypatch, wired_report, language):
    service, stats = wired_report
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', language)
    stats['new_trials'] = 0
    stats['trial_to_paid_conversions'] = 0
    service._get_top_referrers.return_value = []
    delivery = AsyncMock()
    monkeypatch.setattr(service, '_deliver_report', delivery)
    text = await service.send_report(ReportPeriod.DAILY, report_date=date(2030, 1, 8), send_to_topic=True)
    texts = get_texts(language)
    assert texts.t('ADMIN_REPORT_UI_009').format(value1=0, value2=0.0) in text
    assert texts.t('ADMIN_REPORT_UI_028') in text
    delivery.assert_awaited_once()
    assert delivery.await_args.args == (text,)
    assert delivery.await_args.kwargs['texts'].language == language


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_rich_report_footer_keeps_destination(monkeypatch, language):
    from app.utils import rich_admin

    service = ReportingService()
    service.set_bot(SimpleNamespace(send_message=AsyncMock()))
    monkeypatch.setattr(type(settings), 'get_reports_chat_id', lambda _: -123)
    monkeypatch.setattr(type(settings), 'get_reports_topic_id', lambda _: 17)
    render = Mock(return_value='<b>rendered</b>')
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(rich_admin, 'classic_admin_html_to_rich', render)
    monkeypatch.setattr(rich_admin, 'try_send_rich_admin_message', deliver)
    await service._deliver_report('body', texts=get_texts(language))
    render.assert_called_once_with('body', footer_label=get_texts(language).t('ADMIN_REPORT_UI_029'))
    deliver.assert_awaited_once_with(service.bot, -123, '<b>rendered</b>', thread_id=17)
    service.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('failure', ['missing_bot', 'missing_chat', 'forbidden'])
@pytest.mark.asyncio
async def test_report_delivery_errors_are_localized_and_fail_closed(monkeypatch, language, failure):
    from app.utils import rich_admin

    service = ReportingService()
    send = AsyncMock(
        side_effect=TelegramForbiddenError(method=SendMessage(chat_id=7, text='report'), message='blocked')
    )
    if failure != 'missing_bot':
        service.set_bot(SimpleNamespace(send_message=send))
    monkeypatch.setattr(type(settings), 'get_reports_chat_id', lambda _: None if failure == 'missing_chat' else -123)
    monkeypatch.setattr(type(settings), 'get_reports_topic_id', lambda _: 17)
    monkeypatch.setattr(rich_admin, 'try_send_rich_admin_message', AsyncMock(return_value=False))
    with pytest.raises(ReportingServiceError) as exc:
        await service._deliver_report('body', texts=get_texts(language))
    key = {'missing_bot': '026', 'missing_chat': '027', 'forbidden': '030'}[failure]
    assert str(exc.value) == get_texts(language).t(f'ADMIN_REPORT_UI_{key}')
    if failure == 'forbidden':
        send.assert_awaited_once_with(chat_id=-123, text='body', message_thread_id=17, parse_mode='HTML')
    else:
        send.assert_not_awaited()


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_manual_report_passes_caller_language(monkeypatch, language):
    send = AsyncMock(return_value='prepared report')
    monkeypatch.setattr(reports.reporting_service, 'send_report', send)
    callback = SimpleNamespace(message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
    await inspect.unwrap(reports.send_daily_report)(callback, SimpleNamespace(language=language), None)
    send.assert_awaited_once_with(ReportPeriod.DAILY, send_to_topic=True, language=language)
    assert callback.message.answer.await_args.args == ('prepared report',)
    callback.answer.assert_awaited_once_with(get_texts(language).t('ADMIN_REPORT_UI_001'))


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_user_fallback_is_localized_without_changing_real_names(language):
    service = ReportingService()
    texts = get_texts(language)
    assert service._user_label(SimpleNamespace(id=7), texts=texts) == texts.t('ADMIN_REPORT_UI_003').format(value1=7)
    assert service._user_label(SimpleNamespace(username='name'), texts=texts) == '@name'
    assert service._user_label(SimpleNamespace(first_name='<name>', last_name='&'), texts=texts) == '<name> &'


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_invalid_period_fails_before_collecting_data(monkeypatch, language):
    service = ReportingService()
    session_factory = Mock()
    monkeypatch.setattr(module, 'AsyncSessionLocal', session_factory)
    with pytest.raises(ReportingServiceError) as exc:
        await service.send_report('invalid', language=language)
    assert str(exc.value) == get_texts(language).t('ADMIN_REPORT_UI_032').format(value1='invalid')
    session_factory.assert_not_called()
