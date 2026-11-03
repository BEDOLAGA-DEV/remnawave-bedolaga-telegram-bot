"""Regression for #634720 — referral invite links must survive tap-to-copy.

The invite is shown inside a <blockquote> with "tap to copy". Telegram keeps
<code> content in the clipboard but DROPS auto-linked raw URLs when copying a
quote, so plain-URL bot/cabinet links fell out of the copied text. The fix
wraps both links in <code>.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.referral as ref


async def test_create_invite_message_wraps_links_in_code(monkeypatch):
    captured = {}

    async def fake_edit(callback, text, keyboard):
        captured['text'] = text

    monkeypatch.setattr(ref, 'edit_or_answer_photo', fake_edit)
    # get_*_referral_link are methods on the Settings class — patch on the class.
    monkeypatch.setattr(
        type(ref.settings), 'get_bot_referral_link', lambda self, code, bot: 'https://t.me/bot?start=ref_X'
    )
    monkeypatch.setattr(
        type(ref.settings), 'get_cabinet_referral_link', lambda self, code: 'https://cab.example/?ref=X&u=1'
    )
    monkeypatch.setattr(ref.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 0)

    db_user = SimpleNamespace(referral_code='X', language='ru')
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username='bot'))
    callback = MagicMock()
    callback.bot = bot
    callback.answer = AsyncMock()

    await ref.create_invite_message(callback, db_user, None)

    html = captured['text']
    # Both links are wrapped in <code> so tap-to-copy captures them whole.
    assert '<code>https://t.me/bot?start=ref_X</code>' in html
    # `&` in the cabinet URL is HTML-escaped, but the <code> tags are NOT escaped.
    assert '<code>https://cab.example/?ref=X&amp;u=1</code>' in html
    assert '&lt;code&gt;' not in html
    # Still rendered inside the copyable quote.
    assert '<blockquote>' in html and '</blockquote>' in html


async def test_referral_qr_cache_tracks_link_and_owner(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        type(ref.settings),
        'get_bot_referral_link',
        lambda self, code, bot: f'https://example.com/{bot}?ref={code}',
    )
    monkeypatch.setattr(type(ref.settings), 'get_cabinet_referral_link', lambda self, code: None)
    links = []

    def make_image(link):
        links.append(link)
        return SimpleNamespace(save=lambda path: path.write_bytes(link.encode()))

    monkeypatch.setattr(ref.qrcode, 'make', make_image)
    callback = SimpleNamespace(
        answer=AsyncMock(),
        bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='example_bot'))),
        message=SimpleNamespace(edit_media=AsyncMock()),
    )
    user = SimpleNamespace(id=1, referral_code='first', language='ru')
    await ref.show_referral_qr(callback, user)
    await ref.show_referral_qr(callback, user)
    assert len(links) == 1
    user.referral_code = 'second'
    await ref.show_referral_qr(callback, user)
    user.id = 2
    await ref.show_referral_qr(callback, user)
    assert len(links) == 3
    images = list((tmp_path / 'data' / 'referral_qr').glob('*.png'))
    assert len(images) == 3
    assert sorted(path.read_text() for path in images) == sorted(links)
    assert callback.message.edit_media.await_count == 4
