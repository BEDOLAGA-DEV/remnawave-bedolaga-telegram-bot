from types import SimpleNamespace

import pytest

from ai_support_bot.app.services.service_identity import (
    classify_service_source, renewal_payment_request, renewal_verification_answer,
)


@pytest.mark.parametrize(('name', 'url', 'location', 'status'), [
    ('Other VPN', 'https://sub.other-vpn.example/personal-key', 'payment_confirmation', 'foreign'),
    ('v2rayTun VPN | VLESS | HAPP', 'https://sub.other-vpn.example/private', 'payment_confirmation', 'foreign'),
    ('Happ', '', 'client', 'irrelevant'),
    ('Other VPN', 'https://other-vpn.example/', 'advertisement', 'irrelevant'),
    ('Happ', 'https://happ.su/', 'payment_confirmation', 'unknown'),
    ('Банк', 'https://bank.example/', 'merchant', 'unknown'),
    ('', 'https://unknown.example/key', 'payment_confirmation', 'unknown'),
    ('', '', 'unknown', 'unknown'),
    ('SligVPN', 'https://web.slig.app/subscriptions', 'payment_confirmation', 'ours'),
    ('Slig VPN', 'https://t.me/SligVPN_bot?start=paid', 'seller_chat', 'ours'),
    ('', 'https://web.slig.app.evil.example/', 'merchant', 'unknown'),
    ('SligVPN', 'https://web.slig.app.evil.example/', 'merchant', 'conflict'),
    ('Other VPN', 'https://web.slig.app', 'merchant', 'conflict'),
    ('', 'https://t.me/OtherVPN_bot', 'seller_chat', 'unknown'),
    ('', 'https://web.slig.app@other-vpn.example/', 'merchant', 'unknown'),
])
def test_service_identity_distinguishes_seller_client_ad_and_ambiguous_sources(name, url, location, status):
    result = classify_service_source(name, url, location)
    assert result['status'] == status
    assert 'personal-key' not in str(result) and '/private' not in str(result)


def test_owned_subscription_domain_is_recognized_without_sending_key():
    result = classify_service_source('', 'https://custom.example/private-key?secret=123', 'payment_confirmation', {'custom.example'})
    assert result['status'] == 'ours'
    assert result['host'] == 'custom.example'
    assert 'private-key' not in str(result) and 'secret' not in str(result)


def test_trusted_domain_never_trusts_all_telegram_bots():
    result = classify_service_source('', 'https://t.me/OtherVPN_bot', 'seller_chat', {'t.me'})
    assert result['status'] == 'unknown'


@pytest.mark.parametrize('question', [
    'Проплатил 3 мес. подписки, думал продлю имеющиеся, но не получается. Можете помочь?',
    'Оплатила ещё месяц, он к текущей подписке добавился?',
    'Заплатил за продление, срок тот же',
])
def test_new_payment_renewal_claims_need_verification(question):
    assert renewal_payment_request(question, [])


@pytest.mark.parametrize('question', ['К ней 3 проплаченных месяца добавятся?', 'Текущая подписка продлится на 3 месяца?'])
def test_renewal_confirmation_keeps_payment_context_after_old_messages_leave_window(question):
    assert renewal_payment_request(question, [], pending=True)


def test_normal_renewal_instructions_are_not_forced_into_payment_dispute():
    history = [SimpleNamespace(role='user', content='Оплатил другой сервис')]
    assert not renewal_payment_request('Как продлить нашу подписку?', history, pending=True)


@pytest.mark.parametrize('source', [None, {'status': 'ours'}, {'status': 'unknown'}, {'status': 'conflict'}, {'status': 'foreign'}])
def test_no_service_source_can_confirm_paid_months_were_added(source):
    answer, _ = renewal_verification_answer(source, {})
    assert 'уже добавлены' not in answer
    assert 'автоматически' not in answer
    assert 'https://' not in answer


@pytest.mark.parametrize('reference', ['@SligApp', '@sligapp', 'https://t.me/SligApp', 'https://t.me/SligVPN_bot'])
def test_official_bot_aliases_are_recognized(reference):
    from ai_support_bot.app.services.service_identity import official_service_reference
    assert official_service_reference('В телеграм боте ' + reference)['status'] == 'ours'


@pytest.mark.parametrize('reference', ['@SligAppFake', 'https://t.me/SligAppFake', 'https://evil.example/t.me/SligApp'])
def test_similar_names_do_not_become_official_bots(reference):
    from ai_support_bot.app.services.service_identity import official_service_reference
    assert official_service_reference(reference) is None
