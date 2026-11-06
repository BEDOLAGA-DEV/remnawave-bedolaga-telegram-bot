from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import wheel_service
from app.services.wheel_service import FortuneWheelService


def prize(value, manual=None, index=1):
    return SimpleNamespace(id=index, prize_value_kopeks=value, manual_probability=manual)


def distribution(values, cost=330, rtp=70):
    return FortuneWheelService().calculate_prize_probabilities(SimpleNamespace(id=1, rtp_percent=rtp), values, cost)


@pytest.mark.parametrize('cost', [330, 1000])
@pytest.mark.parametrize('rtp', [0, 10, 30, 70, 100])
@pytest.mark.parametrize('jackpot', [50_000, 500_000])
def test_auto_prize_expected_payout_matches_configured_budget(cost, rtp, jackpot):
    values = [prize(v, index=i) for i, v in enumerate([0, 330, 2500, 10000, jackpot])]
    pairs = distribution(values, cost, rtp)
    assert sum(prob for _, prob in pairs) == pytest.approx(1)
    assert all(0 <= prob <= 1 for _, prob in pairs)
    assert sum(p.prize_value_kopeks * prob for p, prob in pairs) == pytest.approx(cost * rtp / 100)


def test_manual_probability_consumes_expected_payout_before_auto_prizes():
    fixed = prize(2500, 0.02)
    pairs = distribution([fixed, prize(0), prize(330), prize(10000)])
    assert pairs[0] == (fixed, 0.02)
    assert sum(prob for _, prob in pairs) == pytest.approx(1)
    assert sum(p.prize_value_kopeks * prob for p, prob in pairs) == pytest.approx(231)


def test_infeasible_high_target_caps_probability_and_warns(monkeypatch):
    warning = Mock()
    monkeypatch.setattr(wheel_service.logger, 'warning', warning)
    pairs = distribution([prize(0), prize(100)], cost=1000, rtp=70)
    assert sum(prob for _, prob in pairs) == pytest.approx(1)
    assert sum(p.prize_value_kopeks * prob for p, prob in pairs) == pytest.approx(100)
    warning.assert_called()


def test_no_zero_value_sector_warns_instead_of_claiming_target(monkeypatch):
    warning = Mock()
    monkeypatch.setattr(wheel_service.logger, 'warning', warning)
    pairs = distribution([prize(100), prize(200)], cost=1000, rtp=70)
    assert sum(prob for _, prob in pairs) == pytest.approx(1)
    assert all(0 <= prob <= 1 for _, prob in pairs)
    warning.assert_called()


def test_manual_budget_excess_keeps_manual_chance_but_disables_auto_payout(monkeypatch):
    warning = Mock()
    monkeypatch.setattr(wheel_service.logger, 'warning', warning)
    fixed = prize(2500, 0.2)
    pairs = distribution([fixed, prize(0), prize(10000)])
    assert pairs[0] == (fixed, 0.2)
    assert sum(prob for _, prob in pairs) == pytest.approx(1)
    assert sum(p.prize_value_kopeks * prob for p, prob in pairs) == pytest.approx(500)
    warning.assert_called()


def test_multiple_zero_sectors_share_residual_probability():
    pairs = distribution([prize(0, index=1), prize(0, index=2), prize(1000, index=3)], cost=1000)
    assert [prob for _, prob in pairs] == pytest.approx([0.15, 0.15, 0.7])


def test_zero_probability_sector_cannot_win_at_random_zero(monkeypatch):
    paid, nothing = prize(1000), prize(0)
    monkeypatch.setattr(wheel_service.random, 'random', lambda: 0.0)
    assert FortuneWheelService()._select_prize([(paid, 0), (nothing, 1)]) is nothing


@pytest.mark.parametrize('probabilities', [[0.6, 0.6], [0.25, 0.25]])
def test_manual_only_tables_keep_relative_weights_and_report_normalization(monkeypatch, probabilities):
    warning = Mock()
    monkeypatch.setattr(wheel_service.logger, 'warning', warning)
    pairs = distribution([prize(100, probabilities[0]), prize(300, probabilities[1])], cost=1000)
    assert [prob for _, prob in pairs] == pytest.approx([0.5, 0.5])
    warning.assert_called()


def test_valid_manual_override_is_unchanged(monkeypatch):
    warning = Mock()
    monkeypatch.setattr(wheel_service.logger, 'warning', warning)
    pairs = distribution([prize(0, 0.8), prize(1000, 0.2)], cost=1000, rtp=20)
    assert [prob for _, prob in pairs] == pytest.approx([0.8, 0.2])
    warning.assert_not_called()


def test_zero_only_table_reports_unreachable_positive_budget(monkeypatch):
    warning = Mock()
    monkeypatch.setattr(wheel_service.logger, 'warning', warning)
    pairs = distribution([prize(0), prize(0)], cost=1000)
    assert [prob for _, prob in pairs] == pytest.approx([0.5, 0.5])
    warning.assert_called()
