"""Display-only JSON naming rules; device identity and accounting stay unchanged."""
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import shlex

from .common import loads, run


def address(value):
    if not isinstance(value, str) or '%' in value:
        raise ValueError('Expected an IPv4 or IPv6 host address')
    return str(ipaddress.ip_address(value))


def rules(sections):
    result, invalid = [], []
    if not isinstance(sections, list):
        raise ValueError('Device names must be a JSON array')
    for index, section in enumerate(sections):
        section_id = str(index)
        if not isinstance(section, dict):
            invalid.append(section_id)
            continue
        try:
            name, mac, ip = (section.get(key, '') for key in ('name', 'mac', 'ip'))
            if any(not isinstance(value, str) for value in (name, mac, ip)):
                raise ValueError('Expected string options')
            name, mac, ip = name.strip(), mac.strip().lower(), ip.strip()
            name.encode('utf-8')
            if not name or len(name) > 128 or re.search(r'[\x00-\x1f\x7f]', name):
                raise ValueError('Invalid device name')
            if mac and not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
                raise ValueError('Invalid MAC address')
            if ip:
                ip = address(ip)
            if not mac and not ip:
                raise ValueError('A naming rule requires MAC or IP')
            result.append({'section': section_id, 'name': name, 'mac': mac, 'ip': ip})
        except ValueError:
            invalid.append(section_id)
    return result, invalid


def path(configuration):
    return Path(configuration['database']).parent / 'devices.json'


def legacy(path):
    """Use the native UCI parser only when migrating an existing installation."""
    candidates = [path.with_name('devices'), Path('/etc/config/traffic-stats-devices'),
                  Path('/etc/config/traffic-stats')]
    source = next((item.resolve() for item in candidates if item.is_file()), None)
    if source is None:
        return []
    output = run(['uci', '-q', '-c', str(source.parent), 'export', source.name]).decode('utf-8')
    result, current = [], None
    for line in output.splitlines():
        fields = shlex.split(line)
        if not fields:
            continue
        if fields[0] == 'config':
            current = {} if fields[1] == 'device' else None
            if current is not None:
                result.append(current)
        elif current is not None and len(fields) == 3 and fields[1] in ('name', 'mac', 'ip'):
            if fields[0] == 'option':
                current[fields[1]] = fields[2]
            elif fields[0] == 'list':
                if not isinstance(current.get(fields[1]), list):
                    current[fields[1]] = []
                current[fields[1]].append(fields[2])
    return result


def read(path):
    path = Path(path)
    try:
        with path.open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        entries = loads(raw, max_size=1024 * 1024)
    except FileNotFoundError:
        entries = legacy(path)
        raw = json.dumps(entries, ensure_ascii=False).encode('utf-8')
    if not isinstance(entries, list):
        raise ValueError('Device names must be a JSON array')
    return entries, hashlib.sha256(raw).hexdigest()


def write(path, entries):
    """Replace on the destination filesystem, leaving the old file intact on failure."""
    payload = json.dumps(entries, ensure_ascii=False, indent=2) + '\n'
    loads(payload, max_size=1024 * 1024)
    fd, temporary = tempfile.mkstemp(prefix='.devices-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def initialize(path, source=None):
    with (path.parent / '.devices.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            entries, _ = read(source if source and source.is_file() else path)
            write(path, entries)
        else:
            read(path)


class Conflict(ValueError):
    pass


def save(path, action, section, values, revision):
    path = Path(path)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    # A separate lock survives atomic replacement of the data file.
    with (path.parent / '.devices.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries, current = read(path)
        if revision != current:
            raise Conflict('Device names changed since the editor was opened')
        index = int(section) if section else len(entries)
        if section and not 0 <= index < len(entries):
            raise Conflict('The selected rule no longer exists')
        if action == 'delete':
            del entries[index]
        else:
            checked, invalid = rules([values])
            if invalid:
                raise ValueError('Invalid naming rule')
            entry = {key: checked[0][key] for key in ('name', 'mac', 'ip') if checked[0][key]}
            if section:
                entries[index] = entry
            else:
                entries.append(entry)
        write(path, entries)
    return str(index)


def apply(devices, naming_rules):
    # Stable sorting makes the first configured rule win at equal specificity.
    ordered = sorted(naming_rules, key=lambda rule: (bool(rule['mac']) * 2 + bool(rule['ip'])), reverse=True)
    for device in devices:
        mac = (device.get('mac') or '').lower()
        addresses = set()
        for ip in device.get('addresses', []):
            try:
                addresses.add(address(ip))
            except ValueError:
                pass
        device['custom_name'] = ''
        device['name_rule'] = ''
        for rule in ordered:
            if (not rule['mac'] or rule['mac'] == mac) and (not rule['ip'] or rule['ip'] in addresses):
                device['custom_name'] = rule['name']
                device['name_rule'] = rule['section']
                break
