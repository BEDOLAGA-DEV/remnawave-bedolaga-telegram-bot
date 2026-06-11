from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from app.services import overpay_certificate_service as cert_service


@pytest.fixture(scope='module')
def p12_bytes():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'overpay-test')])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(b'test', key, cert, None, BestAvailableEncryption(b'secret'))


@pytest.fixture
def stubbed_service(monkeypatch, tmp_path):
    config_service = SimpleNamespace(set_value=AsyncMock(), is_env_overridden=lambda key: False)
    overpay = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(cert_service, 'CERTS_DIR', tmp_path / 'certs')
    monkeypatch.setattr(cert_service, 'bot_configuration_service', config_service)
    monkeypatch.setattr(cert_service, 'overpay_service', overpay)
    return config_service


def _fake_upload(data: bytes):
    return SimpleNamespace(read=AsyncMock(return_value=data), close=AsyncMock())


def test_admin_overpay_certificate_routes_registered():
    from app.cabinet.routes import router

    methods = set()
    for route in router.routes:
        if route.path == '/cabinet/admin/overpay/certificate':
            methods |= route.methods
    assert methods == {'GET', 'POST', 'DELETE'}


@pytest.mark.asyncio
async def test_upload_certificate_commits(stubbed_service, p12_bytes):
    from app.cabinet.routes import admin_overpay_certificate

    db = AsyncMock()
    response = await admin_overpay_certificate.upload_certificate(
        file=_fake_upload(p12_bytes),
        passphrase='secret',
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert response.subject == 'CN=overpay-test'
    assert response.path == str(cert_service.get_canonical_path())
    assert response.warning is None
    assert cert_service.get_canonical_path().read_bytes() == p12_bytes
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_certificate_env_locked_warning(stubbed_service, p12_bytes):
    from app.cabinet.routes import admin_overpay_certificate

    stubbed_service.is_env_overridden = lambda key: True
    db = AsyncMock()
    response = await admin_overpay_certificate.upload_certificate(
        file=_fake_upload(p12_bytes),
        passphrase='secret',
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert response.env_locked_path is True
    assert response.env_locked_passphrase is True
    assert response.warning
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_certificate_invalid_returns_422(stubbed_service):
    from app.cabinet.routes import admin_overpay_certificate

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await admin_overpay_certificate.upload_certificate(
            file=_fake_upload(b'garbage'),
            passphrase='',
            admin=SimpleNamespace(id=1),
            db=db,
        )

    assert exc_info.value.status_code == 422
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_certificate_oversize_returns_413(stubbed_service):
    from app.cabinet.routes import admin_overpay_certificate

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await admin_overpay_certificate.upload_certificate(
            file=_fake_upload(b'0' * (cert_service.MAX_P12_SIZE + 1)),
            passphrase='',
            admin=SimpleNamespace(id=1),
            db=db,
        )

    assert exc_info.value.status_code == 413
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_certificate_commits(stubbed_service, p12_bytes):
    from app.cabinet.routes import admin_overpay_certificate

    db = AsyncMock()
    await admin_overpay_certificate.upload_certificate(
        file=_fake_upload(p12_bytes),
        passphrase='secret',
        admin=SimpleNamespace(id=1),
        db=db,
    )
    db.reset_mock()

    await admin_overpay_certificate.delete_certificate(admin=SimpleNamespace(id=1), db=db)

    assert not cert_service.get_canonical_path().exists()
    db.commit.assert_awaited_once()
