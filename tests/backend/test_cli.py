#!/usr/bin/env python3
"""无需网络管理权限即可验证命令行行为."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BINARY = str(Path(sys.argv.pop(1)).resolve())
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ruleset = self.root / "ruleset.json"
        self.ruleset.write_text('{"nftables": []}')
        self.calls = self.root / "calls.jsonl"
        fake = self.root / "nft"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "with open(os.environ['TS_TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:] == ['-j', '-a', '-t', 'list', 'ruleset']:\n"
            "    print(pathlib.Path(os.environ['TS_TEST_RULESET']).read_text())\n"
            "else:\n"
            "    sys.exit(19)\n"
        )
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.root) + ':' + os.environ['PATH'],
                        TS_TEST_RULESET=str(self.ruleset), TS_TEST_CALLS=str(self.calls))

    def run_cli(self, command, *args, ok=True):
        result = subprocess.run([BINARY, command, *map(str, args)], env=self.env,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result

    def discovery_args(self):
        return ['--lan', 'lan', '--wan', 'wan', '--leases', FIXTURES / 'dhcp.leases',
                '--neighbors', FIXTURES / 'neighbors.txt']

    def test_offline_plan_and_no_commands(self):
        result = self.run_cli('plan', *self.discovery_args(), '--ruleset', self.ruleset)
        self.assertIn('2001:db8:1::22 : "d_020000000002_up"', result.stdout)
        self.assertIn('unknown_down return', result.stdout)
        self.assertFalse(self.calls.exists())

    def test_invalid_interface_rejected_before_nft(self):
        self.run_cli('sync', '--lan', 'lan";flush ruleset', '--wan', 'wan', ok=False)
        self.assertFalse(self.calls.exists())

    def test_foreign_table_protected(self):
        self.ruleset.write_text(json.dumps({'nftables': [
            {'table': {'family': 'inet', 'name': 'traffic_stats'}}]}))
        result = self.run_cli('sync', *self.discovery_args(), '--lock', self.root / 'lock', ok=False)
        self.assertIn('not managed', result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ['["-j", "-a", "-t", "list", "ruleset"]'])

    def test_missing_discovery_does_not_apply(self):
        self.run_cli('sync', '--lan', 'lan', '--wan', 'wan', '--leases', self.root / 'missing',
                     '--lock', self.root / 'lock', ok=False)
        self.assertEqual(self.calls.read_text().splitlines(), ['["-j", "-a", "-t", "list", "ruleset"]'])

    def test_failed_apply_reports_failure(self):
        self.run_cli('sync', *self.discovery_args(), '--lock', self.root / 'lock', ok=False)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_fixture_cannot_authorize_live_apply(self):
        self.run_cli('sync', *self.discovery_args(), '--ruleset', self.ruleset, ok=False)
        self.assertFalse(self.calls.exists())

    def test_malformed_and_overflowing_json_rejected(self):
        for data in ('{"nftables":[]} {}', '{broken', '{"nftables":[],"x":18446744073709551616}',
                     '{"nftables":[]}\x00garbage', 'null'):
            with self.subTest(data=data):
                self.ruleset.write_text(data)
                result = self.run_cli('status', '--ruleset', self.ruleset, ok=False)
                self.assertGreater(result.returncode, 0, 'Invalid JSON must not crash the reader')

    def test_empty_status_is_explicit(self):
        state = json.loads(self.run_cli('status', '--ruleset', self.ruleset).stdout)
        self.assertFalse(state['table_exists'])
        self.assertFalse(state['table_owned'])

    def test_output_paths_cannot_overwrite_database_or_lock(self):
        database = self.root / 'history.db'
        alias = self.root / 'alias.db'
        alias.symlink_to(database)
        lock = self.root / 'lock'
        for runtime in (database, Path(str(database) + '-wal'),
                        Path(str(database) + '-shm'), Path(str(database) + '-journal'), lock):
            with self.subTest(runtime=runtime):
                self.run_cli('watch', *self.discovery_args(), '--database', database,
                             '--runtime-file', runtime, '--lock', lock, ok=False)
        # 在 SQLite 打开文件前也要拒绝指向相同 inode 的路径.
        database.write_text('do not replace')
        self.run_cli('watch', *self.discovery_args(), '--database', database,
                     '--runtime-file', alias, '--lock', lock, ok=False)
        self.run_cli('watch', *self.discovery_args(), '--database', lock, '--lock', lock, ok=False)
        self.assertEqual(database.read_text(), 'do not replace')
        self.assertFalse(self.calls.exists())

    def test_history_missing_database_is_not_created(self):
        database = self.root / 'missing.db'
        self.run_cli('history', '--database', database, '--from', '2026-11-01',
                     '--to', '2026-11-30', ok=False)
        self.assertFalse(database.exists())

    def test_runtime_invalid_time_is_stale_without_integer_overflow(self):
        runtime = self.root / 'status.json'
        runtime.write_text(json.dumps({
            'pid': os.getpid(), 'running': True, 'sample_mono_ms': -(2**63),
            'interval': 5, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        }))
        state = json.loads(self.run_cli('status', '--runtime-file', runtime).stdout)
        self.assertTrue(state['stale'])

    def test_lock_excludes_other_writers(self):
        import fcntl
        with (self.root / 'lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_cli('sync', *self.discovery_args(), '--lock', self.root / 'lock', ok=False)
        self.assertFalse(self.calls.exists())

    def test_stop_cancels_read_without_starting_transaction(self):
        import time
        fake = self.root / 'nft'
        fake.write_text(fake.read_text().replace(
            "    print(pathlib.Path", "    import time; time.sleep(10)\n    print(pathlib.Path"))
        process = subprocess.Popen([BINARY, 'watch', *map(str, self.discovery_args()),
                                    '--lock', str(self.root / 'lock')], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not self.calls.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.calls.exists())
            process.terminate()
            process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == '__main__':
    unittest.main()
