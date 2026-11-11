#!/usr/bin/env python3
"""验证采样语义及旧版 SQLite 数据库的迁移兼容性."""

import calendar
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from traffic_stats.sample import (build_sample, CLOCK, GAP, RECOVERY, TIMEZONE, UNCERTAIN)
from traffic_stats.store import Store, StoreError, query, devices

BOOT = '11111111-1111-1111-1111-111111111111'
GENERATION = '22222222-2222-2222-2222-222222222222'
INT64_MAX = (1 << 63) - 1
UINT64_MAX = (1 << 64) - 1
WALL = 1793836800000


def utc(year, month, day, hour=0, minute=0, second=0):
    return calendar.timegm((year, month, day, hour, minute, second)) * 1000


def checkpoint(byte_count=100, tick=1, **kwargs):
    result = {'counter': 'total_up', 'handle': 9, 'bytes': byte_count, 'packets': byte_count // 10,
              'boot_id': BOOT, 'generation': GENERATION, 'timezone': 'UTC0',
              'wall_ms': WALL + tick * 1000, 'mono_ms': tick * 1000}
    result.update(kwargs)
    return result


def usage(byte_count=50, **kwargs):
    result = {'counter': 'total_up', 'bucket_start': WALL // 1000, 'day': '2026-11-05',
              'timezone': 'UTC0', 'bytes': byte_count, 'packets': byte_count // 10, 'flags': UNCERTAIN, 'coverage_ms': 0}
    result.update(kwargs)
    return result


class SamplingTests(unittest.TestCase):
    def setUp(self):
        self.timezone = os.environ.get('TZ')
        self.counter = {'name': 'total_up', 'handle': 9, 'bytes': 100, 'packets': 10}

    def tearDown(self):
        if self.timezone is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self.timezone
        time.tzset()

    def build(self, old=None, wall=WALL + 6000, mono=6000, zone='UTC0', resumed=False, max_gap=120000):
        os.environ['TZ'] = zone
        time.tzset()
        state = SimpleNamespace(exists=True, owned=True, generation=GENERATION,
                                counters={'total_up': self.counter})
        return build_sample({old['counter']: old} if old else {}, state, None,
                            BOOT, wall, mono, zone, max_gap, resumed)

    def assert_conserved(self, result, byte_count, packet_count):
        self.assertEqual(sum(row['bytes'] for row in result['batch']['rows']), byte_count)
        self.assertEqual(sum(row['packets'] for row in result['batch']['rows']), packet_count)

    def test_baseline_rates_and_repeated_sample(self):
        first = self.build(wall=WALL + 1000, mono=1000)
        self.assertEqual(first['batch']['rows'], [])
        self.assertEqual(first['batch']['events'][0]['kind'], 'initial_baseline')
        self.assertIsNone(first['rates']['total_up']['bytes_per_second'])
        old = first['batch']['checkpoints']['total_up']
        self.counter.update(bytes=600, packets=15)
        result = self.build(old)
        self.assert_conserved(result, 500, 5)
        self.assertEqual(result['rates']['total_up'], {'bytes_per_second': 100., 'packets_per_second': 1.})
        self.assertEqual(result['batch']['rows'][0]['flags'], 0)
        repeat = self.build(result['batch']['checkpoints']['total_up'])
        self.assertEqual(sum(row['bytes'] for row in repeat['batch']['rows']), 0)
        self.assertEqual(sum(row['coverage_ms'] for row in repeat['batch']['rows']), 0)
        self.assertEqual(repeat['batch']['events'], [])
        self.assertIsNone(repeat['rates']['total_up']['bytes_per_second'])

    def test_unsigned_precision_and_sql_delta_boundary(self):
        old = checkpoint(UINT64_MAX - 100, packets=UINT64_MAX - 10)
        self.counter.update(bytes=UINT64_MAX, packets=UINT64_MAX)
        result = self.build(old)
        self.assert_conserved(result, 100, 10)
        self.assertEqual(result['batch']['checkpoints']['total_up']['bytes'], UINT64_MAX)
        old['bytes'] = 0
        with self.assertRaises(OverflowError):
            self.build(old)

    def test_counter_lifecycles_do_not_invent_old_history(self):
        self.counter.update(bytes=1000, packets=100)
        for key, changed, expected in [('handle', 10, 'counter_replaced'),
                                        ('bytes', 1001, 'counter_reset'),
                                        ('generation', '3' + GENERATION[1:], 'generation_changed'),
                                        ('boot_id', '3' + BOOT[1:], 'boot_changed')]:
            with self.subTest(kind=expected):
                old = checkpoint(**{key: changed})
                result = self.build(old)
                self.assertEqual(result['batch']['rows'], [])
                self.assertEqual(result['batch']['events'][0]['kind'], expected)
                self.assertTrue(result['quality_flags'] & GAP)
                self.assertIsNone(result['rates']['total_up']['bytes_per_second'])

    def test_exact_midnight_split_and_fractional_timezone(self):
        midnight = utc(2026, 11, 6)
        self.counter.update(bytes=INT64_MAX, packets=101)
        result = self.build(checkpoint(0, packets=0, wall_ms=midnight - 1), midnight + 1, 1002)
        rows = result['batch']['rows']
        self.assertEqual([row['day'] for row in rows], ['2026-11-05', '2026-11-06'])
        self.assertEqual([row['packets'] for row in rows], [50, 51])
        self.assert_conserved(result, INT64_MAX, 101)
        midnight = utc(2026, 11, 5, 18, 30)
        self.counter.update(bytes=100, packets=10)
        result = self.build(checkpoint(0, packets=0, wall_ms=midnight - 5000, timezone='IST-5:30'),
                            midnight + 5000, 11000, 'IST-5:30')
        self.assertEqual([row['bucket_start'] for row in result['batch']['rows']],
                         [midnight // 1000 - 1800, midnight // 1000])
        self.assert_conserved(result, 100, 10)

    def test_dst_repeated_skipped_and_half_hour_transitions(self):
        for zone, transition in [('EST5EDT,M3.2.0/2,M11.1.0/2', utc(2026, 3, 8, 7)),
                                 ('EST5EDT,M3.2.0/2,M11.1.0/2', utc(2026, 11, 1, 6)),
                                 ('LHST-10:30LHDT-11,M10.1.0/2,M4.1.0/2', utc(2026, 4, 4, 15))]:
            with self.subTest(zone=zone, transition=transition):
                self.counter.update(bytes=100, packets=10)
                old = checkpoint(0, packets=0, wall_ms=transition - 5000, timezone=zone)
                result = self.build(old, transition + 5000, 11000, zone)
                self.assertEqual([row['bucket_start'] for row in result['batch']['rows']],
                                 [transition // 1000 - 1800, transition // 1000])
                self.assert_conserved(result, 100, 10)

    def test_invalid_clock_defers_traffic_and_recovers(self):
        old = checkpoint()
        self.counter['bytes'] = 600
        result = self.build(old, wall=1000)
        self.assertFalse(result['clock_valid'])
        self.assertEqual(result['deferred_counters'], 1)
        deferred = result['batch']['checkpoints']['total_up']
        self.assertEqual(deferred, dict(old, wall_ms=0))
        self.counter['bytes'] = 1100
        result = self.build(deferred, wall=WALL + 11000, mono=11000)
        self.assertEqual(result['batch']['rows'][0]['bytes'], 1000)
        self.assertTrue(result['batch']['rows'][0]['flags'] & CLOCK)
        self.assertIsNone(result['rates']['total_up']['bytes_per_second'])

    def test_clock_rollback_gap_resume_timezone_and_zero_traffic(self):
        old = checkpoint()
        self.counter['bytes'] = 600
        result = self.build(old, wall=WALL - 4000)
        self.assertTrue(result['batch']['rows'][0]['flags'] & CLOCK)
        self.assertEqual(result['rates']['total_up']['bytes_per_second'], 100.)
        result = self.build(old, WALL + 3601000, 3601000, resumed=True)
        self.assertEqual(result['batch']['rows'][0]['flags'], UNCERTAIN | GAP | RECOVERY)
        self.assertIsNone(result['rates']['total_up']['bytes_per_second'])
        result = self.build(old, zone='JST-9')
        self.assertTrue(result['batch']['rows'][0]['flags'] & TIMEZONE)
        self.counter['bytes'] = 100
        result = self.build(old, wall=WALL + 86401000)
        self.assertEqual(sum(row['bytes'] for row in result['batch']['rows']), 0)
        self.assertEqual(sum(row['coverage_ms'] for row in result['batch']['rows']), 0)
        self.assertTrue(result['quality_flags'] & CLOCK)
        self.assertEqual(len(result['batch']['events']), 1)

    def test_initial_invalid_clock_and_recovery(self):
        first = self.build(wall=0, mono=1000)
        self.assertFalse(first['clock_valid'])
        self.assertEqual(first['batch']['rows'], [])
        self.counter['bytes'] += 100
        result = self.build(first['batch']['checkpoints']['total_up'])
        self.assertEqual(result['batch']['rows'][0]['bytes'], 100)
        self.assertTrue(result['batch']['rows'][0]['flags'] & UNCERTAIN)


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = ROOT / '.local/tmp'
        temp.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='accounting-', dir=temp)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'traffic.db'
        self.store = Store(self.path, '3mo')
        self.addCleanup(lambda: self.store.close())

    def stage(self, cp, row=None, **extra):
        batch = {'rows': [row] if row else [], 'checkpoints': {cp['counter']: cp},
                 'sample_wall_ms': cp['wall_ms']}
        batch.update(extra)
        self.store.stage(batch)

    def flush(self, now=WALL, prune=True):
        self.store.flush(now, prune)

    def total(self, resolution='day'):
        return sum(int(row['bytes']) for row in query(self.path, resolution, '2026-11-01', '2026-11-30')['rows'])

    def durable(self):
        return self.store.load_checkpoints()['total_up']['bytes']

    def test_commit_replay_restart_and_stale_checkpoint(self):
        self.stage(checkpoint())
        self.assertTrue(self.store.dirty)
        self.flush()
        self.stage(checkpoint(150, 2), usage(50))
        self.stage(checkpoint(150, 2), usage(50))
        self.assertEqual(self.total(), 0)
        self.assertEqual(self.durable(), 100)
        self.flush()
        for resolution in ('day', 'month'):
            self.assertEqual(self.total(resolution), 50)
        self.stage(checkpoint(190, 3), usage(40))
        self.store.close()
        self.store = Store(self.path, '3mo')
        self.assertEqual(self.durable(), 150)
        self.stage(checkpoint(210, 4), usage(60))
        self.flush()
        self.assertEqual(self.total(), 110)
        with self.assertRaises(StoreError):
            self.stage(checkpoint(150, 2))
        self.assertEqual(self.durable(), 210)

    def test_busy_and_checkpoint_failure_preserve_atomic_pending_transaction(self):
        self.stage(checkpoint())
        self.flush()
        self.stage(checkpoint(150, 2), usage(50))
        other = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(other.close)
        other.execute('BEGIN IMMEDIATE')
        with self.assertRaises(sqlite3.OperationalError):
            self.flush()
        self.assertTrue(self.store.dirty)
        self.assertEqual(self.durable(), 100)
        self.stage(checkpoint(160, 3), usage(10))
        other.execute('ROLLBACK')
        self.flush()
        self.assertEqual(self.total(), 60)
        self.stage(checkpoint(170, 4), usage(10))
        other.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON checkpoints"
                      " BEGIN SELECT RAISE(ABORT,'checkpoint write failed'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.flush()
        self.assertEqual(self.total(), 60)
        self.assertEqual(self.durable(), 160)
        other.execute('DROP TRIGGER fail_checkpoint')
        self.flush()
        self.flush()
        self.assertEqual(self.total(), 70)

    def test_uint64_checkpoint_and_delta_overflow(self):
        self.stage(checkpoint(UINT64_MAX - 10, handle=UINT64_MAX))
        self.flush()
        self.stage(checkpoint(UINT64_MAX, 2, handle=UINT64_MAX), usage(10))
        self.flush()
        self.assertEqual(self.durable(), UINT64_MAX)
        self.assertEqual(self.total(), 10)
        with self.assertRaises(StoreError):
            self.stage(checkpoint(UINT64_MAX, 3), usage(UINT64_MAX))
        self.assertFalse(self.store.dirty)

    def test_overflow_on_disk_rolls_back_new_hour_and_checkpoint(self):
        self.stage(checkpoint(INT64_MAX), usage(INT64_MAX))
        self.flush()
        self.stage(checkpoint(INT64_MAX + 1, 2), usage(1, bucket_start=WALL // 1000 + 3600))
        with self.assertRaises(sqlite3.IntegrityError):
            self.flush()
        self.assertTrue(self.store.dirty)
        self.assertEqual(self.durable(), INT64_MAX)
        self.assertEqual(self.total(), INT64_MAX)
        self.assertEqual(self.total(), INT64_MAX)

    def test_clock_invalidation_and_pending_overflow(self):
        cp = checkpoint()
        self.stage(cp)
        self.flush()
        self.stage(dict(cp, wall_ms=0))
        self.flush(0, False)
        self.assertEqual(self.store.load_checkpoints()['total_up']['wall_ms'], 0)
        self.stage(checkpoint(INT64_MAX, 2), usage(INT64_MAX))
        with self.assertRaises(sqlite3.IntegrityError):
            self.stage(checkpoint(INT64_MAX + 1, 3), usage(1))
        self.flush(WALL + 91 * 86400000, False)
        self.assertEqual(self.durable(), INT64_MAX)
        self.assertEqual(self.total(), INT64_MAX)

    def test_readonly_missing_unrelated_schema_and_escaped_filename(self):
        missing = self.path.with_name('missing.db')
        with self.assertRaises(sqlite3.Error):
            query(missing, 'day', '2026-11-01', '2026-11-30')
        self.assertFalse(missing.exists())
        other_path = self.path.with_name('other.db')
        with closing(sqlite3.connect(other_path)) as other:
            other.execute('PRAGMA user_version=99')
            other.execute('CREATE TABLE other(x)')
        with self.assertRaises(StoreError):
            Store(other_path, '3mo')
        with self.assertRaises(StoreError):
            query(other_path, 'day', '2026-11-01', '2026-11-30')
        with closing(sqlite3.connect(other_path)) as other:
            self.assertEqual(other.execute('PRAGMA user_version').fetchone()[0], 99)
        weird_path = self.path.with_name('space ?#%.db')
        other_store = Store(weird_path, '3mo')
        other_store.close()
        self.assertEqual(query(weird_path, 'day', '2026-11-01', '2026-11-30')['rows'], [])

    def test_events_devices_retention_and_query_bounds(self):
        inventory = SimpleNamespace(devices={'020000000002': {'id': '020000000002',
                                    'mac': '02:00:00:00:00:02', 'hostname': 'laptop'}})
        event = {'wall_ms': WALL, 'kind': 'initial_baseline', 'counter': 'total_up', 'detail': 'test'}
        self.stage(checkpoint(), usage(100), events=[event], inventory=inventory)
        self.stage(checkpoint(), usage(100), events=[event], inventory=inventory)
        self.flush()
        self.assertEqual(len(query(self.path, 'day', '2026-11-01', '2026-11-30')['events']), 1)
        self.assertEqual(devices(self.path)[0][0]['hostname'], 'laptop')
        inventory.devices['020000000002']['hostname'] = ''
        self.stage(checkpoint(110, 2), usage(10), inventory=inventory)
        self.flush(WALL + 93 * 86400000)
        self.assertEqual(self.total(), 0)
        self.assertEqual(devices(self.path)[0], [])
        for start, counter in [('2026-02-30', None), ('2026-11-01', "total_up'; DELETE FROM usage_daily; --")]:
            with self.assertRaises(StoreError):
                query(self.path, 'day', start, '2026-11-30', counter)
        self.assertEqual(self.total(), 0)

    def test_month_flags_truncation_and_large_integer_strings(self):
        self.stage(checkpoint(9007199254740993), usage(9007199254740993))
        self.flush()
        self.stage(checkpoint(9007199254741000, 2), usage(7, day='2026-11-06', flags=CLOCK))
        self.flush()
        result = query(self.path, 'month', '2026-11-01', '2026-11-30')
        self.assertEqual(result['rows'][0]['bytes'], '9007199254741000')
        self.assertEqual(result['rows'][0]['flags'], UNCERTAIN | CLOCK)
        self.assertTrue(query(self.path, 'day', '2026-11-01', '2026-11-30', limit=1)['truncated'])
        self.assertEqual(query(self.path, 'month', '2026-11-06', '2026-11-06')['rows'][0]['bytes'], '7')

    def test_version_one_database_preserves_totals_and_resumes_checkpoints(self):
        self.stage(checkpoint(1100), usage(100))
        self.flush()
        # 固定旧版表结构即可验证迁移, 无需携带已停用的 C 参考程序.
        self.store.db.execute('DROP TABLE usage_30min')
        self.store.db.execute('CREATE TABLE usage_hourly ('
                              'counter TEXT NOT NULL, hour_start INTEGER NOT NULL, '
                              'day TEXT NOT NULL, timezone TEXT NOT NULL, '
                              "bytes INTEGER NOT NULL CHECK(typeof(bytes)='integer' AND bytes>=0), "
                              "packets INTEGER NOT NULL CHECK(typeof(packets)='integer' AND packets>=0), "
                              'flags INTEGER NOT NULL, PRIMARY KEY(counter,hour_start,day,timezone))')
        self.store.db.execute("INSERT INTO usage_hourly VALUES('total_up',?,'2026-11-05','UTC0',100,10,0)",
                              (WALL // 1000,))
        self.store.db.execute('PRAGMA user_version=1')
        self.store.close()
        self.store = Store(self.path, '3mo')
        cp = self.store.load_checkpoints()['total_up']
        self.assertEqual(cp['bytes'], 1100)
        self.assertEqual(cp['generation'], GENERATION)
        self.assertEqual(self.total(), 100)
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0], 3)
        self.stage(checkpoint(1107, 2), usage(7))
        self.flush()
        for resolution in ('day', 'month'):
            self.assertEqual(self.total(resolution), 107)
        self.assertEqual(self.durable(), 1107)


if __name__ == '__main__':
    unittest.main()
