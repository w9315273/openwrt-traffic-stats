"""Read and update only the collector's explicitly owned nftables table."""

from dataclasses import dataclass, field
import errno
import ipaddress
import json
import re
import socket

from . import proxy


TABLE = "traffic_stats"
OWNER_PREFIX = "traffic-stats:v1:"
UINT64_MAX = (1 << 64) - 1
BASE_COUNTERS = ("total_up", "total_down", "unknown_up", "unknown_down")
CHAINS = ("forward", "upload", "download")
LOCAL_CHAINS = ("input", "output")
MAPS = ("up4", "up6", "down4", "down6")


@dataclass
class NftState:
    exists: bool = False
    owned: bool = False
    flowtables_present: bool = False
    generation: str = ""
    counters: dict = field(default_factory=dict)
    local_chains: bool = False
    interface_maps: bool = True
    signature: str = ""


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12}", value) is not None


def valid_generation(value):
    return isinstance(value, str) and re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value) is not None


def valid_interface(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", value) is not None


def device_counter_id(name):
    match = re.fullmatch(r"d_([0-9a-f]{12})_(up|down)", name) if isinstance(name, str) else None
    return match.groups() if match else None


def _invalid(message="Incompatible nftables statistics table"):
    return OSError(errno.EPROTO, message)


def _u64(value):
    if type(value) is not int or not 0 <= value <= UINT64_MAX:
        raise _invalid("Invalid nftables counter value")
    return value


def _in_table(value):
    return isinstance(value, dict) and value.get("family") == "inet" and value.get("table") == TABLE


def _signature(entries):
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'))

    objects, rules = [], {}
    for entry in entries:
        for kind, item in entry.items():
            is_table = (kind == 'table' and isinstance(item, dict) and
                        item.get('family') == 'inet' and item.get('name') == TABLE)
            if not is_table and not _in_table(item):
                continue
            definition = dict(item)
            if kind == 'counter':
                definition.pop('bytes', None)
                definition.pop('packets', None)
            elif kind == 'map':
                # Kernel hash iteration order is not part of a map's meaning.
                definition['elem'] = sorted(definition.get('elem', []), key=canonical)
            if kind == 'rule':
                # Preserve rule order within each chain, independent of object listing order.
                rules.setdefault(item['chain'], []).append(definition)
            else:
                objects.append({kind: definition})
    return canonical({'objects': sorted(objects, key=canonical), 'rules': rules})


def parse(ruleset):
    entries = ruleset.get("nftables") if isinstance(ruleset, dict) else None
    if not isinstance(entries, list):
        raise _invalid("Expected an nftables JSON ruleset")
    state = NftState()
    for entry in entries:
        if not isinstance(entry, dict):
            raise _invalid()
        if entry.get("flowtable") is not None:
            state.flowtables_present = True
        table = entry.get("table")
        if not isinstance(table, dict) or table.get("family") != "inet" or table.get("name") != TABLE:
            continue
        if state.exists:
            raise _invalid()
        state.exists = True
        comment = table.get("comment")
        if isinstance(comment, str) and comment.startswith(OWNER_PREFIX):
            generation = comment[len(OWNER_PREFIX):]
            if valid_generation(generation):
                state.owned, state.generation = True, generation
    if not state.owned:
        return state
    chains, maps, map_schemas = set(), set(), set()
    for entry in entries:
        for kind, item in entry.items():
            if not _in_table(item):
                continue
            name = item.get("name")
            if kind == "counter":
                if (name not in BASE_COUNTERS and device_counter_id(name) is None) or name in state.counters:
                    raise _invalid()
                state.counters[name] = {"name": name, "handle": _u64(item.get("handle", 0)),
                                        "bytes": _u64(item.get("bytes")), "packets": _u64(item.get("packets"))}
            elif kind == "chain":
                if name not in CHAINS + LOCAL_CHAINS or name in chains:
                    raise _invalid()
                if name in ("forward",) + LOCAL_CHAINS:
                    if (item.get("type") != "filter" or item.get("hook") != name or
                            item.get("policy") != "accept" or type(item.get("prio")) is not int or item["prio"] != 10):
                        raise _invalid()
                elif any(item.get(key) is not None for key in ("hook", "type", "policy")):
                    raise _invalid()
                chains.add(name)
            elif kind == "map":
                if name not in MAPS or name in maps:
                    raise _invalid()
                address_type = "ipv6_addr" if name.endswith("6") else "ipv4_addr"
                interface_map = item.get("type") == ["ifname", address_type]
                if (item.get("map") != "counter" or
                        (not interface_map and item.get("type") != address_type) or
                        item.get("flags") is not None or item.get("timeout") is not None or
                        not isinstance(item.get('elem', []), list)):
                    raise _invalid()
                maps.add(name)
                map_schemas.add(interface_map)
            elif kind == "rule":
                if item.get("chain") not in CHAINS + LOCAL_CHAINS:
                    raise _invalid()
            else:
                raise _invalid()
    if chains not in (set(CHAINS), set(CHAINS + LOCAL_CHAINS)) or maps != set(MAPS) or not all(name in state.counters for name in BASE_COUNTERS):
        raise _invalid()
    if len(map_schemas) != 1:
        raise _invalid()
    state.interface_maps = map_schemas.pop()
    state.local_chains = set(LOCAL_CHAINS).issubset(chains)
    for name in state.counters:
        device = device_counter_id(name)
        if device:
            identity, direction = device
            if f"d_{identity}_{'down' if direction == 'up' else 'up'}" not in state.counters:
                raise _invalid()
    state.signature = _signature(entries)
    return state


def _interfaces(value):
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or not values or any(not valid_interface(item) for item in values):
        raise ValueError('Invalid network interface list')
    return list(dict.fromkeys(values))


def _interface_match(values):
    quoted = [f'"{value}"' for value in values]
    return quoted[0] if len(quoted) == 1 else '{ ' + ', '.join(quoted) + ' }'


def _validate_inventory(inventory, lans):
    if inventory is None:
        raise ValueError("Missing device inventory")
    for identity, device in inventory.devices.items():
        if not valid_id(identity) or device.get("id") != identity:
            raise ValueError("Invalid device identity")
    for key, binding in inventory.bindings.items():
        family, address = binding.get("family"), binding.get("address")
        interface = binding.get('interface')
        if interface in (None, '') and len(lans) == 1 and key == (family, address):
            interface = lans[0]
        elif key != (interface, family, address):
            raise ValueError("Invalid address binding")
        if (family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, str) or
                not valid_interface(interface) or interface not in lans):
            raise ValueError("Invalid address binding")
        try:
            socket.inet_pton(family, address)
        except (ValueError, OSError) as error:
            raise ValueError("Invalid address binding") from error
        if not binding.get("conflict") and binding.get("device_id") not in inventory.devices:
            raise ValueError("Address binding refers to an unknown device")


def plan(inventory, state, lan, wan, generation_if_new, proxy_ips=(), published=()):
    if state.exists and not state.owned:
        raise FileExistsError(errno.EEXIST, "Existing inet traffic_stats table is not managed by this version. Inspect and migrate it explicitly.")
    lans, wans = _interfaces(lan), _interfaces(wan)
    if (set(lans) & set(wans) or (not state.exists and not valid_generation(generation_if_new))):
        raise ValueError("Invalid interface or counter generation")
    _validate_inventory(inventory, lans)
    lan_match, wan_match = _interface_match(lans), _interface_match(wans)
    local_ids = [identity for identity, device in inventory.devices.items() if device.get('local') is True]
    if len(local_ids) > 1:
        raise ValueError('Multiple local devices in inventory')
    proxy_ips = proxy.addresses(proxy_ips)
    lines = []
    if not state.exists:
        lines.extend([
            f'create table inet {TABLE} {{ comment "{OWNER_PREFIX}{generation_if_new}"; }}',
            f"add chain inet {TABLE} forward {{ type filter hook forward priority 10; policy accept; }}",
            f"add chain inet {TABLE} upload", f"add chain inet {TABLE} download",
        ])
    else:
        lines.extend(f"flush chain inet {TABLE} {name}" for name in CHAINS)
    if state.local_chains:
        lines.extend(f"flush chain inet {TABLE} {name}" for name in LOCAL_CHAINS)
    elif published or local_ids:
        lines.extend(f"add chain inet {TABLE} {name} {{ type filter hook {name} priority 10; policy accept; }}"
                     for name in LOCAL_CHAINS)
    # Replacing only the maps keeps live counters and historical checkpoints intact.
    if state.exists and not state.interface_maps:
        lines.extend(f"delete map inet {TABLE} {name}" for name in MAPS)
    if not state.exists or not state.interface_maps:
        for name in MAPS:
            address_type = "ipv6_addr" if name.endswith("6") else "ipv4_addr"
            lines.append(f"add map inet {TABLE} {name} {{ type ifname . {address_type} : counter; }}")
    else:
        lines.extend(f"flush map inet {TABLE} {name}" for name in MAPS)
    counters = list(BASE_COUNTERS)
    for identity in inventory.devices:
        counters.extend((f"d_{identity}_up", f"d_{identity}_down"))
    lines.extend(f"add counter inet {TABLE} {name}" for name in counters if name not in state.counters)
    for binding in inventory.bindings.values():
        if binding.get("conflict"):
            continue
        version = 4 if binding["family"] == socket.AF_INET else 6
        address, identity = binding["address"], binding["device_id"]
        interface = binding.get('interface') or lans[0]
        for direction in ("up", "down"):
            lines.append(f'add element inet {TABLE} {direction}{version} {{ "{interface}" . {address} : "d_{identity}_{direction}" }}')
    # Count the client-facing leg only. goto ends this packet's accounting path.
    for version, family in ((4, "ip"), (6, "ip6")):
        selected = [ip for ip in proxy_ips if (":" in ip) == (version == 6)]
        if not selected:
            continue
        targets = "{ " + ", ".join(selected) + " }"
        prefix = f"add rule inet {TABLE} forward"
        lines.extend([
            f'{prefix} {family} saddr {targets} {family} daddr {targets} return',
            f'{prefix} iifname {lan_match} oifname != {wan_match} {family} daddr {targets} counter name total_up goto upload',
            f'{prefix} oifname {lan_match} iifname != {wan_match} {family} saddr {targets} counter name total_down goto download',
            f'{prefix} {family} saddr {targets} return',
            f'{prefix} {family} daddr {targets} return',
        ])
    for listener in published:
        address = ipaddress.ip_address(listener['address'])
        protocol, port = listener['protocol'], listener['port']
        if '%' in str(address) or protocol not in ('tcp', 'udp') or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Invalid proxy listener')
        family = 'ip' if address.version == 4 else 'ip6'
        nfproto = 'ipv4' if address.version == 4 else 'ipv6'
        for chain, direction, interface, field_name in (('input', 'up', 'iifname', 'daddr'),
                                                        ('output', 'down', 'oifname', 'saddr')):
            host = f'meta nfproto {nfproto}' if address.is_unspecified else f'{family} {field_name} {address}'
            port_field = 'dport' if chain == 'input' else 'sport'
            lines.append(f'add rule inet {TABLE} {chain} {interface} {lan_match} {host} {protocol} {port_field} {port} '
                         f'counter name total_{direction} goto {"upload" if direction == "up" else "download"}')
    if local_ids:
        identity = local_ids[0]
        # Local sockets use WAN addresses, so attribution must not use LAN IP maps.
        lines.extend([
            f'add rule inet {TABLE} input iifname {wan_match} counter name total_down counter name d_{identity}_down',
            f'add rule inet {TABLE} output oifname {wan_match} counter name total_up counter name d_{identity}_up',
        ])
    lines.extend([
        f'add rule inet {TABLE} forward iifname {lan_match} oifname {wan_match} counter name total_up jump upload',
        f'add rule inet {TABLE} forward iifname {wan_match} oifname {lan_match} counter name total_down jump download',
        f"add rule inet {TABLE} upload counter name iifname . ip saddr map @up4 return",
        f"add rule inet {TABLE} upload counter name iifname . ip6 saddr map @up6 return",
        f"add rule inet {TABLE} upload counter name unknown_up return",
        f"add rule inet {TABLE} download counter name oifname . ip daddr map @down4 return",
        f"add rule inet {TABLE} download counter name oifname . ip6 daddr map @down6 return",
        f"add rule inet {TABLE} download counter name unknown_down return",
    ])
    return "\n".join(lines) + "\n"


def _counter_json(counter):
    counter = counter or {}
    return {"bytes": str(counter.get("bytes", 0)), "packets": str(counter.get("packets", 0))}


def _device_json(state, inventory, identity):
    device = inventory.devices.get(identity) if inventory is not None else None
    addresses = [binding["address"] for binding in inventory.bindings.values()
                 if not binding.get("conflict") and binding.get("device_id") == identity] if inventory is not None else []
    return {"id": identity, "mac": ":".join(identity[i:i + 2] for i in range(0, 12, 2)),
            "hostname": device["hostname"] if device else "",
            **({'local': True} if device and device.get('local') else {}),
            "discovered": device is not None if inventory is not None else None,
            "addresses": list(dict.fromkeys(addresses)), "up": _counter_json(state.counters.get(f"d_{identity}_up")),
            "down": _counter_json(state.counters.get(f"d_{identity}_down"))}


def _reconcile(state, direction):
    total, unknown = state.counters.get(f"total_{direction}"), state.counters.get(f"unknown_{direction}")
    result = {"bytes_match": None, "packets_match": None, "sum_overflow": False}
    if not (state.exists and state.owned and total is not None and unknown is not None):
        return result
    for metric in ("bytes", "packets"):
        parts = unknown[metric] + sum(counter[metric] for name, counter in state.counters.items()
                                     if (device_counter_id(name) or (None, None))[1] == direction)
        if parts > UINT64_MAX:
            result["sum_overflow"] = True
        else:
            result[f"{metric}_match"] = parts == total[metric]
    return result


def report(state, inventory=None):
    identities = [device_counter_id(name)[0] for name in state.counters
                  if (device_counter_id(name) or (None, None))[1] == "up"]
    if inventory is not None:
        identities.extend(identity for identity in inventory.devices if f"d_{identity}_up" not in state.counters)
    result = {"table_exists": state.exists, "table_owned": state.owned, "generation": state.generation,
              "flowtables_present": state.flowtables_present, "snapshot_atomic": False,
              "discovery_available": inventory is not None,
              "reconciliation": {direction: _reconcile(state, direction) for direction in ("up", "down")},
              "devices": [_device_json(state, inventory, identity) for identity in identities],
              "conflicting_addresses": [binding["address"] for binding in inventory.bindings.values()
                                        if binding.get("conflict")] if inventory is not None else []}
    result.update({name: _counter_json(state.counters.get(name)) for name in BASE_COUNTERS})
    return result
