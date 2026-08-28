"""Отписка от маркетинговых писем: подпись токена, заголовки RFC 8058,
ссылка в футере и фильтрация получателей email-рассылки по настройкам.

Без отписки Gmail/Yahoo штрафуют отправителя за жалобы «Спам» вместо клика по
ссылке, а bulk-политики этих провайдеров прямо требуют one-click unsubscribe.
До этих тестов email-путь рассылки ещё и игнорировал тумблеры
``news_enabled`` / ``promo_offers_enabled``, которые Telegram-путь уважает:
пользователь выключал новости в кабинете и всё равно получал их на почту.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Self

import pytest

from app.cabinet.services import email_unsubscribe as unsub
from app.cabinet.services.email_service import email_service
from app.cabinet.services.email_templates import EmailNotificationTemplates


class _FakeSMTP:
    """Ловит сырое письмо, отданное в ``sendmail``, и работает как CM."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def sendmail(self, _from_addr: str, _to_addr: str, msg: str) -> None:
        self.messages.append(msg)


@pytest.fixture
def smtp_ready(monkeypatch):
    """Делает email_service «настроенным» и подменяет SMTP-соединение."""
    from app.config import settings

    fake = _FakeSMTP()
    monkeypatch.setattr(type(settings), 'is_smtp_configured', lambda self: True)
    monkeypatch.setattr(settings, 'SMTP_FROM_EMAIL', 'noreply@example.com')
    monkeypatch.setattr(settings, 'SMTP_FROM_NAME', 'Example VPN')
    monkeypatch.setattr(email_service, '_get_smtp_connection', lambda: fake)
    return fake


# --------------------------------------------------------------------------
# Токен
# --------------------------------------------------------------------------


def test_token_roundtrip(monkeypatch):
    """Свежий токен опознаётся: отдаёт user_id и категорию."""
    monkeypatch.setattr(unsub, '_secret', lambda: 'unit-test-secret')

    token = unsub.build_token(42, 'User@Example.com', 'all')
    assert unsub.parse_token(token) == (42, 'all')
    assert unsub.verify_token(token, 'user@example.com') is True


def test_token_email_is_case_insensitive(monkeypatch):
    """Регистр адреса не должен ломать ссылку из письма."""
    monkeypatch.setattr(unsub, '_secret', lambda: 'unit-test-secret')

    token = unsub.build_token(42, 'user@example.com', 'all')
    assert unsub.verify_token(token, 'USER@EXAMPLE.COM') is True


def test_token_rejects_tampering(monkeypatch):
    """Подменённый user_id не проходит проверку подписи."""
    monkeypatch.setattr(unsub, '_secret', lambda: 'unit-test-secret')

    token = unsub.build_token(42, 'user@example.com', 'all')
    forged = token.replace('42.', '43.', 1)

    assert unsub.parse_token(forged) == (43, 'all')
    assert unsub.verify_token(forged, 'user@example.com') is False


def test_token_dies_with_old_email(monkeypatch):
    """Смена адреса обесценивает старые ссылки — токен привязан к email."""
    monkeypatch.setattr(unsub, '_secret', lambda: 'unit-test-secret')

    token = unsub.build_token(42, 'old@example.com', 'all')
    assert unsub.verify_token(token, 'new@example.com') is False


@pytest.mark.parametrize('garbage', ['', 'nonsense', 'a.b.c', '42.all', '42.all.', 'x.all.sig'])
def test_parse_token_survives_garbage(garbage):
    """Мусор в query-параметре не должен ронять публичный эндпоинт."""
    assert unsub.parse_token(garbage) is None


def test_build_url_empty_when_disabled(monkeypatch):
    """Выключенная отписка не должна протаскивать битую ссылку в письмо."""
    from app.config import settings

    monkeypatch.setattr(settings, 'EMAIL_UNSUBSCRIBE_ENABLED', False)
    assert unsub.build_unsubscribe_url(1, 'user@example.com') == ''


def test_build_url_uses_cabinet_url_by_default(monkeypatch):
    """Без явного EMAIL_UNSUBSCRIBE_BASE_URL берём публичный путь кабинета."""
    from app.config import settings

    monkeypatch.setattr(settings, 'EMAIL_UNSUBSCRIBE_ENABLED', True)
    monkeypatch.setattr(settings, 'EMAIL_UNSUBSCRIBE_BASE_URL', '')
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cab.example.com/')
    monkeypatch.setattr(unsub, '_secret', lambda: 'unit-test-secret')

    url = unsub.build_unsubscribe_url(7, 'user@example.com')
    assert url.startswith('https://cab.example.com/api/cabinet/public/unsubscribe?token=7.all.')


# --------------------------------------------------------------------------
# Заголовки письма (RFC 8058)
# --------------------------------------------------------------------------


def test_send_email_adds_one_click_headers(smtp_ready):
    """List-Unsubscribe + One-Click — то, из чего Gmail рисует свою кнопку."""
    ok = email_service.send_email(
        to_email='user@example.com',
        subject='Скидка',
        body_html='<p>hi</p>',
        unsubscribe_url='https://cab.example.com/api/cabinet/public/unsubscribe?token=7.all.sig',
    )

    assert ok is True
    raw = smtp_ready.messages[0]
    assert 'List-Unsubscribe: <https://cab.example.com/api/cabinet/public/unsubscribe?token=7.all.sig>' in raw
    assert 'List-Unsubscribe-Post: List-Unsubscribe=One-Click' in raw


def test_transactional_email_has_no_unsubscribe_headers(smtp_ready):
    """Письмо со сбросом пароля не должно предлагать отписку."""
    email_service.send_email(to_email='user@example.com', subject='Код', body_html='<p>1234</p>')

    raw = smtp_ready.messages[0]
    assert 'List-Unsubscribe' not in raw


def test_unsubscribe_url_is_header_injection_safe(smtp_ready):
    """Перенос строки в URL не должен дописать чужой заголовок."""
    email_service.send_email(
        to_email='user@example.com',
        subject='Скидка',
        body_html='<p>hi</p>',
        unsubscribe_url='https://x.example.com/u\r\nBcc: victim@example.com',
    )

    raw = smtp_ready.messages[0]
    assert 'Bcc: victim@example.com' not in raw


# --------------------------------------------------------------------------
# Шаблоны
# --------------------------------------------------------------------------


def test_base_template_renders_footer_link():
    """Ссылка в футере — для клиентов, которые не рисуют кнопку из заголовка."""
    html = EmailNotificationTemplates()._get_base_template(
        '<p>text</p>', 'ru', unsubscribe_url='https://cab.example.com/u?token=t'
    )
    assert 'https://cab.example.com/u?token=t' in html
    assert 'Отписаться' in html


def test_base_template_without_url_has_no_dangling_footer():
    """У транзакционных писем футер остаётся прежним."""
    html = EmailNotificationTemplates()._get_base_template('<p>text</p>', 'ru')
    assert 'Отписаться' not in html


def test_common_context_exposes_unsubscribe_placeholder():
    """{unsubscribe_url} обязан резолвиться, иначе он утечёт в письмо литералом."""
    from app.cabinet.services.email_template_overrides import COMMON_CONTEXT_VARS, build_common_context

    assert 'unsubscribe_url' in COMMON_CONTEXT_VARS
    assert 'unsubscribe_url' in build_common_context()


# --------------------------------------------------------------------------
# Фильтрация получателей рассылки
# --------------------------------------------------------------------------


class _StubUser:
    def __init__(self, notification_settings: dict | None) -> None:
        self.notification_settings = notification_settings


def test_email_broadcast_filters_by_category():
    """Тумблеры кабинета обязаны резать email-рассылку так же, как Telegram."""
    from app.utils.notification_prefs import filter_users_by_broadcast_category

    opted_out_news = _StubUser({'news_enabled': False})
    opted_out_promo = _StubUser({'promo_offers_enabled': False})
    default_user = _StubUser(None)

    users = [opted_out_news, opted_out_promo, default_user]

    assert filter_users_by_broadcast_category(users, 'news') == [opted_out_promo, default_user]
    assert filter_users_by_broadcast_category(users, 'promo') == [opted_out_news, default_user]
    # Системные письма (истечение подписки, оплата) отписке не подлежат.
    assert filter_users_by_broadcast_category(users, 'system') == users


# --- Публичный маршрут: состояние меняет только POST -----------------------


class _CountingDb:
    """Сессия, которая помнит, дошло ли до записи."""

    def __init__(self):
        self.commits = 0
        self.gets = 0

    async def get(self, _model, _pk):
        self.gets += 1

    async def commit(self):  # pragma: no cover - до сюда доходить не должно
        self.commits += 1


@pytest.mark.asyncio
async def test_get_does_not_unsubscribe(monkeypatch):
    """GET по ссылке не должен ничего менять.

    Корпоративные шлюзы (Defender Safe Links, Proofpoint) при доставке сами
    ходят GET'ом по всем ссылкам письма. Если бы отписывал GET, получатель
    оказывался бы отписан до того, как открыл письмо.
    """
    from app.cabinet.routes import unsubscribe as route

    called = False

    async def spy(*args, **kwargs):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(route, 'apply_unsubscribe', spy)

    response = await route.unsubscribe_page(token='1.all.whatever')

    assert response.status_code == 200
    assert not called, 'GET применил отписку — сканер почты отпишет человека сам'
    # но человеку страница обязана предложить действие, а не тупик
    assert b'method="post"' in response.body
    assert b'<noscript>' in response.body


@pytest.mark.asyncio
async def test_post_applies_and_answers_html_to_browser(monkeypatch):
    """POST из формы: отписка применяется, браузеру отдаём страницу результата."""
    from app.cabinet.routes import unsubscribe as route

    monkeypatch.setattr(route, 'apply_unsubscribe', _always(True))
    request = SimpleNamespace(headers={'accept': 'text/html,application/xhtml+xml'})

    response = await route.unsubscribe_one_click(request, token='1.all.sig', db=None)

    assert response.status_code == 200
    assert 'Вы отписались'.encode() in response.body


@pytest.mark.asyncio
async def test_post_answers_bare_200_to_mail_client(monkeypatch):
    """One-click от Gmail: пустой 200, без HTML."""
    from app.cabinet.routes import unsubscribe as route

    monkeypatch.setattr(route, 'apply_unsubscribe', _always(False))
    request = SimpleNamespace(headers={})

    response = await route.unsubscribe_one_click(request, token='bad', db=None)

    assert response.status_code == 200
    assert not response.body


def _always(value: bool):
    async def _inner(*args, **kwargs):
        return value

    return _inner


# --- Границы токена --------------------------------------------------------


@pytest.mark.parametrize(
    'token',
    [
        '99999999999999999999.all.sig',  # не влезает в int4 — db.get уронил бы asyncpg
        '١٢٣.all.sig',  # isdigit() пропускает не-ASCII цифры
        '-1.all.sig',
    ],
)
def test_parse_token_rejects_ids_outside_int4(token):
    """Публичный эндпоинт не должен падать на подобранном id."""
    assert unsub.parse_token(token) is None


def test_mailto_with_crlf_is_dropped(monkeypatch):
    """mailto уходит в тот же заголовок — перенос строки дописал бы свой."""
    monkeypatch.setattr(unsub.settings, 'EMAIL_UNSUBSCRIBE_MAILTO', 'a@b.com\r\nBcc: evil@x.com', raising=False)
    assert unsub.build_unsubscribe_mailto() == ''

    monkeypatch.setattr(unsub.settings, 'EMAIL_UNSUBSCRIBE_MAILTO', 'a@b.com', raising=False)
    assert unsub.build_unsubscribe_mailto() == 'mailto:a@b.com?subject=unsubscribe'


# --- Маркетинговый гейт в notification_delivery_service --------------------
# Функция целиком не исполнялась ни одним тестом: снятие гейта, потеря ссылки
# и приклеивание ссылки к транзакционному письму — всё проходило незаметно.


def _email_user(**overrides):
    base = dict(
        id=7,
        email='u@example.com',
        email_verified=True,
        language='ru',
        first_name='Иван',
        username='ivan',
        notification_settings={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _capture_email(monkeypatch, service):
    """Ставит заглушку отправки и возвращает список пойманных kwargs."""
    captured: list[dict] = []

    def fake_send(**kwargs):
        captured.append(kwargs)
        return True

    monkeypatch.setattr(service.email_service, 'is_configured', lambda: True)
    monkeypatch.setattr(service.email_service, 'send_email', fake_send)
    return captured


@pytest.mark.asyncio
async def test_marketing_email_blocked_after_unsubscribe(monkeypatch):
    """Отписавшийся не должен получать промо и winback — ради этого всё и делалось."""
    from app.services.notification_delivery_service import (
        MARKETING_NOTIFICATION_TYPES,
        notification_delivery_service as service,
    )

    captured = _capture_email(monkeypatch, service)
    user = _email_user(notification_settings={'promo_offers_enabled': False})

    for notification_type in sorted(MARKETING_NOTIFICATION_TYPES, key=lambda t: t.value):
        sent = await service._send_email_notification(user, notification_type, {})
        assert sent is False, f'{notification_type} ушёл отписавшемуся'

    assert not captured


@pytest.mark.asyncio
async def test_marketing_email_carries_unsubscribe_url(monkeypatch):
    """У маркетингового письма ссылка отписки обязана быть."""
    from app.services.notification_delivery_service import (
        NotificationType,
        notification_delivery_service as service,
    )

    monkeypatch.setattr(unsub.settings, 'EMAIL_UNSUBSCRIBE_ENABLED', True, raising=False)
    monkeypatch.setattr(unsub.settings, 'EMAIL_UNSUBSCRIBE_BASE_URL', '', raising=False)
    monkeypatch.setattr(unsub.settings, 'CABINET_URL', 'https://cab.example.com', raising=False)
    captured = _capture_email(monkeypatch, service)

    await service._send_email_notification(_email_user(), NotificationType.PROMO_OFFER, {})

    assert captured, 'маркетинговое письмо не ушло'
    assert captured[0]['unsubscribe_url'], 'письмо ушло без ссылки отписки'
    assert 'token=' in captured[0]['unsubscribe_url']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'notification_type_name',
    ['SUBSCRIPTION_EXPIRING', 'PAYMENT_SUCCESS', 'SUBSCRIPTION_EXPIRED'],
)
async def test_transactional_email_has_no_unsubscribe(monkeypatch, notification_type_name):
    """Транзакционному письму ссылка отписки не положена.

    Иначе человек «отписывается» с чека и перестаёт узнавать об оплате и
    окончании собственной подписки.
    """
    from app.services.notification_delivery_service import (
        NotificationType,
        notification_delivery_service as service,
    )

    notification_type = getattr(NotificationType, notification_type_name, None)
    if notification_type is None:
        pytest.skip(f'{notification_type_name} нет в этой версии перечисления')

    captured = _capture_email(monkeypatch, service)

    await service._send_email_notification(_email_user(), notification_type, {})

    assert captured, 'транзакционное письмо не ушло'
    assert not captured[0]['unsubscribe_url']


# --- Фильтр категорий на самом пути email-рассылки -------------------------


def test_email_broadcast_path_applies_category_filter():
    """Проверяем не хелпер, а то, что путь рассылки его ЗОВЁТ.

    Ровно эту связку PR и чинит; без неё можно снять одну строку, и выключенные
    в кабинете новости снова поедут на почту, а тест на сам хелпер не заметит.
    """
    import inspect

    from app.services.broadcast_service import EmailBroadcastService
    from app.utils.notification_prefs import filter_users_by_broadcast_category

    opted_out = _email_user(id=1, email='out@example.com', notification_settings={'news_enabled': False})
    opted_in = _email_user(id=2, email='in@example.com', notification_settings={'news_enabled': True})
    assert filter_users_by_broadcast_category([opted_out, opted_in], 'news') == [opted_in]

    # системные письма фильтр не режет — иначе перестанут доходить служебные
    assert filter_users_by_broadcast_category([opted_out, opted_in], 'system') == [opted_out, opted_in]

    # Ассерт именно на ВЫЗОВ: одного упоминания мало — локальный импорт остаётся
    # в функции, даже если сам вызов из цикла убрали.
    source = inspect.getsource(EmailBroadcastService._fetch_email_recipients)
    assert 'filter_users_by_broadcast_category(list(batch), category)' in source, (
        'путь email-рассылки перестал фильтровать получателей по категории'
    )


def test_signature_is_compared_in_constant_time():
    """Сравнение подписи — только hmac.compare_digest.

    Обычное `==` выходит на первом несовпавшем байте, и по времени ответа
    публичного эндпоинта подпись можно подобрать. Функционально обе версии
    ведут себя одинаково, поэтому свойство закрепляем по исходнику.
    """
    import inspect

    source = inspect.getsource(unsub.verify_token)
    assert 'compare_digest' in source
