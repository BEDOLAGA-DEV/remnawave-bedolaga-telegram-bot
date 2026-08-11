"""Быстрые суммы: None = дефолты, [] = отключены админом.

Регрессия: `quick_amounts or DEFAULT_QUICK_AMOUNTS` фолбэчил пустой список в
дефолты, а normalize схлопывал [] в None — выключить кнопки быстрого пополнения
было невозможно в принципе (операторы пытались выдуманными env-переменными).
"""

from app.services.payment_method_config_service import (
    DEFAULT_QUICK_AMOUNTS,
    get_effective_quick_amounts,
    normalize_quick_amounts,
)


def test_normalize_keeps_explicit_empty_list_as_disabled():
    assert normalize_quick_amounts([]) == []


def test_normalize_none_means_defaults():
    assert normalize_quick_amounts(None) is None


def test_normalize_dedup_and_sort_unchanged():
    assert normalize_quick_amounts([50000, 10000, 10000]) == [10000, 50000]


def test_effective_empty_list_disables_buttons():
    assert get_effective_quick_amounts([], 1000, 10000000) == []


def test_effective_none_falls_back_to_defaults():
    expected = [a for a in DEFAULT_QUICK_AMOUNTS if 1000 <= a <= 10000000]
    assert get_effective_quick_amounts(None, 1000, 10000000) == expected


def test_effective_custom_list_still_filtered_by_bounds():
    assert get_effective_quick_amounts([500, 10000, 99999999999], 1000, 10000000) == [10000]
