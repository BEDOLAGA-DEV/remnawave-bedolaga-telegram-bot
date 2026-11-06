"""Locale rendering preserves numeric inputs and escaped contest participant names."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.handlers.admin import contests, referral_levels
from app.localization.texts import get_texts
from app.services.referral_reward_service import legacy_percent_for_import


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_virtual_participants_preserve_escaped_names_and_count(monkeypatch, language):
    monkeypatch.setattr(contests, 'get_referral_contest', AsyncMock(return_value=SimpleNamespace(title='<title>')))
    monkeypatch.setattr(
        contests,
        'list_virtual_participants',
        AsyncMock(return_value=[SimpleNamespace(id=7, display_name='<name>&', referral_count=123)]),
    )
    callback = SimpleNamespace(
        data='admin_contest_vp_9', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    await inspect.unwrap(contests.show_virtual_participants)(callback, SimpleNamespace(language=language), None)
    texts = get_texts(language)
    content = callback.message.edit_text.await_args.args[0]
    assert texts.t('ADMIN_CONTEST_UI_037').format(value1='&lt;title&gt;') in content
    assert texts.t('ADMIN_CONTEST_UI_063').format(value1='&lt;name&gt;&amp;', value2=123) in content
    assert '<name>' not in content
    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    assert keyboard.inline_keyboard[0][0].callback_data == 'admin_contest_vp_add_9'


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_referral_value_keeps_kopeks_and_uses_localized_field(monkeypatch, language):
    save = AsyncMock()
    monkeypatch.setattr(referral_levels, 'upsert_reward_level', save)
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={'referral_level': 3, 'referral_field': 'referrer_fixed_kopeks'}),
        clear=AsyncMock(),
    )
    message = SimpleNamespace(text='12,34', answer=AsyncMock())
    await inspect.unwrap(referral_levels.process_level_value)(message, SimpleNamespace(language=language), None, state)
    save.assert_awaited_once_with(None, 3, referrer_fixed_kopeks=1234)
    texts = get_texts(language)
    assert message.answer.await_args.args[0] == texts.t('ADMIN_REFERRAL_LEVEL_UI_073').format(
        value1=texts.t('ADMIN_REFERRAL_LEVEL_UI_013'), value2=settings.format_price(1234)
    )


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_referral_helper_and_legacy_notes_use_requested_language(monkeypatch, language):
    texts = get_texts(language)
    assert referral_levels._fmt_days(17, None, texts=texts) == texts.t('ADMIN_REFERRAL_LEVEL_UI_007').format(
        value1=17, value2=texts.t('ADMIN_REFERRAL_LEVEL_UI_028')
    )
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', 19)
    monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 11)
    monkeypatch.setattr(settings, 'REFERRAL_RECURRING_COMMISSION_TIERS', '5:20')
    percent, notes = legacy_percent_for_import(language)
    assert percent == 19
    assert notes == [
        texts.t('ADMIN_REFERRAL_LEVEL_LEGACY_FIRST').format(first_payment=19, general_percent=11),
        texts.t('ADMIN_REFERRAL_LEVEL_LEGACY_TIERS'),
    ]


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_daily_bulk_reset_uses_original_round_ids_and_localized_total(monkeypatch, language):
    from app.database.crud import contest as contest_crud
    from app.handlers.admin import daily_contests

    monkeypatch.setattr(
        contest_crud, 'get_active_rounds', AsyncMock(return_value=[SimpleNamespace(id=11), SimpleNamespace(id=22)])
    )
    clear = AsyncMock(side_effect=[4, 8])
    monkeypatch.setattr(daily_contests, 'clear_attempts', clear)
    monkeypatch.setattr(daily_contests, 'show_daily_contests', AsyncMock())
    callback = SimpleNamespace(answer=AsyncMock())
    await inspect.unwrap(daily_contests.reset_all_attempts)(callback, SimpleNamespace(language=language), None)
    assert [call.args for call in clear.await_args_list] == [(None, 11), (None, 22)]
    callback.answer.assert_awaited_once_with(
        get_texts(language).t('ADMIN_DAILY_CONTEST_UI_015').format(value1=12), show_alert=True
    )


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_daily_numeric_edit_preserves_winner_limit(monkeypatch, language):
    from app.handlers.admin import daily_contests

    template = SimpleNamespace(id=7)
    monkeypatch.setattr(daily_contests, '_get_template', AsyncMock(return_value=template))
    save = AsyncMock()
    monkeypatch.setattr(daily_contests, 'update_template_fields', save)
    message = SimpleNamespace(text='31', answer=AsyncMock())
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={'template_id': 7, 'field': 'max_winners'}), clear=AsyncMock()
    )
    await inspect.unwrap(daily_contests.process_edit_field)(message, state, SimpleNamespace(language=language), None)
    save.assert_awaited_once_with(None, template, max_winners=31)
    assert daily_contests._field_labels(get_texts(language))['max_winners'] == get_texts(language).t(
        'ADMIN_DAILY_CONTEST_UI_003'
    )
