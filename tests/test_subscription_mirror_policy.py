from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings


def test_origin_parser_is_available():
    from app.utils import subscription_mirrors

    assert callable(subscription_mirrors.parse_mirror_origins)


@pytest.mark.parametrize(
    'target',
    [
        'http://mirror.example',
        'https://[broken',
        'https://a.example:bad',
        'https://u:p@a.example',
        'https://a.example/path',
        'https://a.example?q=1',
        'https://a.example/#x',
        'https://a.example\\@evil.example',
        'https://a .example',
        'https://a.example\t',
    ],
)
def test_invalid_mirror_rejected_on_write_and_ignored_at_runtime(target):
    from app.utils.subscription_mirrors import parse_mirror_origins

    with pytest.raises(ValueError):
        parse_mirror_origins('Backup|' + target, strict=True)
    assert parse_mirror_origins('Backup|' + target) == []


def test_deduplicate_origins_and_keep_stable_ids(monkeypatch):
    from app.utils.subscription_mirrors import get_subscription_mirrors

    monkeypatch.setattr(
        settings,
        'SUBSCRIPTION_MIRROR_LINKS',
        'One|HTTPS://MIRROR.example:443/;Again|mirror.example;Self|https://sub.example',
    )
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'link')
    monkeypatch.setattr(settings, 'HIDE_SUBSCRIPTION_LINK', False)
    first = get_subscription_mirrors('https://sub.example/a%2Fb?key=a%26b#fragment')
    assert len(first) == 1
    assert first[0]['url'] == 'https://mirror.example/a%2Fb?key=a%26b#fragment'
    assert len(first[0]['id']) == 16
    monkeypatch.setattr(settings, 'SUBSCRIPTION_MIRROR_LINKS', 'Renamed|https://mirror.example')
    assert get_subscription_mirrors('https://sub.example/new')[0]['id'] == first[0]['id']


@pytest.mark.parametrize('mode', ['guide', 'link', 'miniapp_subscription', 'miniapp_custom', 'happ_cryptolink'])
@pytest.mark.parametrize('hidden', [False, True])
def test_mirrors_follow_visibility_policy(monkeypatch, mode, hidden):
    from app.utils.subscription_mirrors import get_subscription_mirrors

    monkeypatch.setattr(settings, 'SUBSCRIPTION_MIRROR_LINKS', 'Backup|https://mirror.example')
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', mode)
    monkeypatch.setattr(settings, 'HIDE_SUBSCRIPTION_LINK', hidden)
    assert bool(get_subscription_mirrors('https://sub.example/demo')) is (
        not hidden and mode in {'guide', 'link', 'miniapp_subscription'}
    )


@pytest.mark.asyncio
async def test_cabinet_returns_mirrors_for_resolved_subscription(monkeypatch):
    from app.cabinet.routes.subscription_modules import status

    monkeypatch.setattr(settings, 'SUBSCRIPTION_MIRROR_LINKS', 'Backup|https://mirror.example')
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'link')
    monkeypatch.setattr(settings, 'HIDE_SUBSCRIPTION_LINK', False)
    sub = SimpleNamespace(id=42, subscription_url='https://sub.example/demo', subscription_crypto_link=None)
    resolver = AsyncMock(return_value=sub)
    monkeypatch.setattr(status, 'resolve_subscription', resolver)
    user, db = SimpleNamespace(id=1), AsyncMock()
    result = await status.get_connection_link(user=user, db=db, subscription_id=42)
    resolver.assert_awaited_once_with(db, user, 42)
    assert result['subscription_id'] == 42
    assert result['mirrors'][0]['url'] == 'https://mirror.example/demo'
    assert sub.subscription_url == 'https://sub.example/demo'


@pytest.mark.parametrize(
    'source',
    [
        None,
        '',
        'not a url',
        'happ://crypt4/demo',
        'https://[broken',
        'https://u:p@sub.example/demo',
        'https://sub.example:99999/demo',
        'https://sub.example/\nsecret',
    ],
)
def test_invalid_source_never_generates_mirror(monkeypatch, source):
    from app.utils.subscription_mirrors import build_subscription_mirror_links

    monkeypatch.setattr(settings, 'SUBSCRIPTION_MIRROR_LINKS', 'Backup|https://mirror.example')
    assert build_subscription_mirror_links(source) == []


def test_size_limits_and_stable_ids_after_reordering(monkeypatch):
    from app.utils.subscription_mirrors import get_subscription_mirrors, parse_mirror_origins

    entries = [f'Backup {i}|https://m{i}.example' for i in range(101)]
    with pytest.raises(ValueError):
        parse_mirror_origins(';'.join(entries), strict=True)
    assert len(parse_mirror_origins(';'.join(entries))) == 100
    with pytest.raises(ValueError):
        parse_mirror_origins('a' * 65 + '|https://mirror.example', strict=True)
    monkeypatch.setattr(settings, 'HIDE_SUBSCRIPTION_LINK', False)
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'link')
    monkeypatch.setattr(settings, 'SUBSCRIPTION_MIRROR_LINKS', ';'.join(entries[:2]))
    original = get_subscription_mirrors('https://sub.example/demo')
    monkeypatch.setattr(settings, 'SUBSCRIPTION_MIRROR_LINKS', ';'.join(reversed(entries[:2])))
    assert get_subscription_mirrors('https://sub.example/demo') == list(reversed(original))


def test_settings_editor_rejects_bad_config_without_raw_value():
    from app.services.system_settings_service import bot_configuration_service

    with pytest.raises(ValueError) as caught:
        bot_configuration_service.parse_user_value(
            'SUBSCRIPTION_MIRROR_LINKS', 'X|https://private:secret@mirror.example'
        )
    assert 'private:secret' not in str(caught.value)
    assert bot_configuration_service.parse_user_value('SUBSCRIPTION_MIRROR_LINKS', '') == ''


@pytest.mark.asyncio
async def test_setting_write_validates_before_db_access():
    from app.services.system_settings_service import bot_configuration_service

    db = AsyncMock()
    with pytest.raises(ValueError):
        await bot_configuration_service.set_value(db, 'SUBSCRIPTION_MIRROR_LINKS', 'X|http://mirror.example')
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cabinet_rejects_other_users_subscription(monkeypatch):
    from fastapi import HTTPException

    from app.cabinet.routes.subscription_modules import status
    from app.database.crud import subscription as crud

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(crud, 'get_subscription_by_id_for_user', lookup)
    user, db = SimpleNamespace(id=1), AsyncMock()
    with pytest.raises(HTTPException) as caught:
        await status.get_connection_link(user=user, db=db, subscription_id=42)
    assert caught.value.status_code == 404
    lookup.assert_awaited_once_with(db, 42, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode, hidden', [('link', True), ('happ_cryptolink', False), ('miniapp_custom', False)])
async def test_cabinet_does_not_offer_mirrors_in_protected_modes(monkeypatch, mode, hidden):
    from app.cabinet.routes.subscription_modules import status

    monkeypatch.setattr(settings, 'SUBSCRIPTION_MIRROR_LINKS', 'Backup|https://mirror.example')
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', mode)
    monkeypatch.setattr(settings, 'HIDE_SUBSCRIPTION_LINK', hidden)
    monkeypatch.setattr(
        status,
        'resolve_subscription',
        AsyncMock(
            return_value=SimpleNamespace(
                id=42,
                subscription_url='https://sub.example/demo',
                subscription_crypto_link=None,
            )
        ),
    )
    result = await status.get_connection_link(user=SimpleNamespace(id=1), db=AsyncMock(), subscription_id=42)
    assert result['mirrors'] == []
    if hidden:
        assert result['subscription_url'] is None
        assert result['display_link'] is None


@pytest.mark.parametrize('strict', [False, True])
def test_windows_line_endings_preserve_all_configured_mirrors(strict):
    from app.utils.subscription_mirrors import parse_mirror_origins

    assert parse_mirror_origins('First|https://first.example\r\nSecond|https://second.example\r\n', strict=strict) == [
        ('First', 'https://first.example'),
        ('Second', 'https://second.example'),
    ]
