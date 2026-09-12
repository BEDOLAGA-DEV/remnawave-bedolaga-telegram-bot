from types import SimpleNamespace

from app.utils.premium_traffic import (
    BYTES_IN_GB,
    PremiumSquadConfig,
    get_premium_squads_for_tariff,
    parse_premium_squad,
    parse_premium_squads,
)


SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'


def test_actual_form_is_parsed():
    config = parse_premium_squad(SQUAD, {'traffic_limit_gb': 5})

    assert config == PremiumSquadConfig(squad_uuid=SQUAD, limit_gb=5)
    assert config.limit_bytes == 5 * BYTES_IN_GB


def test_early_form_with_bare_number_is_still_supported():
    """`{"uuid": 5}` писался до появления вложенного словаря."""
    config = parse_premium_squad(SQUAD, 5)

    assert config is not None
    assert config.limit_gb == 5
    assert config.topup_enabled is False


def test_zero_limit_means_no_premium():
    """Ноль по договорённости поля — «брать общий лимит тарифа»."""
    assert parse_premium_squad(SQUAD, {'traffic_limit_gb': 0}) is None
    assert parse_premium_squad(SQUAD, 0) is None


def test_dict_without_limit_key_is_not_premium():
    assert parse_premium_squad(SQUAD, {'topup_enabled': True}) is None


def test_garbage_does_not_break_parsing():
    assert parse_premium_squad(SQUAD, 'пять') is None
    assert parse_premium_squad(SQUAD, {'traffic_limit_gb': 'пять'}) is None
    assert parse_premium_squad(SQUAD, {'traffic_limit_gb': -5}) is None
    assert parse_premium_squad(SQUAD, None) is None


def test_custom_name_is_parsed():
    """Когда премиум-серверов несколько, без названия строки неразличимы."""
    config = parse_premium_squad(SQUAD, {'traffic_limit_gb': 5, 'name': 'Мобильный резерв'})

    assert config.name == 'Мобильный резерв'


def test_blank_name_falls_back_to_none():
    """Пустая строка перекрыла бы подстановку имени сервера."""
    assert parse_premium_squad(SQUAD, {'traffic_limit_gb': 5, 'name': '   '}).name is None
    assert parse_premium_squad(SQUAD, {'traffic_limit_gb': 5, 'name': 42}).name is None
    assert parse_premium_squad(SQUAD, {'traffic_limit_gb': 5}).name is None


def test_topup_settings_are_parsed():
    config = parse_premium_squad(
        SQUAD,
        {
            'traffic_limit_gb': 5,
            'topup_enabled': True,
            'topup_packages': {'1': 500, '5': 2000},
            'max_topup_gb': 20,
        },
    )

    assert config.topup_enabled is True
    assert config.topup_packages == {1: 500, 5: 2000}
    assert config.max_topup_gb == 20
    assert config.price_kopeks_for(5) == 2000
    assert config.price_kopeks_for(3) is None
    assert config.available_packages() == [(1, 500), (5, 2000)]


def test_topup_without_packages_counts_as_disabled():
    """Иначе интерфейс показал бы кнопку «купить» с пустым списком."""
    config = parse_premium_squad(SQUAD, {'traffic_limit_gb': 5, 'topup_enabled': True})

    assert config.topup_enabled is False
    assert config.available_packages() == []


def test_disabled_topup_sells_nothing():
    config = parse_premium_squad(SQUAD, {'traffic_limit_gb': 5, 'topup_enabled': False, 'topup_packages': {'1': 500}})

    assert config.price_kopeks_for(1) is None
    assert config.available_packages() == []


def test_broken_packages_are_skipped_but_valid_ones_survive():
    config = parse_premium_squad(
        SQUAD,
        {
            'traffic_limit_gb': 5,
            'topup_enabled': True,
            'topup_packages': {'1': 500, 'много': 100, '0': 300, '10': -1, '20': 0},
        },
    )

    # Нулевая цена — осознанная настройка админа, её оставляем.
    assert config.topup_packages == {1: 500, 20: 0}


def test_whole_map_is_parsed_and_non_premium_squads_dropped():
    squads = parse_premium_squads(
        {
            SQUAD: {'traffic_limit_gb': 5},
            'other-squad': {'traffic_limit_gb': 0},
            'legacy-squad': 10,
            '': {'traffic_limit_gb': 7},
        }
    )

    assert set(squads) == {SQUAD, 'legacy-squad'}
    assert squads['legacy-squad'].limit_gb == 10


def test_order_follows_sort_order():
    """Порядок разбора определяет, как строки лягут в карточке пользователя."""
    squads = parse_premium_squads(
        {
            'squad-b': {'traffic_limit_gb': 5, 'sort_order': 1},
            'squad-a': {'traffic_limit_gb': 5, 'sort_order': 0},
        }
    )

    assert list(squads) == ['squad-a', 'squad-b']


def test_order_is_stable_when_not_configured():
    """Иначе строки шли бы в порядке ключей JSON — как когда-то добавляли серверы."""
    limits = {'zzz': {'traffic_limit_gb': 5}, 'aaa': {'traffic_limit_gb': 5}}

    assert list(parse_premium_squads(limits)) == ['aaa', 'zzz']
    # Тот же результат при другом порядке ключей на входе.
    assert list(parse_premium_squads(dict(reversed(list(limits.items()))))) == ['aaa', 'zzz']


def test_sort_order_is_parsed_and_defaults_to_zero():
    assert parse_premium_squad(SQUAD, {'traffic_limit_gb': 5, 'sort_order': 3}).sort_order == 3
    assert parse_premium_squad(SQUAD, {'traffic_limit_gb': 5}).sort_order == 0
    assert parse_premium_squad(SQUAD, {'traffic_limit_gb': 5, 'sort_order': 'два'}).sort_order == 0


def test_empty_and_broken_input_gives_empty_map():
    assert parse_premium_squads(None) == {}
    assert parse_premium_squads({}) == {}
    assert parse_premium_squads([]) == {}


def test_tariff_helper_tolerates_missing_tariff():
    assert get_premium_squads_for_tariff(None) == {}
    assert get_premium_squads_for_tariff(SimpleNamespace()) == {}
    assert get_premium_squads_for_tariff(SimpleNamespace(server_traffic_limits=None)) == {}


def test_tariff_helper_reads_the_tariff_field():
    tariff = SimpleNamespace(server_traffic_limits={SQUAD: {'traffic_limit_gb': 5}})

    squads = get_premium_squads_for_tariff(tariff)

    assert list(squads) == [SQUAD]
    assert squads[SQUAD].limit_bytes == 5 * BYTES_IN_GB
