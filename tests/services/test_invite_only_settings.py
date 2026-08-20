from app.config import Settings
from app.services.system_settings_service import bot_configuration_service as service


def test_invite_only_defaults_are_backward_compatible(monkeypatch):
    monkeypatch.delenv('INVITE_ONLY_ENABLED', raising=False)
    monkeypatch.delenv('INVITE_ONLY_ALLOW_GIFT_LINKS', raising=False)

    config = Settings(BOT_TOKEN='test-token')

    assert config.INVITE_ONLY_ENABLED is False
    assert config.INVITE_ONLY_ALLOW_GIFT_LINKS is True


def test_invite_only_settings_are_exposed_in_registration_access_category():
    service._definitions.clear()
    service.initialize_definitions()

    enabled = service.get_definition('INVITE_ONLY_ENABLED')
    gifts = service.get_definition('INVITE_ONLY_ALLOW_GIFT_LINKS')

    assert enabled.category_key == 'REGISTRATION_ACCESS'
    assert gifts.category_key == 'REGISTRATION_ACCESS'
    assert enabled.category_label == '🔐 Регистрация и доступ'
