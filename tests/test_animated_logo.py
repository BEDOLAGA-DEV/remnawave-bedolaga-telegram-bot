"""Animated logo support (nozapret.mp4 sent as Telegram animation/GIF).

The logo pipeline historically assumed a static PNG sent via sendPhoto.
With an .mp4/.gif LOGO_FILE the bot must:
  - dispatch to sendAnimation / InputMediaAnimation (silent mp4 renders as GIF),
  - cache the file_id from `result.animation` (not `result.photo`),
  - skip the Pillow resize preflight (PNG-only),
  - keep editing messages in place instead of delete+resend.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.types import FSInputFile, InputMediaAnimation, InputMediaPhoto

from app.config import settings
from app.utils import message_patch, photo_message


def _patch_logo(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(message_patch, 'LOGO_PATH', path)
    monkeypatch.setattr(message_patch, '_logo_path_valid', True)
    monkeypatch.setattr(message_patch, '_logo_file_id', None)
    monkeypatch.setattr(message_patch, '_logo_send_path', None)


def _write_stub(path: Path) -> Path:
    path.write_bytes(b'\x00\x00\x00\x18ftypmp42')
    return path


# --- extension dispatch -----------------------------------------------------


def test_logo_is_animation_for_mp4(monkeypatch, tmp_path: Path) -> None:
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'nozapret.mp4'))
    assert message_patch.logo_is_animation() is True


def test_logo_is_animation_for_gif(monkeypatch, tmp_path: Path) -> None:
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'logo.gif'))
    assert message_patch.logo_is_animation() is True


def test_logo_is_animation_false_for_png(monkeypatch, tmp_path: Path) -> None:
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'vpn_logo.png'))
    assert message_patch.logo_is_animation() is False


def test_logo_input_media_class_dispatch(monkeypatch, tmp_path: Path) -> None:
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'nozapret.mp4'))
    assert message_patch.logo_input_media_class() is InputMediaAnimation
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'vpn_logo.png'))
    assert message_patch.logo_input_media_class() is InputMediaPhoto


# --- Pillow preflight must not run on video ---------------------------------


def test_get_logo_media_mp4_skips_resize_preflight(monkeypatch, tmp_path: Path) -> None:
    src = _write_stub(tmp_path / 'nozapret.mp4')
    _patch_logo(monkeypatch, src)

    def _boom(path: Path) -> Path:
        raise AssertionError('PNG resize preflight must not run for animation logo')

    monkeypatch.setattr(message_patch, '_prepare_logo_for_send', _boom)

    media = message_patch.get_logo_media()
    assert isinstance(media, FSInputFile)
    assert Path(str(media.path)) == src


# --- file_id caching --------------------------------------------------------


def test_cache_logo_file_id_from_animation(monkeypatch) -> None:
    monkeypatch.setattr(message_patch, '_logo_file_id', None)
    result = SimpleNamespace(photo=None, animation=SimpleNamespace(file_id='ANIM123'))
    message_patch._cache_logo_file_id(result)
    assert message_patch._logo_file_id == 'ANIM123'


def test_cache_logo_file_id_from_photo_still_works(monkeypatch) -> None:
    monkeypatch.setattr(message_patch, '_logo_file_id', None)
    result = SimpleNamespace(photo=[SimpleNamespace(file_id='PHOTO1')], animation=None)
    message_patch._cache_logo_file_id(result)
    assert message_patch._logo_file_id == 'PHOTO1'


# --- bot-level send helper (start.py / monitoring_service call sites) -------


class _FakeBot:
    def __init__(self):
        self.animation_calls: list[dict] = []
        self.photo_calls: list[dict] = []

    async def send_animation(self, **kwargs):
        self.animation_calls.append(kwargs)
        return SimpleNamespace(photo=None, animation=SimpleNamespace(file_id='ANIM_SENT'))

    async def send_photo(self, **kwargs):
        self.photo_calls.append(kwargs)
        return SimpleNamespace(photo=[SimpleNamespace(file_id='PHOTO_SENT')], animation=None)


@pytest.mark.asyncio
async def test_send_logo_media_dispatches_to_animation(monkeypatch, tmp_path: Path) -> None:
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'nozapret.mp4'))
    bot = _FakeBot()

    result = await message_patch.send_logo_media(bot, chat_id=42, caption='hi', parse_mode='HTML')

    assert len(bot.animation_calls) == 1
    assert bot.photo_calls == []
    call = bot.animation_calls[0]
    assert call['chat_id'] == 42
    assert call['caption'] == 'hi'
    assert result.animation.file_id == 'ANIM_SENT'
    # file_id cached from the animation result
    assert message_patch._logo_file_id == 'ANIM_SENT'


@pytest.mark.asyncio
async def test_send_logo_media_dispatches_to_photo_for_png(monkeypatch, tmp_path: Path) -> None:
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'vpn_logo.png'))
    bot = _FakeBot()

    await message_patch.send_logo_media(bot, chat_id=42, caption='hi')

    assert len(bot.photo_calls) == 1
    assert bot.animation_calls == []
    assert message_patch._logo_file_id == 'PHOTO_SENT'


# --- Message.answer patch sends animation -----------------------------------


class _FakeMessage:
    def __init__(self):
        self.from_user = None
        self.photo = None
        self.animation = None
        self.text = None
        self.answer_animation_calls: list[tuple] = []
        self.answer_photo_calls: list[tuple] = []
        self.edit_media_calls: list[tuple] = []
        self.deleted = False

    async def answer_animation(self, animation, caption=None, **kwargs):
        self.answer_animation_calls.append((animation, caption, kwargs))
        return SimpleNamespace(photo=None, animation=SimpleNamespace(file_id='ANIM_A'))

    async def answer_photo(self, photo, caption=None, **kwargs):
        self.answer_photo_calls.append((photo, caption, kwargs))
        return SimpleNamespace(photo=[SimpleNamespace(file_id='PH_A')], animation=None)

    async def edit_media(self, media, **kwargs):
        self.edit_media_calls.append((media, kwargs))
        return SimpleNamespace(photo=None, animation=SimpleNamespace(file_id='ANIM_E'))

    async def delete(self):
        self.deleted = True


@pytest.mark.asyncio
async def test_answer_with_photo_sends_animation_for_mp4(monkeypatch, tmp_path: Path) -> None:
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'nozapret.mp4'))
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    fake = _FakeMessage()

    await message_patch._answer_with_photo(fake, 'hello')

    assert len(fake.answer_animation_calls) == 1
    assert fake.answer_photo_calls == []
    assert message_patch._logo_file_id == 'ANIM_A'


# --- Message.edit_text patch: edit in place, no delete+resend ----------------


@pytest.mark.asyncio
async def test_edit_with_photo_edits_animation_message_in_place(monkeypatch, tmp_path: Path) -> None:
    """An existing animation message gets edited via InputMediaAnimation, not deleted."""
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'nozapret.mp4'))
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    fake = _FakeMessage()
    fake.animation = SimpleNamespace(file_id='OLD_ANIM')

    await message_patch._edit_with_photo(fake, 'updated')

    assert fake.deleted is False
    assert len(fake.edit_media_calls) == 1
    media, _ = fake.edit_media_calls[0]
    assert isinstance(media, InputMediaAnimation)
    assert media.caption == 'updated'


@pytest.mark.asyncio
async def test_edit_with_photo_swaps_old_photo_message_to_animation(monkeypatch, tmp_path: Path) -> None:
    """Pre-deploy messages were photos; edit must swap them to the animation logo in place."""
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'nozapret.mp4'))
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    fake = _FakeMessage()
    fake.photo = [SimpleNamespace(file_id='OLD_PHOTO')]

    await message_patch._edit_with_photo(fake, 'updated')

    assert fake.deleted is False
    assert len(fake.edit_media_calls) == 1
    media, _ = fake.edit_media_calls[0]
    assert isinstance(media, InputMediaAnimation)


@pytest.mark.asyncio
async def test_edit_with_photo_keeps_photo_class_for_png_logo(monkeypatch, tmp_path: Path) -> None:
    """Regression: PNG logo deployments keep the InputMediaPhoto path."""
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'vpn_logo.png'))
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    fake = _FakeMessage()
    fake.photo = [SimpleNamespace(file_id='OLD_PHOTO')]

    await message_patch._edit_with_photo(fake, 'updated')

    assert len(fake.edit_media_calls) == 1
    media, _ = fake.edit_media_calls[0]
    assert isinstance(media, InputMediaPhoto)


# --- photo_message._resolve_media class dispatch -----------------------------


def test_resolve_media_returns_animation_class_for_mp4_logo(monkeypatch, tmp_path: Path) -> None:
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'nozapret.mp4'))
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    fake = SimpleNamespace(caption=None, photo=None)

    media, media_cls = photo_message._resolve_media(fake)

    assert media is not None
    assert media_cls is InputMediaAnimation


def test_resolve_media_keeps_existing_photo_when_logo_mode_off(monkeypatch, tmp_path: Path) -> None:
    _patch_logo(monkeypatch, _write_stub(tmp_path / 'nozapret.mp4'))
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    fake = SimpleNamespace(caption=None, photo=[SimpleNamespace(file_id='OWN_PHOTO')])

    media, media_cls = photo_message._resolve_media(fake)

    assert media == 'OWN_PHOTO'
    assert media_cls is InputMediaPhoto
