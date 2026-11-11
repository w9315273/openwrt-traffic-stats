"""验证明细用量守恒, 并区分有效覆盖与缺失记录."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import test_python_accounting as accounting
from test_python_accounting import checkpoint, utc, WALL
from traffic_stats.store import Store, query
from traffic_stats.rpc import add_timeline


class DetailTests(unittest.TestCase):
    setUp = accounting.SamplingTests.setUp
    tearDown = accounting.SamplingTests.tearDown
    build = accounting.SamplingTests.build
    def test_half_hour_boundary_and_zero_coverage(self):
        start = utc(2026, 11, 5, 0, 29, 59)
        old = checkpoint(100, wall_ms=start, mono_ms=1000)
        self.counter.update(bytes=107, packets=13)
        batch = self.build(old, wall=start + 2000, mono=3000)['batch']
        self.assertEqual([row['bytes'] for row in batch['rows']], [3, 4])
        self.assertEqual([row['packets'] for row in batch['rows']], [1, 2])
        self.assertEqual([row['coverage_ms'] for row in batch['rows']], [1000, 1000])
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.local/tmp') as root:
            path = Path(root) / 'history.db'
            store = Store(path, '3mo')
            try:
                store.stage(batch)
                store.stage(batch)
                store.flush(start + 2000, True)
                result = query(path, '30min', '2026-11-05', '2026-11-05')
                self.assertEqual(sum(int(row['bytes']) for row in result['rows']), 7)
                self.assertEqual(sum(row['coverage_ms'] for row in result['rows']), 2000)
                self.assertEqual(sum(int(row['bytes']) for row in query(path, 'day', '2026-11-05', '2026-11-05')['rows']), 7)
                zero = self.build(batch['checkpoints']['total_up'], wall=start + 3000, mono=4000)['batch']
                self.assertEqual(zero['rows'][0]['bytes'], 0)
                self.assertEqual(zero['rows'][0]['coverage_ms'], 1000)
                store.stage(zero)
                store.flush(start + 93 * 86400000, True)
                self.assertEqual(query(path, '30min', '2026-11-05', '2026-11-05')['rows'], [])
                self.assertEqual(query(path, 'day', '2026-11-05', '2026-11-05')['rows'], [])
            finally:
                store.close()

    def test_uncertain_gap_has_no_fabricated_coverage(self):
        self.counter.update(bytes=150)
        batch = self.build(checkpoint(), wall=WALL + 601000, mono=601000)['batch']
        self.assertEqual(len(batch['rows']), 1)
        self.assertEqual(batch['rows'][0]['bytes'], 50)
        self.assertEqual(batch['rows'][0]['coverage_ms'], 0)
        self.assertTrue(batch['rows'][0]['flags'] & 1)

    def test_timeline_dst_days(self):
        for day, count in [('2026-03-08', 46), ('2026-11-01', 50), ('2026-11-05', 48)]:
            data = {'rows': []}
            add_timeline(data, day, 'EST5EDT,M3.2.0,M11.1.0')
            self.assertEqual(len(data['slots']), count)
            self.assertEqual(len({row['group_start'] for row in data['slots']}), count)
            self.assertEqual(sum(row['end'] - row['start'] for row in data['slots']), count * 1800)

    def test_schema_one_upgrade_preserves_every_old_table(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.local/tmp') as root:
            path = Path(root) / 'history.db'
            store = Store(path, '3mo')
            self.counter.update(bytes=120)
            batch = self.build(checkpoint())['batch']
            store.stage(batch); store.flush(WALL + 6000, True); store.close()
            with closing(sqlite3.connect(path)) as db:
                db.execute('DROP TABLE usage_30min')
                db.execute('CREATE TABLE usage_hourly(counter TEXT)')
                db.execute('PRAGMA user_version=1')
                tables = ['usage_daily', 'checkpoints', 'metadata', 'events', 'devices']
                before = {name: db.execute('SELECT * FROM ' + name).fetchall() for name in tables}
            self.assertEqual(query(path, '30min', '2026-11-05', '2026-11-05')['rows'], [])
            store = Store(path, '3mo')
            try:
                self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 3)
                for name in tables:
                    self.assertEqual(before[name], store.db.execute('SELECT * FROM ' + name).fetchall())
                self.assertEqual(query(path, '30min', '2026-11-05', '2026-11-05')['rows'], [])
            finally:
                store.close()
