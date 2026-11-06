"""Bounded rpcd executable methods shared by the existing LuCI pages."""

import argparse
from datetime import date, timedelta
import errno
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import sys
import time

from . import common, config, names, proxy, runtime, store, sample

MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 240 * 1024
SCHEMA = {
    "status": {},
    "containers": {},
    "history": {"resolution": "", "from": "", "to": "", "counter": "", "limit": 0},
    "service": {"action": ""},
}
COUNTER = re.compile(r"(?:total|unknown|d_[0-9a-f]{12})_(?:up|down)\Z")


def failure(code, message):
    return {"ok": False, "error": code, "message": message}


def success(data):
    return {"ok": True, "data": data}


def valid_string(value):
    if not isinstance(value, str) or "\0" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def interface_status(name):
    present = False
    if name:
        try:
            present = socket.if_nametoindex(name) != 0
        except OSError:
            pass
    carrier = None
    if present:
        try:
            value = Path("/sys/class/net", name, "carrier").read_text().strip()
            if value in ("0", "1"):
                carrier = value == "1"
        except OSError:
            pass
    return {"name": name, "present": present, "carrier": carrier}


def status(configuration, runtime_file):
    try:
        data = runtime.read_runtime(runtime_file)
        available = True
    except (OSError, ValueError) as error:
        missing = isinstance(error, OSError) and error.errno == errno.ENOENT
        data = {"running": False, "stale": True,
                "reason": "runtime_missing" if missing else "runtime_unreadable"}
        if not missing:
            data["runtime_error"] = "The collector runtime status could not be read."
        available = False
    data["available"] = available
    data["configuration"] = {key: value for key, value in configuration.items() if key not in ("timezone", "name_rules", "invalid_name_rules")}
    data["timezone"] = configuration["timezone"]
    runtime.set_timezone(configuration["timezone"])
    try:
        today = time.strftime("%Y-%m-%d", time.localtime())
        data["local_date"] = today if len(today) == 10 else None
    except (ValueError, OverflowError, OSError):
        data["local_date"] = None
    data["interfaces"] = {
        "lan": [interface_status(name) for name in configuration["lan_devices"]],
        "wan": [interface_status(name) for name in configuration["wan_devices"]],
    }
    devices, truncated = [], False
    try:
        regular = stat.S_ISREG(os.stat(configuration["database"]).st_mode)
    except OSError:
        regular = False
    if regular:
        try:
            devices, truncated = store.devices(configuration["database"], limit=1000)
        except (OSError, ValueError, sqlite3.Error):
            data["history_error"] = "Saved device metadata could not be read."
    naming_rules = configuration.get("name_rules", [])
    names.apply(data.get("devices", []), naming_rules)
    names.apply(devices, naming_rules)
    data["invalid_name_rules"] = configuration.get("invalid_name_rules", [])
    data["known_devices"] = devices
    data["known_devices_truncated"] = truncated
    return success(data)


def valid_request(request, method):
    if not isinstance(request, dict):
        return False
    allowed = set(SCHEMA.get(method, {}))
    for key, value in request.items():
        if key == "ubus_rpc_session":
            if not valid_string(value):
                return False
        elif key not in allowed:
            return False
    return True


def date_number(value):
    if not valid_string(value) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    try:
        return date.fromisoformat(value).toordinal()
    except ValueError:
        return None


def add_timeline(data, day, default_zone):
    zones = sorted({row['timezone'] for row in data['rows']}) or [default_zone]
    first = date.fromisoformat(day)
    following = first + timedelta(days=1)
    slots = []
    for zone in zones[:8]:
        runtime.set_timezone(zone)
        start = int(time.mktime((first.year, first.month, first.day, 0, 0, 0, 0, 0, -1)))
        end = int(time.mktime((following.year, following.month, following.day, 0, 0, 0, 0, 0, -1)))
        if not 0 < end - start <= 90000:
            raise ValueError('Unsupported local calendar day')
        stamp = start
        while stamp < end:
            local = time.localtime(stamp)
            _, boundary, _ = sample.half_hour_at(stamp * 1000)
            next_stamp = min(boundary // 1000, end)
            slots.append({'start': stamp, 'end': next_stamp, 'timezone': zone,
                          'label': time.strftime('%H:%M', local), 'offset': time.strftime('%z', local),
                          'end_label': time.strftime('%H:%M', time.localtime(next_stamp)),
                          'group_start': stamp})
            stamp = next_stamp
    data['slots'] = sorted(slots, key=lambda slot: (slot['start'], slot['timezone']))
    data['multiple_timezones'] = len(zones) > 1
    data['timeline_truncated'] = len(zones) > 8


def history(request, configuration):
    resolution = request.get("resolution", "day")
    from_date, to_date = request.get("from"), request.get("to")
    first, last = date_number(from_date), date_number(to_date)
    counter = request.get("counter", "")
    if (not valid_string(resolution) or first is None or last is None or last < first or
            not valid_string(counter) or (counter and not COUNTER.fullmatch(counter))):
        return failure("invalid_request", "Invalid history dates, resolution or counter.")
    maximum = {"30min": 1, "day": 366, "month": 3660}.get(resolution)
    if maximum is None:
        return failure("invalid_request", "Unsupported history resolution.")
    if last - first + 1 > maximum:
        return failure("range_too_large", "Reduce the date range or choose a coarser resolution.")
    limit = request.get("limit", 1000)
    if type(limit) is not int or not 1 <= limit <= 1000:
        return failure("invalid_request", "History limit must be an integer from 1 to 1000.")
    path = configuration["database"]
    try:
        regular = stat.S_ISREG(os.stat(path).st_mode)
    except FileNotFoundError:
        return failure("not_initialized", "No history database has been created yet.")
    except OSError:
        return failure("history_unavailable", "The history database is not accessible.")
    if not regular:
        return failure("history_unavailable", "The history database is not a regular file.")
    try:
        data = store.query(path, resolution, from_date, to_date, counter or None, limit)
        if resolution == '30min':
            add_timeline(data, from_date, configuration['timezone'])
    except (OSError, ValueError, OverflowError, sqlite3.Error):
        return failure("history_unavailable", "Saved history could not be read.")
    return success(data)


def service(request, configuration, service_script):
    action = request.get("action")
    if not valid_string(action) or action not in ("start", "stop", "restart"):
        return failure("invalid_request", "Unsupported service action.")
    if action != "stop" and not configuration.get("enabled", False):
        return failure("service_disabled", "Enable the collector in its configuration first.")
    try:
        common.run([service_script, action], capture=False)
    except (OSError, ValueError):
        return failure("service_failed", "The service action failed. Check the system log.")
    return success({"action": action, "accepted": True})


def read_request(stream):
    request = common.read_json(stream, max_size=MAX_REQUEST)
    if not isinstance(request, dict):
        raise ValueError("Expected JSON object")
    return request


def dispatch(method, request, paths):
    if not valid_request(request, method):
        return failure("invalid_request", "Expected a bounded JSON object with supported parameters.")
    if method not in SCHEMA:
        return failure("unknown_method", "Unsupported RPC method.")
    if method == "containers":
        try:
            result = proxy.containers()
            return success({'containers': [{key: item[key] for key in ('name', 'image', 'state', 'addresses')}
                                            for item in result['containers']],
                            'excluded_names': result['excluded_names']})
        except (OSError, ValueError):
            return failure('docker_unavailable', 'Cannot read local Docker containers. Check the Docker service and CLI.')
    if method == "service" and request.get("action") == "stop":
        return service(request, {}, paths.service_script)
    try:
        configuration = config.load_configuration()
    except (OSError, ValueError):
        return failure("invalid_configuration", "The traffic statistics configuration is invalid.")
    if method == "status":
        return status(configuration, paths.runtime_file)
    if method == "history":
        return history(request, configuration)
    return service(request, configuration, paths.service_script)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-file", default="/var/run/traffic-stats.json")
    parser.add_argument("--service-script", default="/etc/init.d/traffic-stats")
    parser.add_argument("verb", choices=("list", "call"))
    parser.add_argument("method", nargs="?")
    paths = parser.parse_args(argv)
    if paths.verb == "list" and paths.method is None:
        reply = SCHEMA
    elif paths.verb == "call" and paths.method is not None:
        try:
            request = read_request(sys.stdin.buffer)
        except (ValueError, UnicodeError, OSError, RecursionError):
            reply = failure("invalid_request", "Expected a bounded JSON object with supported parameters.")
        else:
            reply = dispatch(paths.method, request, paths)
    else:
        parser.error("Expected list or call METHOD")
    output = json.dumps(reply, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(output) > MAX_RESPONSE:
        output = json.dumps(failure("response_too_large", "Reduce the query range or row limit."),
                            separators=(",", ":")).encode("utf-8")
    try:
        sys.stdout.buffer.write(output + b"\n")
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
