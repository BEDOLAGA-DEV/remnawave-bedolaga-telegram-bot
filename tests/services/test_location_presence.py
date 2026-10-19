from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.services.location_presence import (
    Device,
    Presence,
    build_location_cards,
    devices_from_report,
    merge_presence,
)
from tests.fixtures.locations import (
    OVERRIDES,
    SKIP_MARKERS,
    TAG_AUTO,
    TAG_DE,
    sample_hosts,
    sample_nodes,
)


NOW = datetime(2026, 10, 19, 7, 5, tzinfo=UTC)


def dev(u, d, sec_ago, node, inbound, exit=None):
    return Device(u, d, NOW - timedelta(seconds=sec_ago), node, inbound, exit)


def test_devices_from_report_parses_utc_and_node():
    report = {
        'node': 'HUB',
        'generated_at': '2026-10-19T07:40:00Z',
        'window_seconds': 300,
        'devices': [
            {
                'u': 'aaaa',
                'd': 'bbbb',
                't': '2026-10-19T07:39:12Z',
                'inbound': 'vless-auto',
                'exit': 'FI2',
            },
            {'u': 'cccc', 'd': 'dddd', 't': '2026-10-19T07:39:50Z', 'inbound': 'vless-de', 'exit': None},
        ],
    }
    devices = devices_from_report(report)
    assert devices == [
        Device(
            'aaaa',
            'bbbb',
            datetime(2026, 10, 19, 7, 39, 12, tzinfo=UTC),
            'HUB',
            'vless-auto',
            'FI2',
        ),
        Device('cccc', 'dddd', datetime(2026, 10, 19, 7, 39, 50, tzinfo=UTC), 'HUB', 'vless-de', None),
    ]
    assert devices[0].last_seen.utcoffset() == timedelta(0)


def test_latest_device_wins_across_nodes():
    p = merge_presence(
        [dev('u1', 'd1', 100, 'NL-A', 'vless-nl'), dev('u1', 'd1', 10, 'HUB', 'vless-de')],
        NOW,
    )
    assert p == {('HUB', 'vless-de'): Presence(1, {})}


def test_stale_devices_dropped():
    assert merge_presence([dev('u1', 'd1', 301, 'HUB', 'a')], NOW) == {}


def test_users_unique_and_exits_counted():
    p = merge_presence(
        [
            dev('u1', 'd1', 5, 'HUB', 'vless-auto', 'FI2'),
            dev('u1', 'd2', 5, 'HUB', 'vless-auto', 'FI2'),
            dev('u2', 'd3', 5, 'HUB', 'vless-auto', 'FI1'),
            dev('u3', 'd4', 5, 'HUB', 'vless-auto', None),
        ],
        NOW,
    )
    assert p[('HUB', 'vless-auto')] == Presence(3, {'FI2': 1, 'FI1': 1})


def test_location_host_node_overrides_parsing():
    parsed = Settings(BOT_TOKEN='t', LOCATION_HOST_NODE_OVERRIDES='ru-b.example.com=RU-B, X.ORG = N')
    assert parsed.get_location_host_node_overrides() == {'ru-b.example.com': 'RU-B', 'x.org': 'N'}
    assert Settings(BOT_TOKEN='t', LOCATION_HOST_NODE_OVERRIDES='').get_location_host_node_overrides() == {}


def test_location_exit_breakdown_inbounds_parsing():
    assert Settings(BOT_TOKEN='t').get_location_exit_breakdown_inbounds() == set()
    parsed = Settings(BOT_TOKEN='t', LOCATION_EXIT_BREAKDOWN_INBOUNDS=' a , b,,a ')
    assert parsed.get_location_exit_breakdown_inbounds() == {'a', 'b'}
    assert Settings(BOT_TOKEN='t', LOCATION_EXIT_BREAKDOWN_INBOUNDS='').get_location_exit_breakdown_inbounds() == set()


def test_location_skip_host_markers_parsing():
    assert Settings(BOT_TOKEN='t').get_location_skip_host_markers() == ()
    parsed = Settings(BOT_TOKEN='t', LOCATION_SKIP_HOST_MARKERS=' 👇 ,, ---')
    assert parsed.get_location_skip_host_markers() == ('👇', '---')


def _cards(
    presence=None,
    reports_at=None,
    overrides=OVERRIDES,
    exit_breakdown_inbounds=frozenset({TAG_AUTO}),
    skip_host_markers=(SKIP_MARKERS,),
):
    return build_location_cards(
        sample_hosts(),
        sample_nodes(),
        presence or {},
        reports_at or {},
        overrides,
        set(exit_breakdown_inbounds),
        skip_host_markers,
    )


def _by_title(cards):
    return {c.title: c for c in cards}


def test_cards_order_filters_and_mapping():
    cards = _cards()
    assert [c.title for c in cards] == [
        '🇪🇺 Europe (AUTO)',
        '🇳🇱 Netherlands',
        '🇩🇪 Germany',
        '🇷🇺 Russia #1',
        '🇷🇺 Russia #2',
        '🇳🇱 Netherlands (ads)',
        '🇸🇪 Sweden',
    ]
    by_title = _by_title(cards)
    assert by_title['🇷🇺 Russia #1'].node_name == 'RU-A'
    assert by_title['🇷🇺 Russia #2'].node_name == 'RU-B'
    assert by_title['🇸🇪 Sweden'].node_name == 'SE-A'


def test_separator_without_skip_markers_takes_the_card():
    # Без маркеров разделитель — обычный хост: он раньше в подписке и забирает карточку своей пары.
    titles = [c.title for c in _cards(skip_host_markers=())]
    assert '👇 Gaming 👇' in titles
    assert '🇷🇺 Russia #1' not in titles


def test_counter_source_with_zero():
    reported = datetime(2026, 10, 19, 7, 4, tzinfo=UTC)
    cards = _by_title(
        _cards(
            presence={('HUB', TAG_AUTO): Presence(5, {'FI2': 3})},
            reports_at={'HUB': reported},
        )
    )
    auto = cards['🇪🇺 Europe (AUTO)']
    assert (auto.users, auto.exits, auto.source, auto.reported_at) == (5, [('FI2', 3)], 'counter', reported)
    assert auto.inbound_tag == TAG_AUTO
    germany = cards['🇩🇪 Germany']
    assert (germany.users, germany.source, germany.inbound_tag) == (0, 'counter', TAG_DE)


def test_exits_sorted_by_count_descending():
    cards = _by_title(
        _cards(
            presence={('HUB', TAG_AUTO): Presence(6, {'FI1': 1, 'UK1': 4, 'FI2': 1})},
            reports_at={'HUB': NOW},
        )
    )
    assert cards['🇪🇺 Europe (AUTO)'].exits[0] == ('UK1', 4)


def test_panel_fallback_single_location():
    cards = _by_title(_cards())
    assert (cards['🇸🇪 Sweden'].users, cards['🇸🇪 Sweden'].source) == (8, 'panel')
    assert (cards['🇷🇺 Russia #1'].users, cards['🇷🇺 Russia #1'].source) == (1, 'panel')


def test_no_data_for_multi_location_node_without_report():
    cards = _by_title(_cards())
    for title in ('🇳🇱 Netherlands', '🇳🇱 Netherlands (ads)', '🇪🇺 Europe (AUTO)', '🇩🇪 Germany'):
        assert (cards[title].users, cards[title].source) == (None, None), title


def test_ambiguous_host_without_override():
    card = _by_title(_cards(overrides={}))['🇷🇺 Russia #2']
    assert (card.node_name, card.users) == (None, None)


def test_exit_breakdown_only_for_configured_inbounds():
    cards = _by_title(
        _cards(
            presence={
                ('HUB', TAG_AUTO): Presence(2, {'FI2': 2}),
                ('HUB', TAG_DE): Presence(5, {'DE1': 5}),
            },
            reports_at={'HUB': NOW},
        )
    )
    assert cards['🇪🇺 Europe (AUTO)'].exits == [('FI2', 2)]
    germany = cards['🇩🇪 Germany']
    assert (germany.users, germany.exits) == (5, [])


def test_exit_breakdown_disabled_when_setting_empty():
    cards = _by_title(
        _cards(
            presence={('HUB', TAG_AUTO): Presence(2, {'FI2': 2})},
            reports_at={'HUB': NOW},
            exit_breakdown_inbounds=set(),
        )
    )
    assert cards['🇪🇺 Europe (AUTO)'].exits == []
