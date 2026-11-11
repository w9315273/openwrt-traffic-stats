"""验证校时后保留策略恢复及离线设备历史行为."""
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import test_python_polling as fixtures


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PollingTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        temporary = tempfile.TemporaryDirectory(dir=fixtures.ROOT / '.local/tmp')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'traffic.db'
        self.writer = fixtures.store.Store(self.path, '1')
        self.addCleanup(self.writer.close)
        self.f.worker.store = self.writer
        self.f.initialize()
        self.f.upload(100)
        self.f.collect(1000)
        self.f.worker.flush()
        self.name = f'd_{fixtures.IDENTITY}_up'

    def device_rows(self):
        return self.writer.db.execute('SELECT * FROM usage_30min WHERE counter=?', (self.name,)).fetchall()

    def usage(self):
        return self.writer.db.execute('SELECT coalesce(sum(bytes),0) FROM usage_daily WHERE counter=?', (self.name,)).fetchone()[0]

    def test_offline_final_delta_zero_history_and_expiry(self):
        self.f.source = fixtures.discovery.Inventory()
        self.f.upload(50)
        self.f.collect(30000)
        self.f.worker.flush()
        self.assertEqual(self.usage(), 150)
        before = self.device_rows()
        self.assertEqual(self.f.worker.latest['devices'], [])
        self.assertEqual(fixtures.store.devices(self.path)[0][0]['id'], fixtures.IDENTITY)
        self.f.collect(3600000)
        self.f.worker.flush()
        self.assertEqual(self.device_rows(), before)
        self.assertTrue(self.f.worker.latest['reconciliation']['up']['bytes_match'])
        self.f.collect(100 * 86400000)
        self.f.worker.flush()
        self.assertEqual(self.device_rows(), [])
        self.assertEqual(fixtures.store.devices(self.path)[0], [])
        self.assertEqual(self.f.worker.latest['devices'], [])

    def test_reconnect_uses_recent_checkpoint_not_entire_absence(self):
        self.f.source = fixtures.discovery.Inventory()
        self.f.collect(30000)
        self.f.collect(3600000)
        self.f.worker.flush()
        offline_end = fixtures.WALL_MS + self.f.mono
        self.assertEqual(self.writer.load_checkpoints()[self.name]['wall_ms'], offline_end)
        self.f.source = fixtures.inventory()
        self.f.upload(90)
        self.f.collect(30000)
        self.f.worker.flush()
        self.assertEqual(self.usage(), 190)
        self.assertEqual(self.f.worker.latest['rates'][self.name]['bytes_per_second'], 3)
        self.assertEqual(self.f.worker.latest['devices'][0]['id'], fixtures.IDENTITY)
        rows = [r for r in self.device_rows() if r[1] * 1000 >= offline_end - 1800000]
        self.assertTrue(rows)

    def test_final_offline_delta_survives_stage_and_flush_retry(self):
        self.f.source = fixtures.discovery.Inventory()
        self.f.upload(50)
        with patch.object(self.writer, 'stage', side_effect=sqlite3.OperationalError('disk unavailable')):
            self.f.collect(30000, expected=1)
        self.f.collect(1000)
        with patch.object(self.writer, 'flush', side_effect=sqlite3.OperationalError('disk unavailable')):
            self.assertEqual(self.f.worker.flush(), 1)
        self.f.collect(1000)
        self.assertEqual(self.f.worker.flush(), 0)
        self.assertEqual(self.usage(), 150)
        self.assertTrue(self.f.worker.latest['reconciliation']['up']['bytes_match'])
        self.f.worker.previous = self.writer.load_checkpoints()
        self.f.worker.resumed = True
        before = self.device_rows()
        self.f.collect(1000)
        self.f.worker.flush()
        self.assertEqual(self.device_rows(), before)

    def test_discovered_idle_device_still_records_coverage(self):
        before = self.device_rows()
        self.f.collect(3600000)
        self.f.worker.flush()
        self.assertGreater(len(self.device_rows()), len(before))

    def test_missing_inventory_does_not_mean_device_is_offline(self):
        state = fixtures.nft.parse(self.f.document)
        measured = fixtures.collector.sample.build_sample(
            self.f.worker.previous, state, None, self.f.worker.boot_id,
            fixtures.WALL_MS + self.f.mono + 1000, self.f.mono + 1000,
            self.f.worker.timezone, 120000, False)
        rows = [row for row in measured['batch']['rows'] if row['counter'] == self.name]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['bytes'], 0)
        self.assertEqual(rows[0]['coverage_ms'], 1000)

    def test_clock_step_recovers_only_after_stable_period(self):
        offset = [20000]
        with patch.object(fixtures.collector, 'clock_ms', side_effect=lambda clock=None:
                          fixtures.WALL_MS + self.f.mono + offset[0] if clock == time.CLOCK_REALTIME else self.f.mono):
            self.f.collect(1000)
            self.assertFalse(self.f.worker.pruning_allowed)
            self.f.collect(1000)
            self.f.collect(29000)
            self.assertFalse(self.f.worker.pruning_allowed)
            self.f.collect(1000)
            self.assertTrue(self.f.worker.pruning_allowed)
            offset[0] -= 40000
            self.f.collect(1000)
            self.assertFalse(self.f.worker.pruning_allowed)
            self.f.collect(1000)
            self.f.collect(30000)
            self.assertTrue(self.f.worker.pruning_allowed)
            self.f.collect(100 * 86400000)
            self.f.worker.flush()
            days = [r[0] for r in self.writer.db.execute('SELECT DISTINCT day FROM usage_daily')]
            self.assertTrue(days)
            self.assertTrue(all(day >= '2027-02-01' for day in days))

    def test_invalid_clock_never_enables_pruning(self):
        with patch.object(fixtures.collector, 'clock_ms', side_effect=lambda clock=None:
                          self.f.mono if clock == time.CLOCK_REALTIME else self.f.mono):
            self.f.collect(1000)
            self.f.collect(60000)
            self.assertFalse(self.f.worker.pruning_allowed)
        self.f.collect(1000)
        self.assertFalse(self.f.worker.pruning_allowed)
        self.f.collect(1000)
        self.f.collect(30000)
        self.assertTrue(self.f.worker.pruning_allowed)


if __name__ == '__main__':
    unittest.main()
