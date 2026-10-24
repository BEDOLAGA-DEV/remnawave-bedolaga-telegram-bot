"""DB-backed switches must retain their effective values when startup reloads them."""

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cabinet.routes.admin_partners import PartnerSettingsUpdateRequest, update_partner_settings
from app.cabinet.services.email_type_switch import is_email_type_enabled, set_email_type_enabled
from app.config import settings
from app.database.models import SystemSetting
from app.services import system_settings_service
from app.services.notification_settings_service import NotificationSettingsService
from app.services.system_settings_service import bot_configuration_service
from tests.fixtures.sqlite_memory import memory_session


DEFAULTS = {
    'REFERRAL_WITHDRAWAL_ENABLED': True,
    'REFERRAL_PARTNER_SECTION_VISIBLE': True,
    'REFERRAL_REWARD_SCHEME': 'legacy',
    'REFERRAL_LEVELS_MODE': 'chain',
    'EMAIL_DISABLED_TYPES': '',
    'NOTIFICATION_EXPIRED_1D_ENABLED': True,
    'NOTIFICATION_EXPIRED_WAVE2_ENABLED': True,
    'NOTIFICATION_EXPIRED_WAVE3_ENABLED': True,
}


@pytest.mark.asyncio
async def test_cabinet_switches_remain_effective_after_loading_committed_db(monkeypatch):
    bot_configuration_service.initialize_definitions()
    monkeypatch.setattr(bot_configuration_service, '_overrides_raw', {})
    monkeypatch.setattr(
        bot_configuration_service, '_env_override_keys', bot_configuration_service._env_override_keys - DEFAULTS.keys()
    )
    for key, value in DEFAULTS.items():
        monkeypatch.setattr(settings, key, value)

    async with memory_session(monkeypatch, [SystemSetting.__table__]) as db:
        monkeypatch.setattr(
            system_settings_service, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False)
        )
        await update_partner_settings(
            PartnerSettingsUpdateRequest(withdrawal_enabled=False, partner_section_visible=False),
            admin=SimpleNamespace(id=1),
            db=db,
        )
        await bot_configuration_service.set_value(db, 'REFERRAL_REWARD_SCHEME', 'levels')
        await bot_configuration_service.set_value(db, 'REFERRAL_LEVELS_MODE', 'tiers')
        await set_email_type_enabled(db, 'promo_offer', False)
        assert await NotificationSettingsService.set_expired_1d_enabled(db, False)
        assert await NotificationSettingsService.set_second_wave_enabled(db, False)
        assert await NotificationSettingsService.set_third_wave_enabled(db, False)
        await db.rollback()

        assert settings.REFERRAL_WITHDRAWAL_ENABLED is False
        assert settings.REFERRAL_PARTNER_SECTION_VISIBLE is False
        assert settings.is_referral_levels_scheme()
        assert settings.is_referral_tier_levels()
        assert not is_email_type_enabled('promo_offer')
        assert not NotificationSettingsService.is_expired_1d_enabled()
        assert not NotificationSettingsService.is_second_wave_enabled()
        assert not NotificationSettingsService.is_third_wave_enabled()

        # Drop live values and reload through the same initializer as startup.
        for key, value in DEFAULTS.items():
            setattr(settings, key, value)
        bot_configuration_service._overrides_raw.clear()
        await bot_configuration_service.initialize(sync_web_api_token=False)

        assert settings.REFERRAL_WITHDRAWAL_ENABLED is False
        assert settings.REFERRAL_PARTNER_SECTION_VISIBLE is False
        assert settings.is_referral_levels_scheme()
        assert settings.is_referral_tier_levels()
        assert not is_email_type_enabled('promo_offer')
        assert is_email_type_enabled('password_reset')
        assert not NotificationSettingsService.is_expired_1d_enabled()
        assert not NotificationSettingsService.is_second_wave_enabled()
        assert not NotificationSettingsService.is_third_wave_enabled()


@pytest.mark.asyncio
async def test_disabling_smtp_closes_existing_queue_without_delivery(monkeypatch):
    from datetime import UTC, datetime

    from app.cabinet.services.email_service import email_service
    from app.database.models import EmailQueueItem
    from app.services import email_retry_service

    bot_configuration_service.initialize_definitions()
    monkeypatch.setattr(bot_configuration_service, '_overrides_raw', {})
    monkeypatch.setattr(
        bot_configuration_service, '_env_override_keys', bot_configuration_service._env_override_keys - {'SMTP_HOST'}
    )
    monkeypatch.setattr(settings, 'SMTP_HOST', 'smtp.example')
    monkeypatch.setattr(settings, 'SMTP_FROM_EMAIL', 'sender@example.org')
    monkeypatch.setattr(email_service, '_get_smtp_connection', lambda: pytest.fail('SMTP must not be contacted'))
    assert email_service.is_configured()

    async with memory_session(monkeypatch, [SystemSetting.__table__, EmailQueueItem.__table__]) as db:
        monkeypatch.setattr(
            email_retry_service, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False)
        )
        queued = EmailQueueItem(
            to_email='recipient@example.org',
            subject='Pending test',
            body_html='Disposable content',
            body_text='Disposable content',
            status='pending',
            attempts=2,
            next_attempt_at=datetime.now(UTC),
        )
        db.add(queued)
        await db.commit()
        await bot_configuration_service.set_value(db, 'SMTP_HOST', None)
        assert not email_service.is_configured()
        assert email_service.send_email('recipient@example.org', 'New test', 'Test') is False

        retry = email_retry_service.EmailRetryService()
        await retry._process_due()
        await retry._process_due()
        await db.refresh(queued)
        assert queued.status == 'dead'
        assert queued.attempts == 2
        assert queued.body_html == ''
        assert queued.body_text is None
        assert queued.last_error == email_retry_service.NO_SMTP_REASON
