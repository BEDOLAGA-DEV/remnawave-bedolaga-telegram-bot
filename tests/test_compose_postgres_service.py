"""Сервис postgres в compose-файлах и сторож перехода на PostgreSQL 18.

Три инварианта ломаются молча и заканчиваются «потерей» базы у пользователей:

1. compose-файлы разъезжаются по версиям PostgreSQL;
2. образ postgres:18+ хранит данные в /var/lib/postgresql/18/docker — том,
   смонтированный по-старому в /var/lib/postgresql/data, оставил бы кластер
   вне тома, и данные пропали бы при пересоздании контейнера;
3. без сторожа PostgreSQL 18 на пустом новом томе молча создал бы пустую базу,
   хотя данные пользователя лежат в старом томе PostgreSQL 15.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ('docker-compose.yml', 'docker-compose.local.yml')
GUARD = REPO_ROOT / 'docker' / 'postgres' / 'pg-upgrade-guard.sh'
GUARD_IN_CONTAINER = '/usr/local/bin/pg-upgrade-guard.sh'
SH = shutil.which('sh') or '/bin/sh'
LEGACY_IN_CONTAINER = '/mnt/pg-legacy-data'


def _compose(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / name).read_text(encoding='utf-8'))


@pytest.mark.parametrize('name', COMPOSE_FILES)
def test_postgres_18_with_new_volume_at_the_18_mount_point(name: str) -> None:
    compose = _compose(name)
    service = compose['services']['postgres']

    assert service['image'] == 'postgres:18-alpine'
    assert 'postgres18_data:/var/lib/postgresql' in service['volumes']
    assert not [v for v in service['volumes'] if v.endswith(':/var/lib/postgresql/data')], (
        'postgres:18 хранит данные в /var/lib/postgresql/18/docker — старый маунт оставит их вне тома'
    )
    assert {'postgres18_data', 'postgres_data'} <= set(compose['volumes'])


@pytest.mark.parametrize('name', COMPOSE_FILES)
def test_guard_is_the_entrypoint_and_sees_the_old_volume_read_only(name: str) -> None:
    service = _compose(name)['services']['postgres']

    assert service['entrypoint'] == ['/bin/sh', GUARD_IN_CONTAINER]
    # entrypoint в compose сбрасывает CMD образа — без command сервер не запустится.
    assert service['command'] == ['postgres']
    assert f'postgres_data:{LEGACY_IN_CONTAINER}:ro' in service['volumes']
    assert f'./docker/postgres/pg-upgrade-guard.sh:{GUARD_IN_CONTAINER}:ro' in service['volumes']


def test_compose_files_agree_on_the_postgres_service() -> None:
    services = [_compose(name)['services']['postgres'] for name in COMPOSE_FILES]
    keys = ('image', 'entrypoint', 'command', 'volumes', 'environment', 'healthcheck')
    first, *rest = services
    for other in rest:
        for key in keys:
            assert other[key] == first[key], f'compose-файлы расходятся в postgres.{key}'


def test_initdb_args_and_healthcheck_preserved() -> None:
    for name in COMPOSE_FILES:
        service = _compose(name)['services']['postgres']
        assert service['environment']['POSTGRES_INITDB_ARGS'] == '--encoding=UTF8 --locale=C', name
        assert 'pg_isready' in ' '.join(service['healthcheck']['test']), name


# ------------------------------------------------------------------ сторож


@pytest.fixture
def guard_env(tmp_path: Path) -> dict:
    """Окружение как в контейнере: PGDATA нового кластера, старый том, подставной entrypoint."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    entrypoint = bin_dir / 'docker-entrypoint.sh'
    entrypoint.write_text('#!/bin/sh\necho "ENTRYPOINT $*"\n', encoding='utf-8')
    entrypoint.chmod(entrypoint.stat().st_mode | stat.S_IEXEC)

    pgdata = tmp_path / 'var' / '18' / 'docker'
    pgdata.mkdir(parents=True)
    legacy = tmp_path / 'legacy'
    legacy.mkdir()

    return {
        'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        'PGDATA': str(pgdata),
        'PG_LEGACY_DATA': str(legacy),
    }


def _run_guard(env: dict) -> subprocess.CompletedProcess:
    # Путь к скрипту фиксирован и лежит в репозитории — внешних данных в команде нет.
    return subprocess.run([SH, str(GUARD), 'postgres'], env=env, capture_output=True, text=True, check=False)  # noqa: S603


def test_guard_starts_a_fresh_install(guard_env: dict) -> None:
    result = _run_guard(guard_env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ENTRYPOINT postgres'


def test_guard_refuses_empty_18_while_15_data_exists(guard_env: dict) -> None:
    (Path(guard_env['PG_LEGACY_DATA']) / 'PG_VERSION').write_text('15\n', encoding='utf-8')

    result = _run_guard(guard_env)

    assert result.returncode == 1
    assert 'ENTRYPOINT' not in result.stdout, 'сторож пустил initdb поверх необработанной старой базы'
    assert 'make pg-upgrade' in result.stderr
    assert 'PostgreSQL 15' in result.stderr


def test_guard_starts_after_migration_even_if_old_volume_remains(guard_env: dict) -> None:
    (Path(guard_env['PG_LEGACY_DATA']) / 'PG_VERSION').write_text('15\n', encoding='utf-8')
    (Path(guard_env['PGDATA']) / 'PG_VERSION').write_text('18\n', encoding='utf-8')

    result = _run_guard(guard_env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ENTRYPOINT postgres'
