"""Check that the application version is consistent across release files."""

import json
import re
import sys
import tomllib
from pathlib import Path


def check_versions(root: Path) -> list[str]:
    project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    expected = project['version']
    packages = tomllib.loads((root / 'uv.lock').read_text())['package']
    locked_versions = [package['version'] for package in packages if package['name'] == project['name']]
    docker_versions = re.findall(r'^ARG VERSION="v([^\"]+)"', (root / 'Dockerfile').read_text(), re.MULTILINE)
    manifest_version = json.loads((root / '.release-please-manifest.json').read_text()).get('.')
    versions = {
        'uv.lock': locked_versions,
        'Dockerfile': docker_versions,
        '.release-please-manifest.json': [manifest_version],
    }
    return [
        f'{name}: expected exactly one application version {expected}, found {actual}'
        for name, actual in versions.items()
        if actual != [expected]
    ]


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    errors = check_versions(root)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print('Release versions match pyproject.toml, uv.lock, Dockerfile and manifest')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
