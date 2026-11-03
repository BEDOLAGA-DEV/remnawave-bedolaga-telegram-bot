"""Render every built-in email and editor metadata in every supported language."""

import re

import pytest

from app.cabinet.routes.admin_email_templates import (
    AVAILABLE_LANGUAGES,
    EDITOR_LANGUAGES,
    LAYOUT_TEMPLATE_META,
    SAMPLE_CONTEXTS,
    TEMPLATE_TYPES,
    _get_default_template,
    _placeholder_context,
)
from app.cabinet.services.email_layout import DEFAULT_EMAIL_LAYOUT, extract_content_fragment, render_email_layout


@pytest.mark.parametrize('language', AVAILABLE_LANGUAGES)
@pytest.mark.parametrize('meta', TEMPLATE_TYPES, ids=lambda meta: meta['type'])
def test_real_email_body_and_editor_metadata_are_localized(meta, language):
    assert language in EDITOR_LANGUAGES
    assert meta['label'][language]
    assert meta['description'][language]
    context = SAMPLE_CONTEXTS[meta['type']]
    rendered = _get_default_template(meta['type'], language, context)
    assert rendered and rendered['subject']
    content = extract_content_fragment(rendered['body_html'])
    assert content
    if language != 'ru':
        ru = _get_default_template(meta['type'], 'ru', context)
        assert content != extract_content_fragment(ru['body_html']), f'{meta["type"]} falls back to RU'
    if language == 'fa':
        assert re.search(r'[\u0600-\u06ff]', rendered['subject'])
        assert '<html lang="fa" dir="rtl">' in rendered['body_html']
    editor = _get_default_template(meta['type'], language, _placeholder_context(meta['type']))
    english = _get_default_template(meta['type'], 'en', _placeholder_context(meta['type']))
    assert sorted(re.findall(r'href="([^"]+)"', editor['body_html'])) == sorted(
        re.findall(r'href="([^"]+)"', english['body_html'])
    )


def test_custom_layout_is_not_rewritten_for_rtl():
    custom = '<html lang="en" dir="ltr"><body>{content}</body></html>'
    assert (
        render_email_layout(custom, 'fa', {'content': '<p>Operator content</p>'}, mark_content=False)
        == '<html lang="en" dir="ltr"><body><p>Operator content</p></body></html>'
    )
    assert render_email_layout(DEFAULT_EMAIL_LAYOUT, 'fa', {'content': 'متن'}).count('dir="rtl"') == 1
    assert set(LAYOUT_TEMPLATE_META['label']) == set(AVAILABLE_LANGUAGES)


@pytest.mark.parametrize(
    ('language', 'plan', 'level', 'bonus'),
    [
        ('fa', 'طرح:', 'سطح شبکه شما:', 'پاداش شما:'),
        ('ua', 'Тариф:', 'Рівень вашої мережі:', 'Ваш бонус:'),
        ('zh', '套餐', '您的推荐网络层级：', '您的奖励：'),
    ],
)
def test_optional_nested_blocks_use_recipient_language(language, plan, level, bonus):
    renewed = _get_default_template(
        'subscription_renewed', language, {'tariff_name': 'Operator <plan>', 'new_expires_at': '2030-01-01'}
    )
    assert plan in renewed['body_html']
    assert 'Operator &lt;plan&gt;' in renewed['body_html']
    referral = _get_default_template(
        'referral_bonus', language, {'level': 2, 'formatted_reward': '7 days', 'referral_name': 'Operator'}
    )
    assert level in referral['body_html']
    welcome = _get_default_template(
        'referral_welcome', language, {'referrer_name': 'Operator', 'bonus_promise': 'Custom promise'}
    )
    assert bonus in welcome['body_html']
    assert 'Custom promise' in welcome['body_html']
    promo = _get_default_template(
        'promo_offer', language, {'message_html': '<em>Operator custom</em>', 'valid_hours': 24, 'discount_percent': 0}
    )
    assert '<em>Operator custom</em>' in promo['body_html']


@pytest.mark.parametrize(
    'notification_type,date_field',
    [
        ('subscription_activated', 'expires_at'),
        ('subscription_expiring', 'expires_at'),
        ('subscription_renewed', 'new_expires_at'),
        ('autopay_success', 'new_expires_at'),
        ('grace_access_granted', 'until'),
        ('winback_discount', 'expires_at'),
    ],
)
def test_fa_dates_keep_the_original_value_in_ltr_isolation(notification_type, date_field):
    context = {**SAMPLE_CONTEXTS[notification_type], date_field: '2030-12-31 23:59'}
    rendered = _get_default_template(notification_type, 'fa', context)
    assert '<bdi dir="ltr">2030-12-31 23:59</bdi>' in rendered['body_html']


def test_fa_auth_code_and_visible_url_are_ltr_without_changing_values():
    code = _get_default_template('email_change_code', 'fa', {'code': '123456', 'expire_minutes': 10})
    assert '<p dir="ltr" style=' in code['body_html']
    assert '>123456</p>' in code['body_html']
    verification = _get_default_template(
        'email_verification',
        'fa',
        {'verification_url': 'https://cabinet.example/verify?token=sample', 'expire_hours': 1},
    )
    assert '<bdi dir="ltr">https://cabinet.example/verify?token=sample</bdi>' in verification['body_html']
    assert 'href="https://cabinet.example/verify?token=sample"' in verification['body_html']


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_referral_reward_plan_is_plain_data_in_email_html(language):
    from app.cabinet.services.email_templates import EmailNotificationTemplates

    reward = '7 days <b>Operator plan</b> & custom'
    context = {'formatted_reward': reward, 'referral_name': 'Example', 'level': 1}
    rendered = EmailNotificationTemplates()._referral_bonus_template(language, context)
    assert '<b>Operator plan</b>' not in rendered['body_html']
    assert '&lt;b&gt;Operator plan&lt;/b&gt; &amp; custom' in rendered['body_html']
    assert reward in rendered['subject']
    assert context['formatted_reward'] == reward
