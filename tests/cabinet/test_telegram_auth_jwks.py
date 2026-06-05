"""Regression tests for Telegram OIDC JWKS parsing.

Telegram added EC + OKP keys to https://oauth.telegram.org/.well-known/jwks.json
alongside the existing RSA key. Old code blindly passed every JWK through
`pyjwt.algorithms.RSAAlgorithm.from_jwk()`, which raises `InvalidKeyError:
Not an RSA key` for non-RSA entries and brought down the cabinet OIDC
auth endpoint with HTTP 500.
"""

from __future__ import annotations

from app.cabinet.auth.telegram_auth import _build_public_keys


# Real Telegram JWKS as observed at 2026-05-15 (4 keys: RSA + EC P-256 + Ed25519 + EC secp256k1)
_TELEGRAM_JWKS_SNAPSHOT = {
    'keys': [
        {
            'alg': 'RS256',
            'e': 'AQAB',
            'ext': True,
            'key_ops': ['verify'],
            'kty': 'RSA',
            'n': (
                '5RneLtsKvVcxdv6gu6gxEQu30Cru5NiMQnY6SNr9ZyZFZ4ya-pfHNuaZXJ6QPG0JSFwoxeOk'
                'EO2-eZN_REVPm448PvjjsR1eQdZ5QpEkNxnItFcmxkHH91v5cgf52_EI9BGO-MT6f1vaBSg3'
                'uWHFlDxI7J2AYxNvd1_Nf3TkgrrR7gyJFTmEIai5RefGnA0KGNYDlRIGUzrz2F05n6gTaHFT'
                '_iHL5UHatTZA4GCiUSjIOuwqu5pE5uZge20TFv3cxXMQaFw_xv1pgQt_Rq8eoCN7TS0RQ0zj'
                'WKiad-W286BcFectXsUm03p5Nq_kY4mf_7rqwX_B8yy_bBreyKn7RQ'
            ),
            'kid': 'oidc-1',
        },
        {
            'alg': 'ES256',
            'kty': 'EC',
            'x': 'ahVYrohhX6YA7w0P2gUNSwMFbaabCgBZFkeq9bWdmwU',
            'y': 'Ea8nKJ34VQMA7zv8aYDfzcBhXEjnWQ9C06jVke_eUV0',
            'crv': 'P-256',
            'kid': 'oidc-es256-1',
            'use': 'sig',
        },
        {
            'alg': 'EdDSA',
            'crv': 'Ed25519',
            'x': 'i6BEafXMEe4osXgUTffpKAm6Cn6F2bhqPZoclunTAV4',
            'kty': 'OKP',
            'kid': 'oidc-eddsa-1',
            'use': 'sig',
        },
        {
            'alg': 'ES256K',
            'kty': 'EC',
            'x': 'vsk5i5YJu8H_VPL7DWTgVGXBPrqgkyNmYvfgOrVut38',
            'y': 'Mrg56tBhVeorHPXK1LbTX2jP7rEqOHIatM96HFzVMIU',
            'crv': 'secp256k1',
            'kid': 'oidc-es256k-1',
            'use': 'sig',
        },
    ]
}


def test_build_public_keys_handles_mixed_jwks() -> None:
    """Real Telegram JWKS (RSA + EC + OKP + EC-secp256k1) must not raise."""
    result = _build_public_keys(_TELEGRAM_JWKS_SNAPSHOT)

    # Old buggy code raised InvalidKeyError on the first non-RSA entry.
    # Now we expect at least the RSA key to load. EC/OKP loading depends on
    # the local cryptography backend — both should load on the project's
    # `pyjwt>=2.11.0` with `cryptography`.
    assert 'oidc-1' in result, 'RSA key missing from result'

    rsa_key, rsa_alg = result['oidc-1']
    assert rsa_alg == 'RS256'
    assert rsa_key is not None


def test_build_public_keys_loads_ec_and_okp_keys() -> None:
    """EC P-256 + Ed25519 + EC secp256k1 keys should all parse with pyjwt 2.11+."""
    result = _build_public_keys(_TELEGRAM_JWKS_SNAPSHOT)

    # All 4 telegram-published kids should be present with their declared alg.
    expected_algs = {
        'oidc-1': 'RS256',
        'oidc-es256-1': 'ES256',
        'oidc-eddsa-1': 'EdDSA',
        'oidc-es256k-1': 'ES256K',
    }
    for kid, expected_alg in expected_algs.items():
        assert kid in result, f'kid {kid} missing'
        _, alg = result[kid]
        assert alg == expected_alg, f'kid {kid}: expected alg {expected_alg}, got {alg}'


def test_build_public_keys_skips_unsupported_kty() -> None:
    """Unknown kty (e.g. future-Telegram-quantum key) is silently skipped, not crashes."""
    jwks = {
        'keys': [
            {'kty': 'RSA', 'kid': 'good', 'alg': 'RS256', 'n': 'AQAB', 'e': 'AQAB'},  # malformed
            {'kty': 'UNKNOWN_FUTURE_TYPE', 'kid': 'mystery', 'alg': 'X25519'},
        ]
    }

    # Malformed RSA → caught by try/except. Unknown kty → skipped.
    # Neither should propagate an exception out of _build_public_keys.
    result = _build_public_keys(jwks)

    assert 'mystery' not in result
    # 'good' is malformed → also dropped, but the function returned cleanly.


def test_build_public_keys_skips_jwk_without_kid() -> None:
    """JWK без kid не может быть selected'ом по header'у токена — пропускаем."""
    jwks = {'keys': [{'kty': 'RSA', 'alg': 'RS256'}]}

    result = _build_public_keys(jwks)

    assert result == {}


def test_build_public_keys_returns_tuple_compatible_with_pyjwt_decode() -> None:
    """Возврат должен быть (public_key, alg) — иначе validate_telegram_oidc_token упадёт.

    Также инвариант: alg ВСЕГДА non-empty для принятого ключа. На этом полагается
    validate_telegram_oidc_token (передаёт `algorithms=[key_alg]` без fallback'а).
    """
    result = _build_public_keys(_TELEGRAM_JWKS_SNAPSHOT)
    assert result, 'expected at least one parsed key from the production snapshot'

    for kid, value in result.items():
        assert isinstance(value, tuple), f'kid {kid}: expected tuple, got {type(value)}'
        assert len(value) == 2, f'kid {kid}: expected (key, alg) tuple'
        key, alg = value
        assert isinstance(alg, str) and alg, f'kid {kid}: alg must be non-empty string, got {alg!r}'
        assert key is not None
        # pyjwt API contract: keys returned from `from_jwk` must be usable by
        # the matching algorithm class. Sanity-check that we didn't accidentally
        # store the raw JWK dict.
        assert not isinstance(key, dict), f'kid {kid}: got raw JWK dict instead of parsed key'


def test_build_public_keys_defaults_alg_when_jwk_omits_it() -> None:
    """JWK без поля `alg` → берём _KTY_DEFAULT_ALG[kty]; alg всё равно не пустой."""
    # Snapshot реального RSA-ключа Telegram'а, но без `alg`.
    jwks = {
        'keys': [
            {
                'e': 'AQAB',
                'ext': True,
                'key_ops': ['verify'],
                'kty': 'RSA',
                'n': (
                    '5RneLtsKvVcxdv6gu6gxEQu30Cru5NiMQnY6SNr9ZyZFZ4ya-pfHNuaZXJ6QPG0JSFwoxeOk'
                    'EO2-eZN_REVPm448PvjjsR1eQdZ5QpEkNxnItFcmxkHH91v5cgf52_EI9BGO-MT6f1vaBSg3'
                    'uWHFlDxI7J2AYxNvd1_Nf3TkgrrR7gyJFTmEIai5RefGnA0KGNYDlRIGUzrz2F05n6gTaHFT'
                    '_iHL5UHatTZA4GCiUSjIOuwqu5pE5uZge20TFv3cxXMQaFw_xv1pgQt_Rq8eoCN7TS0RQ0zj'
                    'WKiad-W286BcFectXsUm03p5Nq_kY4mf_7rqwX_B8yy_bBreyKn7RQ'
                ),
                'kid': 'rsa-no-alg',
            },
        ],
    }

    result = _build_public_keys(jwks)

    assert 'rsa-no-alg' in result
    _, alg = result['rsa-no-alg']
    assert alg == 'RS256', f'RSA fallback alg должен быть RS256, получил {alg!r}'
