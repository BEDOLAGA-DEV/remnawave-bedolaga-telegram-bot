from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TARIFFS = ROOT / 'app' / 'handlers' / 'admin' / 'tariffs.py'
CUSTOM = ROOT / 'app' / 'handlers' / 'admin' / 'tariff_custom_traffic.py'
STATES = ROOT / 'app' / 'states.py'


def _source(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _function_source(path: Path, name: str) -> str:
    source = _source(path)
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            assert node.end_lineno is not None
            return '\n'.join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f'function {name!r} not found in {path}')


def test_tariff_card_exposes_custom_traffic_entry() -> None:
    body = _function_source(TARIFFS, 'get_tariff_view_keyboard')
    assert '⚙️ Произвольный трафик' in body
    assert 'admin_tariff_edit_custom_traffic:' in body


def test_tariff_summary_includes_custom_traffic_block() -> None:
    body = _function_source(TARIFFS, 'format_tariff_info')
    assert 'format_custom_traffic_settings(tariff)' in body
    assert '<b>Произвольный трафик:</b>' in body


def test_custom_traffic_module_is_registered_from_tariff_router() -> None:
    body = _function_source(TARIFFS, 'register_handlers')
    assert 'register_custom_traffic_handlers(dp)' in body


def test_custom_traffic_screen_and_handlers_are_registered() -> None:
    body = _function_source(CUSTOM, 'register_custom_traffic_handlers')
    for callback_prefix in (
        'admin_tariff_edit_custom_traffic:',
        'admin_tariff_toggle_custom_traffic:',
        'admin_tariff_edit_custom_traffic_price:',
        'admin_tariff_edit_custom_traffic_min:',
        'admin_tariff_edit_custom_traffic_max:',
    ):
        assert callback_prefix in body

    for state_name in (
        'editing_tariff_custom_traffic_price',
        'editing_tariff_custom_traffic_min',
        'editing_tariff_custom_traffic_max',
    ):
        assert state_name in body


def test_generic_tariff_toggle_excludes_custom_traffic_callback() -> None:
    body = _function_source(TARIFFS, 'register_handlers')
    assert "~F.data.startswith('admin_tariff_toggle_custom_traffic:')" in body


def test_custom_traffic_fsm_states_exist() -> None:
    source = _source(STATES)
    for state_name in (
        'editing_tariff_custom_traffic_price = State()',
        'editing_tariff_custom_traffic_min = State()',
        'editing_tariff_custom_traffic_max = State()',
    ):
        assert state_name in source


def test_disable_path_updates_only_enabled_flag() -> None:
    body = _function_source(CUSTOM, 'toggle_custom_traffic')
    assert 'custom_traffic_enabled=False' in body
    disable_branch = body.split('if tariff.custom_traffic_enabled:', 1)[1].split('else:', 1)[0]
    assert 'traffic_price_per_gb_kopeks=' not in disable_branch
    assert 'min_traffic_gb=' not in disable_branch
    assert 'max_traffic_gb=' not in disable_branch


def test_field_handlers_use_existing_crud_boundary() -> None:
    source = _source(CUSTOM)
    for name in (
        'process_custom_traffic_price_input',
        'process_custom_traffic_min_input',
        'process_custom_traffic_max_input',
    ):
        body = _function_source(CUSTOM, name)
        assert 'update_tariff(' in body
        assert 'execute(' not in body
        assert 'UPDATE tariffs' not in body

    assert 'from app.services.tariff_custom_traffic import (' in source
