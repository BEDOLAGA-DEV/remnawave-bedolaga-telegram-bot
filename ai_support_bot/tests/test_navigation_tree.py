import os

import pytest
import pytest_asyncio

os.environ.setdefault('AISUP_DATABASE_URL', 'sqlite+aiosqlite:///./data/ai_support_test.db')
os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.navigation import registry
from ai_support_bot.app.navigation.builder import build_navigation_tree
from ai_support_bot.app.navigation.renderer import node_to_dict, render_node, render_overview
from ai_support_bot.app.navigation.search import search
from ai_support_bot.app.navigation.tool import build_prompt_block, lookup


@pytest_asyncio.fixture
async def tree():
    registry.reset()
    return await build_navigation_tree('ru')


@pytest.mark.asyncio
async def test_tree_is_built_with_localized_labels(tree):
    assert tree.size > 30
    assert 'blueprint' in tree.sources

    referral = tree.get('menu_referrals') or tree.get('referral')
    assert referral is not None
    assert referral.bot_label
    assert referral.bot_label != 'Партнёрская программа'


@pytest.mark.asyncio
async def test_every_node_is_reachable_from_roots(tree):
    walked = {node.id for node in tree.iter_nodes()}
    assert walked == set(tree.index)


@pytest.mark.asyncio
async def test_parent_links_are_consistent(tree):
    for node in tree.iter_nodes():
        for child in node.children:
            assert child.parent_id == node.id
        if node.parent_id is not None:
            assert node.parent_id in tree.index


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('question', 'expected'),
    [
        ('где вывести реферальный бонус', 'referral_withdrawal'),
        ('как пополнить баланс', 'balance_topup'),
        ('где посмотреть мои тикеты', 'support_my_tickets'),
        ('как сбросить трафик', 'subscription_reset_traffic'),
        ('где сменить язык интерфейса', 'info_language'),
        ('как докупить гб трафика', 'subscription_buy_traffic'),
        ('где вводить промокод', 'promocode'),
        ('как отключить автоплатеж', 'subscription_autopay'),
        ('где скидки за траты', 'info_promo_groups'),
        ('как перевыпустить ссылку подписки', 'subscription_revoke'),
    ],
)
async def test_search_finds_expected_section(tree, question, expected):
    matches = search(tree, question, limit=3)
    assert [match.node.id for match in matches]
    assert expected in [match.node.id for match in matches]


@pytest.mark.asyncio
async def test_search_returns_nothing_for_smalltalk(tree):
    assert search(tree, 'привет как дела', limit=3) == []
    assert search(tree, '', limit=3) == []


@pytest.mark.asyncio
async def test_search_ranks_specific_node_above_parent(tree):
    matches = search(tree, 'запросить вывод партнерских средств', limit=3)
    assert matches[0].node.id == 'referral_withdrawal'


@pytest.mark.asyncio
async def test_rendered_node_contains_both_surfaces(tree):
    node = tree.get('referral_withdrawal')
    rendered = render_node(tree, node)

    assert 'Бот:' in rendered
    assert 'Кабинет:' in rendered
    assert '/referral/withdrawal' in rendered
    assert node.bot_label in rendered


@pytest.mark.asyncio
async def test_web_only_node_is_marked_as_unavailable_in_bot(tree):
    node = tree.get('referral_partner')
    rendered = render_node(tree, node)

    assert 'Бот: недоступно' in rendered
    assert '/referral/partner' in rendered


@pytest.mark.asyncio
async def test_overview_lists_top_level_sections(tree):
    overview = render_overview(tree)
    assert 'Верхний уровень навигации' in overview
    assert overview.count('•') >= 5


@pytest.mark.asyncio
async def test_node_to_dict_is_serializable(tree):
    payload = node_to_dict(tree, tree.get('support'), depth=1)
    assert payload['id'] == 'support'
    assert payload['web_path'] == '/support'
    assert isinstance(payload['children'], list)


@pytest.mark.asyncio
async def test_lookup_returns_only_relevant_subtree():
    registry.reset()
    result = await lookup('где запросить вывод реферального бонуса', language='ru', limit=2)

    assert result['found'] is True
    assert result['nodes'][0]['id'] == 'referral_withdrawal'
    assert 'Подписка' not in result['text']
    assert len(result['text']) < 3000


@pytest.mark.asyncio
async def test_lookup_falls_back_to_overview_for_unknown_topic():
    registry.reset()
    result = await lookup('погода в москве завтра', language='ru', limit=2)

    assert result['found'] is False
    assert 'Верхний уровень навигации' in result['text']


@pytest.mark.asyncio
async def test_prompt_block_is_truncated_to_limit():
    registry.reset()
    block = await build_prompt_block('подписка', language='ru', limit=3, max_chars=200)

    assert block
    assert len(block) < 400


@pytest.mark.asyncio
async def test_prompt_block_is_empty_for_smalltalk():
    registry.reset()
    assert await build_prompt_block('спасибо', language='ru') == ''


@pytest.mark.asyncio
async def test_registry_caches_tree_and_refreshes_on_demand():
    registry.reset()
    first = await registry.get_tree('ru')
    second = await registry.get_tree('ru', ttl_seconds=3600)
    assert first is second

    third = await registry.refresh('ru')
    assert third is not first
    assert registry.is_ready('ru')
    assert registry.stats()['ru']['nodes'] == third.size
