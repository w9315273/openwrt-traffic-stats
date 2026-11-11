"""验证数据库迁移保持用量, 自然月到期清理全部历史."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from traffic_stats import store, runtime, sample, config
from test_python_accounting import utc, checkpoint, usage, WALL


class HistoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.zone = os.environ.get('TZ')
        runtime.set_timezone('UTC0')
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.local/tmp')
        self.path = Path(self.temp.name) / 'history.db'

    def tearDown(self):
        self.temp.cleanup()
        if self.zone is None:
            os.environ.pop('TZ', None)
            time.tzset()
        else:
            runtime.set_timezone(self.zone)

    def legacy(self, rows):
        with closing(store.Store(self.path, '3mo')) as db:
            db.db.execute('ALTER TABLE usage_30min RENAME TO usage_10min')
            db.db.execute('CREATE TABLE usage_hourly(counter TEXT)')
            db.db.execute('PRAGMA user_version=2')
            db.db.executemany('INSERT INTO usage_10min VALUES(?,?,?,?,?,?,?,?)', rows)
            db.db.execute("INSERT INTO usage_daily VALUES('total_up','2026-11-05','UTC0',60,6,7)")
            db.db.execute("INSERT INTO metadata VALUES('last_commit_ms',?)", (WALL,))

    def test_migration_conserves_totals_flags_coverage_and_timezone(self):
        start = WALL // 1000
        rows = [('total_up', start + offset, '2026-11-05', 'UTC0', amount, 1, flag, 600000)
                for offset, amount, flag in [(0, 10, 1), (600, 20, 2), (1200, 30, 4), (1800, 0, 0)]]
        # 验证非整小时时区, 其本地半小时边界与 UTC 不重合.
        runtime.set_timezone('NPT-5:45')
        stamp = int(time.mktime((2026, 11, 5, 0, 10, 0, 0, 0, -1)))
        expected = sample.half_hour_at(stamp * 1000)[0]
        rows.append(('total_down', stamp, '2026-11-05', 'NPT-5:45', 9, 2, 0, 600000))
        runtime.set_timezone('UTC0')
        self.legacy(rows)
        with closing(store.Store(self.path, '3mo')) as db:
            self.assertEqual(os.environ['TZ'], 'UTC0')
            tables = {r[0] for r in db.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('usage_hourly', tables)
            self.assertNotIn('usage_10min', tables)
            result = store.query(self.path, '30min', '2026-11-05', '2026-11-05', 'total_up')['rows']
            self.assertEqual([r['bytes'] for r in result], ['60', '0'])
            self.assertEqual(result[0]['flags'], 7)
            self.assertEqual(result[0]['coverage_ms'], 1800000)
            self.assertEqual(store.query(self.path, 'day', '2026-11-05', '2026-11-05')['rows'][0]['bytes'], '60')
            self.assertEqual(db.last_commit_ms, WALL)
            row = store.query(self.path, '30min', '2026-11-05', '2026-11-05', 'total_down')['rows'][0]
            self.assertEqual(row['bucket_start'], expected)
        with closing(store.Store(self.path, '1y')) as db:
            self.assertEqual(db.db.execute('SELECT sum(bytes) FROM usage_30min').fetchone()[0], 69)

    def test_migration_failure_rolls_back_schema_and_data(self):
        self.legacy([('total_up', WALL // 1000 + i * 600, '2026-11-05', 'UTC0', store.INT64_MAX, 0, 0, 1) for i in range(2)])
        with self.assertRaises(sqlite3.Error):
            store.Store(self.path, '3mo')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM usage_10min').fetchone()[0], 2)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='usage_30min'").fetchone())

    def test_calendar_month_cycles_and_legacy_units(self):
        self.assertEqual(store.retention_cutoff(utc(2028, 3, 31), 1), '2028-03-01')
        self.assertEqual(store.retention_cutoff(utc(2028, 2, 29), 12), '2027-03-01')
        self.assertEqual(store.retention_cutoff(utc(2026, 1, 31), 3), '2025-11-01')
        self.assertEqual(store.retention_cutoff(utc(2026, 11, 1), 3), '2026-09-01')
        self.assertEqual(store.retention_cutoff(utc(2026, 11, 30), 3), '2026-09-01')
        self.assertEqual(store.retention_cutoff(utc(2026, 12, 1), 3), '2026-10-01')
        for text, months in [('0', 0), (0, 0), ('3', 3), (12, 12), ('120', 120), ('3mo', 3), ('1y', 12), ('2年', 24), ('6个月', 6), (' 2 MO ', 2)]:
            self.assertEqual(store.retention_months(text), months)
        for text in ['-1', '121', '1.5', True, '0mo', '121mo', '11y', '1.5y', '90d', '3m', '', None, ['1y']]:
            with self.assertRaises(ValueError):
                store.retention_months(text)
        self.assertEqual(config.retention({'retention_days': '90'}), '3')
        self.assertEqual(config.retention({'retention': '0'}), '0')
        self.assertEqual(config.retention({'retention': '1y', 'retention_days': '1'}), '12')

    def test_expiry_preserves_current_month_and_counter_checkpoint(self):
        with closing(store.Store(self.path, '1mo')) as db:
            cp = checkpoint(200)
            db.stage({'checkpoints': {'total_up': cp}, 'rows': [usage(10, day='2026-10-31'), usage(20, day='2026-11-01')]})
            db.flush(utc(2026, 11, 6), False)
            self.assertEqual(db.db.execute('SELECT sum(bytes) FROM usage_daily').fetchone()[0], 30)
            db.stage({'checkpoints': {'total_up': checkpoint(201, 2)}, 'rows': []})
            db.flush(utc(2026, 11, 6), True)
            for table in ('usage_daily', 'usage_30min'):
                self.assertEqual(db.db.execute('SELECT day,bytes FROM ' + table).fetchall(), [('2026-11-01', 20)])
            self.assertEqual(db.load_checkpoints()['total_up']['bytes'], 201)

    def test_three_month_window_advances_only_at_month_boundary(self):
        with closing(store.Store(self.path, '3')) as db:
            db.stage({'checkpoints': {'total_up': checkpoint(200)}, 'rows': [
                usage(10, day='2026-08-31'), usage(20, day='2026-09-01'),
                usage(30, day='2026-10-01'), usage(40, day='2026-11-01')]})
            db.flush(utc(2026, 11, 30), True)
            for table in ('usage_daily', 'usage_30min'):
                self.assertEqual(db.db.execute('SELECT day FROM ' + table + ' ORDER BY day').fetchall(),
                                 [('2026-09-01',), ('2026-10-01',), ('2026-11-01',)])
            db.stage({'checkpoints': {'total_up': checkpoint(201, 2)}})
            db.flush(utc(2026, 12, 1), True)
            for table in ('usage_daily', 'usage_30min'):
                self.assertEqual(db.db.execute('SELECT day FROM ' + table + ' ORDER BY day').fetchall(),
                                 [('2026-10-01',), ('2026-11-01',)])

    def test_unlimited_survives_restart_and_resumes_expiry_when_changed(self):
        from traffic_stats.collector import parse_options
        self.assertEqual(parse_options(['status', '--retention', '0']).retention, '0')
        with closing(store.Store(self.path, '0')) as db:
            db.db.execute("INSERT INTO devices VALUES('020000000002','02:00:00:00:00:02','old client',1)")
            db.stage({'checkpoints': {'total_up': checkpoint(200)},
                      'rows': [usage(25, day='2020-01-01')],
                      'events': [{'wall_ms': 1, 'kind': 'initial_baseline', 'counter': 'total_up', 'detail': 'old event'}]})
            db.flush(utc(2026, 11, 6), True)
        with closing(store.Store(self.path, '0')) as db:
            db.stage({'checkpoints': {'total_up': checkpoint(201, 2)}})
            db.flush(utc(2036, 1, 1), True)
            for table in ('usage_daily', 'usage_30min', 'events', 'devices'):
                self.assertEqual(db.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)
            self.assertEqual(db.load_checkpoints()['total_up']['bytes'], 201)
        with closing(store.Store(self.path, '3')) as db:
            db.stage({'checkpoints': {'total_up': checkpoint(202, 3)}})
            db.flush(utc(2036, 1, 1), True)
            for table in ('usage_daily', 'usage_30min', 'events', 'devices'):
                self.assertEqual(db.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
            self.assertEqual(db.load_checkpoints()['total_up']['bytes'], 202)
