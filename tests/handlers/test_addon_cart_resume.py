"""Корзина докупки трафика/устройств доживает до покупки — и сама, и по кнопке.

Жалоба: докупил трафик, денег не хватило, пополнил — «трафик не покупается
сам», а «Вернуться к оформлению подписки» отвечает «Корзина повреждена» и
удаляет корзину; следующее нажатие — «Корзина не найдена».

Две причины:

* корзины докупки сохранялись без метки намерения (``return_to_cart``) — тихая
  автопокупка после пополнения выходила на «нет свежего намерения», не дойдя до
  диспетчера, который про ``add_traffic``/``add_devices`` знает;
* общий обработчик кнопки требовал у корзины ``period_days`` — у докупки его
  нет и быть не может.

Сторож в конце: каждая корзина докупки в коде (бот и кабинет, 8 мест) обязана
нести ``'return_to_cart': True`` — новая без флага не соберётся.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import subscription_auto_purchase_service as auto_service
from app.services.user_cart_service import UserCartService


APP_ROOT = Path(__file__).resolve().parents[2] / 'app'
ADDON_MODES = {'add_traffic', 'add_devices'}


class _MockRedis:
    def __init__(self):
        self.storage: dict[str, str] = {}

    async def setex(self, key, ttl, value):
        self.storage[key] = value
        return True

    async def get(self, key):
        return self.storage.get(key)

    async def delete(self, key):
        return 1 if self.storage.pop(key, None) is not None else 0

    async def exists(self, key):
        return 1 if key in self.storage else 0

    async def scan(self, cursor=0, match=None, count=50):
        import fnmatch

        return 0, [k for k in self.storage if match is None or fnmatch.fnmatch(k, match)]


@pytest.fixture
def cart_service():
    service = UserCartService()
    service._redis_client = _MockRedis()
    service._initialized = True
    return service


def _traffic_cart(**overrides) -> dict:
    cart = {
        'cart_mode': 'add_traffic',
        'subscription_id': 42,
        'traffic_gb': 100,
        'price_kopeks': 5000,
        'source': 'bot',
        'return_to_cart': True,
    }
    return {**cart, **overrides}


# ---------------------------------------------------------------------------
# Тихая автопокупка после пополнения: корзина докупки теперь доходит до диспетчера
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topup_auto_purchases_addon_cart(monkeypatch, cart_service):
    monkeypatch.setattr(auto_service, 'user_cart_service', cart_service)
    monkeypatch.setattr(auto_service, 'settings', SimpleNamespace(is_auto_purchase_after_topup_enabled=lambda: True))
    processed: list[dict] = []

    async def fake_process(db, user, cart_data, *, bot=None, manual=False):
        processed.append({'mode': cart_data['cart_mode'], 'manual': manual})
        return True

    monkeypatch.setattr(auto_service, '_process_single_cart', fake_process)
    user = SimpleNamespace(id=7, telegram_id=1)
    await cart_service.save_user_cart(7, _traffic_cart())
    assert await cart_service.has_topup_intent(7) is True, 'флаг return_to_cart ставит метку намерения'

    succeeded = await auto_service.auto_purchase_saved_cart_after_topup(AsyncMock(), user, bot=None)

    assert succeeded is True
    assert processed == [{'mode': 'add_traffic', 'manual': False}]
    assert await cart_service.has_topup_intent(7) is False, 'намерение одноразовое'


@pytest.mark.asyncio
async def test_topup_without_intent_leaves_addon_cart_alone(monkeypatch, cart_service):
    """Документирует старую поломку: корзина без флага — «нет свежего намерения»."""
    monkeypatch.setattr(auto_service, 'user_cart_service', cart_service)
    monkeypatch.setattr(auto_service, 'settings', SimpleNamespace(is_auto_purchase_after_topup_enabled=lambda: True))
    processed: list[dict] = []

    async def fake_process(db, user, cart_data, *, bot=None, manual=False):
        processed.append(cart_data)
        return True

    monkeypatch.setattr(auto_service, '_process_single_cart', fake_process)
    await cart_service.save_user_cart(7, _traffic_cart(return_to_cart=False))

    succeeded = await auto_service.auto_purchase_saved_cart_after_topup(
        AsyncMock(), SimpleNamespace(id=7, telegram_id=1), bot=None
    )

    assert succeeded is False
    assert processed == []


# ---------------------------------------------------------------------------
# Кнопка «Вернуться к оформлению»: докупка доводится до конца, а не «повреждена»
# ---------------------------------------------------------------------------


def _callback() -> AsyncMock:
    callback = AsyncMock()
    callback.message = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    callback.bot = MagicMock()
    return callback


def _user(balance: int) -> SimpleNamespace:
    return SimpleNamespace(id=7, telegram_id=1, language='ru', balance_kopeks=balance)


@pytest.mark.asyncio
async def test_button_resumes_addon_cart_instead_of_corrupting_it():
    from app.handlers.subscription.purchase import return_to_saved_cart

    callback = _callback()
    resumed: list[tuple] = []

    async def fake_resume(db, user, cart_data, *, bot=None):
        resumed.append((cart_data['cart_mode'], bot))
        return True

    with (
        patch('app.handlers.subscription.purchase.user_cart_service') as cart_service,
        patch('app.handlers.subscription.addon_cart.resume_addon_cart', fake_resume),
    ):
        cart_service.get_user_cart = AsyncMock(return_value=_traffic_cart())
        cart_service.delete_user_cart = AsyncMock()
        cart_service.delete_subscription_cart = AsyncMock()

        await return_to_saved_cart(callback, AsyncMock(), _user(balance=10000), AsyncMock())

    assert resumed == [('add_traffic', callback.bot)]
    cart_service.delete_user_cart.assert_not_called()
    cart_service.delete_subscription_cart.assert_not_called()
    alerts = [str(call) for call in callback.answer.call_args_list]
    assert not any('повреждена' in text or 'не найдена' in text for text in alerts), alerts
    assert '✅' in callback.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_button_with_still_insufficient_balance_sends_back_to_topup():
    from app.handlers.subscription.addon_cart import resume_addon_cart_from_button

    callback = _callback()
    resumed = AsyncMock(return_value=True)

    with patch('app.handlers.subscription.addon_cart.resume_addon_cart', resumed):
        await resume_addon_cart_from_button(callback, _user(balance=1000), AsyncMock(), _traffic_cart())

    resumed.assert_not_called()
    text = callback.message.edit_text.call_args.args[0]
    assert 'недостаточно' in text.lower()
    assert '40' in text, 'не хватает 40 ₽ (5000 − 1000 копеек)'
    keyboard = callback.message.edit_text.call_args.kwargs['reply_markup']
    assert keyboard.inline_keyboard, 'предложены способы пополнения'


@pytest.mark.asyncio
async def test_button_reports_failure_without_deleting_cart():
    from app.handlers.subscription.addon_cart import resume_addon_cart_from_button

    callback = _callback()

    with patch('app.handlers.subscription.addon_cart.resume_addon_cart', AsyncMock(return_value=False)):
        await resume_addon_cart_from_button(callback, _user(balance=10000), AsyncMock(), _traffic_cart())

    args, kwargs = callback.answer.call_args
    assert '❌' in args[0] and kwargs.get('show_alert') is True


@pytest.mark.asyncio
async def test_devices_cart_uses_the_same_button_path():
    from app.handlers.subscription.purchase import return_to_saved_cart

    callback = _callback()
    resumed: list[str] = []

    async def fake_resume(db, user, cart_data, *, bot=None):
        resumed.append(cart_data['cart_mode'])
        return True

    devices_cart = {'cart_mode': 'add_devices', 'devices_to_add': 2, 'price_kopeks': 3000, 'return_to_cart': True}
    with (
        patch('app.handlers.subscription.purchase.user_cart_service') as cart_service,
        patch('app.handlers.subscription.addon_cart.resume_addon_cart', fake_resume),
    ):
        cart_service.get_user_cart = AsyncMock(return_value=devices_cart)
        await return_to_saved_cart(callback, AsyncMock(), _user(balance=10000), AsyncMock())

    assert resumed == ['add_devices']


# ---------------------------------------------------------------------------
# Ручной запуск: тот же диспетчер, но без «автоматически» в сообщении
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_resume_goes_through_dispatcher_as_manual(monkeypatch):
    seen: list[dict] = []

    async def fake_process(db, user, cart_data, *, bot=None, manual=False):
        seen.append({'mode': cart_data['cart_mode'], 'manual': manual})
        return True

    monkeypatch.setattr(auto_service, '_process_single_cart', fake_process)

    assert await auto_service.resume_addon_cart(AsyncMock(), SimpleNamespace(id=7), _traffic_cart()) is True
    assert await auto_service.resume_addon_cart(AsyncMock(), SimpleNamespace(id=7), {'cart_mode': 'extend'}) is False
    assert seen == [{'mode': 'add_traffic', 'manual': True}]


def test_manual_success_text_has_no_word_automatically():
    source = ' '.join(
        (APP_ROOT / 'services' / 'subscription_auto_purchase_service.py').read_text(encoding='utf-8').split()
    )
    assert "'✅ <b>Трафик добавлен!</b>\\n\\n' if manual else '✅ <b>Трафик добавлен автоматически!</b>" in source
    assert (
        "'✅ <b>Устройства добавлены!</b>\\n\\n' if manual else '✅ <b>Устройства добавлены автоматически!</b>"
        in source
    )


# ---------------------------------------------------------------------------
# Сторож: каждая корзина докупки в коде несёт флаг намерения
# ---------------------------------------------------------------------------


def _literal(node: ast.AST):
    return node.value if isinstance(node, ast.Constant) else None


def _addon_cart_dicts(tree: ast.AST) -> list[ast.Dict]:
    """Словари-корзины: cart_mode ∈ ADDON_MODES и есть цена — ответ API с тем же cart_mode не корзина."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {_literal(k): v for k, v in zip(node.keys, node.values, strict=True) if k is not None}
        if _literal(keys.get('cart_mode')) in ADDON_MODES and 'price_kopeks' in keys:
            found.append(node)
    return found


def test_every_addon_cart_in_code_carries_topup_intent():
    missing: list[str] = []
    total = 0
    for path in sorted(APP_ROOT.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in _addon_cart_dicts(tree):
            total += 1
            keys = {_literal(k): v for k, v in zip(node.keys, node.values, strict=True) if k is not None}
            if _literal(keys.get('return_to_cart')) is not True:
                missing.append(f'{path.relative_to(APP_ROOT.parent)}:{node.lineno}')

    assert total >= 8, f'сторож видит слишком мало корзин докупки: {total}'
    assert not missing, f"корзины докупки без 'return_to_cart': True — автопокупка их пропустит: {missing}"
