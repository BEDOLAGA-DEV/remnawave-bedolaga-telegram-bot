"""Regression test: logo resize must not write next to the (read-only) source.

Telegram log: "Logo resize preflight failed — sending original ...
[Errno 13] Permission denied: 'vpn_logo.bot_resized.png'". The resized copy was saved
beside the source logo, but the app/logo directory is read-only in container deploys, so
the resize crashed and every send fell back to the oversized original. The resized copy
now goes to a writable temp dir.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.utils import message_patch as mp


def test_oversized_logo_resized_into_writable_tempdir(tmp_path):
    pytest.importorskip('PIL')
    from PIL import Image

    src = tmp_path / 'vpn_logo.png'  # stands in for the read-only source dir
    Image.new('RGB', (2000, 1500), (10, 20, 30)).save(src, format='PNG')

    out = mp._prepare_logo_for_send(src)

    assert out != src, 'oversized logo should be resized, not returned as-is'
    assert out.parent == Path(tempfile.gettempdir()), 'resized copy must go to the temp dir'
    assert out.exists()
    assert out.stat().st_size <= mp._LOGO_MAX_BYTES
    out.unlink(missing_ok=True)


def test_small_logo_returned_unchanged(tmp_path):
    pytest.importorskip('PIL')
    from PIL import Image

    src = tmp_path / 'small.png'
    Image.new('RGB', (64, 64), (0, 0, 0)).save(src, format='PNG')

    assert mp._prepare_logo_for_send(src) == src
