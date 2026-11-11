"""Prepare a storage directory while the collector holds its exclusive writer lock."""
import os
from pathlib import Path
import sqlite3
import tempfile

from . import names, store
from .runtime import read_runtime


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def directory(value):
    if (not isinstance(value, str) or not value.startswith('/') or
            any(ord(char) < 32 or ord(char) == 127 for char in value) or
            any(part in ('.', '..') for part in value.split('/'))):
        raise ValueError('Storage directory must be an absolute directory path')
    return value.rstrip('/') or '/'


def prepare(options):
    root = Path(directory(options.storage_dir))
    try:
        root.mkdir(mode=0o700, exist_ok=True)
    except FileNotFoundError as error:
        raise ValueError('Parent storage directory must exist; mount external storage first') from error
    target = root / 'traffic.db'
    source = options.database
    if options.runtime_file:
        try:
            previous = read_runtime(options.runtime_file)
            source = previous.get('storage', {}).get('database') or source
        except (OSError, ValueError):
            pass
    name_source = Path(source).parent / 'devices.json' if source else None
    if not source or not Path(source).is_file():
        source = str(root / 'db' / 'traffic.db')
    if not target.exists() and source and Path(source).is_file() and Path(source).resolve() != target.resolve():
        fd, temporary = tempfile.mkstemp(prefix='.migrate-', suffix='.db', dir=root)
        os.close(fd)
        try:
            old = store._readonly(source)
            try:
                copied = sqlite3.connect(temporary)
                try:
                    old.backup(copied)
                finally:
                    copied.close()
            finally:
                old.close()
            with open(temporary, 'rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            sync_directory(root)
        finally:
            Path(temporary).unlink(missing_ok=True)
    # 由 Store 统一校验已有库并初始化首次启动中断留下的空库,
    # 避免提前只读校验将可恢复的空库拒绝.
    names.initialize(root / 'devices.json', name_source)
    options.database = str(target)
