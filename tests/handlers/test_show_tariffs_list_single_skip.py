"""Первая покупка: один доступный тариф — сразу к периоду, без экрана списка."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.handlers.subscription import tariff_purchase as m


def _callback(data: str = 'menu_buy'):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def _state():
    state = MagicMock()
    state.clear = AsyncMock()
    state.update_data = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    return state


def _user():
    user = MagicMock()
    user.language = 'ru'
    user.promo_group_id = None
    user.balance_kopeks = 100_000
    user.get_primary_promo_group = MagicMock(return_value=None)
    return user


def _period_tariff(tariff_id: int = 42):
    tariff = MagicMock()
    tariff.id = tariff_id
    tariff.name = 'Единственный'
    tariff.is_active = True
    tariff.is_daily = False
    tariff.can_purchase_custom_days = MagicMock(return_value=False)
    tariff.can_purchase_custom_traffic = MagicMock(return_value=False)
    tariff.period_prices = {'30': 10000}
    tariff.device_limit = 1
    tariff.traffic_limit_gb = 0
    return tariff


async def test_show_tariffs_list_single_tariff_skips_list_and_proceeds(monkeypatch):
    """Один тариф из get_tariffs_for_user → не рисуем список, сразу _proceed с skip_selection."""
    tariff = SimpleNamespace(id=42, name='Единственный')
    proceed = AsyncMock()

    monkeypatch.setattr(m, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))
    monkeypatch.setattr(m, '_proceed_with_selected_tariff', proceed)
    monkeypatch.setattr(m, 'format_tariffs_list_text', MagicMock(return_value='LIST'))
    monkeypatch.setattr(m, 'get_tariffs_keyboard', MagicMock(return_value='KB'))

    callback = _callback()
    await m.show_tariffs_list.__wrapped__(callback, _user(), AsyncMock(), _state())

    proceed.assert_awaited_once()
    assert proceed.await_args.args[4] == 42
    assert proceed.await_args.kwargs.get('skip_selection') is True
    callback.message.edit_text.assert_not_awaited()


async def test_show_tariffs_list_multiple_tariffs_shows_list(monkeypatch):
    """Два и больше тарифов → список как раньше, без авто-перехода."""
    tariffs = [
        SimpleNamespace(id=1, name='A'),
        SimpleNamespace(id=2, name='B'),
    ]
    proceed = AsyncMock()

    monkeypatch.setattr(m, 'get_tariffs_for_user', AsyncMock(return_value=tariffs))
    monkeypatch.setattr(m, '_proceed_with_selected_tariff', proceed)
    monkeypatch.setattr(m, 'format_tariffs_list_text', MagicMock(return_value='LIST TEXT'))
    monkeypatch.setattr(m, 'get_tariffs_keyboard', MagicMock(return_value='LIST KB'))
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)

    callback = _callback()
    await m.show_tariffs_list.__wrapped__(callback, _user(), AsyncMock(), _state())

    proceed.assert_not_awaited()
    callback.message.edit_text.assert_awaited_once()
    assert callback.message.edit_text.await_args.args[0] == 'LIST TEXT'
    assert callback.message.edit_text.await_args.kwargs['reply_markup'] == 'LIST KB'
    callback.answer.assert_awaited_once()


async def test_select_tariff_wrapper_parses_id_and_delegates(monkeypatch):
    """select_tariff остаётся тонкой обёрткой: парсит id и зовёт _proceed без skip_selection."""
    proceed = AsyncMock()
    monkeypatch.setattr(m, '_proceed_with_selected_tariff', proceed)

    callback = _callback('tariff_select:77')
    db_user = _user()
    db = AsyncMock()
    state = _state()

    await m.select_tariff.__wrapped__(callback, db_user, db, state)

    proceed.assert_awaited_once_with(callback, db_user, db, state, 77)


async def test_proceed_skip_selection_uses_back_to_menu(monkeypatch):
    """Схлопнутый выбор: клавиатура периодов с back_callback=back_to_menu."""
    tariff = _period_tariff(42)
    periods_kb = MagicMock(name='periods_kb')

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(m, 'format_tariff_info_for_user', MagicMock(return_value='INFO'))
    monkeypatch.setattr(m, 'get_tariff_periods_keyboard', MagicMock(return_value=periods_kb))

    callback = _callback()
    db_user = _user()
    await m._proceed_with_selected_tariff(callback, db_user, AsyncMock(), _state(), 42, skip_selection=True)

    m.get_tariff_periods_keyboard.assert_called_once()
    assert m.get_tariff_periods_keyboard.call_args.kwargs['back_callback'] == 'back_to_menu'
    assert callback.message.edit_text.await_args.kwargs['reply_markup'] is periods_kb


async def test_proceed_normal_selection_uses_menu_buy_back(monkeypatch):
    """Обычный выбор из списка: клавиатура периодов с back_callback=menu_buy."""
    tariff = _period_tariff(77)
    periods_kb = MagicMock(name='periods_kb')

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(m, 'format_tariff_info_for_user', MagicMock(return_value='INFO'))
    monkeypatch.setattr(m, 'get_tariff_periods_keyboard', MagicMock(return_value=periods_kb))

    callback = _callback('tariff_select:77')
    db_user = _user()
    await m._proceed_with_selected_tariff(callback, db_user, AsyncMock(), _state(), 77)

    m.get_tariff_periods_keyboard.assert_called_once()
    assert m.get_tariff_periods_keyboard.call_args.kwargs['back_callback'] == 'menu_buy'


async def test_back_target_survives_a_redraw_of_the_custom_screen(monkeypatch):
    """«Назад» не должен деградировать при перерисовке конфигуратора.

    Экран списка пропущен, значит «Назад» ведёт в главное меню. Но перерисовка
    происходит в другом хендлере (`handle_custom_days_change`), у которого этого
    контекста нет: без чтения признака из состояния кнопка после первого же
    «+7 дней» превращалась в `menu_buy`, тот снова попадал на пропуск и
    возвращал на ТОТ ЖЕ экран, попутно сбрасывая уже выбранные дни.
    """
    captured = {}

    def _fake_keyboard(**kwargs):
        captured.update(kwargs)
        return 'KB'

    tariff = _period_tariff()
    tariff.can_purchase_custom_days = MagicMock(return_value=True)
    tariff.min_days, tariff.max_days = 7, 365
    tariff.min_traffic_gb, tariff.max_traffic_gb = 1, 1000

    monkeypatch.setattr(m, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(m, 'get_custom_tariff_keyboard', _fake_keyboard)
    monkeypatch.setattr(m, 'format_custom_tariff_preview', AsyncMock(return_value='PREVIEW'))
    # Скидки к сути теста не относятся, а на MagicMock-юзере они лезут в даты.
    monkeypatch.setattr(m, '_get_user_period_discount', MagicMock(return_value=(0, 0, 0)))

    state = _state()
    # Признак, который проставил пропуск списка.
    state.get_data = AsyncMock(return_value={'tariff_selection_skipped': True, 'custom_days': 7, 'custom_traffic': 10})

    callback = _callback('custom_days:42:14')
    await m.handle_custom_days_change.__wrapped__(callback, _user(), AsyncMock(), state)

    assert captured.get('back_callback') == 'back_to_menu', (
        'после перерисовки «Назад» снова ведёт на пропущенный экран — получится петля'
    )


async def test_single_owned_tariff_shows_the_list_instead_of_a_dead_button(monkeypatch):
    """Если покупать нечего, пропуск превращает «Купить» в мёртвую кнопку.

    В мультитарифе `_proceed_with_selected_tariff` на уже активном тарифе только
    показывает всплывающее уведомление и выходит, ничего не перерисовывая. При
    пропуске списка пользователь жмёт «Купить подписку» и остаётся на прежнем
    экране без объяснений. Список в этом случае нужен: тариф в нём помечен.
    """
    tariff = SimpleNamespace(id=42, name='Единственный')
    proceed = AsyncMock()

    monkeypatch.setattr(m, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))
    monkeypatch.setattr(m, '_proceed_with_selected_tariff', proceed)
    monkeypatch.setattr(m, 'format_tariffs_list_text', MagicMock(return_value='LIST TEXT'))
    monkeypatch.setattr(m, 'get_tariffs_keyboard', MagicMock(return_value='LIST KB'))
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)

    import app.database.crud.subscription as sub_crud

    owned = SimpleNamespace(tariff_id=42, is_trial=False)
    monkeypatch.setattr(sub_crud, 'get_active_subscriptions_by_user_id', AsyncMock(return_value=[owned]))

    callback = _callback()
    await m.show_tariffs_list.__wrapped__(callback, _user(), AsyncMock(), _state())

    proceed.assert_not_awaited()
    callback.message.edit_text.assert_awaited_once(), 'пользователю должен показаться список, а не пустой экран'
