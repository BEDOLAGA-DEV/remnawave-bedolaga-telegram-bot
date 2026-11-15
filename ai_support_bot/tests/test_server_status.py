import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_support_bot.app.services import server_status as ss


SAMPLE_PATH = Path(__file__).parent / 'fixtures' / 'xray_metrics_sample.txt'
SAMPLE = SAMPLE_PATH.read_text(encoding='utf-8')

# 15.11.2026 11:05 UTC = 14:05 МСК
T0 = datetime(2026, 11, 15, 11, 5, tzinfo=UTC).timestamp()

FAKE_PASSWORD = 'pw-not-real-123'
FAKE_TOKEN = 'token-not-real-456'


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def clock(monkeypatch):
    clock = Clock(T0)
    monkeypatch.setattr(ss, '_now', clock)
    return clock


@pytest.fixture
def configured(monkeypatch, clock):
    ss.clear_cache()
    monkeypatch.setattr(ss.settings, 'SERVER_STATUS_ENABLED', True)
    monkeypatch.setattr(ss.settings, 'SERVER_STATUS_TTL', 120)
    monkeypatch.setattr(ss.settings, 'SERVER_STATUS_MAX_STALE', 1800)
    monkeypatch.setattr(ss.settings, 'XRAY_CHECKER_URL', 'https://checker.test')
    monkeypatch.setattr(ss.settings, 'XRAY_CHECKER_USERNAME', 'checker-user')
    monkeypatch.setattr(ss.settings, 'XRAY_CHECKER_PASSWORD', FAKE_PASSWORD)
    monkeypatch.setattr(ss.settings, 'REMNAWAVE_API_URL', '')
    monkeypatch.setattr(ss.settings, 'REMNAWAVE_API_TOKEN', '')
    yield
    ss.clear_cache()


def _metric_line(metric: str, name: str, value: float, stable_id: str = 'abc') -> str:
    escaped = name.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return (
        f'{metric}{{address="198.51.100.7:443",group_name="",name="{escaped}",protocol="vless",'
        f'stable_id="{stable_id}",sub_name="🦎 SligVPN │ 30 Дней"}} {value}'
    )


def _metrics(entries: list[tuple[str, int, float]]) -> str:
    lines = []
    for index, (name, status, latency) in enumerate(entries):
        lines.append(_metric_line('xray_proxy_latency_ms', name, latency, stable_id=f'id{index}'))
        lines.append(_metric_line('xray_proxy_status', name, status, stable_id=f'id{index}'))
    return '\n'.join(lines) + '\n'


def _sample_with(down: set[str]) -> str:
    """Образец, где у записей с этими сырыми именами статус 0."""
    out = []
    for line in SAMPLE.splitlines():
        match = re.search(r'[{,]name="([^"]*)"', line)
        if match and match.group(1) in down:
            line = re.sub(r' \d+(\.\d+)?$', ' 0', line)
        out.append(line)
    return '\n'.join(out)


def _status(records, nodes=None, fetched_at=T0, stale=False) -> ss.ServerStatus:
    return ss.ServerStatus(
        checked_at=fetched_at,
        proxies=ss.SourceSnapshot(items=tuple(records), fetched_at=fetched_at, stale=stale) if records is not None else None,
        nodes=ss.SourceSnapshot(items=tuple(nodes), fetched_at=fetched_at) if nodes is not None else None,
    )


def _line_for(block: str, label: str) -> str:
    for line in block.splitlines():
        if line.startswith(f'• {label}:'):
            return line
    raise AssertionError(f'нет строки для {label}:\n{block}')


# ---------------------------------------------------------------- разбор образца


def test_sample_is_parsed():
    records = ss.parse_xray_metrics(SAMPLE)
    by_name = {r.name: r for r in records}

    assert len(records) == 18
    assert by_name['🇩🇪 Германия ⚡'].ok is True
    assert by_name['🇩🇪 Германия ⚡'].latency_ms == 318
    assert by_name['🇺🇸 USA'].latency_ms == 8372
    assert all(r.ok for r in records)


def test_balancers_are_skipped():
    records = ss.parse_xray_metrics(SAMPLE)
    assert not [r for r in records if 'Авто выбор' in r.name]
    # «Авто выбор» в образце всегда 0 — без пропуска блок показывал бы сбой
    block = ss.format_status_block(_status(records), now=T0)
    assert 'Авто' not in block
    assert 'не отвечает' not in block


def test_prometheus_label_escaping():
    text = (
        'xray_proxy_status{name="🇩🇪 Сервер \\"Альфа\\" \\\\ {x}\\nдва",stable_id="s1"} 1\n'
        'xray_proxy_latency_ms{name="🇩🇪 Сервер \\"Альфа\\" \\\\ {x}\\nдва",stable_id="s1"} 120\n'
        'go_goroutines 12\n'
        '# HELP something else\n'
        'xray_proxy_status{name="broken} 1\n'
    )
    records = ss.parse_xray_metrics(text)

    assert len(records) == 1
    assert records[0].name == '🇩🇪 Сервер "Альфа" \\ {x}\nдва'
    assert records[0].latency_ms == 120
    assert ss.display_name(records[0].name) == 'Сервер "Альфа" \\ {x} два'


def test_latency_without_status_and_zero_latency():
    text = (
        'xray_proxy_latency_ms{name="🇫🇷 Франция",stable_id="a"} 140\n'
        'xray_proxy_latency_ms{name="🇫🇮 Финляндия",stable_id="b"} 0\n'
    )
    by_name = {r.name: r for r in ss.parse_xray_metrics(text)}
    assert by_name['🇫🇷 Франция'].ok is True
    assert by_name['🇫🇮 Финляндия'].ok is False
    assert by_name['🇫🇮 Финляндия'].latency_ms is None


# ---------------------------------------------------------------- группировка


def test_display_name_strips_emoji_and_spaces():
    assert ss.display_name('🇩🇪 Германия FFM ⚡') == 'Германия FFM'
    assert ss.display_name('🇪🇺 Белые списки #2 🏳️') == 'Белые списки #2'
    assert ss.display_name('🇷🇺   Москва  ') == 'Москва'
    assert ss.display_name('🇫🇮 Hysteria ⚡') == 'Hysteria'


def test_flag_to_country_code():
    assert ss.flag_country_code('🇯🇵 Токио') == 'JP'
    assert ss.flag_country_code('🇪🇺 Белые списки') == 'EU'
    assert ss.flag_country_code('Германия') is None


def test_grouping_of_sample():
    groups = dict(ss.group_proxies(ss.parse_xray_metrics(SAMPLE)))

    assert list(groups['Германия']) == ['Германия', 'Германия FFM']
    assert list(groups['Финляндия']) == ['Финляндия', 'Финляндия gRPC', 'Hysteria']
    assert list(groups['Россия']) == ['Москва', 'Россия · Банки', 'Россия · YouTube']
    assert list(groups['Белые списки']) == ['Белые списки', 'Белые списки #2']
    assert list(groups['США']) == ['США 2', 'USA']
    # одинаковые имена схлопнуты, но каждая запись посчитана
    assert groups['Финляндия']['Hysteria'].total == 2
    assert groups['Германия']['Германия'].total == 1

    order = [label for label, _ in ss.group_proxies(ss.parse_xray_metrics(SAMPLE))]
    assert order[-2:] == ['Россия', 'Белые списки']
    assert order[:3] == ['Германия', 'Латвия', 'Нидерланды']


def test_grouping_without_flag_uses_name_text():
    records = [
        ss.ProxyRecord('Германия Берлин', True),
        ss.ProxyRecord('Netherlands 2', True),
        ss.ProxyRecord('Москва резерв', True),
        ss.ProxyRecord('Белые списки LTE', True),
        ss.ProxyRecord('Резерв X', True),
        ss.ProxyRecord('🇪🇺 Европа', True),
    ]
    groups = dict(ss.group_proxies(records))

    assert list(groups['Германия']) == ['Германия Берлин']
    assert list(groups['Нидерланды']) == ['Netherlands 2']
    assert list(groups['Россия']) == ['Москва резерв']
    assert list(groups['Белые списки']) == ['Белые списки LTE']
    assert list(groups['Прочие']) == ['Резерв X']
    assert list(groups['Европа']) == ['Европа']


def test_block_for_healthy_sample():
    block = ss.format_status_block(_status(ss.parse_xray_metrics(SAMPLE)), now=T0)
    lines = block.splitlines()

    assert lines[0] == '<server_status>'
    assert lines[-1] == '</server_status>'
    assert 'на 15.11 14:05 МСК' in lines[1]
    assert 'других стран сейчас нет' in lines[1]
    assert _line_for(block, 'Германия') == '• Германия: Германия, Германия FFM — работают'
    assert _line_for(block, 'Франция') == '• Франция: Франция — работает'
    assert _line_for(block, 'Россия') == '• Россия: Москва, Россия · Банки, Россия · YouTube — работают'
    assert _line_for(block, 'Белые списки') == '• Белые списки: Белые списки, Белые списки #2 — работают'
    assert 'медленно отвечает: USA' in _line_for(block, 'США')
    assert 'мобильного оператора' in block
    assert len(block) < 1500


# ---------------------------------------------------------------- сбои


def test_partial_outage():
    text = _sample_with({'🇫🇷 Франция', '🇫🇮 Hysteria ', '🇫🇮 Hysteria ⚡', '🇩🇪 Германия ⚡'})
    block = ss.format_status_block(_status(ss.parse_xray_metrics(text)), now=T0)

    assert _line_for(block, 'Франция') == '• Франция: Франция — сейчас не отвечает'
    assert _line_for(block, 'Финляндия') == '• Финляндия: Финляндия, Финляндия gRPC, Hysteria — работают, кроме Hysteria'
    assert _line_for(block, 'Германия') == '• Германия: Германия, Германия FFM — работают, кроме Германия'
    assert _line_for(block, 'Россия').endswith('— работают')
    assert 'Ни один сервер' not in block


def test_one_of_collapsed_variants_down():
    text = _sample_with({'🇫🇮 Hysteria ⚡'})
    block = ss.format_status_block(_status(ss.parse_xray_metrics(text)), now=T0)
    assert _line_for(block, 'Финляндия').endswith('— работают, кроме Hysteria (не отвечает 1 из 2 вариантов)')

    single = [ss.ProxyRecord('🇩🇪 Германия ⚡', True), ss.ProxyRecord('🇩🇪 Германия', False)]
    block = ss.format_status_block(_status(single), now=T0)
    assert _line_for(block, 'Германия') == '• Германия: Германия — работает частично: не отвечает 1 из 2 вариантов'


def test_full_outage():
    names = set(re.findall(r'[{,]name="([^"]*)"', SAMPLE))
    assert '' not in names and len(names) == 20
    block = ss.format_status_block(_status(ss.parse_xray_metrics(_sample_with(names))), now=T0)

    assert _line_for(block, 'Франция') == '• Франция: Франция — сейчас не отвечает'
    assert _line_for(block, 'Финляндия').endswith('— сейчас не отвечают')
    assert 'работа' not in '\n'.join(line for line in block.splitlines() if line.startswith('•'))
    assert 'Ни один сервер не ответил мониторингу' in block


# ---------------------------------------------------------------- кеш, запас и сеть


@pytest.mark.asyncio
async def test_stale_fallback_after_error(configured, clock, monkeypatch):
    calls = {'n': 0}

    async def fetch():
        calls['n'] += 1
        if calls['n'] == 1:
            return SAMPLE
        raise ss.SourceError(f'connection reset https://checker-user:{FAKE_PASSWORD}@checker.test')

    monkeypatch.setattr(ss, '_fetch_xray_metrics', fetch)

    fresh = await ss.get_server_status()
    assert fresh.proxies is not None and not fresh.proxies.stale

    clock.value = T0 + 600
    stale = await ss.get_server_status(force=True)
    assert calls['n'] == 2
    assert stale.proxies is not None and stale.proxies.stale
    assert stale.proxies.fetched_at == T0

    block = ss.format_status_block(stale, now=clock.value)
    assert 'данные на 14:05 МСК, свежие пока недоступны' in block
    assert '• Германия:' in block

    # запас старше SERVER_STATUS_MAX_STALE больше не отдаётся
    clock.value = T0 + 1801
    assert await ss.build_status_block() == ''


@pytest.mark.asyncio
async def test_error_without_previous_data_gives_empty_block(configured, monkeypatch):
    async def fetch():
        raise RuntimeError('boom')

    monkeypatch.setattr(ss, '_fetch_xray_metrics', fetch)

    status = await ss.get_server_status()
    assert status is not None and status.empty
    assert await ss.build_status_block() == ''


@pytest.mark.asyncio
async def test_empty_metrics_count_as_failure(configured, monkeypatch):
    async def fetch():
        return '# HELP go_goroutines\ngo_goroutines 7\n'

    monkeypatch.setattr(ss, '_fetch_xray_metrics', fetch)
    assert await ss.build_status_block() == ''


@pytest.mark.asyncio
async def test_timeout_is_caught(configured, monkeypatch):
    monkeypatch.setattr(ss, 'SOURCE_TIMEOUT', 0.01)

    async def fetch():
        await asyncio.sleep(5)
        return SAMPLE

    monkeypatch.setattr(ss, '_fetch_xray_metrics', fetch)
    assert await ss.build_status_block() == ''


@pytest.mark.asyncio
async def test_cache_ttl_and_force(configured, clock, monkeypatch):
    calls = {'n': 0}

    async def fetch():
        calls['n'] += 1
        return SAMPLE

    monkeypatch.setattr(ss, '_fetch_xray_metrics', fetch)

    await ss.get_server_status()
    clock.value = T0 + 60
    await ss.get_server_status()
    assert calls['n'] == 1

    await ss.get_server_status(force=True)
    assert calls['n'] == 2

    clock.value = T0 + 60 + 121
    await ss.get_server_status()
    assert calls['n'] == 3


@pytest.mark.asyncio
async def test_parallel_requests_share_one_fetch(configured, monkeypatch):
    calls = {'n': 0}

    async def fetch():
        calls['n'] += 1
        await asyncio.sleep(0.05)
        return SAMPLE

    monkeypatch.setattr(ss, '_fetch_xray_metrics', fetch)

    results = await asyncio.gather(*(ss.get_server_status() for _ in range(5)))
    assert calls['n'] == 1
    assert all(result is results[0] for result in results)

    results = await asyncio.gather(*(ss.get_server_status(force=True) for _ in range(3)))
    assert calls['n'] == 2


@pytest.mark.asyncio
async def test_disabled_or_not_configured(configured, monkeypatch):
    async def fetch():
        raise AssertionError('не должно вызываться')

    monkeypatch.setattr(ss, '_fetch_xray_metrics', fetch)
    monkeypatch.setattr(ss, '_fetch_remnawave_nodes', fetch)

    monkeypatch.setattr(ss.settings, 'SERVER_STATUS_ENABLED', False)
    assert await ss.build_status_block() == ''
    assert await ss.get_server_status() is None

    monkeypatch.setattr(ss.settings, 'SERVER_STATUS_ENABLED', True)
    monkeypatch.setattr(ss.settings, 'XRAY_CHECKER_URL', '')
    assert await ss.build_status_block() == ''
    assert await ss.get_server_status() is None


def test_format_handles_missing_data():
    assert ss.format_status_block(None) == ''
    assert ss.format_status_block(ss.ServerStatus(checked_at=T0)) == ''
    assert ss.format_status_block(_status([]), now=T0) == ''


def test_error_description_hides_secrets(configured):
    error = RuntimeError(f'failed https://checker-user:{FAKE_PASSWORD}@checker.test/metrics pw={FAKE_PASSWORD}')
    text = ss._describe_error(error)
    assert FAKE_PASSWORD not in text
    assert 'checker-user' not in text


# ---------------------------------------------------------------- приватность


def test_block_has_no_addresses_or_internal_labels():
    nodes = ss.parse_remnawave_nodes(_nodes_payload())
    block = ss.format_status_block(_status(ss.parse_xray_metrics(SAMPLE), nodes=nodes), now=T0)

    stable_ids = re.findall(r'stable_id="([^"]+)"', SAMPLE)
    assert stable_ids
    for stable_id in stable_ids:
        assert stable_id not in block
    for forbidden in ('203.0.113', ':443', 'stable_id', 'sub_name', 'address', '26410', '│', 'Дней',
                      'vless', 'hysteria', 'shadowsocks', 'FI_02', 'DE_Bridge', '192.0.2', 'node-uuid'):
        assert forbidden not in block, forbidden
    assert not re.search(r'\d+\.\d+\.\d+\.\d+', block)


# ---------------------------------------------------------------- Remnawave


def _nodes_payload() -> dict:
    return {
        'response': [
            {'uuid': 'node-uuid-1', 'name': 'FI_02_H', 'address': '192.0.2.10', 'countryCode': 'FI',
             'isConnected': False, 'isDisabled': False, 'isConnecting': False, 'usersOnline': 0},
            {'uuid': 'node-uuid-2', 'name': 'FI_03', 'address': '192.0.2.11', 'countryCode': 'FI',
             'isConnected': True, 'isDisabled': False, 'usersOnline': 41},
            {'uuid': 'node-uuid-3', 'name': 'FI_04', 'address': '192.0.2.12', 'countryCode': 'fi',
             'isConnected': True, 'isDisabled': False, 'usersOnline': 3},
            {'uuid': 'node-uuid-4', 'name': 'DE_Bridge', 'address': '192.0.2.13', 'countryCode': 'DE',
             'isConnected': True, 'isDisabled': False, 'usersOnline': 12},
            {'uuid': 'node-uuid-5', 'name': 'NL_old', 'address': '192.0.2.14', 'countryCode': 'NL',
             'isConnected': False, 'isDisabled': True, 'usersOnline': 0},
        ]
    }


def test_remnawave_nodes_parsing():
    nodes = ss.parse_remnawave_nodes(_nodes_payload())

    assert len(nodes) == 4  # выключенная NL не считается
    assert {n.country_code for n in nodes} == {'FI', 'DE'}
    assert sum(1 for n in nodes if not n.connected) == 1
    # без обёртки response тоже разбирается
    assert len(ss.parse_remnawave_nodes(_nodes_payload()['response'])) == 4
    with pytest.raises(ss.SourceError):
        ss.parse_remnawave_nodes({'response': {'message': 'Unauthorized'}})


def test_remnawave_line_in_block():
    nodes = ss.parse_remnawave_nodes(_nodes_payload())
    block = ss.format_status_block(_status(ss.parse_xray_metrics(SAMPLE), nodes=nodes), now=T0)
    panel_line = next(line for line in block.splitlines() if line.startswith('По панели'))
    assert panel_line == 'По панели сейчас не на связи: Финляндия — 1 из 3 узлов.'

    healthy = [ss.NodeRecord('DE', True)]
    block = ss.format_status_block(_status(ss.parse_xray_metrics(SAMPLE), nodes=healthy), now=T0)
    assert 'По панели' not in block

    only_panel = ss.format_status_block(_status(None, nodes=nodes), now=T0)
    assert 'по панели на 15.11 14:05 МСК' in only_panel
    assert 'Финляндия — 1 из 3 узлов' in only_panel
    assert 'полный список' not in only_panel

    one = ss.format_status_block(_status(None, nodes=[ss.NodeRecord('DE', False)]), now=T0)
    assert 'Германия — 1 из 1 узла' in one


@pytest.mark.asyncio
async def test_remnawave_source_used_only_with_token(configured, monkeypatch):
    async def fetch_metrics():
        return SAMPLE

    calls = {'n': 0}

    async def fetch_nodes():
        calls['n'] += 1
        return _nodes_payload()

    monkeypatch.setattr(ss, '_fetch_xray_metrics', fetch_metrics)
    monkeypatch.setattr(ss, '_fetch_remnawave_nodes', fetch_nodes)

    status = await ss.get_server_status()
    assert status.nodes is None and calls['n'] == 0

    monkeypatch.setattr(ss.settings, 'REMNAWAVE_API_URL', 'https://panel.test:8443')
    monkeypatch.setattr(ss.settings, 'REMNAWAVE_API_TOKEN', FAKE_TOKEN)
    ss.clear_cache()
    block = await ss.build_status_block()
    assert calls['n'] == 1
    assert 'Финляндия — 1 из 3 узлов' in block
    assert FAKE_TOKEN not in block


@pytest.mark.asyncio
async def test_remnawave_failure_does_not_break_block(configured, monkeypatch):
    async def fetch_metrics():
        return SAMPLE

    async def fetch_nodes():
        raise ss.SourceError('Remnawave HTTP 401')

    monkeypatch.setattr(ss, '_fetch_xray_metrics', fetch_metrics)
    monkeypatch.setattr(ss, '_fetch_remnawave_nodes', fetch_nodes)
    monkeypatch.setattr(ss.settings, 'REMNAWAVE_API_URL', 'https://panel.test:8443')
    monkeypatch.setattr(ss.settings, 'REMNAWAVE_API_TOKEN', FAKE_TOKEN)

    block = await ss.build_status_block()
    assert '• Германия:' in block
    assert 'По панели' not in block


def test_local_panel_host_detection():
    assert ss._is_local_host('http://remnawave:3000')
    assert ss._is_local_host('https://10.0.0.5:3000')
    assert not ss._is_local_host('https://pugna.sligvpn.ru:8443')


# ---------------------------------------------------------------- нужен ли блок


@pytest.mark.parametrize('question', [
    'Добавьте Японию, пожалуйста',
    'а Казахстан есть?',
    'Есть сервер в Турции?',
    'хочу Польшу',
    'есть Великобритания или Англия?',
    'нужна Америка для работы',
    'есть ли Канада',
    'Индию добавите?',
    'есть ОАЭ?',
    'Грузия есть?',
    'армения нужна',
    'Сингапур или Гонконг',
    'а Корея?',
    'какие страны есть',
    'смените локацию',
    'мой регион блокирует',
    'какой у меня IP',
    'айпи не меняется',
    'не работает впн',
    'не подключается',
    'не грузит инстаграм',
    'сайт не открывается',
    'сервер упал?',
    'Германия лежит',
    'опять сбой?',
    'очень медленно',
    'какая скорость',
    'большой пинг',
    'всё тормозит',
    'белые списки работают?',
    'как обход работает',
    'у меня заглушки на мтс',
    'глушилки включили',
    'на LTE не работает',
    'мобильная сеть не тянет',
    'YouTube не идёт',
    'ютуб тормозит',
    'банк не открывается',
    'госуслуги не пускают',
    'добавить страну можно?',
    'не могу подключиться к Финляндии',
])
def test_status_relevant_positive(question):
    assert ss.status_relevant(question) is True


@pytest.mark.parametrize('question', [
    'как оплатить',
    'сколько стоит',
    'хочу продлить',
    'как оплатить банковской картой',
    'спасибо, всё понятно',
    'где мой промокод',
    'как загрузить чек',
    'установил английский язык',
    'это странно',
    'верните деньги',
])
def test_status_relevant_negative(question):
    assert ss.status_relevant(question) is False


def test_status_relevant_follow_up_uses_recent():
    assert ss.status_relevant('а сейчас?', recent='Германия не работает') is True
    assert ss.status_relevant('а сейчас?', recent='как оплатить') is False
    long_question = 'как оплатить подписку на год, если у меня карта другого банка нет, только СБП ' * 2
    assert ss.status_relevant(long_question.replace('банка', 'кредитки'), recent='Германия не работает') is False
