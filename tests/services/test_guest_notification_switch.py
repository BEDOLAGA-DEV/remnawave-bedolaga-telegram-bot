"""Выключатель писем и письмо гостевой покупки.

Основное письмо (доставка / активация / подарок) можно отключить, но письмо
с доступами кабинета идёт следом в той же функции и от него не зависит —
иначе покупатель остался бы без пароля.
"""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# Тесты подменяют app.cabinet.services.email_service заглушкой в sys.modules; если
# пакет app.cabinet.services ещё не загружен, его __init__ импортирует
# EmailService из заглушки и падает. Загружаем пакет и ленивые зависимости
# заранее — тогда файл проходит и в одиночку, а не только после других тестов.
importlib.import_module('app.cabinet.services.email_type_switch')
from app.config import settings
from app.services.guest_purchase_service import send_guest_notification


def _purchase(**overrides) -> SimpleNamespace:
    base = dict(
        id=1,
        is_gift=False,
        token='T' * 64,
        period_days=30,
        gift_message=None,
        gift_recipient_type=None,
        gift_recipient_value=None,
        contact_type='email',
        contact_value='buyer@example.com',
        subscription_url='https://sub.example/x',
        cabinet_password='pw-123',
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _patches(send_mock: MagicMock):
    email_module = SimpleNamespace(email_service=SimpleNamespace(send_email=send_mock))

    async def get_rendered_override(*a, **k):
        return None

    overrides_module = SimpleNamespace(get_rendered_override=get_rendered_override)
    return (
        patch.dict(
            'sys.modules',
            {
                'app.cabinet.services.email_service': email_module,
                'app.cabinet.services.email_template_overrides': overrides_module,
            },
        ),
        patch('app.services.guest_purchase_service.settings.CABINET_URL', 'https://cab.example'),
    )


@pytest.mark.asyncio
async def test_disabled_main_email_does_not_silence_credentials(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'EMAIL_DISABLED_TYPES', 'guest_activation_required', raising=False)
    send = MagicMock(return_value=True)
    mods, cab = _patches(send)
    with mods, cab:
        await send_guest_notification(
            _purchase(), is_pending_activation=True, tariff_name='Lite', language='ru', is_new_account=True
        )
    assert len(send.call_args_list) == 1, 'должно уйти только письмо с доступами'
    assert 'pw-123' in send.call_args.kwargs['body_html']


@pytest.mark.asyncio
async def test_enabled_main_email_sends_both(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'EMAIL_DISABLED_TYPES', '', raising=False)
    send = MagicMock(return_value=True)
    mods, cab = _patches(send)
    with mods, cab:
        await send_guest_notification(
            _purchase(), is_pending_activation=True, tariff_name='Lite', language='ru', is_new_account=True
        )
    bodies = [c.kwargs['body_html'] for c in send.call_args_list]
    assert len(bodies) == 2
    assert '/buy/success/' in bodies[0]
    assert 'pw-123' in bodies[1]
