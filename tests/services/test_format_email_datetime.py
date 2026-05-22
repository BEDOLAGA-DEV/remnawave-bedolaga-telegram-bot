"""Regression tests for ``app.utils.timezone.format_email_datetime``.

Bug report (2026-05-18): subscription-expiring email rendered raw
ISO with microseconds and offset (``2026-05-20 07:32:13.837000+00:00``)
straight into the template body. Root cause:
``notification_delivery_service`` did ``str(datetime)`` when building
the context dict.

The helper plus the producer-side wiring close the leak. These tests
pin:

  1. The helper's resolution order: explicit arg → settings → fallback.
  2. The default format is locale-independent (``%d.%m.%Y, %H:%M``)
     so output is identical regardless of the locale package shipped
     in the Docker image.
  3. ``settings.EMAIL_DATE_FORMAT`` overrides take effect on the next
     call (no restart required by design).
  4. Negative-control: the raw-ISO leak the user reported MUST NOT
     return — passing an aware datetime never produces a string
     containing microseconds.
  5. Producer call sites in ``notification_delivery_service`` and
     ``subscription_auto_purchase_service`` actually invoke the
     helper (source-level pin against regression).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import settings
from app.utils.timezone import format_email_datetime


# ---------------------------------------------------------------------------
# Resolution order & basic formatting.
# ---------------------------------------------------------------------------


def test_default_format_is_locale_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback shape must be ``DD.MM.YYYY, HH:MM`` — no month
    names (locale-dependent), no offset, no microseconds.
    """
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC', raising=False)
    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '%d.%m.%Y, %H:%M', raising=False)

    dt = datetime(2026, 5, 20, 7, 32, 13, 837000, tzinfo=UTC)
    out = format_email_datetime(dt)

    assert out == '20.05.2026, 07:32'


def test_explicit_fmt_arg_overrides_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller-provided ``fmt`` wins over the global setting — lets
    subject lines stay compact (date-only) while body uses datetime.
    """
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC', raising=False)
    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '%d.%m.%Y, %H:%M', raising=False)

    dt = datetime(2026, 5, 20, 7, 32, tzinfo=UTC)
    assert format_email_datetime(dt, fmt='%Y-%m-%d') == '2026-05-20'


def test_settings_override_takes_effect_without_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """An admin who updates ``EMAIL_DATE_FORMAT`` via system_settings
    UI sees the new format on the very next notification. We pin
    this by mutating the setting and observing the change.
    """
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC', raising=False)

    dt = datetime(2026, 5, 20, 7, 32, tzinfo=UTC)

    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '%d.%m.%Y, %H:%M', raising=False)
    assert format_email_datetime(dt) == '20.05.2026, 07:32'

    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '%Y/%m/%d %H:%M:%S', raising=False)
    assert format_email_datetime(dt) == '2026/05/20 07:32:00'


def test_empty_or_invalid_setting_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator misconfiguration (empty / non-string) must not crash
    the email pipeline. Fall back to the documented default.
    """
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC', raising=False)

    dt = datetime(2026, 5, 20, 7, 32, tzinfo=UTC)

    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '', raising=False)
    assert format_email_datetime(dt) == '20.05.2026, 07:32'

    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '   ', raising=False)
    assert format_email_datetime(dt) == '20.05.2026, 07:32'


# ---------------------------------------------------------------------------
# Timezone localization.
# ---------------------------------------------------------------------------


def test_localizes_to_configured_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """UTC datetime gets shifted to ``settings.TIMEZONE`` before formatting.

    The user's screenshot showed ``07:32`` (UTC) — once we localize
    to Europe/Moscow it should render as ``10:32``.
    """
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow', raising=False)
    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '%d.%m.%Y, %H:%M', raising=False)

    # Clear the lru_cache so the new TIMEZONE takes effect.
    from app.utils.timezone import get_local_timezone

    get_local_timezone.cache_clear()

    dt = datetime(2026, 5, 20, 7, 32, tzinfo=UTC)
    assert format_email_datetime(dt) == '20.05.2026, 10:32'


def test_naive_datetime_is_treated_as_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some legacy paths still pass naive datetimes. Treat them as
    UTC (matches the existing ``to_local_datetime`` contract) rather
    than crashing or rendering a misleading local time.
    """
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC', raising=False)
    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '%d.%m.%Y, %H:%M', raising=False)

    from app.utils.timezone import get_local_timezone

    get_local_timezone.cache_clear()

    naive = datetime(2026, 5, 20, 7, 32)
    assert format_email_datetime(naive) == '20.05.2026, 07:32'


# ---------------------------------------------------------------------------
# Input shapes — strings, None, empty.
# ---------------------------------------------------------------------------


def test_iso_string_is_parsed_and_reformatted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy callers that pre-isoformat'd their datetime get parsed
    and reformatted to the chosen shape — backward-compatibility for
    the ``new_expires_at=dt.isoformat()`` call sites we still support
    while refactoring producers."""
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC', raising=False)
    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '%d.%m.%Y, %H:%M', raising=False)

    from app.utils.timezone import get_local_timezone

    get_local_timezone.cache_clear()

    assert format_email_datetime('2026-05-20T07:32:13+00:00') == '20.05.2026, 07:32'


def test_unparseable_string_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the caller pre-formatted the string in some custom shape we
    can't parse, return it unchanged. Beats crashing the pipeline or
    silently producing 'N/A' in place of human-readable content."""
    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '%d.%m.%Y, %H:%M', raising=False)
    assert format_email_datetime('20 мая 2026') == '20 мая 2026'


@pytest.mark.parametrize('value', [None, ''])
def test_empty_input_returns_placeholder(value: object) -> None:
    """``None`` / empty must not render the literal Python repr or
    'None' into the email body."""
    assert format_email_datetime(value) == ''


def test_custom_placeholder_respected() -> None:
    assert format_email_datetime(None, na_placeholder='—') == '—'


def test_non_datetime_non_string_input_returns_placeholder() -> None:
    """Garbage input (int, list, etc.) → placeholder. Defensive."""
    assert format_email_datetime(42) == ''
    assert format_email_datetime([1, 2, 3]) == ''


# ---------------------------------------------------------------------------
# Negative-control: the raw-ISO leak the user reported is GONE.
# ---------------------------------------------------------------------------


def test_no_microseconds_in_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESSION (2026-05-18): user saw
    ``2026-05-20 07:32:13.837000+00:00`` in the email. The helper
    must never emit microseconds with the default format.
    """
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC', raising=False)
    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '%d.%m.%Y, %H:%M', raising=False)

    from app.utils.timezone import get_local_timezone

    get_local_timezone.cache_clear()

    dt = datetime(2026, 5, 20, 7, 32, 13, 837000, tzinfo=UTC)
    out = format_email_datetime(dt)

    assert '.837000' not in out
    assert '837000' not in out
    assert '+00:00' not in out
    # The pathological output literal we MUST NOT regress to.
    assert out != '2026-05-20 07:32:13.837000+00:00'


def test_no_offset_in_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-UTC TZ values also must not leak the offset into the email."""
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC', raising=False)
    monkeypatch.setattr(settings, 'EMAIL_DATE_FORMAT', '%d.%m.%Y, %H:%M', raising=False)

    from app.utils.timezone import get_local_timezone

    get_local_timezone.cache_clear()

    dt = datetime(2026, 5, 20, 7, 32, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    out = format_email_datetime(dt)

    assert '+' not in out
    assert '-' not in out.split(', ')[1] if ', ' in out else True


# ---------------------------------------------------------------------------
# Producer-side source pins. The bug was that producers called
# ``str(datetime)``; if anyone reverts to that pattern, the regression
# returns silently. Source-level pin is the cheapest catch.
# ---------------------------------------------------------------------------


def test_notification_delivery_service_uses_format_email_datetime() -> None:
    """``notify_subscription_expiring`` and ``notify_autopay_success``
    must invoke ``format_email_datetime`` instead of bare ``str()``.
    """
    path = Path(__file__).resolve().parents[2] / 'app' / 'services' / 'notification_delivery_service.py'
    source = path.read_text(encoding='utf-8')

    # The buggy literal patterns must NOT reappear.
    assert "'expires_at': str(expires_at)" not in source, (
        'notify_subscription_expiring regressed back to str(datetime) — '
        'this is the exact line that emitted raw ISO with microseconds'
    )
    assert "'new_expires_at': str(new_expires_at)" not in source

    # And the helper must be called.
    assert 'format_email_datetime(expires_at)' in source
    assert 'format_email_datetime(new_expires_at)' in source


def test_auto_purchase_service_uses_format_email_datetime() -> None:
    """All ``expires_at`` / ``new_expires_at`` kwarg call sites in
    ``subscription_auto_purchase_service`` must use the helper, not
    raw ``.isoformat()`` which also leaks microseconds + offset.
    """
    path = Path(__file__).resolve().parents[2] / 'app' / 'services' / 'subscription_auto_purchase_service.py'
    source = path.read_text(encoding='utf-8')

    # Pre-fix shape — must NOT return.
    assert '.isoformat() if new_end_date else' not in source
    assert ".isoformat() if subscription.end_date else ''" not in source

    # Helper must be imported AND called.
    assert 'from app.utils.timezone import format_email_datetime' in source
    assert 'format_email_datetime(' in source


def test_helper_signature_is_stable() -> None:
    """The helper is called from 9+ production sites. Lock its
    signature so a refactor doesn't silently break callers."""
    sig = inspect.signature(format_email_datetime)
    params = list(sig.parameters.keys())
    # First positional: dt. Then keyword-only: fmt, na_placeholder.
    assert params[0] == 'dt'
    assert 'fmt' in params
    assert 'na_placeholder' in params
    assert sig.parameters['fmt'].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters['na_placeholder'].kind == inspect.Parameter.KEYWORD_ONLY
