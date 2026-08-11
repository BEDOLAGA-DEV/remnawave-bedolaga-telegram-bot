"""device_limit = 0 означает «без ограничения», а не «ноль устройств».

При выключенном HWID RemnaWave отдаёт 0, и resolve_hwid_device_limit сам
трактует <= 0 как «лимит в панель не слать». Пользователю это показывалось
сырым нулём («Устройства: 3 / 0» — читается как «устройств не осталось»),
а местами строка с нулём просто пряталась.
"""

import pytest

from app.localization.texts import Texts


@pytest.mark.parametrize('limit', [0, None, -1])
def test_absent_limit_renders_as_infinity(limit):
    assert Texts.format_device_limit(limit) == '∞'


@pytest.mark.parametrize('limit', [1, 3, 10, 999])
def test_real_limit_renders_as_number(limit):
    assert Texts.format_device_limit(limit) == str(limit)


def test_traffic_and_device_limits_agree_on_unlimited():
    """Трафик уже показывал безлимит бесконечностью — устройства не должны отставать."""
    assert '∞' in Texts.format_traffic(0, is_limit=True)
    assert '∞' in Texts.format_device_limit(0)


def test_zero_used_devices_is_not_confused_with_zero_limit():
    """Форматтер — только для ЛИМИТА: счётчик использованных нулём и остаётся."""
    # ноль в числителе («использовано») печатается как есть, помощник его не трогает
    used, limit = 0, 0
    assert f'{used} / {Texts.format_device_limit(limit)}' == '0 / ∞'
