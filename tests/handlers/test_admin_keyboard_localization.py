"""Shared admin keyboards must not fall back to Russian outside Russian locales."""

import re

import pytest

from app.keyboards import admin


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('kind', ['contest', 'restrictions', 'monitoring'])
def test_shared_admin_buttons_translate_without_changing_actions(language, kind):
    def build(lang):
        if kind == 'contest':
            return admin.get_referral_contest_manage_keyboard(7, is_active=True, can_delete=True, language=lang)
        if kind == 'restrictions':
            return admin.get_user_restrictions_keyboard(
                7, restriction_topup=True, restriction_subscription=True, language=lang
            )
        return admin.get_monitoring_keyboard(language=lang)

    baseline = build('ru')
    localized = build(language)
    assert [[b.callback_data for b in row] for row in localized.inline_keyboard] == [
        [b.callback_data for b in row] for row in baseline.inline_keyboard
    ]
    if language in {'en', 'zh', 'fa'}:
        assert not re.search('[А-Яа-яЁё]', '\n'.join(b.text for row in localized.inline_keyboard for b in row))
