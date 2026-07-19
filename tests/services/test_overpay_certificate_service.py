import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, NoEncryption, pkcs12
from cryptography.x509.oid import NameOID

from app.services import overpay_certificate_service as cert_service


@pytest.fixture(scope='module')
def cert_and_key():
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
    return cert, key


def _build_p12(cert, key, password: bytes | None, cas=None) -> bytes:
    encryption = BestAvailableEncryption(password) if password else NoEncryption()
    return pkcs12.serialize_key_and_certificates(b'test', key, cert, cas, encryption)


def test_validate_p12_with_password(cert_and_key):
    cert, key = cert_and_key
    p12_bytes = _build_p12(cert, key, b'secret')

    metadata = cert_service.validate_p12(p12_bytes, 'secret')

    assert metadata['subject'] == 'CN=overpay-test'
    assert datetime.fromisoformat(metadata['not_valid_after']) > datetime.now(UTC)
    assert metadata['has_chain'] is False


def test_validate_p12_without_password(cert_and_key):
    cert, key = cert_and_key
    p12_bytes = _build_p12(cert, key, None, cas=[cert])

    metadata = cert_service.validate_p12(p12_bytes, None)

    assert metadata['subject'] == 'CN=overpay-test'
    assert metadata['has_chain'] is True


def test_validate_p12_wrong_passphrase(cert_and_key):
    cert, key = cert_and_key
    p12_bytes = _build_p12(cert, key, b'secret')

    with pytest.raises(ValueError, match='неверный пароль'):
        cert_service.validate_p12(p12_bytes, 'wrong')


def test_validate_p12_garbage_bytes():
    with pytest.raises(ValueError, match='неверный пароль'):
        cert_service.validate_p12(b'not a p12 file', None)


def test_validate_p12_oversize():
    with pytest.raises(ValueError, match='1 МБ'):
        cert_service.validate_p12(b'0' * (cert_service.MAX_P12_SIZE + 1), None)


@pytest.fixture
def stubbed_env(monkeypatch, tmp_path):
    config_service = SimpleNamespace(set_value=AsyncMock(), is_env_overridden=lambda key: False)
    overpay = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(cert_service, 'CERTS_DIR', tmp_path / 'certs')
    monkeypatch.setattr(cert_service, 'bot_configuration_service', config_service)
    monkeypatch.setattr(cert_service, 'overpay_service', overpay)
    return config_service, overpay


@pytest.mark.asyncio
async def test_store_certificate(cert_and_key, stubbed_env):
    cert, key = cert_and_key
    config_service, overpay = stubbed_env
    p12_bytes = _build_p12(cert, key, b'secret')
    db = AsyncMock()

    metadata = await cert_service.store_certificate(db, p12_bytes, 'secret')

    stored_path = cert_service.get_canonical_path()
    assert stored_path.read_bytes() == p12_bytes
    assert stat.S_IMODE(stored_path.stat().st_mode) == 0o600
    assert metadata['subject'] == 'CN=overpay-test'
    assert metadata['path'] == str(stored_path)
    assert metadata['env_locked_path'] is False
    assert metadata['warning'] is None
    config_service.set_value.assert_any_await(db, 'OVERPAY_P12_PATH', str(stored_path))
    config_service.set_value.assert_any_await(db, 'OVERPAY_P12_PASSPHRASE', 'secret')
    db.commit.assert_awaited_once()
    overpay.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_certificate_env_locked_warning(cert_and_key, stubbed_env):
    cert, key = cert_and_key
    config_service, _overpay = stubbed_env
    config_service.is_env_overridden = lambda key: key == 'OVERPAY_P12_PATH'
    p12_bytes = _build_p12(cert, key, None)
    db = AsyncMock()

    metadata = await cert_service.store_certificate(db, p12_bytes, None)

    assert metadata['env_locked_path'] is True
    assert metadata['env_locked_passphrase'] is False
    assert 'переменные окружения' in metadata['warning']
    assert cert_service.get_canonical_path().exists()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_certificate_invalid_writes_nothing(stubbed_env):
    config_service, overpay = stubbed_env
    db = AsyncMock()

    with pytest.raises(ValueError):
        await cert_service.store_certificate(db, b'garbage', None)

    assert not cert_service.get_canonical_path().exists()
    config_service.set_value.assert_not_awaited()
    db.commit.assert_not_awaited()
    overpay.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_certificate(cert_and_key, stubbed_env):
    cert, key = cert_and_key
    config_service, overpay = stubbed_env
    db = AsyncMock()
    await cert_service.store_certificate(db, _build_p12(cert, key, None), None)
    db.reset_mock()
    config_service.set_value.reset_mock()
    overpay.close.reset_mock()

    await cert_service.delete_certificate(db)

    assert not cert_service.get_canonical_path().exists()
    config_service.set_value.assert_any_await(db, 'OVERPAY_P12_PATH', None)
    config_service.set_value.assert_any_await(db, 'OVERPAY_P12_PASSPHRASE', None)
    db.commit.assert_awaited_once()
    overpay.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_status(cert_and_key, stubbed_env, monkeypatch):
    cert, key = cert_and_key
    monkeypatch.setattr(cert_service.settings, 'OVERPAY_P12_PATH', None, raising=False)
    monkeypatch.setattr(cert_service.settings, 'OVERPAY_P12_PASSPHRASE', 'secret', raising=False)

    status = cert_service.get_status()
    assert status['uploaded'] is False
    assert status['valid'] is False
    assert status['path'] == str(cert_service.get_canonical_path())

    db = AsyncMock()
    await cert_service.store_certificate(db, _build_p12(cert, key, b'secret'), 'secret')

    status = cert_service.get_status()
    assert status['uploaded'] is True
    assert status['valid'] is True
    assert status['subject'] == 'CN=overpay-test'

    monkeypatch.setattr(cert_service.settings, 'OVERPAY_P12_PASSPHRASE', 'wrong', raising=False)
    status = cert_service.get_status()
    assert status['uploaded'] is True
    assert status['valid'] is False
