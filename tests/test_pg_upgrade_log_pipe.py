"""Readiness must consume large docker logs under pipefail, without running an upgrade."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'pg-upgrade.sh'


@pytest.mark.parametrize(
    ('marker', 'need_init', 'logs_status', 'psql_status', 'expected'),
    [(True, 1, 0, 0, 0), (False, 1, 0, 0, 9), (False, 0, 0, 0, 0), (True, 1, 1, 0, 9), (True, 1, 0, 1, 9)],
)
def test_pg_readiness_large_log(tmp_path, marker, need_init, logs_status, psql_status, expected):
    source = SCRIPT.read_text()
    function = source[source.index('wait_ready() {') : source.index('\n# Снимок,')]
    log = tmp_path / 'postgres.log'
    log.write_text(('PostgreSQL init process complete\n' if marker else '') + 'database log line\n' * 100_000)
    docker = tmp_path / 'docker'
    docker.write_text(
        '#!/bin/bash\nset -e\n'
        'case "$1" in\n'
        'inspect) echo true ;;\n'
        'logs) cat "$TEST_LOG"; exit "$TEST_LOG_STATUS" ;;\n'
        'exec) exit "$TEST_PSQL_STATUS" ;;\n'
        '*) exit 99 ;;\n'
        'esac\n'
    )
    docker.chmod(0o755)
    result = subprocess.run(  # noqa: S603 — repository function and local Docker stub only
        [
            shutil.which('bash') or '/bin/bash',
            '-c',
            'set -Eeuo pipefail\nPOSTGRES_USER=test\nREADY_TIMEOUT=0\n'
            'die() { echo "$*" >&2; exit 9; }\n' + function + f'\nwait_ready test-container {need_init}',
        ],
        env={
            **os.environ,
            'PATH': f'{tmp_path}{os.pathsep}{os.environ["PATH"]}',
            'TEST_LOG': str(log),
            'TEST_LOG_STATUS': str(logs_status),
            'TEST_PSQL_STATUS': str(psql_status),
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == expected, result.stderr[-500:]
