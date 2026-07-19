"""Сообщение после сохранения настройки говорит правду про env-переопределение (#2749).

set_value пишет значение в БД всегда, но ключи, заданные через окружение,
рантайм продолжает читать из .env. Раньше бот в этом случае всё равно отвечал
«✅ Настройка обновлена» — админ видел подтверждение, а поведение не менялось
(классический случай — вся секция рефералки, приехавшая из .env.example).
"""

from __future__ import annotations

from app.handlers.admin.bot_configuration import _build_save_confirmation
from app.services.system_settings_service import bot_configuration_service


def test_env_pinned_setting_warns_instead_of_ok(monkeypatch):
    monkeypatch.setattr(bot_configuration_service, '_env_override_keys', {'REFERRAL_COMMISSION_PERCENT'})

    message = _build_save_confirmation('REFERRAL_COMMISSION_PERCENT')

    assert 'не применено' in message
    assert 'REFERRAL_COMMISSION_PERCENT' in message
    assert '✅' not in message


def test_regular_setting_reports_ok(monkeypatch):
    monkeypatch.setattr(bot_configuration_service, '_env_override_keys', set())

    assert _build_save_confirmation('SUPPORT_USERNAME') == '✅ Настройка обновлена'
