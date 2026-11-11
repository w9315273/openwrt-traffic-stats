#!/usr/bin/env python3
"""使用真实格式的 UCI JSON 验证 rpcd 接口及 SQLite 只读访问."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

BINARY = str(Path(sys.argv.pop(1)).resolve())
TEMPORARY_ROOT = Path(__file__).resolve().parents[2] / '.local/tmp'


class RpcTests(unittest.TestCase):
    def setUp(self):
        TEMPORARY_ROOT.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='traffic-rpc-', dir=TEMPORARY_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / 'traffic.db'
        self.runtime = self.root / 'runtime.json'
        self.config = self.root / 'traffic-stats'
        self.actions = self.root / 'actions'
        self.config_reads = self.root / 'config-reads'
        self.service = self.root / 'service'
        self.service.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "' + str(self.actions) + '"\n')
        self.service.chmod(0o700)
        ubus = self.root / 'ubus'
        ubus.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, pathlib, sys\n'
            'assert sys.argv[1:4] == ["call", "uci", "get"]\n'
            'name = json.loads(sys.argv[4])["config"]\n'
            'root = pathlib.Path(os.environ["TS_RPC_CONFIG_DIR"])\n'
            'with (root / "config-reads").open("a") as log: log.write(name + "\\n")\n'
            'print((root / name).read_text())\n')
        ubus.chmod(0o700)
        self.env = dict(os.environ, PATH=str(self.root) + ':' + os.environ['PATH'],
                        TS_RPC_CONFIG_DIR=str(self.root))
        self.write_config()
        (self.root / 'system').write_text(json.dumps({'values': {
            'main': {'.type': 'system', 'timezone': 'KST-9'}}}))

    def write_config(self, enabled=False):
        self.config.write_text(json.dumps({'values': {'main': {
            '.type': 'traffic_stats', 'enabled': str(int(enabled)),
            'lan_device': 'lan-test', 'wan_device': 'wan-test',
            'database': str(self.database), 'interval': '5',
            'commit_interval': '60', 'retention_days': '90'}}}))

    def invoke(self, verb, method=None, request=None, raw=None):
        command = [BINARY, '--runtime-file', str(self.runtime),
                   '--service-script', str(self.service), verb]
        if method:
            command.append(method)
        result = subprocess.run(command, env=self.env,
                                input=raw if raw is not None else json.dumps(request or {}),
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(len(result.stdout.encode()), 256 * 1024)
        return json.loads(result.stdout)

    def call(self, method, request=None, **kwargs):
        return self.invoke('call', method, request, **kwargs)

    def create_history(self):
        with sqlite3.connect(self.database) as db:
            db.executescript("""
                PRAGMA application_id=1414747219;
                PRAGMA user_version=1;
                CREATE TABLE devices(id TEXT PRIMARY KEY, mac TEXT, hostname TEXT, last_seen_ms INTEGER);
                CREATE TABLE usage_hourly(counter TEXT, hour_start INTEGER, day TEXT, timezone TEXT,
                                          bytes INTEGER, packets INTEGER, flags INTEGER);
                CREATE TABLE usage_daily(counter TEXT, day TEXT, timezone TEXT,
                                         bytes INTEGER, packets INTEGER, flags INTEGER);
                CREATE TABLE events(id INTEGER PRIMARY KEY, wall_ms INTEGER, kind TEXT,
                                    counter TEXT, detail TEXT);
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value INTEGER);
                INSERT INTO metadata VALUES('last_commit_ms', 1793836800000);
                INSERT INTO devices VALUES('020000000002','02:00:00:00:00:02','<b>device</b>',1793836800000);
                INSERT INTO usage_hourly VALUES('total_up',1793836800,'2026-11-05','KST-9',12345,100,16);
                INSERT INTO usage_daily VALUES('total_up','2026-11-05','KST-9',12345,100,16);
                INSERT INTO events VALUES(1,1793836800000,'generation_changed','total_up','test gap');
            """)

    def test_list_exposes_only_narrow_methods(self):
        self.assertEqual(set(self.invoke('list')), {'status', 'history', 'service', 'containers', 'name_save', 'name_list'})
        self.assertFalse(self.config_reads.exists())

    def test_config_uses_two_native_typed_uci_reads(self):
        self.assertTrue(self.call('status')['ok'])
        self.assertEqual(self.config_reads.read_text().splitlines(), ['traffic-stats', 'system'])
        data = json.loads(self.config.read_text())
        data['values']['main']['interval'] = ['5']
        self.config.write_text(json.dumps(data))
        self.assertEqual(self.call('status')['error'], 'invalid_configuration')

    def test_invalid_configuration_never_becomes_defaults(self):
        self.config.write_text('not JSON')
        self.assertEqual(self.call('status')['error'], 'invalid_configuration')
        self.write_config()
        for value in (None, True, 'nan', '-1', '1\n', '9' * 32):
            data = json.loads(self.config.read_text())
            data['values']['main']['interval'] = value
            self.config.write_text(json.dumps(data))
            self.assertEqual(self.call('status')['error'], 'invalid_configuration')
        self.write_config()
        (self.root / 'system').write_text(json.dumps({'values': {
            'main': {'.type': 'system', 'timezone': ['KST-9']}}}))
        self.assertEqual(self.call('status')['error'], 'invalid_configuration')

    def test_missing_runtime_is_available_as_state_without_fabricated_counts(self):
        reply = self.call('status')
        self.assertTrue(reply['ok'])
        data = reply['data']
        self.assertFalse(data['available'])
        self.assertFalse(data['running'])
        self.assertTrue(data['stale'])
        self.assertEqual(data['reason'], 'runtime_missing')
        self.assertNotIn('total_up', data)
        self.assertEqual(data['configuration']['database'], str(self.database))
        self.assertEqual(data['interfaces']['lan'][0]['name'], 'lan-test')
        self.assertFalse(data['interfaces']['lan'][0]['present'])
        self.assertEqual(data['timezone'], 'KST-9')
        self.assertFalse(self.database.exists())
        self.assertFalse(self.runtime.exists())

    def test_live_runtime_keeps_uint64_strings_and_reports_saved_devices(self):
        self.create_history()
        self.runtime.write_text(json.dumps({
            'pid': os.getpid(), 'running': True, 'interval': 5,
            'sample_mono_ms': int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'total_up': {'bytes': '18446744073709551615', 'packets': '100'},
        }))
        data = self.call('status')['data']
        self.assertTrue(data['available'])
        self.assertTrue(data['running'])
        self.assertFalse(data['stale'])
        self.assertEqual(data['total_up']['bytes'], '18446744073709551615')
        self.assertEqual(data['known_devices'][0]['hostname'], '<b>device</b>')
        self.assertFalse(data['known_devices_truncated'])

    def test_json_name_rpc_crud_conflict_and_malformed_file(self):
        snapshot = self.call('name_list')['data']
        request = {'action':'save', 'section':'', 'name':'我的笔记本',
                   'mac':'02:00:00:00:00:02', 'revision':snapshot['revision']}
        self.assertTrue(self.call('name_save', request)['ok'])
        self.assertEqual(self.call('name_save', request)['error'], 'name_conflict')
        snapshot = self.call('name_list')['data']
        self.assertEqual(snapshot['rules'][0]['name'], request['name'])
        self.assertTrue(self.call('name_save', {'action':'delete','section':'0','revision':snapshot['revision']})['ok'])
        self.assertEqual(json.loads((self.root / 'devices.json').read_text()), [])
        (self.root / 'devices.json').write_text('[broken')
        self.assertEqual(self.call('name_list')['error'], 'name_read_failed')
        self.assertEqual(self.call('name_save', request)['error'], 'name_save_failed')
        self.assertEqual((self.root / 'devices.json').read_text(), '[broken')

    def test_names_from_json_are_display_only_and_invalid_rules_are_isolated(self):
        self.create_history()
        (self.root / 'devices.json').write_text(json.dumps([
            {'mac':'02:00:00:00:00:02', 'name':'我的 笔记本'},
            {'name':'没有匹配条件'}]))
        response = self.call('status')['data']
        self.assertEqual(response['known_devices'][0]['custom_name'], '我的 笔记本')
        self.assertEqual(response['known_devices'][0]['name_rule'], '0')
        self.assertEqual(response['known_devices'][0]['hostname'], '<b>device</b>')
        self.assertEqual(response['invalid_name_rules'], ['1'])
        self.assertNotIn('name_rules', response['configuration'])
        self.assertEqual(self.call('history', {'from':'2026-11-05','to':'2026-11-05'})['data']['rows'][0]['bytes'], '12345')

    def test_bad_runtime_still_returns_configuration(self):
        self.runtime.write_text('not json')
        reply = self.call('status')
        self.assertTrue(reply['ok'])
        self.assertFalse(reply['data']['available'])
        self.assertEqual(reply['data']['reason'], 'runtime_unreadable')
        self.assertIn('configuration', reply['data'])

    def test_history_absence_does_not_create_database(self):
        reply = self.call('history', {'from': '2026-11-01', 'to': '2026-11-05'})
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['error'], 'not_initialized')
        self.assertFalse(self.database.exists())

    def test_history_formats_saved_timezone_and_preserves_quality(self):
        self.create_history()
        for resolution in ('day', 'month'):
            with self.subTest(resolution=resolution):
                reply = self.call('history', {
                    'from': '2026-11-01', 'to': '2026-11-05',
                    'resolution': resolution, 'counter': 'total_up', 'limit': 100,
                })
                self.assertTrue(reply['ok'], reply)
                data = reply['data']
                self.assertFalse(data['pending_included'])
                self.assertEqual(data['rows'][0]['bytes'], '12345')
                self.assertEqual(data['rows'][0]['flags'], 16)
                self.assertEqual(data['events_scope'], 'latest')
                self.assertEqual(data['events'][0]['kind'], 'generation_changed')

    def test_request_types_dates_ranges_paths_and_actions_are_restricted(self):
        cases = [
            ('status', {'path': '/etc/shadow'}, 'invalid_request'),
            ('history', {'from': '2026-02-29', 'to': '2026-03-01'}, 'invalid_request'),
            ('history', {'from': '2026-11-05', 'to': '2026-11-01'}, 'invalid_request'),
            ('history', {'from': '2026-11-01', 'to': '2026-11-02', 'limit': '10'}, 'invalid_request'),
            ('history', {'from': '2026-11-01', 'to': '2026-11-02', 'limit': 1001}, 'invalid_request'),
            ('history', {'from': '2026-11-01', 'to': '2026-11-02', 'limit': None}, 'invalid_request'),
            ('history', {'from': '2026-11-01', 'to': '2026-11-02', 'resolution': None}, 'invalid_request'),
            ('history', {'from': '2026-11-01', 'to': '2026-11-02', 'counter': None}, 'invalid_request'),
            ('history', {'from': '2026-11-01', 'to': '2026-11-02', 'counter': "x';DROP TABLE devices"}, 'invalid_request'),
            ('history', {'from': '2026-01-01', 'to': '2026-02-01', 'resolution': '30min'}, 'range_too_large'),
            ('history', {'from': '2020-01-01', 'to': '2026-11-01'}, 'range_too_large'),
            ('history', {'from': '2026-11-01', 'to': '2026-11-02', 'database': '/etc/passwd'}, 'invalid_request'),
            ('history', {'from': '2026-11-01\0extra', 'to': '2026-11-02'}, 'invalid_request'),
            ('service', {'action': 'restart;echo bad'}, 'invalid_request'),
            ('service', {'action': True}, 'invalid_request'),
            ('service', {'action': 'restart', 'command': 'reboot'}, 'invalid_request'),
        ]
        for method, request, code in cases:
            with self.subTest(method=method, request=request):
                self.assertEqual(self.call(method, request)['error'], code)
        self.assertFalse(self.actions.exists())
        for raw in ('[]', '{} {}', '{}\0garbage', '{', '{"value":"' + 'x' * 65536 + '"}'):
            self.assertEqual(self.call('status', raw=raw)['error'], 'invalid_request')

    def test_service_control_executes_only_literal_actions(self):
        self.assertEqual(self.call('service', {'action': 'start'})['error'], 'service_disabled')
        self.assertFalse(self.actions.exists())
        self.write_config(enabled=True)
        reply = self.call('service', {'action': 'restart'})
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(self.actions.read_text(), 'restart\n')
        self.config.write_text("config traffic_stats 'main'\n option interval 'invalid'\n")
        self.assertTrue(self.call('service', {'action': 'stop'})['ok'])
        self.assertEqual(self.actions.read_text(), 'restart\nstop\n')

    def test_unrelated_database_is_not_modified(self):
        self.database.write_bytes(b'not a database')
        before = self.database.read_bytes()
        reply = self.call('history', {'from': '2026-11-01', 'to': '2026-11-05'})
        self.assertEqual(reply['error'], 'history_unavailable')
        self.assertEqual(self.database.read_bytes(), before)

    def test_rows_and_known_devices_are_explicitly_truncated(self):
        self.create_history()
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO usage_daily VALUES('total_down','2026-11-05','KST-9',10,1,0)")
            db.executemany('INSERT INTO devices VALUES(?,?,?,?)', (
                (f'{number:012x}', '02:00:00:00:00:01', 'test', 1793836800001)
                for number in range(1000)))
        reply = self.call('history', {'from': '2026-11-05', 'to': '2026-11-05', 'limit': 1})
        self.assertTrue(reply['ok'])
        self.assertEqual(len(reply['data']['rows']), 1)
        self.assertTrue(reply['data']['truncated'])
        data = self.call('status')['data']
        self.assertEqual(len(data['known_devices']), 1000)
        self.assertTrue(data['known_devices_truncated'])

    def test_response_limit_returns_complete_json_error(self):
        self.runtime.write_text(json.dumps({
            'pid': os.getpid(), 'running': True, 'interval': 5,
            'sample_mono_ms': int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'oversized': 'x' * (240 * 1024),
        }))
        reply = self.call('status')
        self.assertEqual(reply['error'], 'response_too_large')


if __name__ == '__main__':
    unittest.main()
