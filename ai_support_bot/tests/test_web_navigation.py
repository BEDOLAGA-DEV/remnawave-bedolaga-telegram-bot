import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ai_support_bot.app.navigation import registry, web
from ai_support_bot.app.navigation.builder import build_navigation_tree
from ai_support_bot.app.navigation.renderer import render_node
from ai_support_bot.app.navigation.tool import build_prompt_block, lookup, resolve_surface


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_web_routes_match_frontend_router():
    app = (PROJECT_ROOT / 'bedolaga-cabinet/src/App.tsx').read_text()
    routes = set(re.findall(r'\bpath="([^"]+)"', app))
    for entry in web.WEB_ENTRIES:
        assert urlsplit(entry.route).path in routes, entry.id
        if entry.link_route:
            assert entry.link_route in routes, entry.id


def test_web_translation_keys_and_packaged_fallback_match_frontend():
    locale: dict[str, str] = {}
    payload = json.loads((PROJECT_ROOT / 'bedolaga-cabinet/src/locales/ru.json').read_text())
    web._flatten(payload, '', locale)
    fallback = json.loads(Path(web.__file__).with_name('web_labels_ru.json').read_text())
    for entry in web.WEB_ENTRIES:
        for key in entry.labels:
            if key.startswith('['):
                continue
            assert key in locale, (entry.id, key)
            assert fallback[key] == locale[key]


def test_missing_frontend_assets_still_provide_readable_labels(monkeypatch, tmp_path):
    monkeypatch.setattr(web, 'WEB_LOCALES_DIR', tmp_path)
    locale = web.load_web_locale('en')
    assert locale['nav.profile'] == 'Профиль'
    assert locale['balance.savedCards.title'] == 'Сохранённые карты'


@pytest.mark.asyncio
async def test_web_labels_use_requested_frontend_language():
    tree = await build_navigation_tree('en')
    locale = web.load_web_locale('en')
    assert tree.get('web_accounts').web_label == locale['profile.accounts.goToAccounts']
    assert tree.get('balance_topup').web_steps[0] == locale['nav.balance']


@pytest.mark.parametrize(
    ('query', 'surface'),
    [
        ('как привязать почту на сайте', 'web'),
        ('где это в Telegram', 'bot'),
        ('как в боте и на сайте', None),
        ('как продлить', None),
        ('ответ: в боте\nа на сайте?', 'web'),
    ],
)
def test_surface_uses_latest_explicit_channel(query, surface):
    assert resolve_surface(query) == surface


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('query', 'expected'),
    [
        ('где привязанные аккаунты на сайте', 'web_accounts'),
        ('как изменить email в кабинете', 'web_change_email'),
        ('где история операций на сайте', 'balance_history'),
        ('как активировать промокод на сайте', 'promocode'),
        ('где сохраненные карты в кабинете', 'saved_cards'),
        ('как отключить автоплатеж на сайте', 'subscription_autopay'),
        ('как удалить устройство на сайте', 'subscription_reset_device'),
        ('как активировать подарок на сайте', 'web_activate_gift'),
        ('где мои обращения в кабинете', 'web_tickets'),
        ('где последние вращения на сайте', 'web_wheel_history'),
    ],
)
async def test_web_lookup_finds_specific_action(query, expected):
    registry.reset()
    result = await lookup(query)
    assert expected in [node['id'] for node in result['nodes']]
    assert all(node['web_path'] for node in result['nodes'])


@pytest.mark.asyncio
async def test_bot_lookup_excludes_web_only_actions():
    registry.reset()
    result = await lookup('где мои тикеты в боте')
    assert all(node['bot_path'] for node in result['nodes'])
    assert 'web_tickets' not in [node['id'] for node in result['nodes']]


@pytest.mark.asyncio
async def test_web_promocode_is_on_balance_and_not_a_fictional_route():
    tree = await build_navigation_tree('ru')
    node = tree.get('promocode')
    assert node.web_steps == ('Баланс', 'Промокод', 'Активировать')
    assert node.web_path == '/balance'
    assert node.available_in_bot


@pytest.mark.asyncio
async def test_inline_autopay_path_and_conditions_survive_prompt_budget():
    registry.reset()
    block = await build_prompt_block('как отключить автоплатеж на сайте')
    assert '→ Все настройки подписки → Автопродление' not in block
    assert 'Автопродление' in block
    assert 'прямо на карточке' in block
    assert ':subscriptionId' not in block


@pytest.mark.asyncio
async def test_web_overview_contains_core_sections_and_conditions():
    registry.reset()
    block = await build_prompt_block('какие разделы есть на сайте')
    for label in ('Подписка', 'Баланс', 'Профиль', 'Поддержка', 'Рефералы', 'Информация', 'Подарить'):
        assert label in block
    assert 'Некоторые разделы' in block
    assert 'https://web.slig.app' in block


@pytest.mark.asyncio
async def test_qr_link_opens_connection_page_instead_of_missing_router_state():
    tree = await build_navigation_tree('ru')
    rendered = render_node(tree, tree.get('web_connection_qr'), depth=0)
    assert '(https://web.slig.app/connection)' in rendered
    assert '(https://web.slig.app/connection/qr)' not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('node_id', 'component'),
    [
        ('subscription_autopay', 'AutopayToggle'),
        ('subscription_manage_devices', 'DevicesPanel'),
        ('subscription_revoke', 'ReissueLinkButton'),
        ('web_pause', 'DailyPausePanel'),
    ],
)
async def test_lite_inline_controls_do_not_require_full_settings(node_id, component):
    source = (PROJECT_ROOT / 'bedolaga-cabinet/src/pages/SubscriptionLite.tsx').read_text()
    assert re.search(rf'<{component}\b', source)
    tree = await build_navigation_tree('ru')
    node = tree.get(node_id)
    assert 'Все настройки подписки' not in node.web_steps
    assert '?full=1' not in node.web_path


@pytest.mark.asyncio
@pytest.mark.parametrize('query', [
    'Где на сайте найти выигранный код колеса?',
    'Как продлить подписку на сайте?',
    'Как изменить email на сайте?',
    'Где пополнить баланс на сайте?',
])
async def test_public_navigation_never_copies_internal_hints(query, monkeypatch):
    from ai_support_bot.app.navigation import tool

    tree = await build_navigation_tree('ru')
    for node in tree.iter_nodes():
        node.web_hint = 'INTERNAL_ONLY: не обещай и не предлагай'

    async def resolve(*args):
        return tree

    monkeypatch.setattr(tool, '_resolve_tree', resolve)
    answer = await tool.answer_request(query)
    assert 'https://web.slig.app' in answer
    assert 'INTERNAL_ONLY' not in answer
    assert 'не обещай' not in answer
    assert 'не предлагай' not in answer
