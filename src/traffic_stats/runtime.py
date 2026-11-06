"""Atomic transient state and checks against the current boot and process."""
import os
import re
import tempfile
import time

from .common import dumps, read_json


def clock_ms(clock=time.CLOCK_BOOTTIME):
    return time.clock_gettime_ns(clock) // 1_000_000


def set_timezone(zone):
    os.environ["TZ"] = zone
    # OpenWrt's musl reloads TZ in localtime; its Python may omit tzset.
    if hasattr(time, "tzset"):
        time.tzset()


def read_id(path):
    with open(path, encoding="ascii") as source:
        value = source.read(128).strip()
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
        raise ValueError("Invalid clock identity")
    return value


def write_runtime(path, status):
    directory = os.path.dirname(os.path.abspath(path))
    descriptor, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(dumps(status) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_runtime(path):
    with open(path, "rb") as source:
        status = read_json(source)
    if not isinstance(status, dict) or any(type(status.get(key)) is not int for key in
            ("pid", "sample_mono_ms", "interval")) or type(status.get("running")) is not bool or \
            not isinstance(status.get("boot_id"), str):
        raise ValueError("Invalid collector runtime status")
    alive = status["running"] and 0 < status["pid"] <= (1 << 31) - 1 and \
        status["boot_id"] == read_id("/proc/sys/kernel/random/boot_id")
    if alive:
        try:
            os.kill(status["pid"], 0)
        except ProcessLookupError:
            alive = False
        except PermissionError:
            pass
    now, sampled, period = clock_ms(), status["sample_mono_ms"], status["interval"]
    status["running"] = bool(alive)
    status["stale"] = not alive or not 0 < sampled <= now or not 1 <= period <= 3600 or \
        now - sampled > period * 3000 + 15000
    return status
