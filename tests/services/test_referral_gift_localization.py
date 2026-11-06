"""Referral and gift notices use each recipient's locale without changing rewards."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.localization.texts import get_texts
from app.services import guest_purchase_service, referral_service
from app.services.referral_reward_service import RewardEvent
from tests.services import test_referral_rewards_baseline as baseline


@pytest.fixture
def wired(monkeypatch):
    data = baseline.wired.__wrapped__(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_referral_levels_scheme', lambda _: False)
    return data


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize(
    'branch,amount,expected', [('below', 4000, [1000]), ('first', 20000, [5000, 12000]), ('repeat', 20000, [5000])]
)
@pytest.mark.asyncio
async def test_classic_rewards_and_recipient_locales(wired, language, branch, amount, expected):
    wired.referrer.language = language
    wired.referral.language = 'fa' if language != 'fa' else 'en'
    wired.referral.full_name = '<name>&'
    wired.referral.has_made_first_topup = branch == 'repeat'
    assert await referral_service.process_referral_topup(AsyncMock(), 2, amount, bot=object()) is True
    assert [credit['amount'] for credit in wired.balance_credits] == expected
    calls = referral_service.send_referral_notification.await_args_list
    inviter_call = next(call for call in calls if call.kwargs['user'] is wired.referrer)
    text = inviter_call.args[2]
    texts = get_texts(language)
    if branch == 'first':
        detail = ' + '.join(
            [
                texts.t('REFERRAL_GIFT_UI_028').format(value1=settings.format_price(7000)),
                texts.t('REFERRAL_GIFT_UI_029').format(value1=25, value2=settings.format_price(5000)),
            ]
        )
        assert (
            texts.t('REFERRAL_GIFT_UI_024').format(
                value1='&lt;name&gt;&amp;',
                value2=settings.format_price(amount),
                value3=settings.format_price(12000),
                value4=detail,
            )
            in text
        )
        referee_call = next(call for call in calls if call.kwargs['user'] is wired.referral)
        assert referee_call.args[2] == get_texts(wired.referral.language).t('REFERRAL_GIFT_UI_022').format(
            value1=settings.format_price(5000)
        )
        assert wired.earnings[0]['reason'] == 'referral_first_topup'
    else:
        assert text == texts.t('REFERRAL_GIFT_UI_026').format(
            value1='&lt;name&gt;&amp;',
            value2=settings.format_price(amount),
            value3=25,
            value4=settings.format_price(expected[0]),
        )
        assert wired.earnings[0]['reason'] == 'referral_commission_topup'
    assert '<name>' not in text


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('levels', [False, True])
@pytest.mark.asyncio
async def test_registration_uses_referee_and_referrer_language(monkeypatch, wired, language, levels):
    from app.services import referral_reward_service
    from app.services.referral_contest_service import referral_contest_service

    wired.referral.language = language
    wired.referrer.language = 'fa' if language != 'fa' else 'en'
    wired.referral.full_name = '<new>'
    wired.referrer.full_name = '<inviter>'
    monkeypatch.setattr(type(settings), 'is_referral_levels_scheme', lambda _: levels)
    monkeypatch.setattr(referral_contest_service, 'on_referral_registration', AsyncMock())
    promise = AsyncMock(return_value='reward')
    active = AsyncMock(return_value=['level reward'])
    monkeypatch.setattr(referral_reward_service, 'describe_referee_bonus', promise)
    monkeypatch.setattr(referral_reward_service, 'describe_active_levels', active)
    monkeypatch.setattr(referral_reward_service, 'award_referral_rewards', AsyncMock(return_value=[]))
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    assert await referral_service.process_referral_registration(db, 2, 1, bot=object()) is True
    welcome, inviter = referral_service.send_referral_notification.await_args_list
    assert get_texts(language).t('REFERRAL_GIFT_UI_007').format(value1='&lt;inviter&gt;') in welcome.args[2]
    if levels:
        promise.assert_awaited_once_with(db, referrer=wired.referrer, language=language)
        active.assert_awaited_once_with(db, viewer=wired.referrer, language=wired.referrer.language)
        assert (
            get_texts(wired.referrer.language).t('REFERRAL_GIFT_UI_011').format(value1='&lt;new&gt;') in inviter.args[2]
        )
    else:
        assert (
            get_texts(language)
            .t('REFERRAL_GIFT_UI_015')
            .format(value1=settings.format_price(5000), value2=settings.format_price(10000))
            == welcome.kwargs['bonus_promise']
        )
        assert (
            get_texts(wired.referrer.language)
            .t('REFERRAL_GIFT_UI_008')
            .format(value1='&lt;new&gt;', value2=settings.format_price(10000))
            in inviter.args[2]
        )
    assert wired.earnings[0]['reason'] == 'referral_registration_pending'
    assert wired.earnings[0]['amount_kopeks'] == 0


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('event', [RewardEvent.REGISTRATION, RewardEvent.FIRST_TOPUP, RewardEvent.REPEAT_TOPUP])
@pytest.mark.asyncio
async def test_level_reward_helpers_use_actual_recipient(monkeypatch, language, event):
    from app.database.crud import user as user_crud

    recipient = SimpleNamespace(id=1, telegram_id=101, language=language)
    referee = SimpleNamespace(id=2, full_name='<name>&', language='ru')
    outcome = SimpleNamespace(
        granted_anything=True,
        money_credited=1234,
        days_credited=7,
        tariff_name='<plan>&',
        component=SimpleNamespace(recipient_id=1, is_referrer=True, level=1),
    )
    monkeypatch.setattr(user_crud, 'get_user_by_id', AsyncMock(return_value=recipient))
    send = AsyncMock()
    monkeypatch.setattr(referral_service, 'send_referral_notification', send)
    await referral_service._notify_level_outcome(object(), None, referee, outcome, event=event)
    call = send.await_args
    assert call.args[1] == 101
    assert call.kwargs['bonus_kopeks'] == 1234
    assert call.kwargs['bonus_days'] == 7
    assert call.kwargs['tariff_name'] == '<plan>&'
    assert '&lt;name&gt;&amp;' in call.args[2]
    assert '&lt;plan&gt;&amp;' in call.args[2]
    texts = get_texts(language)
    assert texts.t('REFERRAL_GIFT_UI_005').split('{value1}')[0] in call.args[2]
    assert settings.format_price(1234) in call.args[2]


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('pending', [False, True])
@pytest.mark.asyncio
async def test_gift_preserves_custom_message_and_activation_callback(monkeypatch, language, pending):
    from app import bot_factory

    purchase = SimpleNamespace(
        id=7,
        user=SimpleNamespace(telegram_id=101, language=language),
        contact_value='<sender>&',
        gift_message='Custom <b>gift</b> & текст',
        period_days=31,
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    context = AsyncMock()
    context.__aenter__.return_value = bot
    monkeypatch.setattr(bot_factory, 'create_bot', lambda: context)
    monkeypatch.setattr(settings, 'BOT_TOKEN', 'test-token')
    await guest_purchase_service._send_telegram_gift_notification(
        purchase, is_pending_activation=pending, tariff_name='<plan>'
    )
    call = bot.send_message.await_args
    assert call.kwargs['chat_id'] == 101
    text = call.kwargs['text']
    texts = get_texts(language)
    assert texts.t('REFERRAL_GIFT_UI_032').format(value1=31) in text
    assert texts.t('REFERRAL_GIFT_UI_031').format(value1='&lt;sender&gt;&amp;') in text
    assert 'Custom &lt;b&gt;gift&lt;/b&gt; &amp; текст' in text
    assert '&lt;plan&gt;' in text
    if pending:
        assert texts.t('REFERRAL_GIFT_UI_033') in text
        button = call.kwargs['reply_markup'].inline_keyboard[0][0]
        assert button.callback_data == 'gift_activate:7'
        assert button.text == texts.t('REFERRAL_GIFT_UI_034')
    else:
        assert call.kwargs['reply_markup'] is None


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_unused_purchase_notice_keeps_existing_reward_math(wired, language):
    """Exercise the isolated legacy helper without wiring it into purchase flows."""
    wired.referrer.language = language
    wired.referral.full_name = '<name>&'
    wired.referral.has_had_paid_subscription = False
    assert (
        await referral_service.process_referral_purchase(AsyncMock(), 2, 10000, transaction_id=7, bot=object()) is True
    )
    assert [credit['amount'] for credit in wired.balance_credits] == [2500]
    assert wired.earnings[0]['reason'] == 'referral_commission'
    assert wired.earnings[0]['referral_transaction_id'] == 7
    call = referral_service.send_referral_notification.await_args
    assert call.args[2] == get_texts(language).t('REFERRAL_GIFT_UI_035').format(
        value1='&lt;name&gt;&amp;', value2=settings.format_price(10000), value3=25, value4=settings.format_price(2500)
    )
