"""Уведомления email-юзерам в flow «пополнение → автопокупка» (#2952).

Оба пользовательских уведомления этого flow («Пополнение успешно!» в 18+
провайдерах и «Подписка активирована/продлена» в auto-purchase обработчиках)
гейтились ``if bot and user.telegram_id`` — юзеры без Telegram (авторизация
по email) не получали вообще ничего и узнавали о результате, только зайдя в
кабинет.

Фикс подключает мультиканальный роутер ``notification_delivery_service``:

- ``notify_email_user_topup`` вызывается из ЕДИНСТВЕННОЙ общей точки после
  зачисления — ``send_cart_notification_after_topup`` (её зовут все
  провайдеры), до автопокупки, чтобы письма шли в порядке
  «пополнение → подписка»;
- ``_notify_email_user_auto_purchase`` вызывается из всех пяти подписочных
  auto-purchase обработчиков после telegram-гейта.

Оба хелпера — no-op для telegram-юзеров (им сообщение уже отправлено ботом)
и глотают сбои: уведомление не должно ронять webhook после зачисления денег.
"""

import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.config import settings
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service,
)
from app.services.payment import common as payment_common
from app.services.subscription_auto_purchase_service import _notify_email_user_auto_purchase


def _email_user(**overrides):
    defaults = {
        'id': 7,
        'telegram_id': None,
        'email': 'user@test.dev',
        'email_verified': True,
        'balance_kopeks': 50000,
        'language': 'ru',
        'status': 'active',
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Перехватывает send_notification роутера."""
    calls: list[dict] = []

    async def fake_send_notification(user, notification_type, context, bot=None, **kwargs):
        calls.append({'user': user, 'type': notification_type, 'context': context, 'bot': bot})
        return True

    monkeypatch.setattr(notification_delivery_service, 'send_notification', fake_send_notification)
    return calls


# ============ notify_email_user_topup ============


async def test_topup_notification_sent_for_email_user(sent) -> None:
    user = _email_user()

    await payment_common.notify_email_user_topup(user, 25000)

    assert len(sent) == 1
    call = sent[0]
    assert call['type'] == NotificationType.BALANCE_TOPUP
    assert call['bot'] is None
    assert call['context']['amount_kopeks'] == 25000
    assert call['context']['new_balance_kopeks'] == 50000
    assert call['context']['formatted_amount']
    assert call['context']['formatted_balance']


async def test_topup_notification_skipped_for_telegram_user(sent) -> None:
    # Telegram-юзеру «Пополнение успешно!» уже отправил провайдер напрямую —
    # роутер вызывать нельзя, иначе будет дубль.
    await payment_common.notify_email_user_topup(_email_user(telegram_id=12345), 25000)

    assert sent == []


async def test_topup_notification_skipped_without_email(sent) -> None:
    await payment_common.notify_email_user_topup(_email_user(email=None), 25000)

    assert sent == []


async def test_topup_notification_swallows_router_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError('smtp down')

    monkeypatch.setattr(notification_delivery_service, 'send_notification', boom)

    # Не должно поднять исключение: деньги уже зачислены, webhook падать не должен.
    await payment_common.notify_email_user_topup(_email_user(), 25000)


async def test_topup_hook_notifies_before_auto_purchase(monkeypatch: pytest.MonkeyPatch) -> None:
    """Порядок каналов: письмо о пополнении уходит ДО автопокупки из корзины,
    чтобы уведомления пришли в хронологическом порядке «пополнение → подписка»."""
    import app.services.subscription_auto_purchase_service as auto_mod

    order: list[str] = []

    async def fake_topup_notify(user, amount_kopeks):
        order.append('topup_email')

    async def fake_daily(db, user, bot=None):
        order.append('daily')
        return False

    async def fake_extend(db, user, bot=None):
        order.append('extend')
        return False

    async def fake_get_cart(user_id):
        return None

    monkeypatch.setattr(payment_common, 'notify_email_user_topup', fake_topup_notify)
    monkeypatch.setattr(auto_mod, 'try_resume_disabled_daily_after_topup', fake_daily)
    monkeypatch.setattr(auto_mod, 'try_auto_extend_expired_after_topup', fake_extend)
    monkeypatch.setattr(payment_common.user_cart_service, 'get_user_cart', fake_get_cart)

    await payment_common.send_cart_notification_after_topup(_email_user(), 25000, db=None, bot=None)

    assert order and order[0] == 'topup_email', 'email о пополнении должен уходить до side-effect автопокупки'


# ============ _notify_email_user_auto_purchase ============


def _subscription():
    return SimpleNamespace(
        end_date=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        traffic_limit_gb=100,
        device_limit=3,
    )


async def test_auto_purchase_notification_activated(sent) -> None:
    await _notify_email_user_auto_purchase(_email_user(), _subscription(), 'Базовый', renewed=False)

    assert len(sent) == 1
    call = sent[0]
    assert call['type'] == NotificationType.SUBSCRIPTION_ACTIVATED
    assert call['bot'] is None
    assert call['context']['expires_at'] == '15.08.2026'
    assert call['context']['tariff_name'] == 'Базовый'
    assert call['context']['traffic_limit_gb'] == 100
    assert call['context']['device_limit'] == 3


async def test_auto_purchase_notification_renewed(sent) -> None:
    await _notify_email_user_auto_purchase(_email_user(), _subscription(), 'Базовый', renewed=True)

    assert len(sent) == 1
    assert sent[0]['type'] == NotificationType.SUBSCRIPTION_RENEWED
    assert sent[0]['context']['new_expires_at'] == '15.08.2026'


async def test_auto_purchase_notification_skipped_for_telegram_user(sent) -> None:
    await _notify_email_user_auto_purchase(_email_user(telegram_id=12345), _subscription(), 'X', renewed=False)

    assert sent == []


async def test_auto_purchase_notification_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError('smtp down')

    monkeypatch.setattr(notification_delivery_service, 'send_notification', boom)

    await _notify_email_user_auto_purchase(_email_user(), _subscription(), 'X', renewed=True)


# ============ Реальные шаблоны рендерятся с контекстом хелперов ============
# Тесты выше подменяют роутер и НЕ доказывают, что письмо реально соберётся.
# Здесь прогоняем те же контексты через настоящие email-шаблоны, чтобы
# переименование ключа или пропавший шаблон ловились, а не проходили зелёными.


def test_real_topup_template_renders_with_helper_context() -> None:
    context = {
        'formatted_amount': settings.format_price(25000),
        'formatted_balance': settings.format_price(50000),
        'amount_kopeks': 25000,
        'new_balance_kopeks': 50000,
    }
    template = EmailNotificationTemplates().get_template(NotificationType.BALANCE_TOPUP, 'ru', context)

    assert template is not None, 'нет email-шаблона для BALANCE_TOPUP'
    assert template['subject'].strip()
    assert settings.format_price(25000) in template['body_html'], 'сумма пополнения не попала в письмо'


def test_real_activated_template_renders_with_helper_context() -> None:
    context = {
        'expires_at': '15.08.2026',
        'new_expires_at': '15.08.2026',
        'traffic_limit_gb': 100,
        'device_limit': 3,
        'tariff_name': 'Базовый',
    }
    template = EmailNotificationTemplates().get_template(NotificationType.SUBSCRIPTION_ACTIVATED, 'ru', context)

    assert template is not None, 'нет email-шаблона для SUBSCRIPTION_ACTIVATED'
    assert template['subject'].strip()
    assert '15.08.2026' in template['body_html'], 'дата окончания не попала в письмо об активации'


def test_real_renewed_template_renders_with_helper_context() -> None:
    context = {
        'expires_at': '15.08.2026',
        'new_expires_at': '15.08.2026',
        'traffic_limit_gb': 100,
        'device_limit': 3,
        'tariff_name': 'Базовый',
    }
    template = EmailNotificationTemplates().get_template(NotificationType.SUBSCRIPTION_RENEWED, 'ru', context)

    assert template is not None, 'нет email-шаблона для SUBSCRIPTION_RENEWED'
    assert template['subject'].strip()
    assert '15.08.2026' in template['body_html'], 'новая дата окончания не попала в письмо о продлении'


# ============ Trial→paid = активация, а не продление (#2952) ============


def test_trial_conversion_labeled_activated_not_renewed() -> None:
    """Пин: сайты автопокупки тарифа/суточного/extend не должны помечать
    конверсию триала как RENEWED (для email-юзера это первая активация)."""
    source = (
        Path(__file__).resolve().parents[2] / 'app' / 'services' / 'subscription_auto_purchase_service.py'
    ).read_text(encoding='utf-8')
    tree = ast.parse(source)

    renewed_exprs: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '_notify_email_user_auto_purchase'
        ):
            for kw in node.keywords:
                if kw.arg == 'renewed':
                    renewed_exprs.append(ast.unparse(kw.value))

    assert renewed_exprs, 'не найдены вызовы _notify_email_user_auto_purchase'
    # Ни один renewed не должен быть голым bool(existing_subscription) без учёта триала.
    for expr in renewed_exprs:
        if 'existing_subscription' in expr:
            assert 'was_trial_conversion' in expr, (
                f'renewed={expr!r} помечает конверсию триала как продление — '
                'должно исключать was_trial_conversion (#2952)'
            )
