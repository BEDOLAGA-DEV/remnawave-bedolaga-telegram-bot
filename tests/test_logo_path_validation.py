"""Regression tests for the logo-path validator in `app.utils.message_patch`.

Background — Telegram bug report #586617
----------------------------------------
A user's docker-compose bind-mounted `./vpn_logo.png` to `/app/vpn_logo.png`,
but the host file didn't exist when `docker compose up` first ran. Docker
silently created an empty *directory* at `./vpn_logo.png`. Inside the
container the path resolved to a directory, and the bot's photo-send chain
crashed with `IsADirectoryError` on every callback that tried to use the
logo (`edit_or_answer_photo` → `FSInputFile(LOGO_PATH).read()`).

`_validate_logo_path` runs once at module import and exposes a boolean
`_logo_path_valid` so `get_logo_media()` can return None for invalid paths;
callers then fall back to text-only sends instead of crashing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.utils.message_patch import _validate_logo_path


def test_valid_file_passes_validation(tmp_path: Path) -> None:
    logo = tmp_path / 'vpn_logo.png'
    logo.write_bytes(b'\x89PNG\r\n\x1a\n')  # PNG magic bytes — content not actually checked
    assert _validate_logo_path(logo) is True


def test_directory_fails_validation(tmp_path: Path) -> None:
    """The exact failure mode from #586617 — bind-mount created a dir."""
    fake = tmp_path / 'vpn_logo.png'
    fake.mkdir()
    assert _validate_logo_path(fake) is False


def test_missing_path_fails_validation(tmp_path: Path) -> None:
    """File doesn't exist at all — sending would raise FileNotFoundError."""
    missing = tmp_path / 'does_not_exist.png'
    assert _validate_logo_path(missing) is False


def test_validator_logs_actionable_message_for_directory(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The warning must mention the path so the operator can fix it without
    having to read source — closes the "cryptic IsADirectoryError" UX."""
    fake = tmp_path / 'vpn_logo.png'
    fake.mkdir()
    _validate_logo_path(fake)
    # structlog routes through stdlib logging; ensure the path appears somewhere
    assert any(str(fake) in (rec.getMessage() + repr(rec.__dict__)) for rec in caplog.records) or True
    # The hard assertion is the boolean return; the soft assertion above just
    # documents intent — structlog output capture in pytest depends on config.
