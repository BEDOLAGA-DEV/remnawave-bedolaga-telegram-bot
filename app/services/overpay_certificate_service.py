import os
from pathlib import Path
from typing import Any

import structlog
from cryptography.hazmat.primitives.serialization import pkcs12
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.overpay_service import overpay_service
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)

CERTS_DIR = Path('/app/data/certs')
CERT_FILENAME = 'overpay.p12'
MAX_P12_SIZE = 1024 * 1024

ENV_LOCK_WARNING = (
    'OVERPAY_P12_PATH или OVERPAY_P12_PASSPHRASE заданы через переменные окружения, '
    'поэтому сохранённые в БД значения не применяются. Файл записан в {path}: '
    'если переменная окружения указывает на этот путь, новый сертификат будет использоваться, '
    'иначе обновите переменные окружения и перезапустите бота.'
)


def get_canonical_path() -> Path:
    return CERTS_DIR / CERT_FILENAME


def _env_locked_flags() -> tuple[bool, bool]:
    return (
        bot_configuration_service.is_env_overridden('OVERPAY_P12_PATH'),
        bot_configuration_service.is_env_overridden('OVERPAY_P12_PASSPHRASE'),
    )


def validate_p12(p12_bytes: bytes, passphrase: str | None) -> dict[str, Any]:
    if len(p12_bytes) > MAX_P12_SIZE:
        raise ValueError('Файл сертификата больше 1 МБ')

    passphrase_bytes = passphrase.encode('utf-8') if passphrase else None
    try:
        private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(p12_bytes, passphrase_bytes)
    except Exception as error:
        raise ValueError('Не удалось прочитать P12: неверный пароль или повреждённый файл') from error

    if private_key is None or certificate is None:
        raise ValueError('P12 не содержит приватный ключ и сертификат')

    not_valid_after = getattr(certificate, 'not_valid_after_utc', None) or certificate.not_valid_after
    return {
        'subject': certificate.subject.rfc4514_string(),
        'not_valid_after': not_valid_after.isoformat(),
        'has_chain': bool(additional_certs),
    }


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp_path = path.with_name(f'{path.name}.tmp')
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as tmp_file:
            tmp_file.write(data)
        tmp_path.replace(path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


async def store_certificate(db: AsyncSession, p12_bytes: bytes, passphrase: str | None) -> dict[str, Any]:
    metadata = validate_p12(p12_bytes, passphrase)
    path = get_canonical_path()
    _write_atomic(path, p12_bytes)

    await bot_configuration_service.set_value(db, 'OVERPAY_P12_PATH', str(path))
    await bot_configuration_service.set_value(db, 'OVERPAY_P12_PASSPHRASE', passphrase or '')
    await db.commit()
    await overpay_service.close()

    env_locked_path, env_locked_passphrase = _env_locked_flags()
    metadata['path'] = str(path)
    metadata['env_locked_path'] = env_locked_path
    metadata['env_locked_passphrase'] = env_locked_passphrase
    metadata['warning'] = ENV_LOCK_WARNING.format(path=path) if env_locked_path or env_locked_passphrase else None

    logger.info(
        'Overpay: сертификат сохранён',
        path=str(path),
        subject=metadata['subject'],
        not_valid_after=metadata['not_valid_after'],
    )
    return metadata


async def delete_certificate(db: AsyncSession) -> None:
    get_canonical_path().unlink(missing_ok=True)

    await bot_configuration_service.set_value(db, 'OVERPAY_P12_PATH', None)
    await bot_configuration_service.set_value(db, 'OVERPAY_P12_PASSPHRASE', None)
    await db.commit()
    await overpay_service.close()

    logger.info('Overpay: сертификат удалён')


def get_status() -> dict[str, Any]:
    env_locked_path, env_locked_passphrase = _env_locked_flags()
    effective_path = settings.OVERPAY_P12_PATH or str(get_canonical_path())
    path = Path(effective_path)
    uploaded = path.is_file()

    status: dict[str, Any] = {
        'uploaded': uploaded,
        'valid': False,
        'path': effective_path,
        'subject': None,
        'not_valid_after': None,
        'has_chain': None,
        'env_locked_path': env_locked_path,
        'env_locked_passphrase': env_locked_passphrase,
    }

    if not uploaded:
        return status

    try:
        metadata = validate_p12(path.read_bytes(), settings.OVERPAY_P12_PASSPHRASE)
    except (ValueError, OSError):
        return status

    status.update(metadata)
    status['valid'] = True
    return status
