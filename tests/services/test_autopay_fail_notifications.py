"""Tests for the configurable autopay-failure antispam notifier.

Replaces the old hardcoded 6h cooldown (AUTOPAY_INSUFFICIENT_BALANCE_COOLDOWN_SECONDS).
The guarantee under test: with default config the bot sends at most TWO failure
notifications per subscription cycle (first failure + final reminder), then stays
silent — including in the <=2h window after the subscription has expired.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings


def test_config_defaults_present():
    assert settings.AUTOPAY_FAIL_MAX_NOTIFICATIONS == 2
    assert settings.AUTOPAY_FAIL_FINAL_REMINDER_HOURS == 3
    assert settings.AUTOPAY_FAIL_REPEAT_INTERVAL_HOURS == 0


from app.services.monitoring_service import (
    AutopayFailState,
    apply_autopay_fail_notification,
    decide_autopay_fail_notification,
)


DEFAULTS = dict(max_notifications=2, final_reminder_hours=3, repeat_interval_hours=0)


def test_state_dict_roundtrip():
    s = AutopayFailState(count=1, last_sent_ts=123.5, final_sent=True)
    assert AutopayFailState.from_dict(s.to_dict()) == s


def test_state_from_none_is_empty():
    s = AutopayFailState.from_dict(None)
    assert s.count == 0 and s.final_sent is False and s.last_sent_ts == 0.0


def test_first_failure_outside_final_window_returns_first():
    assert decide_autopay_fail_notification(AutopayFailState(), hours_left=40, now_ts=0, **DEFAULTS) == 'first'


def test_silent_between_first_and_final_when_no_repeat():
    state = AutopayFailState(count=1, last_sent_ts=0, final_sent=False)
    assert decide_autopay_fail_notification(state, hours_left=20, now_ts=3600, **DEFAULTS) is None


def test_final_reminder_inside_window():
    state = AutopayFailState(count=1, last_sent_ts=0, final_sent=False)
    assert decide_autopay_fail_notification(state, hours_left=2.5, now_ts=99999, **DEFAULTS) == 'final'


def test_max_cap_blocks_after_two():
    state = AutopayFailState(count=2, last_sent_ts=0, final_sent=True)
    assert decide_autopay_fail_notification(state, hours_left=1, now_ts=99999, **DEFAULTS) is None


def test_post_expiry_blocked_when_cap_reached():
    state = AutopayFailState(count=2, last_sent_ts=0, final_sent=True)
    assert decide_autopay_fail_notification(state, hours_left=-0.5, now_ts=99999, **DEFAULTS) is None


def test_max_zero_disables_all():
    assert (
        decide_autopay_fail_notification(
            AutopayFailState(),
            hours_left=40,
            now_ts=0,
            max_notifications=0,
            final_reminder_hours=3,
            repeat_interval_hours=0,
        )
        is None
    )


def test_late_first_failure_inside_window_sends_final_only():
    # First-ever failure happens already inside the final window → single 'final', not 'first'.
    assert decide_autopay_fail_notification(AutopayFailState(), hours_left=2, now_ts=0, **DEFAULTS) == 'final'


def test_repeat_interval_sends_after_elapsed():
    state = AutopayFailState(count=1, last_sent_ts=0, final_sent=False)
    assert (
        decide_autopay_fail_notification(
            state,
            hours_left=20,
            now_ts=7 * 3600,
            max_notifications=10,
            final_reminder_hours=3,
            repeat_interval_hours=6,
        )
        == 'repeat'
    )


def test_repeat_interval_not_yet_elapsed_stays_silent():
    state = AutopayFailState(count=1, last_sent_ts=0, final_sent=False)
    assert (
        decide_autopay_fail_notification(
            state,
            hours_left=20,
            now_ts=5 * 3600,
            max_notifications=10,
            final_reminder_hours=3,
            repeat_interval_hours=6,
        )
        is None
    )


def test_full_cycle_default_yields_exactly_two_then_silence():
    """Core guarantee: across ticks from window-open through post-expiry, default config
    sends exactly ['first', 'final'] and nothing after — incl. after end_date passes."""
    state = AutopayFailState()
    sent = []
    ticks = [
        (40, 0),
        (30, 36000),
        (10, 108000),
        (4, 129600),
        (3, 133200),
        (2, 136800),
        (1, 140400),
        (-0.5, 145800),
    ]
    for hours_left, now_ts in ticks:
        reason = decide_autopay_fail_notification(state, hours_left=hours_left, now_ts=now_ts, **DEFAULTS)
        if reason is not None:
            sent.append(reason)
            apply_autopay_fail_notification(state, reason, now_ts)
    assert sent == ['first', 'final']
    assert state.count == 2


def test_fresh_cycle_allows_notifications_again():
    """A renewal advances end_date → caller loads a FRESH state for the new cycle_token."""
    fresh = AutopayFailState()
    assert decide_autopay_fail_notification(fresh, hours_left=40, now_ts=200000, **DEFAULTS) == 'first'


async def test_load_save_state_in_memory_roundtrip(monkeypatch):
    """With Redis returning nothing, the in-memory fallback must persist state across
    load/save within the process (single bot process in prod)."""
    from app.services import monitoring_service as ms

    svc = ms.MonitoringService(bot=None)
    monkeypatch.setattr(ms.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(ms.cache, 'set', AsyncMock(return_value=True))

    loaded = await svc._load_autopay_fail_state(subscription_id=7, cycle_token=111)
    assert loaded.count == 0

    apply_autopay_fail_notification(loaded, 'first', now_ts=10.0)
    await svc._save_autopay_fail_state(7, 111, loaded, ttl_seconds=3600)

    again = await svc._load_autopay_fail_state(7, 111)
    assert again.count == 1 and again.final_sent is False


async def test_cleanup_evicts_old_cycles(monkeypatch):
    """In-memory state for cycles whose end_date is >72h in the past must be evicted."""
    from app.services import monitoring_service as ms

    svc = ms.MonitoringService(bot=None)
    now = datetime.now(UTC)
    old_token = int((now - timedelta(hours=100)).timestamp())
    fresh_token = int((now + timedelta(hours=10)).timestamp())
    svc._autopay_fail_state = {
        (1, old_token): AutopayFailState(count=2).to_dict(),
        (2, fresh_token): AutopayFailState(count=1).to_dict(),
    }
    # Force the cleanup's time-gate open.
    svc._last_cleanup = now - timedelta(hours=2)
    await svc._cleanup_notification_cache()

    assert (1, old_token) not in svc._autopay_fail_state
    assert (2, fresh_token) in svc._autopay_fail_state


async def test_maybe_notify_sends_first_then_silent(monkeypatch):
    """_maybe_notify_autopay_failure sends on first failure, records state, and stays
    silent on an immediate second tick (no repeat configured)."""
    from app.services import monitoring_service as ms

    svc = ms.MonitoringService(bot=object())  # truthy bot so the telegram branch is taken
    sent: list[bool] = []

    async def fake_send(user, balance, required, *, subscription=None, is_final=False):
        sent.append(is_final)

    monkeypatch.setattr(svc, '_send_autopay_failed_notification', fake_send)
    monkeypatch.setattr(ms.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(ms.cache, 'set', AsyncMock(return_value=True))

    now = datetime.now(UTC)
    user = SimpleNamespace(id=1, telegram_id=555, balance_kopeks=0)
    sub = SimpleNamespace(id=7, end_date=now + timedelta(hours=40))

    await svc._maybe_notify_autopay_failure(user, 50000, sub, now)
    assert sent == [False]  # 'first' → is_final False

    await svc._maybe_notify_autopay_failure(user, 50000, sub, now)
    assert sent == [False]  # second tick: silent (not in final window, repeat disabled)


async def test_maybe_notify_final_when_inside_window(monkeypatch):
    """When the first failure lands inside the final window, the wrapper sends a final
    (is_final=True) message."""
    from app.services import monitoring_service as ms

    svc = ms.MonitoringService(bot=object())
    sent: list[bool] = []

    async def fake_send(user, balance, required, *, subscription=None, is_final=False):
        sent.append(is_final)

    monkeypatch.setattr(svc, '_send_autopay_failed_notification', fake_send)
    monkeypatch.setattr(ms.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(ms.cache, 'set', AsyncMock(return_value=True))

    now = datetime.now(UTC)
    user = SimpleNamespace(id=1, telegram_id=555, balance_kopeks=0)
    sub = SimpleNamespace(id=8, end_date=now + timedelta(hours=2))  # inside default 3h window

    await svc._maybe_notify_autopay_failure(user, 50000, sub, now)
    assert sent == [True]


# ── Regression tests for the per-cycle policy hardening ──
# The final "about to be disconnected" reminder must never be starved: it fires
# exactly once per cycle even if periodic repeats already hit the cap, and even if
# a coarse monitoring interval steps past the final window onto a post-expiry tick.


def test_final_not_starved_by_repeats_when_cap_reached():
    """max=2 + repeats=6h: first + repeat exhaust the cap, but the final still fires
    once (bypasses the cap), so the user always gets the last-chance warning."""
    cfg = dict(max_notifications=2, final_reminder_hours=3, repeat_interval_hours=6)
    state = AutopayFailState()
    sent = []
    # (hours_left, now_ts): far failure → first; +6h still far → repeat (count hits cap=2);
    # then inside the 3h window → final must still fire despite count >= max.
    for hours_left, now_ts in [(40, 0), (34, 7 * 3600), (2, 200000)]:
        reason = decide_autopay_fail_notification(state, hours_left=hours_left, now_ts=now_ts, **cfg)
        if reason is not None:
            sent.append(reason)
            apply_autopay_fail_notification(state, reason, now_ts)
    assert sent == ['first', 'repeat', 'final']
    assert state.final_sent is True


def test_final_fires_even_if_interval_steps_past_window():
    """If the monitoring tick jumps over the final window (4h → expired), the still-unsent
    final reminder must go out on the post-expiry tick (no lower 0-bound on hours_left)."""
    state = AutopayFailState()
    sent = []
    for hours_left, now_ts in [(4, 0), (-0.5, 200000)]:
        reason = decide_autopay_fail_notification(state, hours_left=hours_left, now_ts=now_ts, **DEFAULTS)
        if reason is not None:
            sent.append(reason)
            apply_autopay_fail_notification(state, reason, now_ts)
    assert sent == ['first', 'final']


def test_max_one_still_delivers_final():
    """Even with max=1 the final is guaranteed (cap bounds repeats, not the final)."""
    cfg = dict(max_notifications=1, final_reminder_hours=3, repeat_interval_hours=0)
    state = AutopayFailState()
    assert decide_autopay_fail_notification(state, hours_left=40, now_ts=0, **cfg) == 'first'
    apply_autopay_fail_notification(state, 'first', 0)
    assert decide_autopay_fail_notification(state, hours_left=2, now_ts=200000, **cfg) == 'final'


async def test_load_reads_redis_on_inmemory_miss(monkeypatch):
    """Cross-restart durability: on an in-memory miss, _load must consult Redis and
    reconstruct the state from the stored JSON dict (this branch is otherwise uncovered)."""
    from app.services import monitoring_service as ms

    svc = ms.MonitoringService(bot=None)  # empty in-memory state
    get_mock = AsyncMock(return_value={'count': 1, 'last_sent_ts': 5.0, 'final_sent': True})
    monkeypatch.setattr(ms.cache, 'get', get_mock)

    loaded = await svc._load_autopay_fail_state(subscription_id=7, cycle_token=111)

    assert loaded.count == 1 and loaded.final_sent is True and loaded.last_sent_ts == 5.0
    get_mock.assert_awaited_once_with('autopay_fail:7:111')


async def test_save_persists_with_ttl_floor(monkeypatch):
    """_save writes key/value/TTL to Redis; the TTL is floored at 60s so a near-zero
    ttl can't evict state before the cycle ends."""
    from app.services import monitoring_service as ms

    svc = ms.MonitoringService(bot=None)
    set_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(ms.cache, 'set', set_mock)

    await svc._save_autopay_fail_state(7, 111, AutopayFailState(count=1, last_sent_ts=5.0), ttl_seconds=10)

    args, kwargs = set_mock.await_args
    assert args[0] == 'autopay_fail:7:111'
    assert args[1] == {'count': 1, 'last_sent_ts': 5.0, 'final_sent': False}
    assert kwargs['expire'] == 60  # floored up from 10


async def test_email_only_path_uses_cause_specific_reason(monkeypatch):
    """Non-Telegram (email) users: the reason text reflects the failure cause —
    'charge_error' must NOT be mislabelled as insufficient balance."""
    from app.services import monitoring_service as ms

    svc = ms.MonitoringService(bot=None)
    reasons: list[str] = []

    async def fake_notify(*, user, reason):
        reasons.append(reason)

    monkeypatch.setattr(ms.notification_delivery_service, 'notify_autopay_failed', fake_notify)
    monkeypatch.setattr(ms.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(ms.cache, 'set', AsyncMock(return_value=True))

    now = datetime.now(UTC)
    user = SimpleNamespace(id=1, telegram_id=None, balance_kopeks=0)

    await svc._maybe_notify_autopay_failure(
        user, 50000, SimpleNamespace(id=7, end_date=now + timedelta(hours=40)), now, cause='charge_error'
    )
    await svc._maybe_notify_autopay_failure(
        user, 50000, SimpleNamespace(id=8, end_date=now + timedelta(hours=40)), now, cause='insufficient_balance'
    )

    assert reasons == ['Ошибка списания средств', 'Недостаточно средств на балансе']


async def test_email_only_final_reminder_reason(monkeypatch):
    """Email users get the distinct final-reminder wording when the cycle reaches the window."""
    from app.services import monitoring_service as ms

    svc = ms.MonitoringService(bot=None)
    reasons: list[str] = []

    async def fake_notify(*, user, reason):
        reasons.append(reason)

    monkeypatch.setattr(ms.notification_delivery_service, 'notify_autopay_failed', fake_notify)
    monkeypatch.setattr(ms.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(ms.cache, 'set', AsyncMock(return_value=True))

    now = datetime.now(UTC)
    user = SimpleNamespace(id=1, telegram_id=None, balance_kopeks=0)
    sub = SimpleNamespace(id=7, end_date=now + timedelta(hours=40))

    await svc._maybe_notify_autopay_failure(user, 50000, sub, now)  # 'first'
    # Same cycle (end_date unchanged) but the tick now lands inside the 3h window → 'final'.
    await svc._maybe_notify_autopay_failure(user, 50000, sub, now + timedelta(hours=38))

    assert reasons[0] == 'Недостаточно средств на балансе'
    assert reasons[1].startswith('Последнее напоминание')
