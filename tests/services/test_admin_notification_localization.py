"""Admin notifications use the recipient locale and isolate concurrent render contexts."""

import asyncio
import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings, settings
from app.localization.texts import get_texts
from app.services import admin_notification_service as module
from app.services.admin_notification_service import AdminNotificationService, NotificationCategory


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']


def _service(monkeypatch, chat_id=-100123):
    service = AdminNotificationService(SimpleNamespace(send_message=AsyncMock()))
    service.enabled = True
    service.chat_id = chat_id
    service.topic_id = None
    service.ticket_topic_id = None
    service.category_topics = dict.fromkeys(NotificationCategory)
    monkeypatch.setattr(module, 'try_send_rich_admin_message', AsyncMock(return_value=False))
    return service


def _user(language='ru', name='Example'):
    return SimpleNamespace(
        id=7,
        telegram_id=456,
        language=language,
        full_name=name,
        first_name=name,
        last_name=None,
        username=None,
        email=None,
        balance_kopeks=50000,
    )


def _assert_language(text, language):
    assert 'ADMIN_NOTIFY_' not in text
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', text.replace('Оператор', '')) is None, text


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_shared_topic_uses_project_locale_including_withdrawal_buttons(monkeypatch, language):
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', language)
    session = MagicMock(side_effect=AssertionError('Shared topics must not query a recipient account'))
    monkeypatch.setattr(module, 'AsyncSessionLocal', session)
    service = _service(monkeypatch)
    service.category_topics[NotificationCategory.PARTNERS] = 42
    user = _user(language='ru' if language != 'ru' else 'fa')
    assert await service.send_withdrawal_request_notification(user, 12345, 'Оператор <details>', request_id=91)
    call = service.bot.send_message.await_args.kwargs
    assert call['chat_id'] == -100123
    assert call['message_thread_id'] == 42
    assert 'Оператор &lt;details&gt;' in call['text']
    _assert_language(call['text'], language)
    buttons = [b for row in call['reply_markup'].inline_keyboard for b in row]
    assert buttons
    _assert_language(' '.join(b.text for b in buttons), language)
    assert all('91' in b.callback_data for b in buttons)
    session.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_personal_events_keep_separate_recipient_contexts(monkeypatch):
    monkeypatch.setattr(Settings, 'is_admin', lambda self, value: value == 123)
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'ru')
    service = _service(monkeypatch, chat_id=123)
    session = MagicMock(side_effect=AssertionError('Existing caller sessions must be reused'))
    monkeypatch.setattr(module, 'AsyncSessionLocal', session)
    db_en = SimpleNamespace(scalar=AsyncMock(return_value='en'), no_autoflush=nullcontext())
    db_fa = SimpleNamespace(scalar=AsyncMock(return_value='fa'), no_autoflush=nullcontext())
    started = 0
    both_started = asyncio.Event()

    async def record(*args, **kwargs):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        return True

    monkeypatch.setattr(service, '_record_subscription_event', record)
    monkeypatch.setattr(service, '_get_user_promo_group', AsyncMock(return_value=None))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    subscription = SimpleNamespace(
        id=8, end_date=now + timedelta(days=30), traffic_limit_gb=10, device_limit=2, connected_squads=[]
    )
    transaction = SimpleNamespace(id=9, amount_kopeks=12345, completed_at=now, created_at=now, payment_method='balance')
    results = await asyncio.gather(
        service.send_subscription_extension_notification(
            db_en, _user('zh', 'English event'), subscription, transaction, 30, now
        ),
        service.send_subscription_extension_notification(
            db_fa, _user('en', 'Persian event'), subscription, transaction, 30, now
        ),
    )
    assert results == [True, True]
    messages = [call.kwargs['text'] for call in service.bot.send_message.await_args_list]
    english = next(text for text in messages if 'English event' in text)
    persian = next(text for text in messages if 'Persian event' in text)
    assert '<b>SUBSCRIPTION RENEWAL</b>' in english
    assert '<b>تمدید اشتراک</b>' in persian
    assert get_texts('en').t('ADMIN_NOTIFY_025') in english
    assert get_texts('fa').t('ADMIN_NOTIFY_025') in persian
    for db in [db_en, db_fa]:
        db.scalar.assert_awaited_once()
        assert list(db.scalar.await_args.args[0].compile().params.values()) == [123]
    session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('private', [True, False])
async def test_missing_recipient_account_falls_back_without_using_affected_user(monkeypatch, private):
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'fa')
    monkeypatch.setattr(Settings, 'is_admin', lambda self, value: private)
    service = _service(monkeypatch, chat_id=123)
    db = SimpleNamespace(scalar=AsyncMock(return_value=None), no_autoflush=nullcontext())
    texts = await service._notification_texts(db)
    assert texts.language == 'fa'
    assert db.scalar.await_count == int(private)


@pytest.mark.parametrize('language', LANGUAGES)
def test_presentation_helpers_translate_labels_and_keep_operator_data(monkeypatch, language):
    service = _service(monkeypatch)
    texts = get_texts(language)
    group = SimpleNamespace(
        name='Оператор <group>',
        server_discount_percent=10,
        traffic_discount_percent=20,
        device_discount_percent=0,
        period_discounts={'30': 15},
        apply_discounts_to_addons=True,
    )
    output = service._format_promo_group_block(group, texts=texts)
    assert 'Оператор &lt;group&gt;' in output
    assert '-10%' in output and '-15%' in output
    _assert_language(output, language)
    for value in [None, 'balance', 'yookassa', 'manual', 'custom_provider']:
        _assert_language(service._get_payment_method_display(value, texts=texts), language)
    assert module._format_waiting(1501, texts=texts)
    assert '1' in module._format_waiting(1501, texts=texts)
    user = _user()
    transaction = SimpleNamespace(
        payment_method='balance',
        amount_kopeks=12345,
        id=9,
        external_id='masked-1',
        description='Оператор <receipt>',
        created_at=None,
        completed_at=None,
        receipt_uuid=None,
    )
    output = service._build_balance_topup_message(
        user,
        transaction,
        37655,
        topup_status='🆕 Первое пополнение',
        referrer_info='Нет',
        subscription=None,
        promo_group=None,
        texts=texts,
    )
    _assert_language(output, language)
    assert 'Оператор &lt;receipt&gt;' in output
    assert get_texts(language).t('ADMIN_NOTIFY_FIRST_TOPUP_ICON') in output
    custom = service._build_balance_topup_message(
        user,
        transaction,
        37655,
        topup_status='Оператор custom',
        referrer_info='',
        subscription=None,
        promo_group=None,
        texts=texts,
    )
    assert 'Оператор custom' in custom


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['en', 'fa'])
async def test_infrastructure_and_raw_admin_messages_keep_routing_and_payload(monkeypatch, language):
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', language)
    service = _service(monkeypatch)
    assert await service.send_remnawave_panel_status_notification('online', {'response_time': 0.125})
    _assert_language(service.bot.send_message.await_args.kwargs['text'], language)
    raw = 'Оператор <b>unchanged</b>'
    assert await service.send_admin_notification(raw)
    assert service.bot.send_message.await_args.kwargs['text'] == raw


@pytest.mark.asyncio
async def test_purchase_event_metadata_stays_original_while_message_is_localized(monkeypatch):
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'fa')
    service = _service(monkeypatch)
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_record_subscription_event', record)
    monkeypatch.setattr(service, '_get_tariff_name', AsyncMock(return_value=None))
    user = _user('en')
    user.has_had_paid_subscription = False
    user.referred_by_id = None
    now = datetime(2026, 1, 1, tzinfo=UTC)
    subscription = SimpleNamespace(id=8, end_date=now, connected_squads=[], traffic_limit_gb=10, device_limit=2)
    transaction = SimpleNamespace(
        id=9, amount_kopeks=-12345, payment_method='yookassa', completed_at=now, created_at=now
    )
    assert await service.send_subscription_purchase_notification(object(), user, subscription, transaction, 30)
    assert record.await_args.kwargs['extra']['payment_method'] == '💳 YooKassa (карта)'
    assert record.await_args.kwargs['amount_kopeks'] == 12345
    message = service.bot.send_message.await_args.kwargs['text']
    assert get_texts('fa').t('ADMIN_NOTIFY_026') in message
    _assert_language(message, 'fa')


@pytest.mark.asyncio
async def test_disabled_admin_notifications_do_not_lookup_recipient(monkeypatch):
    service = _service(monkeypatch, chat_id=123)
    service.enabled = False
    monkeypatch.setattr(Settings, 'is_admin', lambda self, value: True)
    db = SimpleNamespace(scalar=AsyncMock())
    await service._notification_texts(db)
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('lookup_fails', [False, True])
async def test_recipient_locale_lookup_suppresses_only_its_own_autoflush(monkeypatch, lookup_fails):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import User

    monkeypatch.setattr(Settings, 'is_admin', lambda self, value: value == 123)
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'fa')
    service = _service(monkeypatch, chat_id=123)
    async with AsyncSession() as db:
        pending_user = User(id=99, telegram_id=999, first_name='Example', language='ru')
        db.add(pending_user)
        observed = []

        async def scalar(query):
            observed.append(db.autoflush)
            assert pending_user in db.new
            if lookup_fails:
                raise RuntimeError('Synthetic lookup failure')
            return 'en'

        monkeypatch.setattr(db, 'scalar', AsyncMock(side_effect=scalar))
        assert db.autoflush
        texts = await service._notification_texts(db)
        assert observed == [False]
        assert db.autoflush
        assert pending_user in db.new
        assert texts.language == ('fa' if lookup_fails else 'en')
