from types import SimpleNamespace


def test_current_oauth_provider_must_be_trusted_for_admin_email_recovery(monkeypatch):
    from app.cabinet.routes import oauth

    monkeypatch.setattr(type(oauth.settings), 'get_admin_emails', lambda self: ['admin@example.com'])
    info = SimpleNamespace(email='admin@example.com', email_verified=True)

    assert oauth._oauth_current_proof_is_admin('google', info) is True
    assert oauth._oauth_current_proof_is_admin('discord', info) is True
    assert oauth._oauth_current_proof_is_admin('vk', info) is False
    assert oauth._oauth_current_proof_is_admin('yandex', info) is False
