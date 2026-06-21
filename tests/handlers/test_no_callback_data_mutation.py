import pathlib


def test_no_callback_data_assignment_in_tariff_purchase():
    """aiogram v3 CallbackQuery is frozen — assigning `callback.data = ...` raises
    pydantic ValidationError (frozen_instance). Handlers must pass tariff_id/period
    explicitly instead of mutating the callback. Guard against re-introduction.
    """
    src = pathlib.Path('app/handlers/subscription/tariff_purchase.py').read_text(encoding='utf-8')
    assert 'callback.data =' not in src, (
        'Do not mutate frozen CallbackQuery.data; pass tariff_id/period explicitly.'
    )
