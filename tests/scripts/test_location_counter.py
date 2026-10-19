import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta

from scripts.location_counter import location_counter
from scripts.location_counter.location_counter import (
    DeviceTracker,
    LogEvent,
    build_report,
    make_key,
    parse_line,
)


PREFIX = 'BRIDGE_'

LINE = (
    '2026/10/18 17:34:07.590389 from 198.51.100.7:51234 accepted tcp:203.0.113.80:443 '
    '[vless-auto -> BRIDGE_FI2] email: 123'
)


def test_parse_regular_line():
    e = parse_line(LINE)
    assert e == LogEvent(
        datetime(2026, 10, 18, 17, 34, 7, 590389, tzinfo=UTC), '198.51.100.7', 'vless-auto', 'BRIDGE_FI2', '123'
    )


def test_parse_double_arrow_and_tcp_prefix():
    e = parse_line(
        '2026/10/18 17:34:07.1 from tcp:198.51.100.7:3832 accepted udp:8.8.8.8:53 [vless-auto >> direct] email: 7'
    )
    assert (e.ip, e.outbound, e.user) == ('198.51.100.7', 'direct', '7')


def test_parse_ipv6_client():
    e = parse_line('2026/10/18 17:34:07.1 from [2001:db8::1]:443 accepted tcp:x.com:443 [vless-nl -> direct] email: 9')
    assert e.ip == '2001:db8::1'


def test_skip_lines_without_user():
    assert parse_line('2026/10/18 17:34:07.1 from DNS accepted udp:1.1.1.1:53 [xray.system.49d7 >> direct]') is None
    assert parse_line('garbage') is None


def ev(sec, inbound, outbound, user='1', ip='1.1.1.1'):
    return LogEvent(datetime(2026, 10, 19, 7, 0, 0, tzinfo=UTC) + timedelta(seconds=sec), ip, inbound, outbound, user)


def test_last_connection_wins_and_exit_tracked():
    t = DeviceTracker(PREFIX)
    t.observe(ev(0, 'vless-auto', 'BRIDGE_FI1'))
    t.observe(ev(10, 'vless-auto', 'direct'))
    [d] = t.snapshot(ev(20, 'x', 'y').ts)
    assert (d.inbound, d.exit, d.last_seen) == ('vless-auto', 'FI1', ev(10, 'x', 'y').ts)


def test_inbound_switch_resets_exit():
    t = DeviceTracker(PREFIX)
    t.observe(ev(0, 'vless-auto', 'BRIDGE_FI1'))
    t.observe(ev(5, 'vless-de', 'BRIDGE_DE1'))
    t.observe(ev(9, 'vless-auto', 'direct'))
    [d] = t.snapshot(ev(10, 'x', 'y').ts)
    assert (d.inbound, d.exit) == ('vless-auto', None)


def test_older_event_ignored():
    t = DeviceTracker(PREFIX)
    t.observe(ev(10, 'a', 'direct'))
    t.observe(ev(5, 'b', 'direct'))
    assert t.snapshot(ev(11, 'x', 'y').ts)[0].inbound == 'a'


def test_snapshot_prunes_after_window():
    t = DeviceTracker(PREFIX)
    t.observe(ev(0, 'a', 'direct'))
    t.observe(ev(0, 'a', 'direct', ip='2.2.2.2'))
    t.observe(ev(250, 'a', 'direct', ip='2.2.2.2'))
    assert [d.ip for d in t.snapshot(ev(301, 'x', 'y').ts)] == ['2.2.2.2']


def test_report_has_no_raw_ids():
    t = DeviceTracker(PREFIX)
    t.observe(ev(0, 'vless-auto', 'BRIDGE_FI2', user='4242', ip='9.9.9.9'))
    now = ev(30, 'x', 'y').ts
    r = build_report('HUB', t.snapshot(now), 'secret', now)
    assert r['node'] == 'HUB' and r['window_seconds'] == 300 and r['generated_at'] == '2026-10-19T07:00:30Z'
    [d] = r['devices']
    assert d == {
        'u': make_key('secret', '4242'),
        'd': make_key('secret', '4242|9.9.9.9'),
        't': '2026-10-19T07:00:00Z',
        'inbound': 'vless-auto',
        'exit': 'FI2',
    }
    assert len(d['u']) == 16 and '4242' not in json.dumps(r) and '9.9.9.9' not in json.dumps(r)


def test_stale_exit_dropped_but_device_stays():
    t = DeviceTracker(PREFIX)
    t.observe(ev(0, 'vless-auto', 'BRIDGE_FI1'))
    t.observe(ev(100, 'vless-auto', 'direct'))
    t.observe(ev(200, 'vless-auto', 'direct'))
    # последний мост был 305 с назад (вне окна), а само устройство подключалось 105 с назад
    [d] = t.snapshot(ev(305, 'x', 'y').ts)
    assert (d.last_seen, d.exit) == (ev(200, 'x', 'y').ts, None)


def test_fresh_exit_kept_in_window():
    t = DeviceTracker(PREFIX)
    t.observe(ev(0, 'vless-auto', 'BRIDGE_FI1'))
    t.observe(ev(250, 'vless-auto', 'direct'))
    [d] = t.snapshot(ev(299, 'x', 'y').ts)
    assert d.exit == 'FI1'


def test_exit_refreshed_by_new_bridge_connection():
    t = DeviceTracker(PREFIX)
    t.observe(ev(0, 'vless-auto', 'BRIDGE_FI1'))
    t.observe(ev(200, 'vless-auto', 'BRIDGE_DE1'))
    [d] = t.snapshot(ev(400, 'x', 'y').ts)
    assert d.exit == 'DE1'


def test_exit_not_leaked_across_inbound_switch_via_exit_seen():
    t = DeviceTracker(PREFIX)
    t.observe(ev(0, 'vless-auto', 'BRIDGE_FI1'))
    t.observe(ev(5, 'vless-de', 'direct'))
    [d] = t.snapshot(ev(10, 'x', 'y').ts)
    assert (d.inbound, d.exit) == ('vless-de', None)


def test_exit_requires_prefix_and_is_reported_without_it():
    t = DeviceTracker(PREFIX)
    t.observe(ev(0, 'vless-auto', 'direct'))
    t.observe(ev(0, 'vless-auto', 'BRIDGE_', ip='2.2.2.2'))
    t.observe(ev(0, 'vless-auto', 'BRIDGE_UK1', ip='3.3.3.3'))
    exits = {d.ip: d.exit for d in t.snapshot(ev(1, 'x', 'y').ts)}
    assert exits == {'1.1.1.1': None, '2.2.2.2': None, '3.3.3.3': 'UK1'}


def test_empty_prefix_disables_exit_tracking():
    t = DeviceTracker()
    t.observe(ev(0, 'vless-auto', 'BRIDGE_FI1'))
    [d] = t.snapshot(ev(1, 'x', 'y').ts)
    assert (d.inbound, d.exit) == ('vless-auto', None)


def test_log_command_wraps_tail_with_stdin_watchdog():
    cmd = location_counter.log_command()
    assert cmd[:4] == ['docker', 'exec', '-i', 'remnanode']
    assert cmd[4:6] == ['sh', '-c']
    script = cmd[6]
    assert 'tail -F -n +1 /var/log/xray/current' in script
    assert 'cat >/dev/null' in script and 'kill $p' in script


def test_log_command_takes_container_and_quoted_path():
    cmd = location_counter.log_command('my-node', '/logs/xray access.log')
    assert cmd[3] == 'my-node'
    assert "tail -F -n +1 '/logs/xray access.log' &" in cmd[6]


def test_log_reader_keeps_stdin_pipe_open(monkeypatch):
    captured = {}

    class FakeProcess:
        stdout = iter([])

        def wait(self):
            return 0

    def fake_popen(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['kwargs'] = kwargs
        return FakeProcess()

    monkeypatch.setattr(location_counter.subprocess, 'Popen', fake_popen)
    stopped = threading.Event()
    command = location_counter.log_command()
    location_counter._read_log(command, DeviceTracker(), threading.Lock(), stopped)
    assert captured['cmd'] == command
    assert captured['kwargs']['stdin'] == subprocess.PIPE
    assert stopped.is_set()


def test_clock_skew_flags_both_directions_beyond_limit():
    now = ev(1000, 'x', 'y').ts
    assert location_counter.clock_skew(None, now) is None
    assert location_counter.clock_skew(now - timedelta(seconds=299), now) is None
    assert location_counter.clock_skew(now - timedelta(seconds=3600), now) == 3600
    assert location_counter.clock_skew(now + timedelta(seconds=301), now) == -301


def test_tracker_remembers_newest_event_and_count():
    t = DeviceTracker(PREFIX)
    t.observe(ev(10, 'a', 'direct'))
    t.observe(ev(5, 'a', 'direct', ip='2.2.2.2'))
    assert (t.events_seen, t.newest_ts) == (2, ev(10, 'x', 'y').ts)
