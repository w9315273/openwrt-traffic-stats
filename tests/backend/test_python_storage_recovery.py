"""验证存储目录准备不会阻断空库恢复."""
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from traffic_stats import names, storage, store


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / '.local/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / 'traffic.db'
        self.options = SimpleNamespace(storage_dir=str(self.root), database=str(self.target), runtime_file=None)
        legacy = patch.object(names, 'legacy', return_value=[])
        legacy.start()
        self.addCleanup(legacy.stop)

    def open_prepared(self):
        storage.prepare(self.options)
        return store.Store(self.options.database)

    def test_first_initialization_failure_recovers_without_removing_file(self):
        storage.prepare(self.options)
        with patch.object(store, '_create_tables', side_effect=sqlite3.OperationalError('database or disk is full')):
            with self.assertRaises(sqlite3.OperationalError):
                store.Store(self.options.database)
        self.assertTrue(self.target.exists())
        self.assertEqual(self.target.stat().st_size, 0)
        database = self.open_prepared()
        try:
            self.assertEqual(database.db.execute('PRAGMA user_version').fetchone()[0], store.SCHEMA_VERSION)
            self.assertEqual(database.db.execute('SELECT count(*) FROM usage_daily').fetchone()[0], 0)
        finally:
            database.close()

    def test_uninitialized_sqlite_header_also_recovers(self):
        database = sqlite3.connect(self.target)
        database.execute('VACUUM')
        database.close()
        self.assertGreater(self.target.stat().st_size, 0)
        database = self.open_prepared()
        database.close()
        verified = store._readonly(self.target)
        verified.close()

    def test_unrelated_database_is_rejected_without_changing_existing_data(self):
        database = sqlite3.connect(self.target)
        database.execute('CREATE TABLE unrelated (value TEXT)')
        database.execute("INSERT INTO unrelated VALUES ('keep me')")
        database.commit()
        database.close()
        previous = self.target.read_bytes()
        with self.assertRaises(store.StoreError):
            self.open_prepared()
        self.assertEqual(self.target.read_bytes(), previous)

    def test_incompatible_metadata_without_tables_is_not_treated_as_empty(self):
        database = sqlite3.connect(self.target)
        database.execute('PRAGMA application_id=123')
        database.close()
        previous = self.target.read_bytes()
        with self.assertRaises(store.StoreError):
            self.open_prepared()
        self.assertEqual(self.target.read_bytes(), previous)

    def test_corrupt_database_is_rejected_without_overwriting_it(self):
        self.target.write_bytes(b'This is not a SQLite database')
        previous = self.target.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            self.open_prepared()
        self.assertEqual(self.target.read_bytes(), previous)

    def test_valid_migration_preserves_history(self):
        source = self.root / 'old.db'
        original = store.Store(source)
        original.db.execute("INSERT INTO usage_daily VALUES ('total_up','2026-11-05','UTC0',12345,67,0)")
        original.close()
        self.options.database = str(source)
        database = self.open_prepared()
        try:
            self.assertEqual(database.db.execute("SELECT bytes FROM usage_daily WHERE counter='total_up'").fetchone()[0], 12345)
        finally:
            database.close()
        self.assertTrue(source.exists())

    def test_invalid_migration_source_is_not_copied(self):
        source = self.root / 'unrelated.db'
        database = sqlite3.connect(source)
        database.execute('CREATE TABLE unrelated (value TEXT)')
        database.close()
        previous = source.read_bytes()
        self.options.database = str(source)
        with self.assertRaises(store.StoreError):
            storage.prepare(self.options)
        self.assertFalse(self.target.exists())
        self.assertEqual(source.read_bytes(), previous)
        self.assertFalse(list(self.root.glob('.migrate-*')))

    def test_existing_target_takes_precedence_over_migration_source(self):
        source = self.root / 'old.db'
        original = store.Store(source)
        original.db.execute("INSERT INTO metadata VALUES ('test_marker', 42)")
        original.close()
        self.target.touch()
        self.options.database = str(source)
        database = self.open_prepared()
        try:
            self.assertIsNone(database.db.execute("SELECT value FROM metadata WHERE key='test_marker'").fetchone())
        finally:
            database.close()
        original = store._readonly(source)
        try:
            self.assertEqual(original.execute("SELECT value FROM metadata WHERE key='test_marker'").fetchone()[0], 42)
        finally:
            original.close()


if __name__ == '__main__':
    unittest.main()
