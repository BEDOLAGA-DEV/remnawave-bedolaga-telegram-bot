from app.config import Settings


def _settings(**overrides):
    base = {
        'BOT_TOKEN': 'test-token',
        'YOOKASSA_ENABLED': True,
        'YOOKASSA_DISPLAY_NAME': 'Legacy YooKassa',
        'YOOKASSA_SHOP_ID': 'legacy-shop',
        'YOOKASSA_SECRET_KEY': 'legacy-secret',
        'YOOKASSA_RETURN_URL': 'https://legacy.example/return',
        'YOOKASSA_SBP_ENABLED': True,
        'YOOKASSA_RECURRENT_ENABLED': True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_legacy_yookassa_enables_bot_and_cabinet_when_scoped_env_is_unset():
    settings = _settings()

    assert settings.is_yookassa_enabled('bot') is True
    assert settings.is_yookassa_enabled('cabinet') is True


def test_cabinet_enabled_false_disables_only_cabinet_with_legacy_enabled():
    settings = _settings(YOOKASSA_CABINET_ENABLED=False)

    assert settings.is_yookassa_enabled('bot') is True
    assert settings.is_yookassa_enabled('cabinet') is False


def test_bot_enabled_false_disables_only_bot_with_legacy_enabled():
    settings = _settings(YOOKASSA_BOT_ENABLED=False)

    assert settings.is_yookassa_enabled('bot') is False
    assert settings.is_yookassa_enabled('cabinet') is True


def test_scoped_return_display_sbp_and_recurrent_override_legacy_values():
    settings = _settings(
        YOOKASSA_BOT_DISPLAY_NAME='Bot YooKassa',
        YOOKASSA_BOT_RETURN_URL='https://bot.example/return',
        YOOKASSA_BOT_SBP_ENABLED=False,
        YOOKASSA_BOT_RECURRENT_ENABLED=False,
        YOOKASSA_CABINET_DISPLAY_NAME='Cabinet YooKassa',
        YOOKASSA_CABINET_RETURN_URL='https://cabinet.example/return',
        YOOKASSA_CABINET_SBP_ENABLED=False,
        YOOKASSA_CABINET_RECURRENT_ENABLED=False,
    )

    assert settings.get_yookassa_display_name('bot') == 'Bot YooKassa'
    assert settings.get_yookassa_return_url('bot') == 'https://bot.example/return'
    assert settings.is_yookassa_sbp_enabled('bot') is False
    assert settings.is_yookassa_recurrent_enabled('bot') is False
    assert settings.get_yookassa_display_name('cabinet') == 'Cabinet YooKassa'
    assert settings.get_yookassa_return_url('cabinet') == 'https://cabinet.example/return'
    assert settings.is_yookassa_sbp_enabled('cabinet') is False
    assert settings.is_yookassa_recurrent_enabled('cabinet') is False


def test_scoped_return_display_sbp_and_recurrent_fallback_to_legacy_values():
    settings = _settings()

    assert settings.get_yookassa_display_name('bot') == 'Legacy YooKassa'
    assert settings.get_yookassa_return_url('bot') == 'https://legacy.example/return'
    assert settings.is_yookassa_sbp_enabled('bot') is True
    assert settings.is_yookassa_recurrent_enabled('bot') is True
    assert settings.get_yookassa_display_name('cabinet') == 'Legacy YooKassa'
    assert settings.get_yookassa_return_url('cabinet') == 'https://legacy.example/return'
    assert settings.is_yookassa_sbp_enabled('cabinet') is True
    assert settings.is_yookassa_recurrent_enabled('cabinet') is True


def test_partial_scoped_credentials_do_not_mix_with_legacy_credentials():
    cabinet_shop_only = _settings(
        YOOKASSA_CABINET_ENABLED=True,
        YOOKASSA_CABINET_SHOP_ID='cabinet-shop',
    )
    cabinet_config = cabinet_shop_only.get_yookassa_config('cabinet')

    assert cabinet_config.enabled is False
    assert cabinet_config.shop_id == 'cabinet-shop'
    assert cabinet_config.secret_key is None

    bot_secret_only = _settings(
        YOOKASSA_BOT_ENABLED=True,
        YOOKASSA_BOT_SECRET_KEY='bot-secret',
    )
    bot_config = bot_secret_only.get_yookassa_config('bot')

    assert bot_config.enabled is False
    assert bot_config.shop_id is None
    assert bot_config.secret_key == 'bot-secret'


def test_legacy_no_scope_helpers_keep_existing_behavior():
    settings = _settings(
        YOOKASSA_BOT_ENABLED=False,
        YOOKASSA_BOT_RETURN_URL='https://bot.example/return',
        YOOKASSA_BOT_DISPLAY_NAME='Bot YooKassa',
    )

    assert settings.is_yookassa_enabled() is True
    assert settings.get_yookassa_display_name() == 'Legacy YooKassa'
    assert settings.get_yookassa_return_url() == 'https://legacy.example/return'
