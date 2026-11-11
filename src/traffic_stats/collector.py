"""One writer owns discovery, nftables reconciliation and durable checkpoints."""
import argparse
import errno
import fcntl
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time

from . import discovery, nft, proxy, sample, storage, store
from .common import dumps, loads, read_json, run
from .runtime import clock_ms, read_id, read_runtime, set_timezone, write_runtime


DISCOVERY_INTERVAL_MS = 30000
CLOCK_STABLE_MS = 30000


def _number(minimum, maximum):
    def parse(value):
        if not re.fullmatch(r"[+]?[0-9]+", value) or not minimum <= int(value) <= maximum:
            raise argparse.ArgumentTypeError("Expected an integer from %d to %d" % (minimum, maximum))
        return int(value)
    return parse


def _same_path(left, right):
    if os.path.realpath(left) == os.path.realpath(right):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _check_paths(options):
    if options.runtime_file and _same_path(options.runtime_file, options.lock):
        raise ValueError("Runtime and writer lock paths must be different")
    if options.database is None:
        return
    if not options.database or options.database == ":memory:":
        raise ValueError("History requires a persistent database file")
    database = os.path.realpath(options.database)
    for suffix in ("", "-wal", "-shm", "-journal"):
        if _same_path(options.lock, database + suffix) or \
                (options.runtime_file and _same_path(options.runtime_file, database + suffix)):
            raise ValueError("Runtime and lock paths must not alias database files")


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description="Per-device nftables traffic accounting")
    parser.add_argument("command", choices=("plan", "sync", "watch", "status", "history"))
    for name in ("leases", "neighbors", "ruleset", "database", "runtime-file", "timezone",
                 "from", "to", "counter"):
        parser.add_argument("--" + name)
    parser.add_argument("--lan", action="append", default=[])
    parser.add_argument("--wan", action="append", default=[])
    parser.add_argument("--lock", default="/var/run/traffic-stats.lock")
    parser.add_argument("--proxy-container", action="append", default=[])
    parser.add_argument("--storage-dir")
    parser.add_argument("--interval", type=_number(1, 3600), default=5)
    parser.add_argument("--commit-interval", type=_number(1, 86400), default=60)
    parser.add_argument("--retention", default="3")
    parser.add_argument("--resolution", default="day", choices=("30min", "day", "month"))
    parser.add_argument("--limit", type=_number(1, 10000), default=1000)
    options = parser.parse_args(argv)
    try:
        store.retention_months(options.retention)
        options.proxy_containers = proxy.names(options.proxy_container)
    except ValueError as error:
        parser.error(str(error))
    options.from_date = getattr(options, "from")
    options.explicit_leases = options.leases is not None
    if options.leases is None:
        options.leases = "/tmp/dhcp.leases"
    options.lan = list(dict.fromkeys(options.lan))
    options.wan = list(dict.fromkeys(options.wan))
    if options.command in ("plan", "sync", "watch") and (
            not options.lan or not options.wan or
            any(not nft.valid_interface(value) for value in options.lan + options.wan) or
            set(options.lan) & set(options.wan)):
        parser.error("Configure non-overlapping LAN and WAN network device lists")
    if options.ruleset and options.command not in ("plan", "status"):
        parser.error("Offline rulesets cannot authorize live changes")
    if options.command == "history" and not (options.database and options.from_date and options.to):
        parser.error("History requires --database, --from and --to")
    if options.runtime_file and options.command in ("plan", "history"):
        parser.error("Runtime status is not an output of this command")
    if options.database and options.command in ("plan", "status"):
        parser.error("This command does not open a history database")
    if options.command == "status" and options.ruleset and options.runtime_file:
        parser.error("Choose either a ruleset or a runtime file")
    if options.database and options.command != "history" and options.commit_interval < options.interval:
        parser.error("History commit interval must not be shorter than the sample interval")
    if options.command in ("sync", "watch"):
        try:
            if options.storage_dir:
                options.storage_dir = storage.directory(options.storage_dir)
            _check_paths(options)
        except ValueError as error:
            parser.error(str(error))
    return options


def _timezone(value):
    zone = value or os.environ.get("TZ")
    if not zone:
        try:
            zone = Path("/etc/TZ").read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            zone = "UTC0"
    if not zone or len(zone.encode("utf-8")) >= 128 or "\0" in zone:
        raise ValueError("Invalid POSIX timezone")
    set_timezone(zone)
    return zone


def read_state(fixture=None, cancel=None, table_only=False):
    if fixture:
        with open(fixture, "rb") as source:
            document = read_json(source)
    else:
        command = (["nft", "-j", "-a", "list", "table", "inet", nft.TABLE] if table_only else
                   ["nft", "-j", "-a", "-t", "list", "ruleset"])
        document = loads(run(command, cancel=cancel))
    return nft.parse(document)


def read_inventory(options, local_device=None):
    inventory = discovery.Inventory()
    if local_device:
        inventory.devices[local_device["id"]] = dict(local_device)
    leases_available = True
    try:
        with open(options.leases, encoding="utf-8", errors="replace") as source:
            inventory.load_leases(source, int(time.time()), options.lan[0] if len(options.lan) == 1 else None)
    except FileNotFoundError:
        if options.explicit_leases:
            raise
        leases_available = False
        print("DHCP lease file absent; using neighbor discovery only.", file=sys.stderr)
    if options.neighbors:
        with open(options.neighbors, encoding="utf-8", errors="replace") as source:
            inventory.load_neighbor_file(source, options.lan)
    else:
        for lan in options.lan:
            try:
                inventory.load_neighbors(lan)
                inventory.load_local(lan)
            except OSError as error:
                if error.errno not in (errno.ENODEV, errno.ENXIO):
                    raise
    return inventory, leases_available


def configuration_key(inventory, listeners):
    devices = tuple(sorted((identity, bool(device.get('local'))) for identity, device in inventory.devices.items()))
    bindings = tuple(sorted((binding.get('interface', ''), binding['family'], binding['address'], binding['device_id'])
                            for binding in inventory.bindings.values() if not binding.get('conflict')))
    endpoints = tuple(sorted((item['address'], item['protocol'], item['port']) for item in listeners))
    return devices, bindings, endpoints


class Collector:
    def __init__(self, options):
        self.options = options
        self.timezone = _timezone(options.timezone)
        self.boot_id = read_id("/proc/sys/kernel/random/boot_id")
        self.store = None
        self.previous = {}
        self.previous_generation = ""
        self.inventory = None
        self.listeners = []
        self.proxy_ips = []
        self.proxy_error = ""
        self.leases_available = True
        self.configuration_key = None
        self.rule_signature = None
        self.next_discovery_mono = 0
        self.flowtables_present = False
        self.latest = None
        self.local_device = None
        if options.runtime_file:
            try:
                previous = read_runtime(options.runtime_file)
                local = next((device for device in previous.get('devices', []) if device.get('local')), None)
                if local and discovery.normalize_mac(local.get('mac')) == (local.get('mac'), local.get('id')):
                    self.local_device = {key: local[key] for key in ('id', 'mac', 'hostname', 'local')}
            except (OSError, ValueError, KeyError):
                pass
        self.storage_error = ""
        self.sample_error = ""
        self.last_flush_mono = 0
        self.resumed = False
        self.first_sample = True
        self.pruning_allowed = True
        self.clock_stable_since = None
        self.stopping = False
        self.result = 0

    def cancelled(self):
        return self.stopping

    def stop(self, signum, frame):
        self.stopping = True

    def collect(self):
        options = self.options
        self.sample_error = ""
        step = "Read nftables state"
        try:
            now = clock_ms()
            refresh_discovery = (self.inventory is None or now >= self.next_discovery_mono or
                                 options.command != 'watch')
            if refresh_discovery:
                state = read_state(options.ruleset, self.cancelled)
            else:
                try:
                    state = read_state(cancel=self.cancelled, table_only=True)
                except OSError:
                    state = read_state(cancel=self.cancelled)
                    refresh_discovery = True
            if state.exists and not state.owned:
                raise ValueError("Existing inet traffic_stats table is not managed by this version. "
                                 "Inspect and migrate it explicitly.")
            table_created = not state.exists
            flowtables_present = state.flowtables_present if refresh_discovery else self.flowtables_present
            inventory, leases_available, listeners = self.inventory, self.leases_available, self.listeners
            proxy_ips = self.proxy_ips
            if refresh_discovery:
                step = "Read device discovery sources"
                inventory, leases_available = read_inventory(options, self.local_device)
                step = "Read proxy containers"
                proxy_error = ""
                try:
                    records = proxy.containers(cancel=self.cancelled)['containers'] if options.proxy_containers else []
                except (OSError, ValueError) as error:
                    records = []
                    proxy_error = str(error)
                if not self.stopping and proxy_error != self.proxy_error:
                    print("Proxy discovery unavailable; continuing ordinary traffic collection: " + proxy_error
                          if proxy_error else "Proxy discovery recovered.", file=sys.stderr)
                self.proxy_error = proxy_error
                selected = [item for item in records if item['name'] in options.proxy_containers]
                proxy_ips = proxy.addresses([address for item in selected for address in item['addresses']])
                endpoints = {(listener['address'], listener['protocol'], listener['port'])
                             for item in selected for listener in item['listeners']}
                listeners = [{'address': address, 'protocol': protocol, 'port': port}
                             for address, protocol, port in sorted(endpoints)]
            if self.stopping:
                return 0
            key = (tuple(options.lan), tuple(options.wan), tuple(options.proxy_containers), tuple(proxy_ips),
                   configuration_key(inventory, listeners))
            reconcile = key != self.configuration_key or table_created or options.command != 'watch'
            if refresh_discovery and not reconcile:
                step = "Read nftables state"
                state = read_state(cancel=self.cancelled, table_only=True)
            if reconcile or state.signature != self.rule_signature:
                generation = read_id("/proc/sys/kernel/random/uuid") if not state.exists else ""
                step = "Prepare nftables transaction"
                transaction = nft.plan(inventory, state, options.lan, options.wan, generation,
                                       proxy_ips, listeners)
                if options.command == "plan":
                    print(transaction, end="", flush=True)
                    return 0
                step = "Apply nftables transaction"
                run(["nft", "-f", "-"], input_data=transaction, capture=False, cancel=self.cancelled)
                step = "Read updated nftables counters"
                updated = read_state(cancel=self.cancelled, table_only=True)
            else:
                updated = state
            if not updated.exists or not updated.owned:
                raise ValueError("Statistics table disappeared or changed ownership")
            updated.flowtables_present = flowtables_present
            wall_ms, mono_ms = clock_ms(time.CLOCK_REALTIME), clock_ms()
            changed = bool(self.previous_generation and self.previous_generation != updated.generation)
            step = "Calculate traffic deltas"
            measured = sample.build_sample(self.previous, updated, inventory, self.boot_id,
                wall_ms, mono_ms, self.timezone, max(120000, options.interval * 2000), self.resumed)
            if not measured["clock_valid"] or measured["quality_flags"] & sample.CLOCK:
                self.pruning_allowed = False
                self.clock_stable_since = None
            elif not self.pruning_allowed:
                # 墙上时钟与单调时钟稳定一段时间后, 才恢复会删除历史的清理操作.
                if self.clock_stable_since is None:
                    self.clock_stable_since = mono_ms
                elif mono_ms - self.clock_stable_since >= CLOCK_STABLE_MS:
                    self.pruning_allowed = True
            if self.store:
                step = "Stage history"
                try:
                    self.store.stage(measured["batch"])
                except (ValueError, sqlite3.Error, OverflowError) as error:
                    self.storage_error = str(error)
                    raise
            self.previous = measured["batch"]["checkpoints"]
            self.resumed = False
            self.previous_generation = updated.generation
            step = 'Calculate monthly usage'
            monthly = (self.store.month_usage(time.strftime('%Y-%m-%d', time.localtime(wall_ms // 1000)))
                       if self.store and measured['clock_valid'] else None)
            self.local_device = next((dict(device) for device in inventory.devices.values() if device.get('local')), None)
            self.latest = nft.report(updated, inventory)
            self.latest.update(rates=measured["rates"], sample_wall_ms=wall_ms, sample_mono_ms=mono_ms,
                clock_valid=measured["clock_valid"], quality_flags=measured["quality_flags"],
                deferred_counters=measured["deferred_counters"], table_created=table_created,
                generation_changed=changed, leases_available=leases_available, timestamp=wall_ms // 1000)
            self.latest['proxy_listeners'] = listeners
            self.latest['proxy_addresses'] = proxy_ips
            self.latest['monthly'] = monthly
            self.inventory, self.listeners, self.leases_available = inventory, listeners, leases_available
            self.proxy_ips = proxy_ips
            self.configuration_key, self.rule_signature = key, updated.signature
            self.flowtables_present = flowtables_present
            if refresh_discovery:
                self.next_discovery_mono = now + DISCOVERY_INTERVAL_MS
            self.first_sample = False
            if changed:
                print("Counter generation changed; historical gaps are marked.", file=sys.stderr)
            if updated.flowtables_present:
                print("Flowtables are present; forwarded traffic may bypass these counters.", file=sys.stderr)
            return 0
        except (OSError, ValueError, sqlite3.Error, OverflowError) as error:
            if self.stopping:
                return 0
            self.sample_error = step + ": " + str(error)
            print(self.sample_error, file=sys.stderr)
            return 1

    def flush(self):
        if not self.store or not self.store.dirty:
            return 0
        try:
            self.store.flush(clock_ms(time.CLOCK_REALTIME), self.pruning_allowed)
            self.storage_error = ""
            self.last_flush_mono = clock_ms()
            return 0
        except (ValueError, sqlite3.Error, OverflowError, OSError) as error:
            self.storage_error = str(error)
            print("History commit failed: " + self.storage_error, file=sys.stderr)
            return 1

    def publish(self):
        if self.latest is None:
            self.latest = {"sample_mono_ms": 0}
        self.latest.update(pid=os.getpid(), boot_id=self.boot_id,
            running=not self.stopping and self.options.command == "watch", interval=self.options.interval,
            last_attempt_ms=clock_ms(time.CLOCK_REALTIME), sampling_ok=not bool(self.sample_error),
            last_error=self.sample_error, timezone=self.timezone, storage={
                "enabled": self.store is not None,
                "database": self.options.database,
                "pending": bool(self.store and self.store.dirty),
                "last_commit_ms": self.store.last_commit_ms if self.store else 0,
                "error": self.storage_error, "pruning_allowed": self.pruning_allowed})
        try:
            if self.options.runtime_file:
                write_runtime(self.options.runtime_file, self.latest)
            else:
                print(dumps(self.latest), flush=True)
            return 0
        except (OSError, ValueError) as error:
            print("Publish runtime status: " + str(error), file=sys.stderr)
            return 1

    def execute(self):
        lock_fd = None
        try:
            if self.options.command != "plan":
                lock_fd = os.open(self.options.lock, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.options.storage_dir:
                if self.options.command not in ('sync', 'watch'):
                    raise ValueError('Storage preparation requires a live collector command')
                storage.prepare(self.options)
                _check_paths(self.options)
            if self.options.database:
                self.store = store.Store(self.options.database, self.options.retention)
                self.local_device = self.store.load_local_device() or self.local_device
                self.previous = self.store.load_checkpoints()
                self.resumed = bool(self.previous)
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)
            if self.options.command == "watch":
                while not self.stopping:
                    first = self.first_sample
                    self.result = self.collect()
                    if first or clock_ms() - self.last_flush_mono >= self.options.commit_interval * 1000:
                        self.result |= self.flush()
                    self.result |= self.publish()
                    deadline = clock_ms() + self.options.interval * 1000
                    while not self.stopping and clock_ms() < deadline:
                        time.sleep(min(0.2, max(0, deadline - clock_ms()) / 1000))
                self.result |= self.flush()
                if self.latest is not None:
                    self.result |= self.publish()
            else:
                self.result = self.collect() | self.flush()
                if self.options.command != "plan":
                    self.result |= self.publish()
            return self.result
        finally:
            if self.store:
                self.store.close()
            if lock_fd is not None:
                os.close(lock_fd)


def main(argv=None):
    os.umask(0o077)
    options = parse_options(argv)
    try:
        if options.command == "history":
            value = store.query(options.database, options.resolution, options.from_date,
                                options.to, options.counter, options.limit)
            print(dumps(value), flush=True)
            return 0
        if options.command == "status":
            value = read_runtime(options.runtime_file) if options.runtime_file else nft.report(read_state(options.ruleset))
            print(dumps(value), flush=True)
            return 0
        return Collector(options).execute()
    except (OSError, ValueError, sqlite3.Error, OverflowError) as error:
        print("traffic-stats: " + str(error), file=sys.stderr)
        return 1
