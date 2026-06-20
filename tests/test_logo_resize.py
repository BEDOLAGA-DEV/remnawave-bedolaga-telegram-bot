"""Regression tests for the logo auto-resize preflight (#339184 — esu).

The original 1024×1024 (and bigger) PNG logos blew past Telegram's effective
photo-send limits (file size > a few MB + the API's "best width 1280" hint),
producing "image size too big" errors on every photo callback. The fix
preflights the file once per process: if dimensions or size exceed our safe
caps, write a resized PNG next to the source and serve that instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.utils import message_patch
from app.utils.message_patch import _LOGO_MAX_BYTES, _LOGO_MAX_DIMENSION, _prepare_logo_for_send


def _write_png(path: Path, size: tuple[int, int]) -> Path:
    img = Image.new('RGBA', size, color=(255, 0, 0, 255))
    img.save(path, format='PNG')
    return path


def test_small_logo_used_as_is(tmp_path: Path) -> None:
    src = _write_png(tmp_path / 'logo.png', (256, 256))
    assert _prepare_logo_for_send(src) == src
    # No cached resized sibling created
    assert not (tmp_path / 'logo.bot_resized.png').exists()


def test_oversized_logo_is_resized_under_cap(tmp_path: Path) -> None:
    """A 2000×2000 PNG (~well over _LOGO_MAX_DIMENSION) gets resized."""
    src = _write_png(tmp_path / 'big.png', (2000, 2000))
    result = _prepare_logo_for_send(src)
    assert result != src, 'oversized logo must be redirected to the resized copy'
    assert result.exists()
    with Image.open(result) as img:
        assert max(img.size) <= _LOGO_MAX_DIMENSION
        # Aspect ratio preserved
        assert img.size[0] == img.size[1]


def test_oversized_non_square_logo_keeps_aspect(tmp_path: Path) -> None:
    """1980×1267 (the literal `vpn_logo.png` shipped in the repo) keeps ratio."""
    src = _write_png(tmp_path / 'wide.png', (1980, 1267))
    result = _prepare_logo_for_send(src)
    assert result != src
    with Image.open(result) as img:
        w, h = img.size
        assert w <= _LOGO_MAX_DIMENSION and h <= _LOGO_MAX_DIMENSION
        # Aspect ratio preserved to within rounding
        original_ratio = 1980 / 1267
        new_ratio = w / h
        assert abs(original_ratio - new_ratio) < 0.01


def test_cached_resized_copy_is_reused(tmp_path: Path) -> None:
    """Subsequent calls on the same source must hit the cached resized file."""
    src = _write_png(tmp_path / 'big.png', (2000, 2000))
    first = _prepare_logo_for_send(src)
    first_mtime = first.stat().st_mtime
    # Second call — should not rewrite the file (mtime unchanged)
    second = _prepare_logo_for_send(src)
    assert second == first
    assert second.stat().st_mtime == first_mtime


def test_missing_pil_falls_back_to_original(monkeypatch, tmp_path: Path) -> None:
    """If Pillow chokes on the file for any reason we return the source path —
    callers downstream still have their OSError catch nets in place."""
    src = _write_png(tmp_path / 'big.png', (2000, 2000))

    # Simulate Pillow blowing up on import-time within the function
    import builtins

    original_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == 'PIL':
            raise RuntimeError('PIL not installed')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', broken_import)
    result = _prepare_logo_for_send(src)
    assert result == src


def test_size_thresholds_are_sane() -> None:
    """Guard against accidental edits that would render the resize a noop."""
    assert _LOGO_MAX_DIMENSION <= 1280, 'Telegram compresses photos beyond ~1280 anyway'
    assert _LOGO_MAX_BYTES <= 10 * 1024 * 1024, 'Telegram hard cap is 10 MB'


@pytest.mark.asyncio
async def test_get_logo_media_uses_resized_copy(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: get_logo_media() returns FSInputFile pointing at the resized copy."""
    src = _write_png(tmp_path / 'big.png', (2000, 2000))

    monkeypatch.setattr(message_patch, 'LOGO_PATH', src)
    monkeypatch.setattr(message_patch, '_logo_path_valid', True)
    monkeypatch.setattr(message_patch, '_logo_file_id', None)
    monkeypatch.setattr(message_patch, '_logo_send_path', None)

    media = message_patch.get_logo_media()
    assert media is not None
    # FSInputFile keeps the resolved path on `.path`
    served = getattr(media, 'path', None)
    assert served is not None
    served_path = Path(str(served))
    assert served_path != src
    assert served_path.name.endswith('.bot_resized.png')
