"""Lava: левая часть — имя метода, правая («через …») — имя провайдера.

Регрессия: для lava_sbp/lava_card в description подставлялось имя самого метода
(«1 Способ - через 1 Способ»), а не провайдера (LAVA_DISPLAY_NAME).
"""

from app.config import settings
from app.utils.payment_utils import get_available_payment_methods


def _method(methods, method_id):
    return next((m for m in methods if m['id'] == method_id), None)


def _enable_lava(monkeypatch, **overrides):
    base = {
        'LAVA_ENABLED': True,
        'LAVA_SHOP_ID': 'shop',
        'LAVA_SECRET_KEY': 'secret',
        'LAVA_WEBHOOK_SECRET': 'hook',
        'LAVA_SBP_ENABLED': True,
        'LAVA_CARD_ENABLED': True,
        'LAVA_DISPLAY_NAME': 'Lava',
        'LAVA_SBP_DISPLAY_NAME': 'СБП (QR)',
        'LAVA_CARD_DISPLAY_NAME': 'Картой',
    }
    base.update(overrides)
    for key, value in base.items():
        monkeypatch.setattr(settings, key, value)


def test_lava_sbp_and_card_describe_provider_not_themselves(monkeypatch):
    _enable_lava(monkeypatch)

    methods = get_available_payment_methods()

    sbp = _method(methods, 'lava_sbp')
    card = _method(methods, 'lava_card')
    assert sbp and card

    assert sbp['name'] == 'СБП (QR)'
    assert sbp['description'] == 'через Lava'
    assert card['name'] == 'Картой'
    assert card['description'] == 'через Lava'


def test_lava_generic_method_keeps_provider_description(monkeypatch):
    _enable_lava(monkeypatch, LAVA_SBP_ENABLED=False, LAVA_CARD_ENABLED=False)

    methods = get_available_payment_methods()

    lava = _method(methods, 'lava')
    assert lava
    assert lava['name'] == 'Lava'
    assert lava['description'] == 'через Lava'
