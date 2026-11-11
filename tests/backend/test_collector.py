#!/usr/bin/env python3
"""使用可控 nft 子进程验证真实采集循环和 SQLite 持久化."""
from datetime import datetime, timedelta, timezone
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
GENERATION = '12345678-1234-1234-1234-123456789abc'
DEVICE_UP = 'd_020000000002_up'


def ruleset(upload):
    objects = [{'table': {'family': 'inet', 'name': 'traffic_stats', 'handle': 1,
                          'comment': 'traffic-stats:v1:' + GENERATION}}]
    for handle, name in enumerate(('forward', 'upload', 'download'), 2):
        chain = dict(family='inet', table='traffic_stats', name=name, handle=handle)
        if name == 'forward':
            chain.update(type='filter', hook='forward', prio=10, policy='accept')
        objects.append({'chain': chain})
    for handle, name in enumerate(('up4', 'up6', 'down4', 'down6'), 10):
        objects.append({'map': dict(family='inet', table='traffic_stats', name=name,
                                    handle=handle, type='ipv4_addr' if name.endswith('4')
                                    else 'ipv6_addr', map='counter')})
    for handle, name in enumerate(('total_up', 'total_down', 'unknown_up', 'unknown_down',
                                   DEVICE_UP, 'd_020000000002_down'), 20):
        value = upload if name in ('total_up', DEVICE_UP) else 0
        objects.append({'counter': dict(family='inet', table='traffic_stats', name=name,
                                        handle=handle, bytes=value, packets=value // 10)})
    return {'nftables': objects}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='traffic-collector-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / 'traffic.db'
        self.runtime = self.root / 'runtime.json'
        self.snapshot = self.root / 'ruleset.json'
        self.fail_reads = self.root / 'fail-reads'
        self.process = None
        self.starts = 0
        self.logs = []
        self.addCleanup(self.stop)
        self.set_upload(1000)
        fake = self.root / 'nft'
        fake.write_text(
            '#!/usr/bin/env python3\n'
            'import os, pathlib, sys\n'
            "if sys.argv[1:] in (['-j', '-a', '-t', 'list', 'ruleset'], ['-j', '-a', 'list', 'table', 'inet', 'traffic_stats']):\n"
            "    if pathlib.Path(os.environ['TS_TEST_FAIL']).exists():\n"
            "        print('Forced nft read failure', file=sys.stderr)\n"
            '        sys.exit(7)\n'
            "    print(pathlib.Path(os.environ['TS_TEST_RULESET']).read_text())\n"
            "elif sys.argv[1:] == ['-f', '-']:\n"
            '    sys.stdin.read()\n'
            'else:\n'
            '    sys.exit(19)\n'
        )
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.root) + ':' + os.environ['PATH'],
                        TS_TEST_FAIL=str(self.fail_reads), TS_TEST_RULESET=str(self.snapshot))

    def set_upload(self, value):
        temporary = self.root / 'ruleset.next'
        temporary.write_text(json.dumps(ruleset(value)))
        temporary.replace(self.snapshot)

    def diagnostics(self):
        return '\n'.join(path.read_text() for path in self.logs if path.exists())

    def wait_for(self, condition, description, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                self.fail(f'Collector exited ({self.process.returncode}): {self.diagnostics()}')
            try:
                result = condition()
                if result:
                    return result
            except (FileNotFoundError, json.JSONDecodeError, sqlite3.OperationalError):
                pass
            time.sleep(0.03)
        self.fail(f'Timed out waiting for {description}: {self.diagnostics()}')

    def state(self):
        return json.loads(self.runtime.read_text())

    def durable_usage(self, counter='total_up'):
        with sqlite3.connect(f'file:{self.database}?mode=ro', uri=True, timeout=0.1) as db:
            return db.execute('SELECT COALESCE(SUM(bytes),0) FROM usage_daily WHERE counter=?',
                              (counter,)).fetchone()[0]

    def durable_checkpoint(self, counter='total_up'):
        with sqlite3.connect(f'file:{self.database}?mode=ro', uri=True, timeout=0.1) as db:
            row = db.execute('SELECT bytes FROM checkpoints WHERE counter=?', (counter,)).fetchone()
            return int(row[0]) if row else None

    def start(self):
        self.assertIsNone(self.process)
        self.starts += 1
        log = self.root / f'watch-{self.starts}.log'
        self.logs.append(log)
        with log.open('w') as stderr:
            self.process = subprocess.Popen([
                BINARY, 'watch', '--lan', 'lan', '--wan', 'wan', '--leases', '/dev/null',
                '--neighbors', '/dev/null', '--database', str(self.database),
                '--runtime-file', str(self.runtime), '--timezone', 'UTC0',
                '--interval', '1', '--commit-interval', '3', '--lock', str(self.root / 'lock')],
                env=self.env, stdout=subprocess.DEVNULL, stderr=stderr)
        pid = self.process.pid
        self.wait_for(lambda: self.state().get('pid') == pid and
                      self.state().get('sampling_ok') and
                      self.state()['storage']['last_commit_ms'] > 0 and
                      not self.state()['storage']['pending'], 'initial durable baseline')

    def stop(self, abrupt=False):
        process = self.process
        if process is None:
            return
        self.process = None
        if process.poll() is None:
            process.kill() if abrupt else process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            self.fail('Collector did not stop promptly: ' + self.diagnostics())
        if not abrupt:
            self.assertEqual(process.returncode, 0, self.diagnostics())

    def cli(self, *args):
        result = subprocess.run([BINARY, *map(str, args)], env=self.env, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_crash_recovery_exactly_once_and_history_queries(self):
        self.start()
        self.assertEqual(self.durable_checkpoint(), 1000)
        self.assertEqual(self.durable_usage(), 0, 'Existing kernel counts are only a baseline')
        self.set_upload(1100)

        def pending_increment():
            state = self.state()
            return state if state.get('total_up', {}).get('bytes') == '1100' and \
                state['storage']['pending'] else None

        pending = self.wait_for(pending_increment, 'uncommitted traffic increment')
        self.assertGreater(pending['rates']['total_up']['bytes_per_second'], 0)
        self.assertEqual(self.durable_usage(), 0)
        self.assertEqual(self.durable_checkpoint(), 1000)
        self.stop(abrupt=True)
        stopped = self.cli('status', '--runtime-file', self.runtime)
        self.assertFalse(stopped['running'])
        self.assertTrue(stopped['stale'])

        self.start()
        self.assertEqual(self.durable_usage(), 100)
        self.assertEqual(self.durable_usage(DEVICE_UP), 100)
        self.assertEqual(self.durable_checkpoint(), 1100)
        today = datetime.now(timezone.utc).date()
        for resolution in ('30min', 'day', 'month'):
            history = self.cli('history', '--database', self.database, '--resolution', resolution,
                               '--from', (today - timedelta(days=1)).isoformat(),
                               '--to', (today + timedelta(days=1)).isoformat(),
                               '--counter', 'total_up', '--limit', '1000')
            self.assertFalse(history['pending_included'])
            self.assertFalse(history['truncated'])
            self.assertEqual(sum(int(row['bytes']) for row in history['rows']), 100)
            self.assertTrue(all(row['flags'] & 16 for row in history['rows']), history)
        self.stop()
        stopped = self.cli('status', '--runtime-file', self.runtime)
        self.assertFalse(stopped['running'])
        self.assertFalse(stopped['storage']['pending'])
        self.start()
        self.assertEqual(self.durable_usage(), 100, 'Restart must not replay committed traffic')
        self.stop()

    def test_failed_sampling_still_flushes_and_write_lock_recovers(self):
        self.start()
        self.set_upload(1050)
        self.wait_for(lambda: self.state().get('total_up', {}).get('bytes') == '1050' and
                      self.state()['storage']['pending'], 'first pending increment')
        self.fail_reads.touch()
        self.wait_for(lambda: not self.state()['sampling_ok'], 'sampling error status')
        self.wait_for(lambda: self.durable_usage() == 50, 'commit while sampling remains broken')
        state = self.state()
        self.assertIn('Read nftables state', state['last_error'])
        self.assertEqual(self.durable_checkpoint(), 1050)
        self.fail_reads.unlink()
        self.wait_for(lambda: self.state()['sampling_ok'], 'sampling recovery')

        writer = sqlite3.connect(self.database, timeout=1)
        try:
            writer.execute('BEGIN IMMEDIATE')
            self.set_upload(1100)
            self.wait_for(lambda: self.state().get('total_up', {}).get('bytes') == '1100' and
                          self.state()['storage']['pending'], 'second pending increment')
            self.wait_for(lambda: bool(self.state()['storage']['error']), 'visible database lock error')
            state = self.state()
            self.assertTrue(state['sampling_ok'], state)
            self.assertEqual(self.durable_usage(), 50)
            self.assertEqual(self.durable_checkpoint(), 1050)
            self.fail_reads.touch()
            self.wait_for(lambda: not self.state()['sampling_ok'] and
                          self.state()['storage']['error'], 'independent sampling and storage errors')
            self.assertIn('Read nftables state', self.state()['last_error'])
        finally:
            writer.rollback()
            writer.close()
            self.fail_reads.unlink(missing_ok=True)

        self.wait_for(lambda: self.durable_usage() == 100 and
                      not self.state()['storage']['error'] and self.state()['sampling_ok'],
                      'successful retry after database unlock')
        self.assertEqual(self.durable_checkpoint(), 1100)
        self.stop()
        self.assertEqual(self.durable_usage(), 100)
        self.start()
        self.assertEqual(self.durable_usage(), 100, 'Retry and restart must not duplicate writes')
        self.stop()


if __name__ == '__main__':
    unittest.main()
