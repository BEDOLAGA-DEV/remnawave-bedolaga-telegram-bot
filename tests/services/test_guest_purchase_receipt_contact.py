"""Адресат чека НПД по гостевой покупке — покупатель, а не одаряемый.

Для подарочных покупок fulfilment-user это получатель подарка
(см. _get_recipient_contact), поэтому чек, адресованный «пользователю покупки»,
уходил не тому: одаряемый узнавал уплаченную дарителем сумму (в письме
GUEST_GIFT_RECEIVED суммы нет), а сам покупатель чек не получал вовсе.
"""

from types import SimpleNamespace

from app.services.guest_purchase_service import _get_receipt_contact


RECIPIENT_TG = 222
RECIPIENT_EMAIL = 'friend@example.com'


def _purchase(**overrides) -> SimpleNamespace:
    base = {
        'is_gift': False,
        'buyer': None,
        'contact_type': 'email',
        'contact_value': 'buyer@example.com',
        'gift_recipient_type': None,
        'gift_recipient_value': None,
    }
    return SimpleNamespace(**(base | overrides))


def _user(telegram_id=None, email=None) -> SimpleNamespace:
    return SimpleNamespace(telegram_id=telegram_id, email=email)


def _gift(**overrides) -> SimpleNamespace:
    gift_defaults = {
        'is_gift': True,
        'gift_recipient_type': 'email',
        'gift_recipient_value': RECIPIENT_EMAIL,
    }
    return _purchase(**(gift_defaults | overrides))


def test_regular_purchase_uses_the_fulfilment_user():
    """Обычная покупка: покупатель и получатель — одно лицо."""
    assert _get_receipt_contact(_purchase(), _user(111, 'buyer@example.com')) == (111, 'buyer@example.com')


def test_gift_receipt_goes_to_the_linked_buyer():
    """Подарок из кабинета: buyer_user_id проставлен — чек уходит дарителю."""
    purchase = _gift(buyer=_user(111, 'buyer@example.com'))

    assert _get_receipt_contact(purchase, _user(RECIPIENT_TG, RECIPIENT_EMAIL)) == (111, 'buyer@example.com')


def test_guest_gift_falls_back_to_the_purchase_contact():
    """Подарок с лендинга: аккаунта дарителя нет, но его почта есть на покупке."""
    purchase = _gift(contact_type='email', contact_value='buyer@example.com')

    assert _get_receipt_contact(purchase, _user(email=RECIPIENT_EMAIL)) == (None, 'buyer@example.com')


def test_guest_gift_with_telegram_contact_has_no_usable_channel():
    """contact_value для telegram — это username, отправить по нему чек нельзя."""
    purchase = _gift(contact_type='telegram', contact_value='@buyer')

    assert _get_receipt_contact(purchase, _user(email=RECIPIENT_EMAIL)) == (None, None)


def test_gift_never_leaks_the_recipient_contacts():
    """Инвариант: контакты одаряемого не попадают в чек ни в одной ветке."""
    recipient = _user(RECIPIENT_TG, RECIPIENT_EMAIL)
    variants = (
        _gift(buyer=_user(111, 'buyer@example.com')),
        _gift(),
        _gift(contact_type='telegram', contact_value='@buyer'),
        # одаряемый указан телеграмом, даритель — почтой
        _gift(gift_recipient_type='telegram', gift_recipient_value='@friend'),
    )

    for purchase in variants:
        telegram_id, email = _get_receipt_contact(purchase, recipient)
        assert telegram_id != RECIPIENT_TG
        assert email != RECIPIENT_EMAIL
