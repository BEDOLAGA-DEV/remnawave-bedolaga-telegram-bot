import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import InputMediaPhoto
from aiohttp import web

from app.config import settings
from app.services import startup_notification_service as startup
from app.utils import rich_admin, rich_menu, rich_notify
from tests.services.test_startup_notification_summary import _stats


@pytest.fixture(autouse=True)
def rich_config(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_RICH_ENABLED', True)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_ENABLED', True)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_INLINE_BUTTONS', False)
    rich_admin._reset_rich_admin_availability()
    rich_menu._reset_rich_menu_availability()
    yield
    rich_admin._reset_rich_admin_availability()
    rich_menu._reset_rich_menu_availability()


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['startup', 'menu', 'notification'])
async def test_outgoing_rich_images_have_referenced_media(surface, monkeypatch):
    url = 'https://logo.example/image.png?v=1&format=png'
    monkeypatch.setattr(rich_menu, '_resolve_rich_logo_url', lambda: url)
    monkeypatch.setattr(rich_notify, '_resolve_rich_logo_url', lambda: url)
    if surface == 'menu':
        message = rich_menu._input_rich_message(f'<img src="{url.replace("&", "&amp;")}"/><p>Body</p>', 'fa')
        assert message.is_rtl is True
    else:
        bot = AsyncMock()
        if surface == 'startup':
            assert await rich_admin.try_send_rich_admin_message(bot, -100123, startup.render_startup_rich(_stats()))
        else:
            assert await rich_notify.try_send_rich_notification(bot, 123, 'Body', with_logo=True)
        message = bot.send_rich_message.await_args.kwargs['rich_message']
    assert message.media and len(message.media) == 1
    media = message.media[0]
    assert isinstance(media.media, InputMediaPhoto)
    assert media.media.media == url
    assert f'tg://photo?id={media.id}' in message.html
    assert 'src="https://' not in message.html
    assert message.skip_entity_detection is True


@pytest_asyncio.fixture
async def bot_api_server():
    requests = []

    async def receive(request):
        data = dict(await request.post())
        requests.append(data)
        rich = json.loads(data['rich_message'])
        media = rich.get('media') or []
        # Enforce the documented Telegram media contract at the actual SDK HTTP boundary.
        if not media or f'tg://photo?id={media[0]["id"]}' not in rich['html']:
            return web.json_response(
                {'ok': False, 'error_code': 400, 'description': 'Bad Request: RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND'},
                status=400,
            )
        return web.json_response(
            {
                'ok': True,
                'result': {
                    'message_id': 1,
                    'date': 1,
                    'chat': {'id': -100123, 'type': 'supergroup', 'title': 'Local test'},
                },
            }
        )

    app = web.Application()
    app.router.add_post('/bot{token}/sendRichMessage', receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    try:
        await site.start()
        host, port = runner.addresses[0]
        yield f'http://{host}:{port}', requests
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_startup_logo_crosses_real_sdk_http_boundary(bot_api_server, monkeypatch):
    base, requests = bot_api_server
    logo = 'https://logo.example/logo.png?v=123&size=full'
    monkeypatch.setattr(rich_menu, '_resolve_rich_logo_url', lambda: logo)
    session = AiohttpSession(api=TelegramAPIServer.from_base(base))
    bot = Bot(token='123456:local-test', session=session)
    try:
        service = startup.StartupNotificationService(bot)
        service.enabled = True
        service.chat_id = -100123
        service.topic_id = 7
        monkeypatch.setattr(service, '_collect_stats', AsyncMock(return_value=_stats()))
        sent = await service.send_startup_notification()
        assert sent is True
        assert len(requests) == 1
        payload = requests[0]
        assert payload['message_thread_id'] == '7'
        rich = json.loads(payload['rich_message'])
        assert rich['media'][0]['media']['type'] == 'photo'
        assert rich['media'][0]['media']['media'] == logo
        assert f'tg://photo?id={rich["media"][0]["id"]}' in rich['html']
        assert 'v4.14.0' in rich['html']
    finally:
        await bot.session.close()


def test_rich_image_attachment_deduplicates_and_avoids_existing_ids():
    content = (
        '<img src="tg://photo?id=photo_0"/>'
        '<img alt="logo" src="https://logo.example/logo.png?a=1&amp;b=2"/>'
        "<IMG src='https://logo.example/logo.png?a=1&amp;b=2'/>"
    )
    message = rich_menu._input_rich_message(content, 'en')
    assert message.media and len(message.media) == 1
    media = message.media[0]
    assert media.id != 'photo_0'
    assert media.media.media == 'https://logo.example/logo.png?a=1&b=2'
    assert message.html.count(f'tg://photo?id={media.id}') == 2
    assert 'tg://photo?id=photo_0' in message.html


def test_text_only_and_escaped_image_text_stay_unchanged():
    content = '<p>&lt;img src=&quot;https://logo.example/logo.png&quot;/&gt;</p>'
    message = rich_menu._input_rich_message(content, 'fa')
    assert message.html == content
    assert not message.media
    assert message.is_rtl is True


@pytest.mark.parametrize(
    'content',
    [
        '<img' + '\t' * 30_000 + '>',
        '<img ' + 'src= ' * 30_000 + '>',
        '<img ' * 30_000,
    ],
    ids=['whitespace', 'missing-quoted-values', 'unclosed-tags'],
)
def test_pathological_image_markup_stays_fast(content):
    import time

    from app.utils.rich_media import prepare_rich_message

    start = time.perf_counter()
    message = prepare_rich_message(content)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f'image parsing took {elapsed:.3f}s'
    assert message.html == content
    assert not message.media


@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        (
            '<IMG alt="a > b" SRC = \'https://logo.example/a.png\' width="2" />',
            '<IMG alt="a > b" SRC = \'tg://photo?id=photo_0\' width="2" />',
        ),
        (
            '<img alt=\' src="https://logo.example/fake.png"\' src="https://logo.example/a.png">',
            '<img alt=\' src="https://logo.example/fake.png"\' src="tg://photo?id=photo_0">',
        ),
        (
            '<img data-src="https://logo.example/fake.png" src="https://logo.example/a.png">',
            '<img data-src="https://logo.example/fake.png" src="tg://photo?id=photo_0">',
        ),
    ],
)
def test_image_sources_preserve_attribute_boundaries_and_markup(content, expected):
    from app.utils.rich_media import prepare_rich_message

    message = prepare_rich_message(content)

    assert message.html == expected
    assert message.media and len(message.media) == 1
    assert message.media[0].media.media == 'https://logo.example/a.png'


@pytest.mark.parametrize(
    'content',
    [
        '<!-- <img src="https://logo.example/a.png"> -->',
        '<span title=\'<img src="https://logo.example/a.png">\'>text</span>',
        '<img src="tg://photo?id=existing" src="https://logo.example/a.png">',
        '<img src="data:image/png;base64,AAAA">',
        '<img src="/local.png">',
        '<img src=https://logo.example/a.png>',
        '<image src="https://logo.example/a.png">',
        '<img.custom src="https://logo.example/a.png">',
        '<img src="https://logo.example/unclosed.png>',
    ],
)
def test_non_external_sources_and_image_text_are_preserved(content):
    from app.utils.rich_media import prepare_rich_message

    message = prepare_rich_message(content)

    assert message.html == content
    assert not message.media
