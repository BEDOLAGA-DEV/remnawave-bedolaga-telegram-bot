"""Счётчик устройств по локациям на VPN-ноде.

Читает access-лог xray из контейнера remnanode, помнит последнее подключение каждого устройства
(пара «пользователь + IP») 300 секунд и раз в минуту отправляет обезличенный отчёт в кабинет.
Только стандартная библиотека, совместим с Python 3.10.

Настройки — переменные окружения (см. location-counter.env.example): CABINET_URL,
LOCATION_COUNTER_TOKEN, NODE_NAME и необязательные EXIT_OUTBOUND_PREFIX, XRAY_CONTAINER,
XRAY_ACCESS_LOG.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


UTC = timezone.utc  # noqa: UP017 -- datetime.UTC появился только в 3.11, а на нодах бывает 3.10

WINDOW_SECONDS = 300
REPORT_INTERVAL_SECONDS = 60
CLOCK_SKEW_SECONDS = 300
CLOCK_WARNING_INTERVAL_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 15
REPORT_PATH = '/api/cabinet/admin/stats/locations/report'
TOKEN_HEADER = 'X-Location-Counter-Token'
DEFAULT_CONTAINER = 'remnanode'
# Сюда remnanode пишет вывод xray (s6-log), в том числе access-лог.
DEFAULT_ACCESS_LOG = '/var/log/xray/current'


def log_command(container: str = DEFAULT_CONTAINER, access_log: str = DEFAULT_ACCESS_LOG) -> list[str]:
    # `docker exec` без -i не передаёт сигналы, а dockerd не убивает процесс в контейнере при обрыве
    # клиента, поэтому одинокий `tail -F` пережил бы перезапуск сервиса. Здесь stdin держится открытым
    # со стороны Python: когда счётчик умирает, `cat` получает EOF, и `kill` останавливает tail.
    return [
        'docker',
        'exec',
        '-i',
        container,
        'sh',
        '-c',
        f'tail -F -n +1 {shlex.quote(access_log)} & p=$!; cat >/dev/null; kill $p',
    ]


_LINE_RE = re.compile(
    r'^(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2})(?:\.(?P<frac>\d+))? '
    r'from (?:(?:tcp|udp):)?(?:\[(?P<ip6>[0-9A-Fa-f:.]+)\]|(?P<ip4>\d{1,3}(?:\.\d{1,3}){3})):\d+ '
    r'accepted \S+ \[(?P<inbound>[^\s\]]+) (?:->|>>) (?P<outbound>[^\s\]]+)\] '
    r'email: (?P<user>\S+)\s*$'
)


@dataclass(frozen=True)
class LogEvent:
    ts: datetime
    ip: str
    inbound: str
    outbound: str
    user: str


@dataclass
class DeviceState:
    user: str
    ip: str
    last_seen: datetime
    inbound: str
    exit: str | None
    exit_seen: datetime | None = None


def parse_line(line: str) -> LogEvent | None:
    m = _LINE_RE.match(line.strip())
    if m is None:
        return None
    try:
        ts = datetime.strptime(f'{m["date"]} {m["time"]}', '%Y/%m/%d %H:%M:%S').replace(tzinfo=UTC)
    except ValueError:
        return None
    if m['frac']:
        ts = ts.replace(microsecond=int(m['frac'][:6].ljust(6, '0')))
    return LogEvent(ts, m['ip6'] or m['ip4'], m['inbound'], m['outbound'], m['user'])


class DeviceTracker:
    """Последнее подключение каждого устройства; устройство = (пользователь, IP).

    `exit_prefix` — префикс тегов outbound'ов, которые считаются выходами (например, мосты на
    другие ноды за балансиром). Выход в отчёте — тег без префикса. Пустой префикс — выходы не
    отслеживаются.
    """

    def __init__(self, exit_prefix: str = '') -> None:
        self._exit_prefix = exit_prefix
        self._devices: dict[tuple[str, str], DeviceState] = {}
        self.events_seen = 0
        self.newest_ts: datetime | None = None

    def _exit_of(self, outbound: str) -> str | None:
        if not self._exit_prefix or not outbound.startswith(self._exit_prefix):
            return None
        return outbound.removeprefix(self._exit_prefix) or None

    def observe(self, event: LogEvent) -> None:
        self.events_seen += 1
        if self.newest_ts is None or event.ts > self.newest_ts:
            self.newest_ts = event.ts
        key = (event.user, event.ip)
        state = self._devices.get(key)
        if state is None:
            exit_tag = self._exit_of(event.outbound)
            self._devices[key] = DeviceState(
                event.user, event.ip, event.ts, event.inbound, exit_tag, event.ts if exit_tag else None
            )
            return
        if event.ts < state.last_seen:
            return
        exit_tag = self._exit_of(event.outbound)
        if event.inbound != state.inbound:
            state.inbound = event.inbound
            state.exit = exit_tag
            state.exit_seen = event.ts if exit_tag else None
        elif exit_tag is not None:
            state.exit = exit_tag
            state.exit_seen = event.ts
        state.last_seen = event.ts

    def snapshot(self, now: datetime) -> list[DeviceState]:
        threshold = now - timedelta(seconds=WINDOW_SECONDS)
        self._devices = {k: v for k, v in self._devices.items() if v.last_seen >= threshold}
        # Выход без мостового подключения в окне в разбивку не попадает, само устройство остаётся.
        return [
            DeviceState(
                s.user,
                s.ip,
                s.last_seen,
                s.inbound,
                s.exit if s.exit_seen is not None and s.exit_seen >= threshold else None,
                s.exit_seen,
            )
            for s in self._devices.values()
        ]


def make_key(token: str, value: str) -> str:
    return hmac.new(token.encode(), value.encode(), hashlib.sha256).hexdigest()[:16]


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def build_report(node: str, devices: list[DeviceState], token: str, now: datetime) -> dict:
    return {
        'node': node,
        'generated_at': _iso(now),
        'window_seconds': WINDOW_SECONDS,
        'devices': [
            {
                'u': make_key(token, d.user),
                'd': make_key(token, f'{d.user}|{d.ip}'),
                't': _iso(d.last_seen),
                'inbound': d.inbound,
                'exit': d.exit,
            }
            for d in devices
        ],
    }


def clock_skew(newest_ts: datetime | None, now: datetime) -> float | None:
    """Расхождение «часы ноды - время событий в логе» в секундах, если оно больше допустимого."""
    if newest_ts is None:
        return None
    skew = (now - newest_ts).total_seconds()
    return skew if abs(skew) > CLOCK_SKEW_SECONDS else None


def _send(url: str, token: str, report: dict) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(report).encode(),
        headers={'Content-Type': 'application/json', TOKEN_HEADER: token},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        response.read()


def _read_log(command: list[str], tracker: DeviceTracker, lock: threading.Lock, stopped: threading.Event) -> None:
    try:
        # stdin = PIPE: не пишем и не закрываем до выхода процесса — см. комментарий в log_command
        process = subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors='replace',
        )
        for line in process.stdout:
            event = parse_line(line)
            if event is not None:
                with lock:
                    tracker.observe(event)
        process.wait()
    except Exception as exc:
        print(f'log reader failed: {exc}', file=sys.stderr, flush=True)
    finally:
        stopped.set()


def main() -> int:
    base_url = os.environ.get('CABINET_URL', '').rstrip('/')
    token = os.environ.get('LOCATION_COUNTER_TOKEN', '')
    node = os.environ.get('NODE_NAME', '')
    missing = [
        name
        for name, value in (('CABINET_URL', base_url), ('LOCATION_COUNTER_TOKEN', token), ('NODE_NAME', node))
        if not value
    ]
    if missing:
        print(f'missing environment variables: {", ".join(missing)}', file=sys.stderr)
        return 2

    url = base_url + REPORT_PATH
    command = log_command(
        os.environ.get('XRAY_CONTAINER') or DEFAULT_CONTAINER,
        os.environ.get('XRAY_ACCESS_LOG') or DEFAULT_ACCESS_LOG,
    )
    tracker = DeviceTracker(os.environ.get('EXIT_OUTBOUND_PREFIX', '').strip())
    lock = threading.Lock()
    stopped = threading.Event()
    threading.Thread(target=_read_log, args=(command, tracker, lock, stopped), daemon=True).start()

    events_before = 0
    first_cycle = True
    last_warning = 0.0
    while not stopped.wait(REPORT_INTERVAL_SECONDS):
        now = datetime.now(UTC)
        with lock:
            devices = tracker.snapshot(now)
            events_total, newest_ts = tracker.events_seen, tracker.newest_ts
        # Строки идут, а время последней далеко от часов ноды: скорее всего, часовой пояс или часы
        # (первый цикл пропускаем: после старта tail перечитывает весь лог, он может быть старым).
        skew = clock_skew(newest_ts, now) if events_total > events_before and not first_cycle else None
        if skew is not None and time.monotonic() - last_warning >= CLOCK_WARNING_INTERVAL_SECONDS:
            last_warning = time.monotonic()
            print(
                f'clock warning: newest log event differs from node time by {skew:.0f}s (timezone or clock?)',
                file=sys.stderr,
                flush=True,
            )
        events_before, first_cycle = events_total, False
        try:
            _send(url, token, build_report(node, devices, token, now))
        except Exception as exc:
            print(f'report failed: {exc}', file=sys.stderr, flush=True)
    print('docker exec finished, exiting', file=sys.stderr, flush=True)
    return 1


if __name__ == '__main__':
    sys.exit(main())
