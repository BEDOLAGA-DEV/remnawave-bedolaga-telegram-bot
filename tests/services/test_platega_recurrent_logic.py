import pytest

from app.services.platega_recurrent import platega_reconcile_decision, resolve_platega_interval


@pytest.mark.parametrize(
    ('period_days', 'is_daily', 'expected'),
    [
        (1, True, (1, 1)),  # daily tariff -> day
        (30, False, (3, 30)),  # exact month
        (7, False, (2, 7)),  # week
        (360, False, (4, 360)),  # year
        (365, False, (4, 365)),  # yearly range 350-380
        (31, False, (3, 31)),  # monthly range 28-31
        (14, False, (3, 30)),  # non-mapping -> month @ 30
        (60, False, (3, 30)),
        (90, False, (3, 30)),
        (180, False, (3, 30)),
    ],
)
def test_resolve_platega_interval(period_days, is_daily, expected):
    assert resolve_platega_interval(period_days, is_daily) == expected


def test_is_daily_wins_over_period_days():
    # a daily tariff is always daily regardless of a stray period value
    assert resolve_platega_interval(30, True) == (1, 1)


@pytest.mark.parametrize(
    ('local_status', 'remote_status', 'age_minutes', 'expected'),
    [
        # remote active -> local PENDING/PAST_DUE moves to ACTIVE
        ('PENDING', 'active', 10.0, 'ACTIVE'),
        ('PAST_DUE', 'active', 10.0, 'ACTIVE'),
        # remote cancelled/canceled -> local moves to CANCELLED (unless already there)
        ('ACTIVE', 'cancelled', 10.0, 'CANCELLED'),
        ('PENDING', 'canceled', 10.0, 'CANCELLED'),
        ('PAST_DUE', 'cancelled', 10.0, 'CANCELLED'),
        ('FAILED', 'cancelled', 10.0, 'CANCELLED'),  # first match wins, even from FAILED
        # remote failed -> local moves to FAILED (unless already there)
        ('ACTIVE', 'failed', 10.0, 'FAILED'),
        ('PENDING', 'failed', 10.0, 'FAILED'),
        ('CANCELLED', 'failed', 10.0, 'FAILED'),  # first match wins, even from CANCELLED
        # remote past-due variants -> local moves to PAST_DUE (unless already PAST_DUE/CANCELLED)
        ('ACTIVE', 'pastdue', 10.0, 'PAST_DUE'),
        ('ACTIVE', 'past_due', 10.0, 'PAST_DUE'),
        ('ACTIVE', 'past due', 10.0, 'PAST_DUE'),
        ('PENDING', 'pastdue', 10.0, 'PAST_DUE'),
        # stuck PENDING with no remote record at all, older than 30 minutes -> FAILED
        ('PENDING', None, 31.0, 'FAILED'),
        ('PENDING', None, 120.0, 'FAILED'),
        # no-change cases
        ('ACTIVE', 'active', 10.0, None),
        ('PENDING', None, 5.0, None),
        ('PENDING', None, 30.0, None),  # boundary: exactly 30 minutes does not count as stuck yet
        ('CANCELLED', 'cancelled', 10.0, None),  # already CANCELLED, no-op
        ('FAILED', 'failed', 10.0, None),  # already FAILED, no-op
        ('PAST_DUE', 'past_due', 10.0, None),  # already PAST_DUE, no-op
        ('CANCELLED', 'pastdue', 10.0, None),  # PAST_DUE excludes CANCELLED as a source state
        ('ACTIVE', None, 999.0, None),  # remote-missing rule only fires for PENDING
        ('ACTIVE', 'some_unknown_status', 10.0, None),  # unrecognized remote status -> no change
    ],
)
def test_platega_reconcile_decision(local_status, remote_status, age_minutes, expected):
    assert platega_reconcile_decision(local_status, remote_status, age_minutes) == expected


def test_reconcile_outage_does_not_bury_stuck_pending():
    """Транспортный сбой (remote_missing=False) — зависший PENDING не хороним:
    провайдер может быть жив, решение откладывается до следующего цикла.
    Достоверное отсутствие (404 → remote_missing=True) по-прежнему даёт FAILED."""
    from app.services.platega_recurrent import platega_reconcile_decision

    assert platega_reconcile_decision('PENDING', None, 45.0, remote_missing=False) is None
    assert platega_reconcile_decision('PENDING', None, 45.0, remote_missing=True) == 'FAILED'
    # Дефолт (легаси-вызовы без kwarg) сохраняет старое поведение.
    assert platega_reconcile_decision('PENDING', None, 45.0) == 'FAILED'
    # Живой remote-статус решает независимо от remote_missing.
    assert platega_reconcile_decision('PENDING', 'active', 45.0, remote_missing=False) == 'ACTIVE'
