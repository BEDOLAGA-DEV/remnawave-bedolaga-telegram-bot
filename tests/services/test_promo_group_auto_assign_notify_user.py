"""Клиент узнаёт об автоназначении промогруппы за траты (issue #3271, часть 1).

До этого уведомление получал только админ; email-пользователи не узнавали о
новом уровне вообще никак.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes.admin_email_templates import SAMPLE_CONTEXTS, TEMPLATE_TYPES
from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.config import settings
from app.database.models import PromoGroup, User, UserPromoGroup, UserStatus
from app.localization.texts import get_texts
from app.services import promo_group_assignment, promo_group_notifications
from app.services.notification_types import NotificationType
from app.services.promo_group_notifications import (
    collect_promo_group_discounts,
    notify_user_about_auto_assignment,
)
from tests.fixtures.sqlite_memory import memory_session


def _group(**changes) -> PromoGroup:
    values = {
        'id': 5,
        'name': 'Продвинутый <VIP>',
        'server_discount_percent': 10,
        'traffic_discount_percent': 0,
        'device_discount_percent': 5,
        'period_discounts': {'180': 15, '90': 10, 'junk': 50},
        'is_default': False,
    }
    values.update(changes)
    return PromoGroup(**values)


def _user(**changes) -> SimpleNamespace:
    values = {'id': 1, 'telegram_id': 111, 'language': 'ru', 'email': None, 'email_verified': False}
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def delivery(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from app.services.notification_delivery_service import notification_delivery_service

    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notification_delivery_service, 'send_notification', send)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr('app.bot_factory.create_bot', lambda token: bot)
    monkeypatch.setattr(settings, 'BOT_TOKEN', '1:token', raising=False)
    monkeypatch.setattr(settings, 'PROMO_GROUP_AUTO_ASSIGN_NOTIFY_USER', True, raising=False)
    send.bot = bot
    return send


def test_discounts_skip_zero_and_junk_periods_and_sort() -> None:
    discounts = collect_promo_group_discounts(_group())

    assert discounts.server_percent == 10
    assert discounts.traffic_percent == 0
    assert discounts.device_percent == 5
    assert discounts.periods == ((90, 10), (180, 15))
    assert not discounts.is_empty
    assert collect_promo_group_discounts(
        _group(server_discount_percent=0, device_discount_percent=0, period_discounts={})
    ).is_empty


@pytest.mark.asyncio
async def test_telegram_user_gets_escaped_message_with_discounts(delivery: AsyncMock) -> None:
    await notify_user_about_auto_assignment(_user(), _group(), 500_000)

    kwargs = delivery.await_args.kwargs
    assert kwargs['notification_type'] is NotificationType.PROMO_GROUP_AUTO_ASSIGNED
    assert kwargs['bot'] is delivery.bot
    message = kwargs['telegram_message']
    assert 'Продвинутый &lt;VIP&gt;' in message
    assert settings.format_price(500_000) in message
    texts = get_texts('ru')
    assert texts.PROMO_GROUP_DISCOUNT_SERVERS.format(percent=10) in message
    assert texts.PROMO_GROUP_DISCOUNT_DEVICES.format(percent=5) in message
    assert texts.PROMO_GROUP_DISCOUNT_TRAFFIC.format(percent=0) not in message
    # Письму — сырые значения, экранирует шаблон.
    context = kwargs['context']
    assert context['group_name'] == 'Продвинутый <VIP>'
    assert context['server_discount'] == 10
    assert '10%' in context['period_discounts'] and '15%' in context['period_discounts']
    delivery.bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_email_only_user_goes_through_router_without_bot(delivery: AsyncMock) -> None:
    await notify_user_about_auto_assignment(
        _user(telegram_id=None, email='a@b.c', email_verified=True), _group(), 100_000
    )

    assert delivery.await_args.kwargs['bot'] is None
    assert delivery.await_args.kwargs['notification_type'] is NotificationType.PROMO_GROUP_AUTO_ASSIGNED


@pytest.mark.asyncio
async def test_group_without_discounts_uses_neutral_text(delivery: AsyncMock) -> None:
    group = _group(server_discount_percent=0, device_discount_percent=0, period_discounts=None)

    await notify_user_about_auto_assignment(_user(), group, 100_000)

    message = delivery.await_args.kwargs['telegram_message']
    expected = (
        get_texts('ru')
        .get('PROMO_GROUP_AUTO_ASSIGNED_NO_DISCOUNTS')
        .format(group_name='Продвинутый &lt;VIP&gt;', total_spent=settings.format_price(100_000), discounts='')
    )
    assert message == expected


@pytest.mark.asyncio
async def test_switch_off_sends_nothing_and_failures_never_raise(
    delivery: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, 'PROMO_GROUP_AUTO_ASSIGN_NOTIFY_USER', False, raising=False)
    await notify_user_about_auto_assignment(_user(), _group(), 1)
    delivery.assert_not_awaited()

    monkeypatch.setattr(settings, 'PROMO_GROUP_AUTO_ASSIGN_NOTIFY_USER', True, raising=False)
    delivery.side_effect = RuntimeError('boom')
    await notify_user_about_auto_assignment(_user(), _group(), 1)


@pytest.mark.parametrize('language', ['ru', 'en', 'zh', 'ua', 'fa'])
def test_locale_keys_exist_and_format(language: str) -> None:
    texts = get_texts(language)
    for key in ('PROMO_GROUP_AUTO_ASSIGNED', 'PROMO_GROUP_AUTO_ASSIGNED_NO_DISCOUNTS'):
        template = texts.get(key)
        assert template, (language, key)
        rendered = template.format(group_name='G', total_spent='1 ₽', discounts='D')
        assert 'G' in rendered and '1 ₽' in rendered


@pytest.mark.parametrize('language', ['ru', 'en', 'zh', 'ua'])
def test_email_template_escapes_and_lists_discounts(language: str) -> None:
    context = {
        'group_name': 'Про <script>',
        'total_spent': '5 000 ₽',
        'period_discounts': '90 дней — 10%',
        'server_discount': 10,
        'traffic_discount': 0,
        'device_discount': 5,
    }
    rendered = EmailNotificationTemplates().get_template(NotificationType.PROMO_GROUP_AUTO_ASSIGNED, language, context)

    assert '<script>' not in rendered['subject'] + rendered['body_html']
    assert 'Про &lt;script&gt;' in rendered['body_html']
    assert '10%' in rendered['body_html'] and '5%' in rendered['body_html']
    assert '<ul>' in rendered['body_html']

    empty = EmailNotificationTemplates().get_template(
        NotificationType.PROMO_GROUP_AUTO_ASSIGNED,
        language,
        {**context, 'server_discount': 0, 'device_discount': 0, 'period_discounts': ''},
    )
    assert '<ul>' not in empty['body_html']


def test_email_type_is_editable_in_cabinet() -> None:
    entry = next(item for item in TEMPLATE_TYPES if item['type'] == 'promo_group_auto_assigned')
    assert set(entry['context_vars']) == set(SAMPLE_CONTEXTS['promo_group_auto_assigned'])


TABLES = (User.__table__, PromoGroup.__table__, UserPromoGroup.__table__)


async def _seed(db) -> tuple[int, int]:
    group = PromoGroup(
        name='Продвинутый',
        server_discount_percent=10,
        traffic_discount_percent=0,
        device_discount_percent=0,
        auto_assign_total_spent_kopeks=100_000,
    )
    user = User(telegram_id=222, status=UserStatus.ACTIVE.value, language='ru', balance_kopeks=0)
    db.add_all([group, user])
    await db.commit()
    return user.id, group.id


@pytest.mark.asyncio
@pytest.mark.parametrize(('kwargs', 'expected_calls'), [({}, 1), ({'notify_admins': False, 'notify_user': False}, 0)])
async def test_assignment_notifies_user_once_and_recalculation_stays_silent(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict, expected_calls: int
) -> None:
    from app.database.models import ServerSquad, server_squad_promo_groups

    notify = AsyncMock()
    monkeypatch.setattr(promo_group_assignment, 'notify_user_about_auto_assignment', notify)
    monkeypatch.setattr(promo_group_assignment, '_notify_admins_about_auto_assignment', AsyncMock())
    monkeypatch.setattr(promo_group_assignment, 'get_user_total_spent_kopeks', AsyncMock(return_value=150_000))

    async def _no_lock(db, user):
        return user

    monkeypatch.setattr(promo_group_assignment, 'lock_user_for_update', _no_lock)

    async with memory_session(monkeypatch, (*TABLES, ServerSquad.__table__, server_squad_promo_groups)) as db:
        user_id, group_id = await _seed(db)

        assigned = await promo_group_assignment.maybe_assign_promo_group_by_total_spent(db, user_id, **kwargs)
        assert assigned is not None and assigned.id == group_id
        # Повторный вызов группу не выдаёт заново — и второго сообщения нет.
        await promo_group_assignment.maybe_assign_promo_group_by_total_spent(db, user_id, **kwargs)

    assert notify.await_count == expected_calls
    if expected_calls:
        _, group, spent = notify.await_args.args
        assert group.id == group_id and spent == 150_000


def test_recalculation_passes_notify_user_false() -> None:
    import inspect

    from app.services import promo_group_recalculation

    assert 'notify_user=False' in inspect.getsource(promo_group_recalculation)
    assert promo_group_notifications.notify_user_about_auto_assignment is notify_user_about_auto_assignment
