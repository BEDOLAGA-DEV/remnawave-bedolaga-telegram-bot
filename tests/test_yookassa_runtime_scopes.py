from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.cabinet.routes import balance as cabinet_balance
from app.cabinet.schemas.balance import TopUpRequest
from app.config import settings
from app.handlers.balance import yookassa as bot_yookassa
from app.keyboards.inline import get_payment_methods_keyboard
from app.services import payment_method_config_service
from app.utils.payment_utils import get_available_payment_methods
from app.webapi.routes import miniapp
from app.webapi.schemas.miniapp import MiniAppPaymentCreateRequest, MiniAppPaymentMethodsRequest


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _set_legacy_yookassa(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', enabled, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SHOP_ID', 'legacy-shop' if enabled else None, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SECRET_KEY', 'legacy-secret' if enabled else None, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SBP_ENABLED', enabled, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_MIN_AMOUNT_KOPEKS', 1000, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_MAX_AMOUNT_KOPEKS', 5000000, raising=False)


def _set_bot_yookassa(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_BOT_ENABLED', enabled, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_BOT_SHOP_ID', 'bot-shop' if enabled else None, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_BOT_SECRET_KEY', 'bot-secret' if enabled else None, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_BOT_SBP_ENABLED', enabled, raising=False)


def _set_cabinet_yookassa(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_CABINET_ENABLED', enabled, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_CABINET_SHOP_ID', 'cabinet-shop' if enabled else None, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_CABINET_SECRET_KEY', 'cabinet-secret' if enabled else None, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_CABINET_SBP_ENABLED', enabled, raising=False)


def _button_callbacks(markup: Any) -> list[str]:
    callbacks: list[str] = []
    for row in markup.inline_keyboard:
        for button in row:
            callback_data = getattr(button, 'callback_data', None)
            if callback_data:
                callbacks.append(callback_data)
    return callbacks


def test_bot_payment_keyboards_use_bot_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_legacy_yookassa(monkeypatch, enabled=False)
    _set_bot_yookassa(monkeypatch, enabled=True)
    _set_cabinet_yookassa(monkeypatch, enabled=False)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', False, raising=False)

    available_methods = get_available_payment_methods()
    callbacks = _button_callbacks(get_payment_methods_keyboard(10000, language='ru'))

    assert {method['id'] for method in available_methods} >= {'yookassa', 'yookassa_sbp'}
    assert 'topup_amount|yookassa|10000' in callbacks
    assert 'topup_amount|yookassa_sbp|10000' in callbacks

    _set_bot_yookassa(monkeypatch, enabled=False)
    _set_cabinet_yookassa(monkeypatch, enabled=True)

    available_methods = get_available_payment_methods()
    callbacks = _button_callbacks(get_payment_methods_keyboard(10000, language='ru'))

    assert 'yookassa' not in {method['id'] for method in available_methods}
    assert all('yookassa' not in callback for callback in callbacks)


@pytest.mark.anyio('asyncio')
async def test_cabinet_payment_methods_use_cabinet_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_legacy_yookassa(monkeypatch, enabled=False)
    _set_bot_yookassa(monkeypatch, enabled=True)
    _set_cabinet_yookassa(monkeypatch, enabled=False)

    async def fake_get_all_configs(db: Any) -> list[Any]:
        return [
            SimpleNamespace(
                method_id='yookassa',
                is_enabled=True,
                display_name=None,
                sub_options={'card': True, 'sbp': True},
                min_amount_kopeks=None,
                max_amount_kopeks=None,
                user_type_filter='all',
                first_topup_filter='any',
                promo_group_filter_mode='all',
                sort_order=1,
                open_url_direct=False,
            )
        ]

    monkeypatch.setattr(payment_method_config_service, 'get_all_configs', fake_get_all_configs)

    bot_only_methods = await payment_method_config_service.get_enabled_methods_for_user(
        db=SimpleNamespace(),
        user=SimpleNamespace(id=1, telegram_id=123, promo_group_id=None),
        is_first_topup=True,
    )
    assert [method['id'] for method in bot_only_methods] == []

    _set_bot_yookassa(monkeypatch, enabled=False)
    _set_cabinet_yookassa(monkeypatch, enabled=True)

    cabinet_methods = await payment_method_config_service.get_enabled_methods_for_user(
        db=SimpleNamespace(),
        user=SimpleNamespace(id=1, telegram_id=123, promo_group_id=None),
        is_first_topup=True,
    )
    assert [method['id'] for method in cabinet_methods] == ['yookassa']


@pytest.mark.anyio('asyncio')
async def test_cabinet_topup_passes_cabinet_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example', raising=False)

    async def fake_get_payment_methods(user: Any, db: Any) -> list[Any]:
        return [
            SimpleNamespace(
                id='yookassa',
                is_available=True,
                min_amount_kopeks=1000,
                max_amount_kopeks=5000000,
            )
        ]

    captured: dict[str, Any] = {}

    class DummyPaymentService:
        async def create_yookassa_sbp_payment(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                'local_payment_id': 123,
                'yookassa_payment_id': 'yk_cabinet_sbp',
                'confirmation_url': 'https://yk/confirm',
                'status': 'pending',
            }

    monkeypatch.setattr(cabinet_balance, 'get_payment_methods', fake_get_payment_methods)
    monkeypatch.setattr(cabinet_balance, 'PaymentService', lambda *args, **kwargs: DummyPaymentService())

    response = await cabinet_balance.create_topup(
        TopUpRequest(amount_kopeks=25000, payment_method='yookassa', payment_option='sbp'),
        user=SimpleNamespace(id=7, telegram_id=777, username='user', restriction_topup=False),
        db=SimpleNamespace(),
    )

    assert response.payment_id == '123'
    assert captured['yookassa_scope'] == 'cabinet'


@pytest.mark.anyio('asyncio')
async def test_miniapp_uses_cabinet_scope_for_yookassa(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_legacy_yookassa(monkeypatch, enabled=False)
    _set_bot_yookassa(monkeypatch, enabled=False)
    _set_cabinet_yookassa(monkeypatch, enabled=True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', False, raising=False)

    async def fake_resolve_user(db: Any, init_data: str) -> tuple[Any, dict[str, Any]]:
        return SimpleNamespace(id=8, telegram_id=888, language='ru'), {}

    captured: dict[str, Any] = {}

    class DummyPaymentService:
        async def create_yookassa_payment(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                'local_payment_id': 321,
                'yookassa_payment_id': 'yk_cabinet',
                'confirmation_url': 'https://yk/confirm',
                'status': 'pending',
            }

    monkeypatch.setattr(miniapp, '_resolve_user_from_init_data', fake_resolve_user)
    monkeypatch.setattr(miniapp, 'PaymentService', lambda *args, **kwargs: DummyPaymentService())

    methods_response = await miniapp.get_payment_methods(
        MiniAppPaymentMethodsRequest(initData='init'),
        db=SimpleNamespace(),
    )
    assert 'yookassa' in {method.id for method in methods_response.methods}

    create_response = await miniapp.create_payment_link(
        MiniAppPaymentCreateRequest(initData='init', method='yookassa', amountKopeks=25000),
        db=SimpleNamespace(),
    )

    assert create_response.payment_url == 'https://yk/confirm'
    assert captured['yookassa_scope'] == 'cabinet'


@pytest.mark.anyio('asyncio')
async def test_bot_balance_creation_passes_bot_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_legacy_yookassa(monkeypatch, enabled=True)
    _set_bot_yookassa(monkeypatch, enabled=True)
    _set_cabinet_yookassa(monkeypatch, enabled=False)
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', 'fallback@example.com', raising=False)

    captured: dict[str, Any] = {}

    class DummyPaymentService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            return None

        async def create_yookassa_payment(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                'local_payment_id': 444,
                'yookassa_payment_id': 'yk_bot',
                'confirmation_url': 'https://yk/confirm',
                'status': 'pending',
            }

    class DummyMessage:
        bot = SimpleNamespace()
        chat = SimpleNamespace(id=777)

        async def answer(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(chat=SimpleNamespace(id=777), message_id=555)

        async def delete(self) -> None:
            return None

    class DummyState:
        async def get_data(self) -> dict[str, Any]:
            return {}

        async def update_data(self, **kwargs: Any) -> None:
            return None

        async def clear(self) -> None:
            return None

    monkeypatch.setattr(bot_yookassa, 'PaymentService', DummyPaymentService)
    monkeypatch.setattr(
        'app.services.payment_service.get_yookassa_payment_by_local_id',
        lambda *args, **kwargs: None,
        raising=False,
    )

    await bot_yookassa.process_yookassa_payment_amount(
        message=DummyMessage(),
        db_user=SimpleNamespace(
            id=9,
            telegram_id=999,
            username='bot_user',
            language='ru',
            restriction_topup=False,
        ),
        db=SimpleNamespace(),
        amount_kopeks=25000,
        state=DummyState(),
    )

    assert captured['yookassa_scope'] == 'bot'
