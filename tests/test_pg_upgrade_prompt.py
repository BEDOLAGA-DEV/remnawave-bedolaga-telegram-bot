"""Подтверждение в scripts/pg-upgrade.sh на настоящем терминале.

На сервере пользователя ответ «y» не распознавался: перед вопросом скрипт вызывает
docker compose run, после которого терминал отдаёт Enter как «\\r», и в ответ
попадало «y\\r». Здесь скрипт запускается в псевдотерминале с подставным docker,
который портит терминал так же, а Enter приходит как «\\r\\n».

Сам перенос здесь не проверяется — только то, что происходит до остановки бота.
Настоящий перенос гоняет CI-workflow pg-upgrade.yml.
"""

from __future__ import annotations

import os
import pty
import select
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / 'scripts' / 'pg-upgrade.sh'
BASH = shutil.which('bash') or '/bin/bash'

pytestmark = pytest.mark.skipif(not hasattr(pty, 'fork'), reason='нужен POSIX-псевдотерминал')

# Отвечает на вызовы, которые скрипт делает до вопроса, и пишет журнал.
# «compose run» выключает у терминала icrnl — после него Enter читается как «\r».
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  'compose version') exit 0 ;;
  'compose config') printf 'name: testproj\nservices:\n  postgres:\n    image: postgres:18-alpine\n'; exit 0 ;;
  'compose stop'*) exit 0 ;;
  'compose run'*)
    stty -icrnl </dev/tty 2>/dev/null || true
    printf 'POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=secret\nPOSTGRES_DB=remnawave_bot\nPOSTGRES_INITDB_ARGS=\n'
    exit 0 ;;
  'pull'*) exit 0 ;;
  'volume ls'*) case "$*" in *postgres18_data*) echo testproj_postgres18_data ;; *) echo testproj_postgres_data ;; esac; exit 0 ;;
  'volume inspect'*) exit 0 ;;
  'info'*) exit 0 ;;
  'ps'*) exit 0 ;;
  *'--entrypoint cat'*) echo 15; exit 0 ;;
  *'test -s /new/'*) exit 1 ;;
  *'find /new'*) exit 0 ;;
  *'--entrypoint du'*) printf '2048\t/old\n'; exit 0 ;;
  'rm '*|'volume rm'*) exit 0 ;;
  # Дальше — уже сам перенос: здесь проверять нечего, останавливаемся.
  *) echo "fake docker: stop at: $*" >&2; exit 97 ;;
esac
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    (tmp_path / 'scripts').mkdir()
    shutil.copy(SCRIPT, tmp_path / 'scripts' / 'pg-upgrade.sh')
    (tmp_path / 'docker-compose.yml').write_text('services: {}\n', encoding='utf-8')
    (tmp_path / '.env').write_text('', encoding='utf-8')

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    docker = bin_dir / 'docker'
    docker.write_text(FAKE_DOCKER, encoding='utf-8')
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)

    log = tmp_path / 'docker.log'
    env = {
        **os.environ,
        'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        'FAKE_DOCKER_LOG': str(log),
        'LC_ALL': 'C.UTF-8',
    }
    return {'script': str(tmp_path / 'scripts' / 'pg-upgrade.sh'), 'env': env, 'log': log}


def _run_in_terminal(sandbox: dict, answer: bytes, *args: str) -> tuple[int, str]:
    """Запускает скрипт в псевдотерминале и отвечает на вопрос, когда тот появится."""
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover — дочерний процесс
        os.execve(BASH, [BASH, sandbox['script'], *args], sandbox['env'])  # noqa: S606 — bash из PATH, скрипт из репозитория

    output = b''
    answered = False
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        if not answered and 'Продолжить?'.encode() in output:
            os.write(fd, answer)
            answered = True
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(status), output.decode('utf-8', 'replace')


def _stopped_services(sandbox: dict) -> bool:
    return sandbox['log'].exists() and 'compose stop' in sandbox['log'].read_text(encoding='utf-8')


def test_y_with_crlf_enter_is_accepted(sandbox: dict) -> None:
    _, output = _run_in_terminal(sandbox, b'y\r\n', '--no-bot')

    assert 'Отменено' not in output, output
    assert _stopped_services(sandbox), f'после «y» перенос не начался:\n{output}'


def test_russian_da_is_accepted(sandbox: dict) -> None:
    _, output = _run_in_terminal(sandbox, 'да\r\n'.encode(), '--no-bot')

    assert 'Отменено' not in output, output
    assert _stopped_services(sandbox)


def test_no_cancels_without_touching_anything(sandbox: dict) -> None:
    code, output = _run_in_terminal(sandbox, b'n\r\n', '--no-bot')

    assert code == 1
    assert 'Отменено' in output
    assert 'ничего не изменено' in output.lower()
    assert 'Бот остановлен' not in output
    assert not _stopped_services(sandbox)


def test_without_terminal_requires_yes(sandbox: dict) -> None:
    # start_new_session отвязывает процесс от управляющего терминала: /dev/tty недоступен.
    result = subprocess.run(  # noqa: S603 — скрипт из репозитория, копия во временной папке
        [BASH, sandbox['script'], '--no-bot'],
        env=sandbox['env'],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        start_new_session=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert '--yes' in result.stderr
    assert not sandbox['log'].exists(), 'без подтверждения скрипт не должен трогать docker'
