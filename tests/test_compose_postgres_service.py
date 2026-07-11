"""Pins for the postgres service definition in the compose files.

Two invariants are easy to break silently:

1. the two compose files drifting to different major versions;
2. the postgres:18+ image moved its VOLUME from /var/lib/postgresql/data to
   /var/lib/postgresql (PGDATA is /var/lib/postgresql/18/docker) — mounting
   the old path would leave the cluster OUTSIDE the named volume, and the
   data would be lost on container re-creation.
"""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ('docker-compose.yml', 'docker-compose.local.yml')


def _postgres_service(compose_name: str) -> dict:
    compose = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding='utf-8'))
    return compose['services']['postgres']


def test_both_compose_files_use_the_same_postgres_image() -> None:
    images = {name: _postgres_service(name)['image'] for name in COMPOSE_FILES}
    assert len(set(images.values())) == 1, f'compose files disagree on the postgres image: {images}'
    image = next(iter(images.values()))
    assert image.startswith('postgres:18'), image


def test_data_volume_uses_the_18_plus_mount_point() -> None:
    for name in COMPOSE_FILES:
        service = _postgres_service(name)
        data_mounts = [v for v in service['volumes'] if 'postgres_data' in v]
        assert data_mounts == ['postgres_data:/var/lib/postgresql'], (
            f'{name}: postgres:18+ images mount /var/lib/postgresql (PGDATA=/var/lib/postgresql/18/docker); '
            f'the old /var/lib/postgresql/data mount would leave the cluster outside the volume: {data_mounts}'
        )


def test_initdb_args_and_healthcheck_preserved() -> None:
    for name in COMPOSE_FILES:
        service = _postgres_service(name)
        assert service['environment']['POSTGRES_INITDB_ARGS'] == '--encoding=UTF8 --locale=C', name
        healthcheck = ' '.join(service['healthcheck']['test'])
        assert 'pg_isready' in healthcheck, name
