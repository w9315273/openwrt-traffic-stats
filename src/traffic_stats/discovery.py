"""Build a device inventory from DHCP leases and kernel neighbor snapshots."""

import errno
import fcntl
from pathlib import Path
import re
import socket
import struct
import time


UINT64_MAX = (1 << 64) - 1
PRIORITY_LEASE = 1
PRIORITY_NEIGHBOR = 2
PRIORITY_LOCAL = 3
NUD_INCOMPLETE = 0x01
NUD_REACHABLE = 0x02
NUD_STALE = 0x04
NUD_DELAY = 0x08
NUD_PROBE = 0x10
NUD_FAILED = 0x20
NUD_NOARP = 0x40
NUD_PERMANENT = 0x80
STATES = {
    "INCOMPLETE": NUD_INCOMPLETE, "REACHABLE": NUD_REACHABLE,
    "STALE": NUD_STALE, "DELAY": NUD_DELAY, "PROBE": NUD_PROBE,
    "FAILED": NUD_FAILED, "NOARP": NUD_NOARP, "PERMANENT": NUD_PERMANENT,
}
USABLE_STATES = NUD_REACHABLE | NUD_STALE | NUD_DELAY | NUD_PROBE | NUD_NOARP | NUD_PERMANENT
NL_HEADER = struct.Struct("=IHHII")
NEIGHBOR = struct.Struct("=BBHiHBB")
ATTRIBUTE = struct.Struct("=HH")


def normalize_mac(value: str):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", value):
        return None
    octets = bytes.fromhex(value.replace(":", ""))
    if not any(octets) or octets[0] & 1:
        return None
    return value.lower(), octets.hex()


def normalize_address(family: int, value: str):
    if family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(value, str):
        return None
    try:
        address = socket.inet_pton(family, value)
    except (ValueError, OSError):
        return None
    if family == socket.AF_INET:
        if address[0] in (0, 127) or address[0] >= 224:
            return None
    elif (not any(address) or address == b"\0" * 15 + b"\1" or
          address[0] == 0xff or (address[0] == 0xfe and address[1] & 0xc0 == 0x80) or
          address[:12] == b"\0" * 10 + b"\xff\xff"):
        return None
    return socket.inet_ntop(family, address)


def _lines(stream):
    """Skip oversized records without allocating their entire contents."""
    while True:
        line = stream.readline(4096)
        if not line:
            return
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        if len(line) >= 4095 and not line.endswith("\n"):
            while line and not line.endswith("\n"):
                line = stream.readline(4096)
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
            continue
        if "\0" not in line:
            yield line


def _protocol_error(message="Malformed neighbor dump"):
    return OSError(errno.EPROTO, message)


class Inventory:
    def __init__(self, devices=None, bindings=None):
        self.devices = devices if devices is not None else {}
        self.bindings = bindings if bindings is not None else {}

    def _copy(self):
        return Inventory({key: dict(value) for key, value in self.devices.items()},
                         {key: dict(value) for key, value in self.bindings.items()})

    def _commit(self, other):
        self.devices, self.bindings = other.devices, other.bindings

    def _add_binding(self, family, address, mac, hostname, priority, interface=""):
        normalized_mac = normalize_mac(mac)
        address = normalize_address(family, address)
        if normalized_mac is None or address is None:
            return
        mac, identity = normalized_mac
        device = self.devices.setdefault(identity, {"id": identity, "mac": mac, "hostname": ""})
        if hostname is not None and hostname != "*" and len(hostname.encode("utf-8")) < 256:
            device["hostname"] = hostname
        # DHCP leases lack interface information; multi-LAN leases supply names only.
        if interface is None:
            return
        key = (interface, family, address) if interface else (family, address)
        binding = self.bindings.get(key)
        if binding is None:
            self.bindings[key] = {"interface": interface, "family": family, "address": address, "device_id": identity,
                                  "conflict": False, "priority": priority}
        elif priority > binding["priority"]:
            binding.update(device_id=identity, conflict=False, priority=priority)
        elif priority == binding["priority"] and binding["device_id"] != identity:
            # Contradictory observations of equal strength remain unresolved for this round.
            binding.update(device_id="", conflict=True)

    def load_leases(self, stream, now=None, interface=""):
        now = int(time.time()) if now is None else now
        candidate = self._copy()
        for line in _lines(stream):
            fields = line.split()
            if len(fields) < 4 or not re.fullmatch(r"[0-9]{1,20}", fields[0]):
                continue
            expires = int(fields[0])
            if expires > UINT64_MAX or (expires and now >= 0 and expires <= now):
                continue
            candidate._add_binding(socket.AF_INET, fields[2], fields[1], fields[3], PRIORITY_LEASE, interface)
        self._commit(candidate)

    def add_neighbor(self, family, address, mac, state, interface=""):
        if state & USABLE_STATES and not state & (NUD_FAILED | NUD_INCOMPLETE):
            self._add_binding(family, address, mac, None, PRIORITY_NEIGHBOR, interface)

    def load_local(self, lan):
        # The LAN MAC keeps local history compatible with normal device identities.
        request = struct.pack('256s', lan.encode('ascii'))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
            hardware = fcntl.ioctl(channel, 0x8927, request)  # SIOCGIFHWADDR
            normalized = normalize_mac(':'.join(f'{part:02x}' for part in hardware[18:24]))
            if normalized is None:
                return
            local = next((device for device in self.devices.values() if device.get('local')), None)
            mac, identity = (local['mac'], local['id']) if local else normalized
            hostname = socket.gethostname()
            if not hostname or len(hostname.encode('utf-8')) >= 256:
                hostname = 'OpenWrt'
            self.devices[identity] = {'id': identity, 'mac': mac, 'hostname': hostname, 'local': True}
            try:
                value = fcntl.ioctl(channel, 0x8915, request)  # SIOCGIFADDR
            except OSError as error:
                if error.errno != errno.EADDRNOTAVAIL:
                    raise
            else:
                self._add_binding(socket.AF_INET, socket.inet_ntop(socket.AF_INET, value[20:24]),
                                  mac, hostname, PRIORITY_LOCAL, lan)
        try:
            records = Path('/proc/net/if_inet6').read_text().splitlines()
        except FileNotFoundError:
            records = []
        for record in records:
            fields = record.split()
            if len(fields) == 6 and fields[5] == lan and not int(fields[4], 16) & 0x48:
                address = socket.inet_ntop(socket.AF_INET6, bytes.fromhex(fields[0]))
                self._add_binding(socket.AF_INET6, address, mac, hostname, PRIORITY_LOCAL, lan)

    def load_neighbor_file(self, stream, interfaces=None):
        candidate = self._copy()
        for line in _lines(stream):
            fields = line.split()
            if not fields:
                continue
            address, mac, state = fields[0], None, 0
            interface = interfaces[0] if interfaces and len(interfaces) == 1 else ""
            tokens = iter(fields[1:])
            for token in tokens:
                if token == "dev":
                    interface = next(tokens, "")
                elif token == "lladdr":
                    mac = next(tokens, None)
                else:
                    state |= STATES.get(token, 0)
            if mac is not None and (interfaces is None or interface in interfaces):
                family = socket.AF_INET6 if ":" in address else socket.AF_INET
                candidate.add_neighbor(family, address, mac, state, interface)
        self._commit(candidate)

    def _neighbor_message(self, payload, ifindex, interface=""):
        if len(payload) < NEIGHBOR.size:
            raise _protocol_error()
        family, _, _, index, state, _, _ = NEIGHBOR.unpack_from(payload)
        if index != ifindex or family not in (socket.AF_INET, socket.AF_INET6):
            return
        address, mac, offset = None, None, NEIGHBOR.size
        while offset < len(payload):
            if len(payload) - offset < ATTRIBUTE.size:
                raise _protocol_error()
            length, kind = ATTRIBUTE.unpack_from(payload, offset)
            if length < ATTRIBUTE.size or offset + length > len(payload):
                raise _protocol_error()
            value = payload[offset + ATTRIBUTE.size:offset + length]
            if kind == 1:  # NDA_DST
                if len(value) != (4 if family == socket.AF_INET else 16):
                    raise _protocol_error()
                address = socket.inet_ntop(family, value)
            elif kind == 2 and len(value) == 6:  # NDA_LLADDR
                mac = ":".join(f"{octet:02x}" for octet in value)
            offset += (length + 3) & ~3
        if offset != len(payload):
            raise _protocol_error()
        if address is not None and mac is not None:
            self.add_neighbor(family, address, mac, state, interface)

    def load_neighbors(self, lan):
        try:
            ifindex = socket.if_nametoindex(lan)
        except OSError as error:
            raise OSError(error.errno or errno.ENODEV, "LAN device is unavailable: " + lan) from error
        candidate = self._copy()
        sequence = 1
        request = NL_HEADER.pack(NL_HEADER.size + NEIGHBOR.size, 30, 0x301, sequence, 0)
        request += NEIGHBOR.pack(socket.AF_UNSPEC, 0, 0, ifindex, 0, 0, 0)
        with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as netlink:
            netlink.bind((0, 0))
            netlink.settimeout(5)
            netlink.sendto(request, (0, 0))
            while True:
                data, _, flags, sender = netlink.recvmsg(65536)
                if not data or flags & socket.MSG_TRUNC:
                    raise OSError(errno.EOVERFLOW, "Truncated neighbor dump")
                if sender[0] != 0:
                    continue
                offset = 0
                while offset < len(data):
                    if len(data) - offset < NL_HEADER.size:
                        raise _protocol_error()
                    length, kind, flags, seq, _ = NL_HEADER.unpack_from(data, offset)
                    if length < NL_HEADER.size or offset + length > len(data):
                        raise _protocol_error()
                    payload = data[offset + NL_HEADER.size:offset + length]
                    offset += (length + 3) & ~3
                    if seq != sequence:
                        continue
                    if flags & 0x10:  # NLM_F_DUMP_INTR
                        raise OSError(errno.EINTR, "Neighbor dump interrupted")
                    if kind == 3:  # NLMSG_DONE
                        if len(payload) >= 4:
                            error = struct.unpack_from("=i", payload)[0]
                            if error:
                                raise OSError(-error if error < 0 else errno.EPROTO,
                                              "Neighbor dump failed")
                        self._commit(candidate)
                        return
                    if kind == 2:  # NLMSG_ERROR
                        if len(payload) < 4 + NL_HEADER.size:
                            raise _protocol_error()
                        error = struct.unpack_from("=i", payload)[0]
                        if error:
                            raise OSError(-error if error < 0 else errno.EPROTO,
                                          "Neighbor dump failed")
                    elif kind == 4:  # NLMSG_OVERRUN
                        raise OSError(errno.ENOBUFS, "Neighbor dump overrun")
                    elif kind == 28:  # RTM_NEWNEIGH
                        candidate._neighbor_message(payload, ifindex, lan)
                if offset != len(data):
                    raise _protocol_error()
