"""Read typed UCI values through the system's existing JSON interface."""

import json
from pathlib import Path
import re

from . import common, names, proxy, storage, store


def package(name):
    try:
        raw = common.run(["ubus", "call", "uci", "get", json.dumps({"config": name})])
    except OSError:
        # Missing packages use defaults; parse and transport failures for an
        # existing configuration must remain visible to the caller.
        if not Path("/etc/config", name).exists():
            return {}
        raise
    data = common.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("values"), dict):
        raise ValueError("Invalid UCI package response")
    return data["values"]


def option(section, name, default, maximum):
    value = section.get(name, default)
    if (not isinstance(value, str) or "\0" in value or "\n" in value or "\r" in value or
            len(value.encode("utf-8")) >= maximum):
        raise ValueError("Invalid UCI string option")
    return value


def number(section, name, default, minimum, maximum):
    value = option(section, name, default, 32)
    if not re.fullmatch(r"[ \t\v\f]*[+-]?[0-9]+", value):
        raise ValueError("Invalid UCI numeric option")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError("UCI numeric option is out of range")
    return result


def devices(section, name, legacy, default):
    value = section.get(name, section.get(legacy, default))
    if isinstance(value, str):
        value = [value] if value else []
    if not isinstance(value, list) or any(
            not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", device)
            for device in value):
        raise ValueError("Invalid network device list")
    return list(dict.fromkeys(value))


def retention(section):
    value = section.get('retention')
    if value is None:
        days = number(section, 'retention_days', '90', 1, 3650)
        value = str(min(120, (days + 29) // 30)) + 'mo'
    months = store.retention_months(value)
    return str(months)


def load_configuration(include_names=True):
    values = package("traffic-stats")
    section = values.get("main", {})
    if not isinstance(section, dict):
        raise ValueError("Invalid traffic statistics configuration section")
    enabled = option(section, "enabled", "0", 16)
    if enabled not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
        raise ValueError("Invalid enabled setting")
    interval = number(section, "interval", "5", 1, 3600)
    result = {
        "enabled": enabled in ("1", "true", "yes", "on"),
        "lan_devices": devices(section, "lan_devices", "lan_device", "br-lan"),
        "wan_devices": devices(section, "wan_devices", "wan_device", ""),
        "database": option(section, "database", "/etc/traffic-stats/traffic.db", 4096),
        "interval": interval,
        "commit_interval": number(section, "commit_interval", "60", interval, 86400),
        "retention": retention(section),
        "proxy_containers": proxy.names(section.get("proxy_containers", [])),
    }
    if set(result['lan_devices']) & set(result['wan_devices']):
        raise ValueError("LAN and WAN network devices must not overlap")
    if result['enabled'] and (not result['lan_devices'] or not result['wan_devices']):
        raise ValueError("Configure at least one LAN and WAN network device")
    if not result["database"].startswith("/"):
        raise ValueError("Invalid database path")
    root = option(section, "storage_dir", "", 4096)
    if root:
        result['storage_dir'] = storage.directory(root)
        result['database'] = str(Path(root) / 'traffic.db')
    if include_names:
        result["name_rules"], result["invalid_name_rules"] = names.rules(names.read(names.path(result))[0])
    system = package("system")
    section = next((value for value in system.values()
                    if isinstance(value, dict) and value.get(".type") == "system"), {})
    result["timezone"] = option(section, "timezone", "UTC0", 128)
    if not result["timezone"]:
        raise ValueError("Invalid empty timezone")
    return result
