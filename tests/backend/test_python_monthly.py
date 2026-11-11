"""验证月累计包含暂存增量, 并在写入及重启后保持一致."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import test_python_accounting as accounting
from test_python_accounting import checkpoint, utc
from traffic_stats.store import Store, StoreError


class MonthlyTests(unittest.TestCase):
    setUp = accounting.SamplingTests.setUp
    tearDown = accounting.SamplingTests.tearDown
    build = accounting.SamplingTests.build
    def test_month_boundary_flush_restart_and_large_totals(self):
        start = utc(2026, 10, 31, 23, 59, 59)
        self.counter.update(bytes=120, packets=14)
        batch = self.build(checkpoint(100, wall_ms=start, mono_ms=1000),
                           wall=start + 2000, mono=3000)['batch']
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.local/tmp') as root:
            path = Path(root) / 'traffic.db'
            db = Store(path, '3mo')
            for day in ('2026-11-02', '2026-11-03'):
                db.db.execute('INSERT INTO main.usage_daily VALUES(?,?,?,?,?,?)',
                              ('total_up', day, 'UTC0', (1 << 63) - 1, 0, 0))
            db.stage(batch)
            self.assertEqual(db.month_usage('2026-10-31')['counters']['total_up'], '10')
            expected = {'month': '2026-11', 'counters': {'total_up': '10'}}
            self.assertEqual(db.month_usage('2026-11-01'), expected)
            db.flush(start + 2000, True)
            self.assertEqual(db.month_usage('2026-11-01'), expected)
            db.close()
            db = Store(path, '3mo')
            self.assertEqual(db.month_usage('2026-11-01'), expected)
            db.stage(batch)  # 重放同一检查点时不得重复计入流量.
            self.assertEqual(db.month_usage('2026-11-01'), expected)
            self.assertEqual(db.month_usage('2026-12-01')['counters'], {})
            self.assertEqual(db.month_usage('2026-11-03')['counters']['total_up'],
                             str(10 + 2 * ((1 << 63) - 1)))
            db.close()

    def test_committed_cache_reuses_reads_while_pending_changes(self):
        self.counter.update(bytes=110)
        batch = self.build(checkpoint())['batch']
        day = batch['rows'][0]['day']
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.local/tmp') as root:
            with closing(Store(Path(root) / 'traffic.db', '3mo')) as db:
                reads = []
                db.db.set_trace_callback(lambda sql: reads.append(sql) if sql.startswith(
                    'SELECT counter, bytes FROM main.usage_daily') else None)
                self.assertEqual(db.month_usage(day)['counters'], {})
                db.stage(batch)
                expected = {'month': day[:7], 'counters': {'total_up': '10'}}
                for _ in range(3):
                    self.assertEqual(db.month_usage(day), expected)
                self.assertEqual(len(reads), 1)
                db.flush(utc(2026, 11, 5), False)
                self.assertEqual(db.month_usage(day), expected)
                self.assertEqual(len(reads), 2)
                db.flush(utc(2026, 11, 5), False)
                self.assertEqual(db.month_usage(day), expected)
                self.assertEqual(len(reads), 2)
                self.assertEqual(db.month_usage('2026-11-06')['counters'], expected['counters'])
                self.assertEqual(len(reads), 3)
                self.assertEqual(db.month_usage('2026-12-01')['counters'], {})
                self.assertEqual(len(reads), 4)

    def test_failed_stage_and_flush_preserve_cache_until_retry(self):
        self.counter.update(bytes=110)
        first = self.build(checkpoint())['batch']
        day = first['rows'][0]['day']
        self.counter.update(bytes=130)
        second = self.build(first['checkpoints']['total_up'],
                            wall=accounting.WALL + 7000, mono=7000)['batch']
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.local/tmp') as root:
            with closing(Store(Path(root) / 'traffic.db', '3mo')) as db:
                db.stage(first)
                db.flush(accounting.WALL + 6000, False)
                reads = []
                db.db.set_trace_callback(lambda sql: reads.append(sql) if sql.startswith(
                    'SELECT counter, bytes FROM main.usage_daily') else None)
                self.assertEqual(db.month_usage(day)['counters']['total_up'], '10')
                broken = dict(second, events=[{'kind': 'test', 'counter': '', 'detail': '', 'wall_ms': 'bad'}])
                with self.assertRaises(StoreError):
                    db.stage(broken)
                self.assertEqual(db.month_usage(day)['counters']['total_up'], '10')
                db.stage(second)
                self.assertEqual(db.month_usage(day)['counters']['total_up'], '30')
                db.db.execute("CREATE TEMP TRIGGER reject_commit BEFORE INSERT ON main.metadata "
                              "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
                with self.assertRaises(sqlite3.Error):
                    db.flush(accounting.WALL + 7000, False)
                self.assertEqual(db.month_usage(day)['counters']['total_up'], '30')
                self.assertEqual(len(reads), 1)
                db.db.execute('DROP TRIGGER reject_commit')
                db.flush(accounting.WALL + 7000, False)
                self.assertEqual(db.month_usage(day)['counters']['total_up'], '30')
                self.assertEqual(len(reads), 2)
                self.assertEqual(db.month_usage(day)['counters']['total_up'], '30')
                self.assertEqual(len(reads), 2)


if __name__ == '__main__':
    unittest.main()
