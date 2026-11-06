"""Admin tariff screens retain payloads while localizing labels and validation."""

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.handlers.admin import tariff_custom_days, tariff_custom_traffic, tariff_panel_settings, tariff_server_limits
from app.handlers.admin.tariffs import format_tariff_info, get_tariff_view_keyboard, get_traffic_reset_mode_keyboard
from app.utils.formatting import format_period, format_traffic


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']


def _tariff():
    return SimpleNamespace(
        id=7,
        name='Example <plan>',
        description=None,
        is_active=True,
        is_trial_available=True,
        trial_duration_days=7,
        traffic_limit_gb=100,
        device_limit=2,
        max_device_limit=None,
        device_price_kopeks=None,
        tier_level=1,
        display_order=0,
        period_prices={'30': 9900},
        highlight_period_days=30,
        allowed_squads=[],
        allowed_promo_groups=[],
        server_traffic_limits={},
        traffic_topup_enabled=False,
        allow_traffic_topup=True,
        traffic_reset_mode=None,
        is_daily=False,
        daily_price_kopeks=0,
        custom_traffic_enabled=False,
        traffic_price_per_gb_kopeks=None,
        min_traffic_gb=None,
        max_traffic_gb=None,
        custom_days_enabled=False,
        price_per_day_kopeks=None,
        min_days=None,
        max_days=None,
        panel_tag=None,
        external_squad_uuid=None,
        lava_product_id=None,
        show_in_gift=True,
    )


@pytest.mark.parametrize('language', LANGUAGES)
def test_all_tariff_screens_localize_and_keep_callback_contract(language):
    tariff = _tariff()
    screens = [
        format_tariff_info(tariff, language),
        tariff_custom_days.render_custom_days_settings(tariff, language),
        tariff_custom_traffic.render_custom_traffic_settings(tariff, language),
        tariff_panel_settings.render_panel_settings(tariff, language),
        tariff_server_limits.render_server_limits(tariff, language),
    ]
    keyboard = get_tariff_view_keyboard(tariff, language)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == [
        button.callback_data for row in get_tariff_view_keyboard(tariff, 'ru').inline_keyboard for button in row
    ]
    reset = get_traffic_reset_mode_keyboard(7, 'MONTH', language)
    assert any(
        button.callback_data == 'admin_tariff_set_reset_mode:7:MONTH' for row in reset.inline_keyboard for button in row
    )
    for screen in screens:
        assert 'Example &lt;plan&gt;' in screen
        assert 'ADMIN_TARIFF_' not in screen
    all_text = '\n'.join(
        screens + [button.text for button in buttons] + [b.text for row in reset.inline_keyboard for b in row]
    )
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', all_text) is None, all_text


@pytest.mark.parametrize('module', [tariff_custom_days, tariff_custom_traffic])
@pytest.mark.parametrize('language', ['en', 'zh', 'fa'])
def test_closed_validation_vocabulary_is_localized(module, language):
    for error in module._VALIDATION_ERROR_KEYS:
        assert re.search('[А-Яа-яЁё]', module._localized_validation_error(error, language)) is None
    assert module._localized_validation_error('unknown validation detail', language) == 'unknown validation detail'


@pytest.mark.parametrize(
    ('language', 'values'),
    [
        ('ru', ['1 день', '2 дня', '5 дней', '21 день']),
        ('ua', ['1 день', '2 дні', '5 днів', '21 день']),
        ('en', ['1 day', '2 days', '5 days', '21 days']),
        ('zh', ['1 天', '2 天', '5 天', '21 天']),
        ('fa', ['1 روز', '2 روز', '5 روز', '21 روز']),
    ],
)
def test_format_period_uses_language_plural_rules(language, values):
    assert [format_period(count, language) for count in (1, 2, 5, 21)] == values


def test_shared_formatters_keep_russian_defaults():
    assert format_traffic(0) == 'Безлимит'
    assert format_traffic(12) == '12 ГБ'
    assert format_period(11) == '11 дней'
    assert format_period(22) == '22 дня'


def test_tariff_and_promo_ui_has_no_untranslated_cyrillic_literals():
    root = Path(__file__).resolve().parents[2] / 'app' / 'handlers' / 'admin'
    files = [
        'tariffs.py',
        'promo_groups.py',
        'tariff_custom_days.py',
        'tariff_custom_traffic.py',
        'tariff_panel_settings.py',
        'tariff_server_limits.py',
    ]
    violations = []
    for filename in files:
        tree = ast.parse((root / filename).read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Constant)
                or not isinstance(node.value, str)
                or not re.search('[А-Яа-яЁё]', node.value)
            ):
                continue
            if isinstance(parents.get(node), ast.Expr):
                continue  # Documentation.
            ancestors = []
            parent = parents.get(node)
            while parent:
                ancestors.append(parent)
                parent = parents.get(parent)
            if any(
                isinstance(a, ast.Call)
                and (
                    (isinstance(a.func, ast.Attribute) and a.func.attr == 't')
                    or ast.unparse(a.func).startswith('logger.')
                    or ast.unparse(a.func)
                    in {'_prompt_for_discount', '_prompt_for_period_discounts', '_prompt_for_auto_assign_threshold'}
                )
                for a in ancestors
            ):
                continue
            if any(
                isinstance(a, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == '_VALIDATION_ERROR_KEYS' for target in a.targets)
                for a in ancestors
            ):
                continue  # Closed identifiers returned by validators, never shown directly.
            if node.value == 'нет' and isinstance(parents.get(node), ast.Set):
                continue  # Accepted input alias, not output.
            violations.append(f'{filename}:{node.lineno}: {node.value}')
    assert violations == []


@pytest.mark.parametrize(
    ('language', 'month', 'days'),
    [
        ('ru', '1 месяц', '7 дней'),
        ('en', '1 month', '7 days'),
        ('ua', '1 місяць', '7 днів'),
        ('zh', '1 个月', '7 天'),
        ('fa', '1 ماه', '7 روز'),
    ],
)
def test_promo_group_discount_periods_do_not_fall_back_to_another_language(language, month, days):
    from app.handlers.admin.promo_groups import _format_period_discounts_lines
    from app.localization.texts import get_texts
    from app.utils.pricing_utils import format_period_description

    assert format_period_description(30, language) == month
    assert format_period_description(7, language) == days
    group = SimpleNamespace(period_discounts={'30': 10, '7': 5}, is_default=False)
    result = '\n'.join(_format_period_discounts_lines(get_texts(language), group, language))
    assert month in result and days in result
    assert '10%' in result and '5%' in result
