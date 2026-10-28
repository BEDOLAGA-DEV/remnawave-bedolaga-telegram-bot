"""Release metadata must agree before installing the locked environment."""

import json
from pathlib import Path

import pytest

from scripts.check_release_versions import check_versions


def test_repository_versions_match():
    assert check_versions(Path(__file__).resolve().parents[2]) == []


@pytest.mark.parametrize('mismatch', [None, 'uv.lock', 'Dockerfile', '.release-please-manifest.json'])
def test_release_metadata_mismatch_is_rejected(tmp_path, mismatch):
    def version(name):
        return '5.1.0' if name == mismatch else '5.1.1'

    (tmp_path / 'pyproject.toml').write_text('[project]\nname="example-bot"\nversion="5.1.1"\n')
    (tmp_path / 'uv.lock').write_text(
        'version=1\n[[package]]\nname="unrelated"\nversion="9.0.0"\n'
        f'[[package]]\nname="example-bot"\nversion="{version("uv.lock")}"\n'
    )
    (tmp_path / 'Dockerfile').write_text(f'ARG VERSION="v{version("Dockerfile")}" # x-release-please-version\n')
    (tmp_path / '.release-please-manifest.json').write_text(json.dumps({'.': version('.release-please-manifest.json')}))
    errors = check_versions(tmp_path)
    if mismatch:
        assert errors and any(mismatch in error for error in errors)
    else:
        assert errors == []
