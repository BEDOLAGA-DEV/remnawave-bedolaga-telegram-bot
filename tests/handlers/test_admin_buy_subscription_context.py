from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.database.crud import subscription as subscription_crud, tariff as tariff_crud, user as user_crud
from app.handlers.admin import users


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(type(settings), 'get_available_subscription_periods', lambda self: [30, 60])
    selected = SimpleNamespace(id=22, tariff_id=None, traffic_limit_gb=100, device_limit=3, connected_squads=[])
    default = SimpleNamespace(id=11, tariff_id=None, traffic_limit_gb=50, device_limit=1, connected_squads=[])
    user = SimpleNamespace(id=7, telegram_id=7, email=None, full_name='Test', balance_kopeks=10000)
    monkeypatch.setattr(
        users.UserService, 'get_user_profile', AsyncMock(return_value={'user': user, 'subscription': default})
    )
    owner_lookup = AsyncMock(return_value=selected)
    monkeypatch.setattr(subscription_crud, 'get_subscription_by_id_for_user', owner_lookup)
    price = AsyncMock(return_value=1000)
    monkeypatch.setattr(users, '_calculate_subscription_period_price', price)
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
    debit = AsyncMock(return_value=False)
    monkeypatch.setattr(user_crud, 'subtract_user_balance', debit)
    callback = SimpleNamespace(data='', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    return SimpleNamespace(
        user=user,
        selected=selected,
        default=default,
        lookup=owner_lookup,
        price=price,
        debit=debit,
        callback=callback,
        db=AsyncMock(),
    )


async def invoke(case, stage, suffix='_s22', period=30):
    data = {
        'select': f'admin_sub_buy_7{suffix}',
        'confirm': f'admin_buy_sub_confirm_7_{period}_1000{suffix}',
        'execute': f'admin_buy_sub_execute_7_{period}_1000{suffix}',
    }
    handlers = {
        'select': users.admin_buy_subscription,
        'confirm': users.admin_buy_subscription_confirm,
        'execute': users.admin_buy_subscription_execute,
    }
    case.callback.data = data[stage]
    case.db.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=case.selected if suffix else case.default)
    )
    handler = handlers[stage]
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    await handler(case.callback, case.user, case.db)


@pytest.mark.parametrize('stage', ['select', 'confirm', 'execute'])
async def test_explicit_subscription_retained(case, stage):
    await invoke(case, stage)
    assert all(call.args[2] is case.selected for call in case.price.await_args_list)
    case.lookup.assert_awaited_once_with(case.db, 22, 7)
    if stage != 'execute':
        markup = case.callback.message.edit_text.await_args.kwargs['reply_markup']
        assert markup.inline_keyboard[0][0].callback_data.endswith('_s22')
        assert '22' in markup.inline_keyboard[-1][0].callback_data


@pytest.mark.parametrize('stage', ['select', 'confirm', 'execute'])
async def test_explicit_missing_or_foreign_never_falls_back(case, stage):
    case.lookup.return_value = None
    await invoke(case, stage)
    case.price.assert_not_awaited()
    case.debit.assert_not_awaited()
    assert case.callback.answer.await_args.kwargs['show_alert']


@pytest.mark.parametrize('stage', ['select', 'confirm', 'execute'])
async def test_legacy_unpinned_uses_profile_subscription(case, stage):
    await invoke(case, stage, suffix='')
    assert all(call.args[2] is case.default for call in case.price.await_args_list)
    case.lookup.assert_not_awaited()


@pytest.mark.parametrize('stage', ['select', 'confirm', 'execute'])
async def test_tariff_mode_requires_tariff_before_pricing(case, monkeypatch, stage):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    await invoke(case, stage)
    case.price.assert_not_awaited()
    case.debit.assert_not_awaited()
    assert 'тариф' in case.callback.answer.await_args.args[0]


async def test_selected_tariff_controls_period_buttons(case, monkeypatch):
    case.selected.tariff_id = 5
    monkeypatch.setattr(
        tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(get_available_periods=lambda: [14, 45]))
    )
    await invoke(case, 'select')
    assert [call.args[3] for call in case.price.await_args_list] == [14, 45]


@pytest.mark.parametrize('stage', ['confirm', 'execute'])
async def test_removed_tariff_period_rejected_before_money(case, monkeypatch, stage):
    case.selected.tariff_id = 5
    monkeypatch.setattr(
        tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(get_available_periods=lambda: [14]))
    )
    await invoke(case, stage)
    case.price.assert_not_awaited()
    case.debit.assert_not_awaited()


@pytest.mark.parametrize('recipient_language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_paid_extension_keeps_price_and_duration_and_uses_recipient_language(
    case, monkeypatch, recipient_language
):
    from datetime import UTC, datetime, timedelta

    from app.database.crud import transaction as transaction_crud
    from app.localization.texts import get_texts

    admin_language = 'fa' if recipient_language != 'fa' else 'en'
    case.user.language = recipient_language
    before = datetime.now(UTC) + timedelta(days=10)
    case.selected.end_date = before
    case.selected.start_date = before - timedelta(days=30)
    case.selected.is_trial = False
    case.selected.is_active = True
    case.selected.tariff = None
    case.selected.remnawave_short_uuid = None
    case.debit.return_value = True
    case.callback.data = 'admin_buy_sub_execute_7_30_1000_s22'
    case.callback.bot = SimpleNamespace(send_message=AsyncMock())
    case.db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=case.selected))
    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: True)
    monkeypatch.setattr(users.SubscriptionService, '_persisted_traffic_mode', AsyncMock(return_value='legacy'))
    monkeypatch.setattr('app.services.cispay_recurrent_guard.require_no_cispay', AsyncMock())
    monkeypatch.setattr('app.services.grace_access_echo.undo_grace_overlay_echo', AsyncMock())
    create_transaction = AsyncMock(return_value=SimpleNamespace(id=1000))
    monkeypatch.setattr(transaction_crud, 'create_transaction', create_transaction)
    monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', AsyncMock())
    # The handler tolerates panel unavailability; this test makes no provider calls.
    monkeypatch.setattr(
        'app.services.remnawave_service.RemnaWaveService',
        lambda: SimpleNamespace(get_api_client=MagicMock(side_effect=RuntimeError('offline test'))),
    )
    handler = users.admin_buy_subscription_execute
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    await handler(case.callback, SimpleNamespace(id=99, language=admin_language), case.db)

    case.debit.assert_awaited_once_with(
        case.db,
        case.user,
        1000,
        'Покупка подписки на 30 дней (администратор)',
        mark_as_paid_subscription=True,
        commit=False,
    )
    assert case.selected.end_date == before + timedelta(days=30)
    assert create_transaction.await_args.kwargs['amount_kopeks'] == 1000
    assert create_transaction.await_args.kwargs['description'] == 'Продление подписки на 30 дней (администратор)'
    recipient = get_texts(recipient_language)
    case.callback.bot.send_message.assert_awaited_once_with(
        chat_id=7,
        text=recipient.t('ADMIN_USERS_PURCHASE_114').format(
            value1=30,
            value2=settings.format_price(1000),
            value3=users.format_datetime(case.selected.end_date),
            value4='',
        ),
        parse_mode='HTML',
    )
    assert (
        get_texts(admin_language).t('ADMIN_USERS_PURCHASE_052').format(value1=30)
        in case.callback.message.edit_text.await_args.args[0]
    )
