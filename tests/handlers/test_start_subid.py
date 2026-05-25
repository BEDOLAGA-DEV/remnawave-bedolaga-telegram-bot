"""Tests for the ``{campaign}_subid_{subid}`` /start deeplink parser."""

import pytest

from app.handlers.start import _split_start_param_subid


class TestSplitStartParamSubid:
    def test_returns_campaign_and_subid_for_canonical_format(self) -> None:
        assert _split_start_param_subid('clkcl_subid_519c82ce-1322-410b-8c7a-9992085a38f0') == (
            'clkcl',
            '519c82ce-1322-410b-8c7a-9992085a38f0',
        )

    def test_returns_param_unchanged_when_no_delimiter(self) -> None:
        assert _split_start_param_subid('clkcl') == ('clkcl', None)
        assert _split_start_param_subid('GIFT_abc123def456') == ('GIFT_abc123def456', None)
        assert _split_start_param_subid('webauth_tok_xyz') == ('webauth_tok_xyz', None)

    def test_returns_none_pair_for_empty_or_none(self) -> None:
        assert _split_start_param_subid(None) == (None, None)
        assert _split_start_param_subid('') == ('', None)

    def test_rejects_empty_campaign_part(self) -> None:
        # `_subid_X` would yield empty head — preserve original, don't split.
        assert _split_start_param_subid('_subid_xyz') == ('_subid_xyz', None)

    def test_rejects_empty_subid_part(self) -> None:
        # `clkcl_subid_` would yield empty tail — preserve original.
        assert _split_start_param_subid('clkcl_subid_') == ('clkcl_subid_', None)

    def test_partitions_on_first_delimiter_only(self) -> None:
        # If subid itself contains the delimiter substring it stays intact.
        assert _split_start_param_subid('clkcl_subid_aa_subid_bb') == ('clkcl', 'aa_subid_bb')

    def test_rejects_subid_exceeding_column_limit(self) -> None:
        oversized = 'x' * 256
        param = f'clkcl_subid_{oversized}'
        assert _split_start_param_subid(param) == (param, None)

    def test_accepts_subid_at_column_limit(self) -> None:
        max_size = 'x' * 255
        assert _split_start_param_subid(f'clkcl_subid_{max_size}') == ('clkcl', max_size)

    @pytest.mark.parametrize(
        ('param', 'expected'),
        [
            ('clkcl_subid_a', ('clkcl', 'a')),
            ('vpn_subid_KEITARO-ID-123', ('vpn', 'KEITARO-ID-123')),
            ('promo_subid_a-b-c', ('promo', 'a-b-c')),
            # Multi-word campaign with underscores stays intact in the head part.
            ('summer_promo_subid_xyz', ('summer_promo', 'xyz')),
        ],
    )
    def test_parametrized_valid_inputs(self, param: str, expected: tuple[str, str]) -> None:
        assert _split_start_param_subid(param) == expected
