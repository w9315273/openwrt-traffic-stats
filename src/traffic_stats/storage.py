"""Prepare a storage directory while the collector holds its exclusive writer lock."""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from . import store
from .common import run
from .runtime import read_runtime

NAME_CONFIG = Path('/etc/config/traffic-stats-devices')
MAIN_CONFIG = Path('/etc/config/traffic-stats')
DEFAULT_DIRECTORY = '/etc/traffic-stats'


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
    if str(root) == DEFAULT_DIRECTORY:
        root.mkdir(mode=0o700, exist_ok=True)
    if not root.is_dir():
        raise ValueError('Storage directory must already exist and be mounted')
    target = root / 'traffic.db'
    source = options.database
    if options.runtime_file:
        try:
            previous = read_runtime(options.runtime_file)
            source = previous.get('storage', {}).get('database') or source
        except (OSError, ValueError):
            pass
    if not source or not Path(source).is_file():
        source = str(root / 'db' / 'traffic.db')
    # A completed backup is published atomically. Existing destination data wins.
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
    if target.exists():
        verified = store._readonly(target)
        verified.close()
    devices = root / 'devices'
    if not devices.exists():
        with tempfile.TemporaryDirectory(prefix='.names-', dir=root) as staging:
            staged = Path(staging) / NAME_CONFIG.name
            if NAME_CONFIG.is_file():
                shutil.copyfile(NAME_CONFIG, staged)
            elif MAIN_CONFIG.is_file():
                shutil.copyfile(MAIN_CONFIG, staged)
                # Use UCI's own parser/serializer, preserving Unicode and quoting.
                delta = Path(staging) / 'delta'
                delta.mkdir()
                args = ['uci', '-q', '-c', staging, '-t', str(delta)]
                run(args + ['delete', NAME_CONFIG.name + '.main'])
                run(args + ['commit', NAME_CONFIG.name])
            else:
                staged.write_text('')
            staged.chmod(0o600)
            with staged.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(staged, devices)
            sync_directory(root)
    if not devices.is_file():
        raise ValueError('Device naming configuration must be a regular file')
    if NAME_CONFIG.resolve() != devices.resolve():
        temporary_link = NAME_CONFIG.with_name(NAME_CONFIG.name + '.new')
        try:
            temporary_link.unlink(missing_ok=True)
            temporary_link.symlink_to(devices)
            os.replace(temporary_link, NAME_CONFIG)
            sync_directory(NAME_CONFIG.parent)
        finally:
            temporary_link.unlink(missing_ok=True)
    options.database = str(target)
