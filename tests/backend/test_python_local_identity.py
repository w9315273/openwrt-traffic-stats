"""验证本机身份与用量一起提交, 且重启后可恢复."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from traffic_stats.discovery import Inventory
from traffic_stats.store import Store, StoreError

ROOT = Path(__file__).resolve().parents[2]
LOCAL = {'id': '00d0b402120b', 'mac': '00:d0:b4:02:12:0b', 'hostname': 'OpenWrt', 'local': True}
CLIENT = {'id': '842f5729061f', 'mac': '84:2f:57:29:06:1f', 'hostname': 'MacBookPro'}


def batch(*devices):
    return {'inventory': Inventory({device['id']: dict(device) for device in devices}),
            'sample_wall_ms': 1793836800000}


class LocalIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / '.local/tmp')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'traffic.db'
        self.store = Store(self.path, '3mo')
        self.addCleanup(lambda: self.store.close())

    def test_identity_saved_with_device_without_changing_client(self):
        self.assertIsNone(self.store.load_local_device())
        self.store.stage(batch(CLIENT, LOCAL))
        self.assertIsNone(self.store.load_local_device())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM main.devices').fetchone()[0], 0)
        self.store.flush(1000, False)
        self.assertEqual(self.store.load_local_device(), LOCAL)
        client = self.store.db.execute('SELECT * FROM main.devices WHERE id=?', (CLIENT['id'],)).fetchone()
        self.assertEqual(client, (CLIENT['id'], CLIENT['mac'], CLIENT['hostname'], 1793836800000))
        self.store.flush(2000, False)
        self.assertEqual(self.store.last_commit_ms, 1000)
        self.store.close()
        self.store = Store(self.path, '3mo')
        self.assertEqual(self.store.load_local_device(), LOCAL)
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0], 3)
        self.store.stage(batch(CLIENT))
        self.store.flush(3000, False)
        self.assertEqual(self.store.load_local_device(), LOCAL)
        self.assertEqual(self.store.db.execute('SELECT * FROM main.devices WHERE id=?', (CLIENT['id'],)).fetchone(), client)

    def test_failed_stage_does_not_replace_staged_identity(self):
        self.store.stage(batch(LOCAL))
        other = {'id': '020000000001', 'mac': '02:00:00:00:00:01', 'hostname': 'Other', 'local': True}
        broken = batch(other)
        broken['events'] = [{'kind': 'broken', 'counter': '', 'detail': '', 'wall_ms': 'invalid'}]
        with self.assertRaises(StoreError):
            self.store.stage(broken)
        self.store.flush(1000, False)
        self.assertEqual(self.store.load_local_device(), LOCAL)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM main.devices WHERE id=?', (other['id'],)).fetchone()[0], 0)

    def test_failed_flush_retries_identity_and_devices_together(self):
        self.store.stage(batch(LOCAL, CLIENT))
        self.store.db.execute("CREATE TEMP TRIGGER reject_identity BEFORE INSERT ON main.metadata "
                              "WHEN NEW.key='local_device_id' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.store.flush(1000, False)
        self.assertIsNone(self.store.load_local_device())
        self.assertTrue(self.store.dirty)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM main.devices').fetchone()[0], 0)
        self.store.db.execute('DROP TRIGGER reject_identity')
        self.store.flush(1000, False)
        self.assertEqual(self.store.load_local_device(), LOCAL)

    def test_invalid_local_identity_is_not_persisted(self):
        for invalid in (dict(LOCAL, id='000000000000', mac='00:00:00:00:00:00'),
                        dict(LOCAL, id='010000000001', mac='01:00:00:00:00:01'),
                        dict(LOCAL, mac=CLIENT['mac'])):
            with self.subTest(device=invalid), self.assertRaises(StoreError):
                self.store.stage(batch(invalid))
        self.assertFalse(self.store.dirty)
        self.assertIsNone(self.store.pending_local_id)
        self.store.db.execute("INSERT INTO metadata VALUES('local_device_id', ?)", (1 << 48,))
        with self.assertRaises(StoreError):
            self.store.load_local_device()


if __name__ == '__main__':
    unittest.main()
