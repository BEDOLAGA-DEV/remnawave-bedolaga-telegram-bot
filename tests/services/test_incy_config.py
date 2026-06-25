from app.config import settings


def test_incy_defaults_ready_out_of_the_box():
    assert settings.get_incy_subscription_name() == 'INCY'
    assert 'apps.apple.com' in settings.get_incy_ios_url()
    assert 'play.google.com' in settings.get_incy_android_url()
    assert settings.get_incy_platforms_repo() == 'INCY-DEV/incy-platforms'
    assert settings.get_incy_release_cache_ttl() >= 60


def test_incy_redirect_falls_back_to_happ(monkeypatch):
    monkeypatch.setattr(settings, 'INCY_CONNECT_REDIRECT_TEMPLATE', None, raising=False)
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_REDIRECT_TEMPLATE', 'https://r.example/?redirect_to=', raising=False)
    assert settings.get_incy_connect_redirect_template() == 'https://r.example/?redirect_to='

    monkeypatch.setattr(settings, 'INCY_CONNECT_REDIRECT_TEMPLATE', 'https://incy.example/?to=', raising=False)
    assert settings.get_incy_connect_redirect_template() == 'https://incy.example/?to='
