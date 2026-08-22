"""Contract tests for canonical gift claim links and Telegram share links."""

from __future__ import annotations

import urllib.parse
from dataclasses import FrozenInstanceError

import pytest

from app.database.crud.landing import generate_purchase_token
from app.utils.gift_links import (
    GIFT_TOKEN_BOT_PREFIX_LENGTH,
    GIFT_TOKEN_MIN_PREFIX_LENGTH,
    TELEGRAM_GIFT_START_PREFIX,
    TELEGRAM_START_PARAM_MAX_LENGTH,
    GiftClaimArtifacts,
    GiftLinkError,
    InvalidBotUsernameError,
    InvalidCabinetUrlError,
    InvalidClaimLinkError,
    InvalidGiftTokenError,
    InvalidShareTextError,
    MissingBotUsernameError,
    MissingCabinetUrlError,
    build_bot_gift_claim_link,
    build_cabinet_gift_claim_link,
    build_gift_claim_artifacts,
    build_gift_public_code,
    build_telegram_gift_share_url,
    parse_gift_claim_input,
)


class TestBuildBotGiftClaimLink:
    """Tests for build_bot_gift_claim_link."""

    def test_start_param_within_telegram_64_char_limit(self) -> None:
        token = generate_purchase_token()
        assert len(token) == 64

        link = build_bot_gift_claim_link(token, 'test_bot')
        parsed = urllib.parse.urlparse(link)
        assert parsed.scheme == 'https'
        assert parsed.netloc == 't.me'
        assert parsed.path == '/test_bot'

        query = urllib.parse.parse_qs(parsed.query)
        assert 'start' in query
        start_param = query['start'][0]

        # Invariant: start param is at most 64 characters total
        assert len(start_param) <= TELEGRAM_START_PARAM_MAX_LENGTH
        assert len(start_param) == 64

    def test_start_param_prefix_and_token_fragment_security_floor(self) -> None:
        token = generate_purchase_token()
        link = build_bot_gift_claim_link(token, 'test_bot')

        parsed = urllib.parse.urlparse(link)
        start_param = urllib.parse.parse_qs(parsed.query)['start'][0]

        assert start_param.startswith(TELEGRAM_GIFT_START_PREFIX)
        fragment = start_param.removeprefix(TELEGRAM_GIFT_START_PREFIX)

        # Security floor: must contain at least 48 characters of entropy
        assert len(fragment) >= GIFT_TOKEN_MIN_PREFIX_LENGTH
        assert len(fragment) == GIFT_TOKEN_BOT_PREFIX_LENGTH  # 59 characters
        assert len(fragment) == 59
        # The fragment is a strict prefix of the full token
        assert fragment == token[:59]

    @pytest.mark.parametrize(
        ('raw_username', 'expected_netloc_path'),
        [
            ('my_gift_bot', '/my_gift_bot'),
            ('@my_gift_bot', '/my_gift_bot'),
            ('  @my_gift_bot  ', '/my_gift_bot'),
            ('bot123', '/bot123'),
            ('@bot_with_numbers_123', '/bot_with_numbers_123'),
        ],
    )
    def test_username_normalization(self, raw_username: str, expected_netloc_path: str) -> None:
        token = generate_purchase_token()
        link = build_bot_gift_claim_link(token, raw_username)
        parsed = urllib.parse.urlparse(link)
        assert parsed.path == expected_netloc_path

    def test_preserves_urlsafe_token_characters(self) -> None:
        token = 'A' * 30 + '-' + 'B' * 20 + '_' + 'C' * 12
        assert len(token) == 64
        link = build_bot_gift_claim_link(token, 'test_bot')
        assert f'start=GIFT_{token[:59]}' in link

    def test_accepts_minimum_length_token(self) -> None:
        token = 'x' * GIFT_TOKEN_MIN_PREFIX_LENGTH
        link = build_bot_gift_claim_link(token, 'test_bot')
        parsed = urllib.parse.urlparse(link)
        start_param = urllib.parse.parse_qs(parsed.query)['start'][0]
        assert start_param == f'GIFT_{token}'
        assert len(start_param) == 5 + 48

    @pytest.mark.parametrize(
        'missing_username',
        [None, '', '   ', '@', '  @  '],
    )
    def test_rejects_missing_or_blank_bot_username(self, missing_username: str | None) -> None:
        token = generate_purchase_token()
        with pytest.raises(MissingBotUsernameError):
            build_bot_gift_claim_link(token, missing_username)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        'malformed_username',
        ['bot with spaces', 'bot#name', 'bot!@?', 'user$name', 'invalid/path'],
    )
    def test_rejects_malformed_bot_username(self, malformed_username: str) -> None:
        token = generate_purchase_token()
        with pytest.raises(InvalidBotUsernameError):
            build_bot_gift_claim_link(token, malformed_username)

    @pytest.mark.parametrize(
        'too_short_token',
        ['', 'abc', 'X' * 8, 'X' * 12, 'X' * 47],
    )
    def test_rejects_too_short_token(self, too_short_token: str) -> None:
        with pytest.raises(InvalidGiftTokenError):
            build_bot_gift_claim_link(too_short_token, 'test_bot')

    @pytest.mark.parametrize(
        'malformed_token',
        [
            None,
            'token with spaces',
            'token!with#special$',
            'token\nwith\nnewlines',
            'токен_с_юникодом_123456789012345678901234567890123456',
        ],
    )
    def test_rejects_malformed_token(self, malformed_token: str | None) -> None:
        with pytest.raises(InvalidGiftTokenError):
            build_bot_gift_claim_link(malformed_token, 'test_bot')  # type: ignore[arg-type]


class TestBuildCabinetGiftClaimLink:
    """Tests for build_cabinet_gift_claim_link."""

    def test_preserves_full_token_and_builds_canonical_url(self) -> None:
        token = generate_purchase_token()
        link = build_cabinet_gift_claim_link(token, 'https://vpn.example.com')
        assert link == f'https://vpn.example.com/buy/gift/{token}'

    @pytest.mark.parametrize(
        ('cabinet_url', 'expected_base'),
        [
            ('https://vpn.example.com', 'https://vpn.example.com'),
            ('https://vpn.example.com/', 'https://vpn.example.com'),
            ('https://vpn.example.com///', 'https://vpn.example.com'),
            ('http://localhost:8000', 'http://localhost:8000'),
            ('  https://cabinet.remnawave.io/  ', 'https://cabinet.remnawave.io'),
        ],
    )
    def test_cabinet_url_slash_and_whitespace_normalization(self, cabinet_url: str, expected_base: str) -> None:
        token = generate_purchase_token()
        link = build_cabinet_gift_claim_link(token, cabinet_url)
        assert link == f'{expected_base}/buy/gift/{token}'

    @pytest.mark.parametrize('missing_url', [None, '', '   '])
    def test_rejects_missing_cabinet_url(self, missing_url: str | None) -> None:
        token = generate_purchase_token()
        with pytest.raises(MissingCabinetUrlError):
            build_cabinet_gift_claim_link(token, missing_url)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        'invalid_url',
        ['ftp://cabinet.com', 'cabinet.example.com', 'not a url', '://missing-scheme'],
    )
    def test_rejects_invalid_cabinet_url(self, invalid_url: str) -> None:
        token = generate_purchase_token()
        with pytest.raises(InvalidCabinetUrlError):
            build_cabinet_gift_claim_link(token, invalid_url)

    def test_rejects_invalid_token(self) -> None:
        with pytest.raises(InvalidGiftTokenError):
            build_cabinet_gift_claim_link('short', 'https://cabinet.example.com')


class TestBuildTelegramGiftShareUrl:
    """Tests for build_telegram_gift_share_url."""

    def test_produces_canonical_telegram_share_url(self) -> None:
        claim_link = 'https://t.me/test_bot?start=GIFT_abcdef123456'
        share_text = 'Вам подарок! Активируйте подписку по ссылке.'

        share_url = build_telegram_gift_share_url(claim_link, share_text)

        parsed = urllib.parse.urlparse(share_url)
        assert parsed.scheme == 'https'
        assert parsed.netloc == 't.me'
        assert parsed.path == '/share/url'

        qs = urllib.parse.parse_qs(parsed.query)
        assert qs['url'] == [claim_link]
        assert qs['text'] == [share_text]

    def test_independently_url_encodes_reserved_characters_and_unicode(self) -> None:
        claim_link = 'https://t.me/test_bot?start=GIFT_123&foo=bar#section'
        share_text = '🎁 Подарок для тебя!\nПлан: Premium (30 дней) & бонус = 100%'

        share_url = build_telegram_gift_share_url(claim_link, share_text)

        parsed = urllib.parse.urlparse(share_url)
        qs = urllib.parse.parse_qs(parsed.query)

        assert qs['url'][0] == claim_link
        assert qs['text'][0] == share_text

    @pytest.mark.parametrize(
        'invalid_claim_link',
        [None, '', '   ', 'not-a-url', 'ftp://bad-scheme.com'],
    )
    def test_rejects_invalid_claim_link(self, invalid_claim_link: str | None) -> None:
        with pytest.raises(InvalidClaimLinkError):
            build_telegram_gift_share_url(invalid_claim_link, 'Gift text')  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        'invalid_share_text',
        [None, '', '   ', 123, []],
    )
    def test_rejects_invalid_or_empty_share_text(self, invalid_share_text: str | None) -> None:
        claim_link = 'https://t.me/test_bot?start=GIFT_123'
        with pytest.raises(InvalidShareTextError):
            build_telegram_gift_share_url(claim_link, invalid_share_text)  # type: ignore[arg-type]

    def test_share_text_contains_no_price_and_no_raw_token(self) -> None:
        """Verify contract: share payload contains claim URL, but no prices or raw tokens."""
        raw_token = generate_purchase_token()
        bot_link = build_bot_gift_claim_link(raw_token, 'my_bot')
        share_text = 'Вам подарили подписку на 30 дней! Нажмите на ссылку, чтобы активировать её.'

        share_url = build_telegram_gift_share_url(bot_link, share_text)
        parsed = urllib.parse.urlparse(share_url)
        qs = urllib.parse.parse_qs(parsed.query)

        decoded_text = qs['text'][0]
        # Must not contain raw token
        assert raw_token not in decoded_text
        # Must not leak financial details
        assert '₽' not in decoded_text
        assert 'rub' not in decoded_text.lower()
        assert 'kopek' not in decoded_text.lower()
        assert 'price' not in decoded_text.lower()

    def test_exceptions_inherit_from_base_gift_link_error(self) -> None:
        assert issubclass(InvalidGiftTokenError, GiftLinkError)
        assert issubclass(InvalidBotUsernameError, GiftLinkError)
        assert issubclass(MissingBotUsernameError, InvalidBotUsernameError)
        assert issubclass(InvalidCabinetUrlError, GiftLinkError)
        assert issubclass(MissingCabinetUrlError, InvalidCabinetUrlError)
        assert issubclass(InvalidClaimLinkError, GiftLinkError)
        assert issubclass(InvalidShareTextError, GiftLinkError)

    def test_invalid_token_exception_does_not_leak_raw_token(self) -> None:
        """P3 Security: exception message must not contain the raw token."""
        raw_secret_token = 'secret_token_123!@#_invalid'
        with pytest.raises(InvalidGiftTokenError) as exc:
            build_bot_gift_claim_link(raw_secret_token, 'my_bot')
        assert raw_secret_token not in str(exc.value)

    def test_invalid_claim_link_exception_does_not_leak_raw_token(self) -> None:
        """P3 Security: claim link validation error must not leak raw token or full url."""
        raw_secret_token = 'secret_token_with_invalid_claim_link'
        bad_claim_link = f'ftp://bad-link/{raw_secret_token}'
        with pytest.raises(InvalidClaimLinkError) as exc:
            build_telegram_gift_share_url(bad_claim_link, 'Gift text')
        assert raw_secret_token not in str(exc.value)


class TestLandingGiftLinkIntegration:
    """Tests for landing route purchase status link generation."""

    def test_build_purchase_status_response_uses_canonical_links(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.cabinet.routes.landing import _build_purchase_status_response
        from app.config import settings
        from app.database.models import GuestPurchase, GuestPurchaseStatus

        monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com/')
        monkeypatch.setattr(settings, 'BOT_USERNAME', 'my_landing_bot')

        token = generate_purchase_token()
        purchase = GuestPurchase(
            id=1,
            token=token,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
        )

        response = _build_purchase_status_response(purchase)

        assert response.is_claimable is True
        assert response.claim_url == f'https://cabinet.example.com/buy/gift/{token}'
        assert response.bot_claim_link == f'https://t.me/my_landing_bot?start=GIFT_{token[:59]}'
        assert len(response.bot_claim_link.split('start=')[1]) == 64
        # Assert legacy 12-char slice is NOT used
        assert response.bot_claim_link != f'https://t.me/my_landing_bot?start=GIFT_{token[:12]}'


class TestBuildGiftPublicCode:
    """Tests for build_gift_public_code."""

    def test_canonical_public_code_format_and_length(self) -> None:
        token = generate_purchase_token()
        assert len(token) == 64

        code = build_gift_public_code(token)
        # Exact prefix 'GIFT_' + 59-char prefix = 64 characters
        assert code.startswith(TELEGRAM_GIFT_START_PREFIX)
        assert len(code) == 64
        assert code == f'GIFT_{token[:59]}'

    def test_deterministic_for_same_token(self) -> None:
        token = generate_purchase_token()
        code1 = build_gift_public_code(token)
        code2 = build_gift_public_code(token)
        assert code1 == code2

    def test_equal_output_for_cabinet_and_bot_origin_purchases(self) -> None:
        # Both bot and cabinet purchases persist standard 64-char purchase tokens in GuestPurchase.token
        shared_token = generate_purchase_token()
        bot_purchase_code = build_gift_public_code(shared_token)
        cabinet_purchase_code = build_gift_public_code(shared_token)
        assert bot_purchase_code == cabinet_purchase_code

    def test_contains_only_urlsafe_characters(self) -> None:
        token = 'Abc-123_XYZ' * 5 + '123456789'
        code = build_gift_public_code(token)
        assert code.startswith('GIFT_')
        # Check that characters after GIFT_ are URL safe
        assert all(c.isalnum() or c in '-_' for c in code)

    def test_fits_telegram_start_param_limit(self) -> None:
        token = generate_purchase_token()
        code = build_gift_public_code(token)
        assert len(code) <= TELEGRAM_START_PARAM_MAX_LENGTH

    def test_accepts_minimum_length_token(self) -> None:
        token = 'k' * GIFT_TOKEN_MIN_PREFIX_LENGTH  # 48 chars
        code = build_gift_public_code(token)
        assert code == f'GIFT_{token}'
        assert len(code) == 5 + 48

    @pytest.mark.parametrize('short_token', ['', 'abc', 'X' * 8, 'X' * 47])
    def test_rejects_token_below_security_threshold(self, short_token: str) -> None:
        with pytest.raises(InvalidGiftTokenError):
            build_gift_public_code(short_token)

    @pytest.mark.parametrize('malformed_token', [None, 'token with space', 'token!invalid@chars'])
    def test_rejects_malformed_token(self, malformed_token: str | None) -> None:
        with pytest.raises(InvalidGiftTokenError):
            build_gift_public_code(malformed_token)  # type: ignore[arg-type]

    def test_exception_does_not_leak_raw_token(self) -> None:
        secret_token = 'secret_raw_token_xyz_12345!@#'
        with pytest.raises(InvalidGiftTokenError) as exc:
            build_gift_public_code(secret_token)
        assert secret_token not in str(exc.value)


class TestParseGiftClaimInput:
    """Tests for parse_gift_claim_input."""

    def test_parses_canonical_public_code(self) -> None:
        token = generate_purchase_token()
        code = f'GIFT_{token[:59]}'
        parsed = parse_gift_claim_input(code)
        assert parsed == token[:59]

    def test_parses_legacy_dash_prefix_code(self) -> None:
        token = generate_purchase_token()
        code = f'GIFT-{token[:59]}'
        parsed = parse_gift_claim_input(code)
        assert parsed == token[:59]

    def test_parses_case_insensitive_prefix(self) -> None:
        token = generate_purchase_token()
        assert parse_gift_claim_input(f'gift_{token[:59]}') == token[:59]
        assert parse_gift_claim_input(f'gift-{token[:59]}') == token[:59]
        assert parse_gift_claim_input(f'giftclaim_{token[:59]}') == token[:59]

    def test_parses_canonical_telegram_deep_link(self) -> None:
        token = generate_purchase_token()
        deep_links = [
            f'https://t.me/my_bot?start=GIFT_{token[:59]}',
            f'https://t.me/my_bot?start=GIFT-{token[:59]}',
            f't.me/my_bot?start=GIFT_{token[:59]}',
            f'tg://resolve?domain=my_bot&start=GIFT_{token[:59]}',
            f'https://t.me/my_bot?start=giftclaim_{token[:59]}',
        ]
        for link in deep_links:
            assert parse_gift_claim_input(link) == token[:59]

    def test_parses_cabinet_gift_claim_url_preserving_full_token(self) -> None:
        token = generate_purchase_token()
        assert len(token) == 64
        cabinet_urls = [
            f'https://cabinet.example.com/buy/gift/{token}',
            f'https://cabinet.example.com/buy/gift/{token}/',
            f'http://localhost:8000/buy/gift/{token}',
            f'https://cabinet.example.com/buy/gift/{token}?ref=123',
        ]
        for url in cabinet_urls:
            assert parse_gift_claim_input(url) == token

    def test_parses_raw_full_token(self) -> None:
        token = generate_purchase_token()
        assert parse_gift_claim_input(token) == token

    def test_parses_raw_token_prefix_meeting_security_threshold(self) -> None:
        token = generate_purchase_token()
        fragment_48 = token[:48]
        fragment_59 = token[:59]
        assert parse_gift_claim_input(fragment_48) == fragment_48
        assert parse_gift_claim_input(fragment_59) == fragment_59

    def test_strips_surrounding_whitespace(self) -> None:
        token = generate_purchase_token()
        assert parse_gift_claim_input(f'  GIFT_{token[:59]}  \n') == token[:59]
        assert parse_gift_claim_input(f'  https://t.me/my_bot?start=GIFT_{token[:59]}  ') == token[:59]

    @pytest.mark.parametrize(
        'malformed_url',
        [
            'https://example.com/unknown/path',
            'https://t.me/my_bot?foo=bar',
            'https://t.me/my_bot?start=',
            'https://cabinet.example.com/buy/gift/',
            'https://cabinet.example.com/buy/gift',
        ],
    )
    def test_rejects_malformed_url_without_gift_parameter(self, malformed_url: str) -> None:
        with pytest.raises(InvalidGiftTokenError):
            parse_gift_claim_input(malformed_url)

    @pytest.mark.parametrize(
        'invalid_input',
        [
            None,
            '',
            '   ',
            'https://t.me/my_bot?start=coupon_123456789012345678901234567890123456789012345678',
            'https://t.me/my_bot?start=ref_123456789012345678901234567890123456789012345678',
            'GIFT_contains invalid characters!@#$123456789012345678901234567890',
            12345,
            [],
        ],
    )
    def test_rejects_invalid_inputs(self, invalid_input: str | None) -> None:
        with pytest.raises(InvalidGiftTokenError):
            parse_gift_claim_input(invalid_input)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        'short_input',
        [
            'GIFT_short',
            'GIFT-12345678',
            '12345678',
            'X' * 47,
            f'https://t.me/my_bot?start=GIFT_{"x" * 12}',
        ],
    )
    def test_rejects_short_prefix_by_default(self, short_input: str) -> None:
        """Secure default: fragments below GIFT_TOKEN_MIN_PREFIX_LENGTH are rejected."""
        with pytest.raises(InvalidGiftTokenError):
            parse_gift_claim_input(short_input, allow_legacy_short=False)

    def test_accepts_legacy_short_code_when_explicitly_allowed(self) -> None:
        """Backward compatibility: legacy cabinet activation allows 8-char to 47-char codes."""
        assert parse_gift_claim_input('GIFT_12345678', allow_legacy_short=True) == '12345678'
        assert parse_gift_claim_input('GIFT-123456789012', allow_legacy_short=True) == '123456789012'
        assert parse_gift_claim_input('12345678', allow_legacy_short=True) == '12345678'
        assert parse_gift_claim_input('abcdefghij12', allow_legacy_short=True) == 'abcdefghij12'

    @pytest.mark.parametrize('too_short_legacy', ['', '1234567', 'GIFT_1234567', 'GIFT-'])
    def test_rejects_below_legacy_minimum_even_when_allowed(self, too_short_legacy: str) -> None:
        with pytest.raises(InvalidGiftTokenError):
            parse_gift_claim_input(too_short_legacy, allow_legacy_short=True)

    def test_exception_does_not_leak_raw_secret_value(self) -> None:
        secret_value = 'my_confidential_secret_value_123!'
        with pytest.raises(InvalidGiftTokenError) as exc:
            parse_gift_claim_input(secret_value)
        assert secret_value not in str(exc.value)


class TestGiftClaimArtifacts:
    """Tests for GiftClaimArtifacts and build_gift_claim_artifacts."""

    def test_artifacts_immutable_dataclass(self) -> None:
        artifacts = GiftClaimArtifacts(
            public_code='GIFT_123456',
            bot_claim_url='https://t.me/bot?start=GIFT_123456',
            cabinet_claim_url='https://cab.com/buy/gift/123456',
            telegram_share_url='https://t.me/share/url?url=...',
        )
        assert artifacts.public_code == 'GIFT_123456'
        with pytest.raises(FrozenInstanceError):
            artifacts.public_code = 'modified'  # type: ignore[misc]

    def test_build_artifacts_with_all_channels(self) -> None:
        token = generate_purchase_token()
        bot_username = 'my_test_bot'
        cabinet_url = 'https://cabinet.example.com'
        share_text = '🎁 Подарок для тебя!'

        artifacts = build_gift_claim_artifacts(
            token=token,
            bot_username=bot_username,
            cabinet_url=cabinet_url,
            share_text=share_text,
        )

        assert artifacts.public_code == f'GIFT_{token[:59]}'
        assert artifacts.bot_claim_url == f'https://t.me/my_test_bot?start=GIFT_{token[:59]}'
        assert artifacts.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{token}'
        assert artifacts.telegram_share_url is not None
        assert f'url=https%3A%2F%2Ft.me%2Fmy_test_bot%3Fstart%3DGIFT_{token[:59]}' in artifacts.telegram_share_url

    def test_build_artifacts_without_bot_username_uses_cabinet_for_share(self) -> None:
        token = generate_purchase_token()
        cabinet_url = 'https://cabinet.example.com'
        share_text = '🎁 Подарок для тебя!'

        artifacts = build_gift_claim_artifacts(
            token=token,
            bot_username=None,
            cabinet_url=cabinet_url,
            share_text=share_text,
        )

        assert artifacts.public_code == f'GIFT_{token[:59]}'
        assert artifacts.bot_claim_url is None
        assert artifacts.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{token}'
        assert artifacts.telegram_share_url is not None
        assert f'url=https%3A%2F%2Fcabinet.example.com%2Fbuy%2Fgift%2F{token}' in artifacts.telegram_share_url

    def test_build_artifacts_without_cabinet_url(self) -> None:
        token = generate_purchase_token()
        artifacts = build_gift_claim_artifacts(
            token=token,
            bot_username='my_test_bot',
            cabinet_url=None,
            share_text='Share text',
        )

        assert artifacts.public_code == f'GIFT_{token[:59]}'
        assert artifacts.bot_claim_url == f'https://t.me/my_test_bot?start=GIFT_{token[:59]}'
        assert artifacts.cabinet_claim_url is None
        assert artifacts.telegram_share_url is not None

    def test_build_artifacts_without_any_channels(self) -> None:
        token = generate_purchase_token()
        artifacts = build_gift_claim_artifacts(
            token=token,
            bot_username=None,
            cabinet_url=None,
            share_text=None,
        )

        # Invariant: public code is always generated, channels produce None without suppressing code
        assert artifacts.public_code == f'GIFT_{token[:59]}'
        assert artifacts.bot_claim_url is None
        assert artifacts.cabinet_claim_url is None
        assert artifacts.telegram_share_url is None

    def test_build_artifacts_rejects_invalid_token(self) -> None:
        with pytest.raises(InvalidGiftTokenError):
            build_gift_claim_artifacts(token='too_short', bot_username='my_bot')
