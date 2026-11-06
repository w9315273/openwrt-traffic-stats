"""Schema-compatible SQLite storage with atomic usage/checkpoint persistence."""

from pathlib import Path
import re
import sqlite3
import os
import time
from datetime import date

from . import runtime, sample

SCHEMA_VERSION = 3
APPLICATION_ID = 1414747219
INT64_MAX = (1 << 63) - 1
UINT64_MAX = (1 << 64) - 1
CHECKPOINT_FIELDS = ('counter', 'handle', 'bytes', 'packets', 'boot_id',
                     'generation', 'timezone', 'wall_ms', 'mono_ms')
TABLES = {
    'usage_30min': '(counter TEXT NOT NULL, bucket_start INTEGER NOT NULL, day TEXT NOT NULL,'
                   ' timezone TEXT NOT NULL, bytes INTEGER NOT NULL CHECK(typeof(bytes)=\'integer\' AND bytes>=0),'
                   ' packets INTEGER NOT NULL CHECK(typeof(packets)=\'integer\' AND packets>=0),'
                   ' flags INTEGER NOT NULL, coverage_ms INTEGER NOT NULL CHECK(coverage_ms>=0),'
                   ' PRIMARY KEY(counter,bucket_start,day,timezone))',
    'checkpoints': '(counter TEXT PRIMARY KEY, handle TEXT NOT NULL, bytes TEXT NOT NULL,'
                   ' packets TEXT NOT NULL, boot_id TEXT NOT NULL, generation TEXT NOT NULL,'
                   ' timezone TEXT NOT NULL, wall_ms INTEGER NOT NULL, mono_ms INTEGER NOT NULL)',
    'usage_daily': '(counter TEXT NOT NULL, day TEXT NOT NULL, timezone TEXT NOT NULL,'
                   " bytes INTEGER NOT NULL CHECK(typeof(bytes)='integer' AND bytes>=0),"
                   " packets INTEGER NOT NULL CHECK(typeof(packets)='integer' AND packets>=0),"
                   ' flags INTEGER NOT NULL, PRIMARY KEY(counter,day,timezone))',
    'devices': '(id TEXT PRIMARY KEY, mac TEXT NOT NULL, hostname TEXT NOT NULL,'
               ' last_seen_ms INTEGER NOT NULL)',
    'events': '(id INTEGER PRIMARY KEY, wall_ms INTEGER NOT NULL, kind TEXT NOT NULL,'
              ' counter TEXT NOT NULL, detail TEXT NOT NULL, UNIQUE(wall_ms,kind,counter,detail))',
}
ADD_USAGE = (' DO UPDATE SET bytes={table}.bytes+excluded.bytes,'
             ' packets={table}.packets+excluded.packets, flags={table}.flags|excluded.flags')
UPDATE_DEVICE = (" DO UPDATE SET mac=excluded.mac, hostname=CASE WHEN excluded.hostname=''"
                 ' THEN devices.hostname ELSE excluded.hostname END, last_seen_ms=excluded.last_seen_ms')


class StoreError(ValueError):
    pass


def valid_counter(value):
    return isinstance(value, str) and re.fullmatch(
        r'(?:total|unknown|d_[0-9a-f]{12})_(?:up|down)', value) is not None


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        return False
    year, month, day = map(int, value.split('-'))
    if not year or not 1 <= month <= 12:
        return False
    days = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return 1 <= day <= days[month - 1] + (month == 2 and leap)


def retention_months(value):
    if str(value).strip() == '0':
        return 0
    match = re.fullmatch(r'([1-9][0-9]*)\s*(mo|y|个月|月|年)?', str(value).strip(), re.I)
    if not match:
        raise StoreError('History retention must be a whole number of months')
    months = int(match[1]) * (12 if (match[2] or '').lower() in ('y', '年') else 1)
    if not 1 <= months <= 120:
        raise StoreError('History retention must be between 1 month and 10 years')
    return months


def retention_cutoff(now_ms, months):
    local = time.localtime(now_ms // 1000)
    index = local.tm_year * 12 + local.tm_mon - months
    year, month = divmod(index, 12)
    return date(year, month + 1, 1).isoformat()


def _text(value, capacity, empty=False):
    if (not isinstance(value, str) or not (empty or value) or '\0' in value or
            len(value.encode('utf-8')) >= capacity):
        raise StoreError('Invalid text field')
    return value


def _integer(value, minimum=0, maximum=INT64_MAX):
    if type(value) is not int or not minimum <= value <= maximum:
        raise StoreError('Invalid or overflowing integer field')
    return value


def _local_identity(device):
    identity, mac = device.get('id'), device.get('mac')
    if (not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{12}', identity) or
            mac != ':'.join(identity[index:index + 2] for index in range(0, 12, 2))):
        raise StoreError('Invalid local device identity')
    value = int(identity, 16)
    if not value or value & (1 << 40):
        raise StoreError('Local device identity must be a unicast MAC')
    return value


def _schema_valid(db):
    return (db.execute('PRAGMA user_version').fetchone()[0] in (1, 2, SCHEMA_VERSION) and
            db.execute('PRAGMA application_id').fetchone()[0] == APPLICATION_ID)


def _read_checkpoint(row):
    checkpoint = dict(zip(CHECKPOINT_FIELDS, row))
    for key, capacity in (('counter', 32), ('boot_id', 37), ('generation', 37), ('timezone', 128)):
        _text(checkpoint[key], capacity, True)
    for key in ('handle', 'bytes', 'packets'):
        value = checkpoint[key]
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,20}', value):
            raise StoreError('Invalid persisted checkpoint')
        checkpoint[key] = _integer(int(value), maximum=UINT64_MAX)
    for key in ('wall_ms', 'mono_ms'):
        _integer(checkpoint[key], -(1 << 63))
    return checkpoint


def _create_tables(db, schema):
    for name, definition in TABLES.items():
        db.execute(f'CREATE TABLE {schema}.{name} {definition}')


class Store:
    def __init__(self, path, retention='3'):
        if not path:
            raise StoreError('Invalid database path')
        self.retention_months = retention_months(retention)
        self.error = ''
        self.dirty = False
        self.last_commit_ms = 0
        self.pending_local_id = None
        self.month_cache = None
        self.db = sqlite3.connect(str(path), timeout=1, isolation_level=None)
        try:
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            application = self.db.execute('PRAGMA application_id').fetchone()[0]
            tables = self.db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
            if not version and not application and not tables:
                self.db.execute('BEGIN IMMEDIATE')
                _create_tables(self.db, 'main')
                for sql in (
                    'CREATE TABLE metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL)',
                    'CREATE INDEX daily_day ON usage_daily(day)',
                    'CREATE INDEX event_time ON events(wall_ms)',
                    'CREATE INDEX detail_day ON usage_30min(day)',
                    'CREATE INDEX detail_start ON usage_30min(bucket_start)',
                    f'PRAGMA application_id={APPLICATION_ID}', f'PRAGMA user_version={SCHEMA_VERSION}',
                ):
                    self.db.execute(sql)
                self.db.execute('COMMIT')
            elif not _schema_valid(self.db):
                raise StoreError('Unsupported or unrelated database schema')
            elif version < SCHEMA_VERSION:
                self._migrate(version)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute("ATTACH DATABASE ':memory:' AS pending")
            _create_tables(self.db, 'pending')
            committed = self.db.execute("SELECT value FROM metadata WHERE key='last_commit_ms'").fetchone()
            self.last_commit_ms = committed[0] if committed else 0
        except Exception:
            self.db.close()
            raise

    def _migrate(self, version):
        # Preserve daily totals and checkpoints; only consolidate the detail buckets.
        previous_zone = os.environ.get('TZ')
        def bucket_start(stamp, zone):
            if os.environ.get('TZ') != zone:
                runtime.set_timezone(zone)
            return sample.half_hour_at(stamp * 1000)[0]
        self.db.create_function('half_hour', 2, bucket_start)
        self.db.create_aggregate('flags_or', 1, _FlagsOr)
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('CREATE TABLE usage_30min ' + TABLES['usage_30min'])
            if version == 2:
                self.db.execute('INSERT INTO usage_30min '
                    'SELECT counter,half_hour(bucket_start,timezone),day,timezone,'
                    'sum(bytes),sum(packets),flags_or(flags),sum(coverage_ms) '
                    'FROM usage_10min GROUP BY counter,half_hour(bucket_start,timezone),day,timezone')
                self.db.execute('DROP TABLE usage_10min')
            self.db.execute('DROP TABLE usage_hourly')
            self.db.execute('CREATE INDEX detail_day ON usage_30min(day)')
            self.db.execute('CREATE INDEX detail_start ON usage_30min(bucket_start)')
            self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            self.db.execute('COMMIT')
        except Exception:
            self.db.rollback()
            raise
        finally:
            if previous_zone is None:
                os.environ.pop('TZ', None)
                if hasattr(time, 'tzset'):
                    time.tzset()
            else:
                runtime.set_timezone(previous_zone)
            self.db.create_function('half_hour', 2, None)

    def close(self):
        self.db.close()

    def load_local_device(self):
        row = self.db.execute("SELECT value FROM main.metadata WHERE key='local_device_id'").fetchone()
        if row is None:
            return None
        value = _integer(row[0], 1, (1 << 48) - 1)
        identity = f'{value:012x}'
        row = self.db.execute('SELECT mac, hostname FROM main.devices WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise StoreError('Persisted local device metadata is missing')
        device = {'id': identity, 'mac': row[0], 'hostname': _text(row[1], 256, True), 'local': True}
        _local_identity(device)
        return device

    def month_usage(self, day):
        if not valid_date(day):
            raise StoreError('Invalid monthly usage date')
        bounds = (day[:7] + '-01', day)
        # Only this writer changes main; staged deltas stay separate until a successful flush.
        if self.month_cache is None or self.month_cache[0] != day:
            committed = {}
            for counter, value in self.db.execute(
                    'SELECT counter, bytes FROM main.usage_daily WHERE day>=? AND day<=?', bounds):
                committed[counter] = committed.get(counter, 0) + value
            self.month_cache = (day, committed)
        counters = dict(self.month_cache[1])
        for counter, value in self.db.execute(
                'SELECT counter, bytes FROM pending.usage_daily WHERE day>=? AND day<=?', bounds):
            counters[counter] = counters.get(counter, 0) + value
        return {'month': day[:7], 'counters': {key: str(value) for key, value in counters.items()}}

    def load_checkpoints(self):
        try:
            result = {}
            for row in self.db.execute('SELECT ' + ','.join(CHECKPOINT_FIELDS) +
                                       ' FROM main.checkpoints ORDER BY counter'):
                checkpoint = _read_checkpoint(row)
                result[checkpoint['counter']] = checkpoint
            self.error = ''
            return result
        except (sqlite3.Error, ValueError, OverflowError) as exc:
            self.error = str(exc)
            raise

    def _previous_checkpoint(self, counter):
        columns = ','.join(CHECKPOINT_FIELDS)
        row = self.db.execute(f'SELECT {columns} FROM pending.checkpoints WHERE counter=?'
                              f' UNION ALL SELECT {columns} FROM main.checkpoints WHERE counter=? LIMIT 1',
                              (counter, counter)).fetchone()
        return _read_checkpoint(row) if row else None

    def stage(self, batch):
        duplicates = set()
        changed = False
        local_id = self.pending_local_id
        try:
            checkpoints = batch.get('checkpoints', {})
            self.db.execute('BEGIN')
            for name, checkpoint in checkpoints.items():
                if name != checkpoint.get('counter') or not valid_counter(name):
                    raise StoreError('Invalid checkpoint counter')
                for key, capacity in (('counter', 32), ('boot_id', 37), ('generation', 37), ('timezone', 128)):
                    _text(checkpoint.get(key), capacity)
                for key in ('handle', 'bytes', 'packets'):
                    _integer(checkpoint.get(key), maximum=UINT64_MAX)
                _integer(checkpoint.get('wall_ms'), -(1 << 63))
                _integer(checkpoint.get('mono_ms'))
                previous = self._previous_checkpoint(name)
                normalized = {key: checkpoint[key] for key in CHECKPOINT_FIELDS}
                if previous == normalized:
                    duplicates.add(name)
                    continue
                clock_invalidated = previous is not None and normalized == dict(previous, wall_ms=0)
                if (previous and checkpoint['boot_id'] == previous['boot_id'] and
                        checkpoint['mono_ms'] <= previous['mono_ms'] and not clock_invalidated):
                    raise StoreError('Sample checkpoint is older than the current baseline')
                values = [str(checkpoint[key]) if key in ('handle', 'bytes', 'packets') else checkpoint[key]
                          for key in CHECKPOINT_FIELDS]
                self.db.execute('INSERT OR REPLACE INTO pending.checkpoints VALUES(?,?,?,?,?,?,?,?,?)', values)
                changed = True
            for row in batch.get('rows', []):
                if row.get('counter') not in checkpoints or not valid_date(row.get('day')):
                    raise StoreError('Invalid detailed history row')
                _text(row.get('timezone'), 128)
                _integer(row.get('bucket_start'), -(1 << 63))
                for key in ('bytes', 'packets', 'flags', 'coverage_ms'):
                    _integer(row.get(key))
                if row['counter'] in duplicates:
                    continue
                self.db.execute('INSERT INTO pending.usage_30min VALUES(?,?,?,?,?,?,?,?)'
                                ' ON CONFLICT(counter,bucket_start,day,timezone)' +
                                ADD_USAGE.format(table='usage_30min') +
                                ', coverage_ms=usage_30min.coverage_ms+excluded.coverage_ms',
                                tuple(row[key] for key in ('counter', 'bucket_start', 'day', 'timezone',
                                                           'bytes', 'packets', 'flags', 'coverage_ms')))
                self.db.execute('INSERT INTO pending.usage_daily VALUES(?,?,?,?,?,?)'
                                ' ON CONFLICT(counter,day,timezone)' + ADD_USAGE.format(table='usage_daily'),
                                tuple(row[key] for key in ('counter', 'day', 'timezone', 'bytes', 'packets', 'flags')))
            inventory = batch.get('inventory')
            if inventory is not None:
                local_devices = [device for device in inventory.devices.values() if device.get('local') is True]
                if len(local_devices) > 1:
                    raise StoreError('Multiple local devices in inventory')
                if local_devices:
                    local_id = _local_identity(local_devices[0])
                for device in inventory.devices.values():
                    self.db.execute('INSERT INTO pending.devices VALUES(?,?,?,?) ON CONFLICT(id)' + UPDATE_DEVICE,
                                    (device['id'], device['mac'], device['hostname'], batch.get('sample_wall_ms', 0)))
                    changed = True
            for event in batch.get('events', []):
                for key, capacity, empty in (('kind', 32, False), ('counter', 32, True), ('detail', 192, True)):
                    _text(event.get(key), capacity, empty)
                _integer(event.get('wall_ms'), -(1 << 63))
                self.db.execute('INSERT OR IGNORE INTO pending.events(wall_ms,kind,counter,detail) VALUES(?,?,?,?)',
                                tuple(event[key] for key in ('wall_ms', 'kind', 'counter', 'detail')))
                changed = True
            self.db.execute('COMMIT')
            self.pending_local_id = local_id
            self.dirty |= changed
            self.error = ''
        except (sqlite3.Error, ValueError, OverflowError, TypeError, KeyError) as exc:
            self.db.rollback()
            self.error = str(exc)
            raise

    def flush(self, now_ms, prune_allowed):
        if not self.dirty:
            return
        try:
            _integer(now_ms, -(1 << 63))
            self.db.execute('BEGIN IMMEDIATE')
            for table, key in (('usage_daily', 'counter,day,timezone'),
                               ('usage_30min', 'counter,bucket_start,day,timezone')):
                self.db.execute(f'INSERT INTO main.{table} SELECT * FROM pending.{table} WHERE 1'
                                f' ON CONFLICT({key})' + ADD_USAGE.format(table=table) +
                                (', coverage_ms=usage_30min.coverage_ms+excluded.coverage_ms' if table == 'usage_30min' else ''))
            self.db.execute('INSERT OR REPLACE INTO main.checkpoints SELECT * FROM pending.checkpoints')
            self.db.execute('INSERT INTO main.devices SELECT * FROM pending.devices WHERE 1'
                            ' ON CONFLICT(id)' + UPDATE_DEVICE)
            self.db.execute('INSERT OR IGNORE INTO main.events(wall_ms,kind,counter,detail)'
                            ' SELECT wall_ms,kind,counter,detail FROM pending.events ORDER BY id')
            if prune_allowed and self.retention_months:
                # Keep the current month and the preceding N-1 local calendar months.
                cutoff = retention_cutoff(now_ms, self.retention_months)
                for table in ('usage_30min', 'usage_daily'):
                    self.db.execute(f'DELETE FROM main.{table} WHERE day < ?', (cutoff,))
                cutoff_ms = int(time.mktime((*map(int, cutoff.split('-')), 0, 0, 0, 0, 0, -1))) * 1000
                self.db.execute('DELETE FROM main.events WHERE wall_ms < ?', (cutoff_ms,))
                self.db.execute("DELETE FROM main.devices WHERE last_seen_ms < ? AND id != "
                    "printf('%012x',coalesce((SELECT value FROM metadata WHERE key='local_device_id'),0)) "
                    "AND id != ?", (cutoff_ms, f'{self.pending_local_id or 0:012x}'))
            self.db.execute("INSERT OR REPLACE INTO main.metadata VALUES('last_commit_ms',?)", (now_ms,))
            if self.pending_local_id is not None:
                self.db.execute("INSERT OR REPLACE INTO main.metadata VALUES('local_device_id',?)",
                                (self.pending_local_id,))
            for table in TABLES:
                self.db.execute(f'DELETE FROM pending.{table}')
            self.db.execute('COMMIT')
            self.pending_local_id = None
            self.month_cache = None
            self.dirty = False
            self.last_commit_ms = now_ms
            self.error = ''
        except (sqlite3.Error, ValueError, OverflowError) as exc:
            self.db.rollback()
            self.error = str(exc)
            raise


def _readonly(path):
    # Escape URI delimiters without pulling urllib into OpenWrt's small runtime.
    filename = str(Path(path).absolute()).replace('%', '%25').replace('?', '%3F').replace('#', '%23')
    db = sqlite3.connect('file:' + filename + '?mode=ro', uri=True,
                         timeout=1, isolation_level=None)
    try:
        if not _schema_valid(db):
            raise StoreError('Unsupported or unrelated database schema')
        return db
    except Exception:
        db.close()
        raise


class _FlagsOr:
    def __init__(self):
        self.value = 0

    def step(self, value):
        self.value |= value

    def finalize(self):
        return self.value


def query(path, resolution, from_date, to_date, counter=None, limit=1000):
    if (not valid_date(from_date) or not valid_date(to_date) or from_date > to_date or
            type(limit) is not int or not 1 <= limit <= 10000 or
            (counter is not None and not valid_counter(counter)) or
            resolution not in ('30min', 'day', 'month')):
        raise StoreError('Invalid history query')
    db = _readonly(path)
    try:
        db.create_aggregate('flags_or', 1, _FlagsOr)
        db.execute('BEGIN')
        if resolution == '30min':
            if db.execute('PRAGMA user_version').fetchone()[0] < SCHEMA_VERSION:
                values = []
            else:
                values = db.execute('SELECT counter,day,timezone,bytes,packets,flags,bucket_start,coverage_ms'
                    ' FROM usage_30min WHERE day>=? AND day<=? AND (? IS NULL OR counter=?)'
                    ' ORDER BY bucket_start,counter,timezone LIMIT ?',
                    (from_date, to_date, counter, counter, limit + 1)).fetchall()
        elif resolution == 'month':
            sql = ('SELECT counter,substr(day,1,7),timezone,sum(bytes),sum(packets),flags_or(flags),NULL'
                   ' FROM usage_daily WHERE day>=? AND day<=? AND (? IS NULL OR counter=?)'
                   ' GROUP BY counter,substr(day,1,7),timezone ORDER BY substr(day,1,7),counter,timezone LIMIT ?')
        else:
            sql = ('SELECT counter,day,timezone,bytes,packets,flags,NULL'
                   ' FROM usage_daily WHERE day>=? AND day<=? AND (? IS NULL OR counter=?)'
                   ' ORDER BY day,counter,timezone LIMIT ?')
        if resolution != '30min':
            values = [(*row, None) for row in db.execute(sql, (from_date, to_date, counter, counter, limit + 1)).fetchall()]
        rows = []
        for name, period, zone, byte_count, packet_count, flags, bucket_start, coverage in values[:limit]:
            _integer(byte_count)
            _integer(packet_count)
            row = {'counter': name, 'month' if resolution == 'month' else 'day': period,
                   'timezone': zone, 'bytes': str(byte_count), 'packets': str(packet_count), 'flags': flags}
            if resolution == '30min':
                row['bucket_start'] = bucket_start
                row['coverage_ms'] = _integer(coverage)
            rows.append(row)
        committed = db.execute("SELECT value FROM metadata WHERE key='last_commit_ms'").fetchone()
        events = [dict(zip(('wall_ms', 'kind', 'counter', 'detail'), row))
                  for row in db.execute('SELECT wall_ms,kind,counter,detail FROM events ORDER BY id DESC LIMIT 100')]
        db.execute('COMMIT')
        return {'rows': rows, 'resolution': resolution, 'from': from_date, 'to': to_date,
                'pending_included': False, 'truncated': len(values) > limit,
                'last_commit_ms': committed[0] if committed else 0, 'events': events, 'events_scope': 'latest'}
    finally:
        db.close()


def devices(path, limit=1000):
    if not path or type(limit) is not int or not 1 <= limit <= 1000:
        raise StoreError('Invalid device query')
    db = _readonly(path)
    try:
        values = db.execute('SELECT id,mac,hostname,last_seen_ms FROM devices'
                            ' ORDER BY last_seen_ms DESC,id LIMIT ?', (limit + 1,)).fetchall()
        result = []
        for identity, mac, hostname, last_seen in values[:limit]:
            result.append({'id': _text(identity, 13, True), 'mac': _text(mac, 18, True),
                           'hostname': _text(hostname, 256, True), 'last_seen_ms': last_seen})
        return result, len(values) > limit
    finally:
        db.close()
