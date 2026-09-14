"""Подпись HTTP-уведомлений ParityPay (X-SIGNATURE).

Схема: поля тела сортируются по ключам, значения склеиваются в строку без
разделителей, от строки считается HMAC-SHA256 на секретном ключе №2.

Главная ловушка — подпись считается по ЗНАЧЕНИЯМ разобранного тела, а не по
сырым байтам. Питон при обычном разборе переписывает числа по-своему
(1200 -> 1200.0), и подпись перестаёт сходиться на ровном месте. Поэтому тело
разбирается с сохранением исходного текста чисел, и это здесь закреплено.
"""

import hashlib
import hmac
import sys
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.services.paritypay_service import ParityPayService, amount_to_kopeks, kopeks_to_amount


SECRET = 'callback-secret-key-2'


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PARITYPAY_CALLBACK_SECRET', SECRET, raising=False)


def _sign(payload: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


INVOICE_BODY = (
    b'{"id":"9beea835-0937-4b5c-8f5a-c3a0d0e60346","order_id":"order-1001",'
    b'"shop_id":"874dfb1e-dbdb-4747-a1c0-005969725b74","amount":"1250.00",'
    b'"credited":1209.01,"comment":"\\u041e\\u043f\\u043b\\u0430\\u0442\\u0430","service":"sbp",'
    b'"custom_fields":null,"expires":"2026-08-24 13:40:00","created":"2026-08-24 12:40:00","status":"PAID"}'
)


def test_signature_payload_sorts_keys_and_joins_values() -> None:
    service = ParityPayService()
    body = service.parse_callback_body(INVOICE_BODY)
    assert body is not None

    payload = service.build_signature_payload(body)

    # Порядок — по алфавиту ключей, без разделителей, null -> пустая строка
    expected = (
        '1250.00'  # amount
        'Оплата'  # comment
        '2026-08-24 12:40:00'  # created
        '1209.01'  # credited
        ''  # custom_fields (null)
        '2026-08-24 13:40:00'  # expires
        '9beea835-0937-4b5c-8f5a-c3a0d0e60346'  # id
        'order-1001'  # order_id
        'sbp'  # service
        '874dfb1e-dbdb-4747-a1c0-005969725b74'  # shop_id
        'PAID'  # status
    )
    assert payload == expected


def test_valid_signature_accepted() -> None:
    service = ParityPayService()
    body = service.parse_callback_body(INVOICE_BODY)
    signature = _sign(service.build_signature_payload(body))

    assert service.verify_callback_signature(body, signature) is True
    assert service.verify_callback_signature(body, signature.upper()) is True


def test_tampered_amount_breaks_signature() -> None:
    service = ParityPayService()
    body = service.parse_callback_body(INVOICE_BODY)
    signature = _sign(service.build_signature_payload(body))

    body['amount'] = '99999.00'
    assert service.verify_callback_signature(body, signature) is False


def test_wrong_key_and_empty_signature_rejected() -> None:
    service = ParityPayService()
    body = service.parse_callback_body(INVOICE_BODY)

    assert service.verify_callback_signature(body, _sign(service.build_signature_payload(body), 'other')) is False
    assert service.verify_callback_signature(body, None) is False
    assert service.verify_callback_signature(body, '') is False


def test_blank_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PARITYPAY_CALLBACK_SECRET', '', raising=False)
    service = ParityPayService()
    body = service.parse_callback_body(INVOICE_BODY)

    forged = hmac.new(b'', service.build_signature_payload(body).encode(), hashlib.sha256).hexdigest()
    assert service.verify_callback_signature(body, forged) is False


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        (b'{"a":1200}', '1200'),
        (b'{"a":1200.0}', '1200.0'),
        (b'{"a":1200.00}', '1200.00'),
        (b'{"a":"1200.00"}', '1200.00'),
        (b'{"a":0.1}', '0.1'),
    ],
)
def test_number_text_from_json_is_preserved(raw: bytes, expected: str) -> None:
    """Числа не должны переформатироваться: иначе подпись отправителя не сойдётся."""
    service = ParityPayService()
    body = service.parse_callback_body(raw)

    assert body['a'] == expected
    assert service.build_signature_payload(body) == expected


def test_parse_rejects_non_object_and_broken_json() -> None:
    service = ParityPayService()
    assert service.parse_callback_body(b'[1,2,3]') is None
    assert service.parse_callback_body(b'{"a": broken') is None


# ---------------------------------------------------------------------------
# Деньги: у провайдера рубли дробным числом, у нас целые копейки
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('1250.00', 125000),
        (1500.5, 150050),
        (990, 99000),
        ('990', 99000),
        ('0.01', 1),
        (199.0, 19900),
        ('1 250.00', None),  # пробел — не наш формат
        ('abc', None),
        (None, None),
        (True, None),
        ('100.005', None),  # доли копейки не округляем молча
    ],
)
def test_amount_to_kopeks(value: object, expected: int | None) -> None:
    assert amount_to_kopeks(value) == expected


@pytest.mark.parametrize(('kopeks', 'expected'), [(19900, 199.0), (150050, 1500.5), (1, 0.01), (99000, 990.0)])
def test_kopeks_to_amount(kopeks: int, expected: float) -> None:
    assert kopeks_to_amount(kopeks) == expected


def test_amount_roundtrip_has_no_float_drift() -> None:
    """Через float 0.1+0.2 ломается; здесь Decimal и обратный путь обязан сходиться."""
    for kopeks in (1, 99, 100, 19900, 150050, 999999, 10_000_000):
        assert amount_to_kopeks(kopeks_to_amount(kopeks)) == kopeks
