from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.payment_method_config_service import (
    DEFAULT_QUICK_AMOUNTS,
    get_effective_quick_amounts,
    normalize_quick_amounts,
)


def test_normalize_none_returns_none():
    assert normalize_quick_amounts(None) is None


def test_normalize_sorts_and_dedupes():
    assert normalize_quick_amounts([50000, 10000, 50000, 30000]) == [10000, 30000, 50000]


def test_normalize_empty_list_returns_none():
    assert normalize_quick_amounts([]) is None


def test_normalize_rejects_non_list():
    with pytest.raises(ValueError):
        normalize_quick_amounts('10000')


def test_normalize_rejects_non_int_items():
    with pytest.raises(ValueError):
        normalize_quick_amounts([10000, '30000'])
    with pytest.raises(ValueError):
        normalize_quick_amounts([True])


def test_normalize_rejects_non_positive_items():
    with pytest.raises(ValueError):
        normalize_quick_amounts([0])
    with pytest.raises(ValueError):
        normalize_quick_amounts([-100])


def test_normalize_rejects_more_than_ten_items():
    with pytest.raises(ValueError):
        normalize_quick_amounts(list(range(100, 1200, 100)))


def test_normalize_caps_after_dedupe():
    assert normalize_quick_amounts([100] * 11 + [200]) == [100, 200]


def test_effective_returns_defaults_when_not_configured():
    assert get_effective_quick_amounts(None, 0, 10000000) == DEFAULT_QUICK_AMOUNTS


def test_effective_filters_by_min_max():
    assert get_effective_quick_amounts(None, 20000, 60000) == [30000, 50000]
    assert get_effective_quick_amounts([5000, 70000, 200000], 10000, 100000) == [70000]


def test_effective_returns_empty_when_all_filtered_out():
    assert get_effective_quick_amounts([5000], 10000, 100000) == []
