"""Точки России по федеральным округам и «Республики» — как чипы на сайте DPI//CHECKER."""

from app.services.dpichecker.regions import DISTRICTS, group_pops


POPS = [
    {'id': 1, 'location': 'russia', 'region': 'Москва', 'operator': None, 'is_healthy': True},
    {'id': 2, 'location': 'russia', 'region': 'Республика Татарстан', 'operator': 'MTS', 'is_healthy': True},
    {'id': 3, 'location': 'russia', 'region': 'Крым', 'operator': None, 'is_healthy': False},
    {'id': 4, 'location': 'russia', 'region': 'Неизвестный край', 'operator': None, 'is_healthy': True},
]


def test_russia_pops_grouped_by_district_and_republics():
    groups = group_pops('russia', POPS)
    by_code = {d['code']: d for d in groups['districts']}
    assert [d['code'] for d in groups['districts']] == ['CFD', 'NWFD', 'SFD', 'NCFD', 'PFD', 'UFD', 'SIBFD', 'FEFD']
    assert by_code['CFD'] == {'code': 'CFD', 'name': 'Центральный ФО', 'pop_ids': [1]}
    assert by_code['PFD']['pop_ids'] == [2]
    assert by_code['SFD']['pop_ids'] == [3]
    assert groups['republics'] == [2, 3]


def test_region_outside_map_is_not_lost_but_not_grouped():
    groups = group_pops('russia', POPS)
    grouped = {pid for d in groups['districts'] for pid in d['pop_ids']}
    assert 4 not in grouped


def test_every_region_belongs_to_one_district():
    regions = [region for _, _, members in DISTRICTS for region in members]
    assert len(regions) == len(set(regions)) == 87


def test_other_countries_have_no_groups():
    assert group_pops('china', [{'id': 9, 'region': 'Beijing'}]) == {'districts': [], 'republics': []}
