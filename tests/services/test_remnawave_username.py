"""Boundary tests for RemnaWave username construction.

Regression cover for: `Validation failed: Username must be less than 36 characters`
on cabinet purchase-tariff. Bug repro:
  email='didykmarin@yandex.ru', user_id=703, short_id='49883b',
  REMNAWAVE_USER_USERNAME_TEMPLATE='{email}_{telegram_id}'
  → 'didykmarin_email_didykmarin_703_49883b' (38 chars > 36).
"""

from __future__ import annotations

import pytest

from app.config import settings


# Note: эти тесты дёргают `format_remnawave_username` напрямую, поэтому
# template управляется через monkeypatch (а не env), чтобы не мешать другим
# тестам в той же сессии.


@pytest.fixture(autouse=True)
def _restore_template(monkeypatch: pytest.MonkeyPatch):
    """Ensure each test starts from the default template."""
    original = settings.REMNAWAVE_USER_USERNAME_TEMPLATE
    yield
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', original, raising=False)


def test_format_remnawave_username_within_max_without_suffix() -> None:
    """Default behaviour stays bounded by REMNAWAVE_USERNAME_MAX_LENGTH."""
    name = settings.format_remnawave_username(
        full_name='Some Long Name That Could Inflate The Username',
        username='averylongnickname',
        telegram_id=12345678901,
        email='averylongemailprefix@example.com',
        user_id=999999,
    )

    assert len(name) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH
    assert len(name) >= settings.REMNAWAVE_USERNAME_MIN_LENGTH


def test_format_remnawave_username_reserves_room_for_caller_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reserve_suffix_chars=N → base fits in MAX-N so caller can append safely."""
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', '{email}_{telegram_id}', raising=False)

    suffix = '_49883b'  # 7 chars
    base = settings.format_remnawave_username(
        full_name='Марина Дидык',
        username=None,
        telegram_id=None,
        email='didykmarin@yandex.ru',
        user_id=703,
        reserve_suffix_chars=len(suffix),
    )
    final = f'{base}{suffix}'

    # The ORIGINAL bug — final length = 38. With the fix it must be ≤ 36.
    assert len(final) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH
    assert final.endswith(suffix)
    # Sanity: still a valid RemnaWave identifier (alnum + underscores + dashes).
    assert all(ch.isalnum() or ch in {'_', '-'} for ch in final)


def test_format_remnawave_username_email_user_default_template() -> None:
    """Email-only user with the bundled default template still fits."""
    name = settings.format_remnawave_username(
        full_name='Марина Дидык',
        username=None,
        telegram_id=None,
        email='didykmarin@yandex.ru',
        user_id=703,
        reserve_suffix_chars=7,  # what subscription_service actually reserves
    )

    assert len(name) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH - 7


def test_format_remnawave_username_does_not_go_below_min_with_huge_reserve() -> None:
    """If caller asks for more reserve than the cap allows, base falls back to MIN."""
    name = settings.format_remnawave_username(
        full_name='X',
        username='x',
        telegram_id=1,
        email=None,
        user_id=None,
        reserve_suffix_chars=settings.REMNAWAVE_USERNAME_MAX_LENGTH + 100,
    )

    assert len(name) >= settings.REMNAWAVE_USERNAME_MIN_LENGTH


def test_format_remnawave_username_repro_38_char_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact production payload from log.rw/ARVm79dH must come out ≤ 36 chars."""
    # Production .env override exposes the duplication path:
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', '{email}_{telegram_id}', raising=False)

    suffix = '_49883b'
    base = settings.format_remnawave_username(
        full_name='Марина Дидык',
        username=None,
        telegram_id=None,
        email='didykmarin@yandex.ru',
        user_id=703,
        reserve_suffix_chars=len(suffix),
    )
    final = base + suffix

    # Before the fix: len(final) == 38 → RemnaWave 400.
    assert len(final) <= 36, f'username still too long: {final!r} ({len(final)} chars)'


# ---------------------------------------------------------------------------
# build_remnawave_subscription_username — high-level helper used by all 3
# multi-tariff create-paths (subscription_service, cabinet admin sync, bulk).
# ---------------------------------------------------------------------------


def test_build_subscription_username_production_repro(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production repro through the high-level helper used by all 3 callers."""
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', '{email}_{telegram_id}', raising=False)

    final = settings.build_remnawave_subscription_username(
        full_name='Марина Дидык',
        username=None,
        telegram_id=None,
        email='didykmarin@yandex.ru',
        user_id=703,
        suffix='_49883b',
    )

    assert len(final) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH
    assert final.endswith('_49883b')


def test_build_subscription_username_empty_suffix_is_legacy_format() -> None:
    """suffix='' → equivalent to plain format_remnawave_username (single-tariff path)."""
    plain = settings.format_remnawave_username(
        full_name='X',
        username='x',
        telegram_id=12345,
        email=None,
        user_id=1,
    )
    helper = settings.build_remnawave_subscription_username(
        full_name='X',
        username='x',
        telegram_id=12345,
        email=None,
        user_id=1,
        suffix='',
    )

    assert helper == plain
    assert len(helper) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH


def test_build_subscription_username_handles_pathological_long_suffix() -> None:
    """Suffix longer than MAX_LENGTH: helper must still produce a string ≤ MAX_LENGTH.

    Regression cover for an edge case in the defensive-truncation branch where
    `keep_for_base = MAX - len(suffix)` could go negative; without `max(0, …)`
    the base-slice silently kept the tail.
    """
    huge_suffix = '_' + 'x' * 80  # 81 chars, way over MAX

    final = settings.build_remnawave_subscription_username(
        full_name='X',
        username='x',
        telegram_id=12345,
        email=None,
        user_id=1,
        suffix=huge_suffix,
    )

    assert len(final) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH
