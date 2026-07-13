"""Source-пины интеграции rich-меню в главное меню.

Пинят инварианты:
1. show_main_menu и handle_back_to_menu пробуют rich-рендер ПЕРЕД классическим
   edit_or_answer_photo, а классический текст строится только в фоллбеке.
2. Все точки показа главного меню в start.py защищены rich-хелперами
   (try_answer_rich_main_menu / try_send_rich_main_menu).
3. Rich-билдер одиночного режима переиспользует _get_subscription_status из
   menu.py — формулировки статусов не форкаются (см.
   tests/test_start_menu_text_consistency.py про единый источник правды).
"""

import inspect
from pathlib import Path

import app.handlers.menu as menu_mod
import app.utils.rich_menu as rich_menu_mod


_START_PATH = Path(__file__).resolve().parents[1] / 'app' / 'handlers' / 'start.py'


def test_show_main_menu_tries_rich_before_classic():
    source = inspect.getsource(menu_mod.show_main_menu)

    rich_call = source.index('try_edit_rich_main_menu')
    classic_call = source.index('edit_or_answer_photo')
    assert rich_call < classic_call

    # Классический текст меню строится только в фоллбек-ветке
    assert 'if not await try_edit_rich_main_menu' in source
    guard = source.index('if not await try_edit_rich_main_menu')
    menu_text_build = source.index('menu_text = await get_main_menu_text')
    assert guard < menu_text_build


def test_back_to_menu_tries_rich_before_classic():
    source = inspect.getsource(menu_mod.handle_back_to_menu)

    rich_call = source.index('try_edit_rich_main_menu')
    classic_call = source.index('edit_or_answer_photo')
    assert rich_call < classic_call
    assert 'if not await try_edit_rich_main_menu' in source


def test_start_menu_sites_guarded_by_rich_helpers():
    source = _START_PATH.read_text(encoding='utf-8')

    # 5 мест показа меню через message.answer + 2 прямых bot.send_* в
    # required_sub_channel_check. Новая точка показа меню обязана либо войти в
    # этот счётчик (добавь rich-гвард), либо осознанно обновить пин.
    assert source.count('if not await try_answer_rich_main_menu(') == 5
    assert source.count('if not await try_send_rich_main_menu(') == 2

    # Каждый классический показ меню строит текст только внутри фоллбека:
    # 'menu_text = await get_main_menu_text' встречается сразу после rich-гварда
    # (окно в 6 строк переживает переносы длинных guard-строк форматтером).
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if 'menu_text = await get_main_menu_text' not in line:
            continue
        prev_lines = '\n'.join(lines[max(0, index - 6) : index])
        assert 'rich_main_menu' in prev_lines, f'start.py:{index + 1}: построение menu_text не защищено rich-гвардом'


def test_single_subscription_block_reuses_menu_status_builder():
    source = inspect.getsource(rich_menu_mod._build_single_subscription_block)
    assert 'from app.handlers.menu import _get_subscription_status' in source
    assert '_get_subscription_status(user, texts' in source


def test_trial_deeplink_wired_in_start():
    """Диплинк /start trial: ветка сташит pending_trial, drain — рядом с купонным
    (до state.clear() и показа меню), платный триал деплинком не активируется."""
    source = _START_PATH.read_text(encoding='utf-8')

    assert "if start_parameter == 'trial':" in source
    assert 'pending_trial=True' in source

    coupon_drain = source.index('await _redeem_pending_coupon(db, state, user, message.answer)')
    trial_drain = source.index('await _activate_pending_trial(db, state, user, message.answer)')
    assert coupon_drain < trial_drain

    helper = source.index('async def _activate_pending_trial(')
    assert 'is_trial_paid_activation_enabled' in source[helper : helper + 3000]
    assert 'is_trial_already_used' in source[helper : helper + 3000]

    # Подтверждение активации — эфемерное: удаляется отложенной задачей
    confirmation = source.index('MAIN_MENU_RICH_TRIAL_ACTIVATED')
    tail = source[confirmation : confirmation + 800]
    assert '_delete_message_later' in tail
    assert 'delay=30' in tail
