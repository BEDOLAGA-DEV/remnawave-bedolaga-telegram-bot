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
