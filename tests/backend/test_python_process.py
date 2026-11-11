"""验证子进程输入输出限额, 运行状态原子替换及路径边界."""
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'src'))
from traffic_stats import common, runtime, store


class ProcessTests(unittest.TestCase):
    def command(self, code):
        return [sys.executable, '-c', code]

    def test_concurrent_pipe_io_does_not_deadlock(self):
        payload = b'x' * 262144
        code = "import sys; sys.stdout.buffer.write(b'y'*262144); sys.stdout.flush(); sys.stdout.buffer.write(sys.stdin.buffer.read())"
        self.assertEqual(common.run(self.command(code), input_data=payload), b'y' * 262144 + payload)

    def test_arguments_are_literal_and_exit_failure_is_reported(self):
        value = '$(echo accidental); `id`'
        self.assertEqual(common.run(self.command('import sys; print(sys.argv[1],end="")') + [value]), value.encode())
        with self.assertRaises(OSError):
            common.run(self.command('raise SystemExit(7)'))

    def test_output_bound_also_applies_when_discarding_stdout(self):
        for capture in (False, True):
            with self.subTest(capture=capture), self.assertRaises(OSError):
                common.run(self.command('import sys; sys.stdout.write("x"*4096)'), capture=capture, max_output=1024)

    def test_timeout_and_cancellation_terminate_the_child(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.local/tmp') as temporary:
            pidfile = Path(temporary) / 'pid'
            code = 'import os,time; open(%r,"w").write(str(os.getpid())); time.sleep(10)' % str(pidfile)
            for cancel in (False, True):
                with self.subTest(cancel=cancel):
                    pidfile.unlink(missing_ok=True)
                    deadline = time.monotonic() + .2
                    with self.assertRaises(InterruptedError if cancel else TimeoutError):
                        common.run(self.command(code), timeout=2 if cancel else .2,
                                   cancel=(lambda: time.monotonic() >= deadline) if cancel else None)
                    pid = int(pidfile.read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_json_numbers_remain_exact_and_invalid_values_are_rejected(self):
        self.assertEqual(common.loads(b'{"n":18446744073709551615}')['n'], (1 << 64) - 1)
        for value in (b'null', b'NaN', b'Infinity', b'1e400', b'18446744073709551616', b'{}\0', b'{}{}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                common.loads(value)
        with self.assertRaises(ValueError):
            common.loads(b'{"long":0}', max_size=2)


class RuntimeTests(unittest.TestCase):
    def test_failed_publication_keeps_previous_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.local/tmp') as temporary:
            path = Path(temporary) / 'runtime.json'
            runtime.write_runtime(path, {'previous': True})
            with patch('traffic_stats.runtime.os.replace', side_effect=OSError('cannot replace')):
                with self.assertRaises(OSError):
                    runtime.write_runtime(path, {'previous': False})
            self.assertEqual(json.loads(path.read_text()), {'previous': True})
            self.assertEqual(list(Path(temporary).iterdir()), [path])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_runtime_from_another_boot_is_stale_even_with_live_pid(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.local/tmp') as temporary:
            path = Path(temporary) / 'runtime.json'
            runtime.write_runtime(path, {'pid': os.getpid(), 'running': True,
                'boot_id': '00000000-0000-0000-0000-000000000000',
                'sample_mono_ms': runtime.clock_ms(), 'interval': 5})
            data = runtime.read_runtime(path)
            self.assertFalse(data['running'])
            self.assertTrue(data['stale'])

    def test_readonly_history_handles_uri_delimiters_without_creating_missing_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.local/tmp') as temporary:
            path = Path(temporary) / '流量 %?#.db'
            database = store.Store(path, '3mo')
            database.close()
            self.assertEqual(store.query(path, 'day', '2026-01-01', '2026-01-01')['rows'], [])
            missing = Path(temporary) / 'missing.db'
            with self.assertRaises(Exception):
                store.query(missing, 'day', '2026-01-01', '2026-01-01')
            self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
