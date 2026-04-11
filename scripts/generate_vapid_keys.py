#!/usr/bin/env python3
"""Generate VAPID keys for Web Push notifications.

Web Push uses ECDSA P-256 keys for authentication (VAPID protocol).
Run this script once on the server to generate a keypair.

Usage:
    python scripts/generate_vapid_keys.py

Output:
    - vapid_private_key.pem       PEM-encoded private key (keep SECRET!)
    - Prints public key in base64url format (for .env)
    - Prints a ready-to-paste .env block

Then set in your .env file:
    WEB_PUSH_ENABLED=true
    WEB_PUSH_VAPID_PRIVATE_KEY=./vapid_private_key.pem
    WEB_PUSH_VAPID_PUBLIC_KEY=<base64url public key from output>
    WEB_PUSH_VAPID_EMAIL=admin@your-domain.com

For Docker deployments: mount vapid_private_key.pem as a volume or
secret, then point WEB_PUSH_VAPID_PRIVATE_KEY to its container path.
"""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


PRIVATE_KEY_FILE = 'vapid_private_key.pem'
DATA_DIR = 'data'  # Docker-persistent directory, matches /app/data volume mount


def generate_vapid_keypair() -> tuple[str, str]:
    """Generate a new VAPID ECDSA P-256 keypair.

    Returns:
        (private_pem_str, public_b64url_str)
    """
    # Generate P-256 private key — standard for VAPID
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    # Serialize private key as PKCS#8 PEM (unencrypted)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode('ascii')

    # Serialize public key as uncompressed point format:
    # 0x04 || X (32 bytes) || Y (32 bytes) = 65 bytes
    # This is what browsers expect as applicationServerKey
    public_numbers = public_key.public_numbers()
    x_bytes = public_numbers.x.to_bytes(32, 'big')
    y_bytes = public_numbers.y.to_bytes(32, 'big')
    public_raw = b'\x04' + x_bytes + y_bytes

    # Base64URL encode without padding
    public_b64url = base64.urlsafe_b64encode(public_raw).decode('ascii').rstrip('=')

    return private_pem, public_b64url


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent

    # Prefer data/ directory if it exists (Docker-persistent). Fallback to project root.
    data_dir = repo_root / DATA_DIR
    if data_dir.is_dir():
        pem_path = data_dir / PRIVATE_KEY_FILE
        env_path_value = f'{DATA_DIR}/{PRIVATE_KEY_FILE}'
    else:
        pem_path = repo_root / PRIVATE_KEY_FILE
        env_path_value = PRIVATE_KEY_FILE

    if pem_path.exists():
        print(f'[ERR] {PRIVATE_KEY_FILE} already exists at {pem_path}')
        print('  Refusing to overwrite. Delete it manually if you really want to regenerate.')
        return 1

    private_pem, public_b64url = generate_vapid_keypair()

    pem_path.write_text(private_pem, encoding='ascii')
    try:
        pem_path.chmod(0o600)
    except Exception:  # Windows/FAT filesystems
        pass

    print('=' * 72)
    print('[OK] VAPID keys generated')
    print('=' * 72)
    print()
    print(f'Private key saved to: {pem_path}')
    print('  Keep this file SECRET. Do NOT commit it to git.')
    print('  Recommended: add "vapid_private_key.pem" to .gitignore')
    print()
    print('Add the following lines to your .env file:')
    print()
    print('# --- Web Push (VAPID) ---')
    print('WEB_PUSH_ENABLED=true')
    print(f'WEB_PUSH_VAPID_PRIVATE_KEY={env_path_value}')
    print(f'WEB_PUSH_VAPID_PUBLIC_KEY={public_b64url}')
    print('WEB_PUSH_VAPID_EMAIL=admin@your-domain.com')
    print()
    print('=' * 72)
    print('After editing .env, restart the bot for changes to take effect.')
    print('Users can then enable push in Cabinet -> Settings -> Notifications.')
    print('=' * 72)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
