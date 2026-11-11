#!/usr/bin/env python3
"""验证设备发现, netlink 消息边界及 nftables 状态解析."""

import copy
import errno
import io
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from traffic_stats import discovery, nft


GENERATION = "12345678-1234-1234-1234-123456789abc"
MAC = "84:2f:57:29:06:1f"
MAC2 = "90:4c:c5:42:8f:ac"


def fixture():
    table = {"family": "inet", "table": "traffic_stats"}
    entries = [
        {"table": {"family": "inet", "name": "traffic_stats", "comment": nft.OWNER_PREFIX + GENERATION}},
        {"chain": dict(table, name="forward", type="filter", hook="forward", prio=10, policy="accept")},
        {"chain": dict(table, name="upload")}, {"chain": dict(table, name="download")},
    ]
    for name in nft.MAPS:
        entries.append({"map": dict(table, name=name, type="ipv6_addr" if name.endswith("6") else "ipv4_addr", map="counter")})
    for name in (*nft.BASE_COUNTERS, "d_001122334455_up", "d_001122334455_down"):
        entries.append({"counter": dict(table, name=name, packets=1, bytes=nft.UINT64_MAX)})
    return {"nftables": entries}


class DiscoveryTests(unittest.TestCase):
    def test_leases_validate_expiration_addresses_and_identity(self):
        inventory = discovery.Inventory()
        inventory.load_leases(io.StringIO(
            f"2000 {MAC.upper()} 192.168.2.2 MacBookPro *\n"
            f"999 {MAC2} 192.168.2.3 expired *\n"
            f"1000 {MAC2} 192.168.2.4 expired-now *\n"
            f"0 {MAC2} 192.168.2.5 * *\n"
            f"-1 {MAC2} 192.168.2.6 invalid *\n"
            f"999999999999999999999999 {MAC2} 192.168.2.7 overflow *\n"
            "0 ff:ff:ff:ff:ff:ff 192.168.2.8 invalid *\n"
            "0 00:00:00:00:00:00 192.168.2.9 invalid *\n"
            + "".join(f"0 {MAC2} {address} invalid *\n" for address in
                      ("127.0.0.1", "224.0.0.1", "0.0.0.0", "999.0.0.1"))
            + f"0\n\n2000 {MAC} 192.168.2.2 MacBookPro *\n"), 1000)
        self.assertEqual(len(inventory.devices), 2)
        self.assertEqual(len(inventory.bindings), 2)
        self.assertEqual(inventory.devices["842f5729061f"], {"id": "842f5729061f", "mac": MAC, "hostname": "MacBookPro"})
        self.assertEqual(inventory.devices["904cc5428fac"]["hostname"], "")

    def test_dual_stack_and_usable_neighbor_states(self):
        inventory = discovery.Inventory()
        inventory.load_neighbor_file(io.StringIO(
            f"192.168.2.2 dev br-lan lladdr {MAC} STALE\n"
            f"2001:0DB8:0000:0000:0000:0000:0000:0002 lladdr {MAC} REACHABLE\n"
            f"2001:db8::abcd lladdr {MAC} DELAY\n"
            f"fd00::2 lladdr {MAC} PROBE\n"
            f"192.168.2.3 lladdr {MAC2} PERMANENT\n"
            f"192.168.2.4 lladdr {MAC2} NOARP\n"
            f"192.168.2.5 FAILED\n192.168.2.6 lladdr {MAC2} FAILED\n"
            f"192.168.2.7 lladdr {MAC2} INCOMPLETE\n"
            f"192.168.2.8 lladdr {MAC2}\n192.168.2.9 lladdr\n"
            + "".join(f"{address} lladdr {MAC} REACHABLE\n" for address in
                      ("fe80::2", "ff02::1", "::", "::1", "::ffff:192.168.2.2"))))
        self.assertEqual(len(inventory.devices), 2)
        self.assertEqual(len(inventory.bindings), 6)
        self.assertEqual(inventory.bindings[(socket.AF_INET6, "2001:db8::2")]["device_id"], "842f5729061f")

    def test_conflict_is_sticky_until_stronger_or_fresh_observation(self):
        inventory = discovery.Inventory()
        inventory.load_leases(io.StringIO(
            f"2000 {MAC} 192.168.2.2 old *\n2000 {MAC2} 192.168.2.2 new *\n"
            f"2000 {MAC} 192.168.2.2 old *\n"), 1000)
        key = (socket.AF_INET, "192.168.2.2")
        self.assertTrue(inventory.bindings[key]["conflict"])
        inventory.add_neighbor(socket.AF_INET, key[1], MAC2, discovery.NUD_REACHABLE)
        self.assertFalse(inventory.bindings[key]["conflict"])
        inventory.load_leases(io.StringIO(f"2000 {MAC} 192.168.2.2 old *\n"), 1000)
        self.assertEqual(inventory.bindings[key]["device_id"], "904cc5428fac")
        inventory.add_neighbor(socket.AF_INET, key[1], MAC, discovery.NUD_STALE)
        inventory.add_neighbor(socket.AF_INET, key[1], MAC2, discovery.NUD_REACHABLE)
        self.assertTrue(inventory.bindings[key]["conflict"])
        fresh = discovery.Inventory()
        fresh.add_neighbor(socket.AF_INET, key[1], MAC2, discovery.NUD_REACHABLE)
        self.assertFalse(fresh.bindings[key]["conflict"])

    def test_oversized_and_injectable_records_are_ignored(self):
        inventory = discovery.Inventory()
        inventory.load_leases(io.StringIO("a" * 20000 + f"\n2000 {MAC} 192.168.2.2 valid *\n"), 1000)
        inventory.add_neighbor(socket.AF_INET, "192.168.2.3", MAC2 + ";bad", discovery.NUD_REACHABLE)
        inventory.add_neighbor(socket.AF_INET, "192.168.2.3;bad", MAC2, discovery.NUD_REACHABLE)
        self.assertEqual(len(inventory.bindings), 1)

    def test_io_failure_does_not_commit_partial_inventory(self):
        class FailingStream:
            calls = 0
            def readline(self, limit):
                self.calls += 1
                if self.calls == 1:
                    return f"0 {MAC} 192.168.2.2 partial *\n"
                raise OSError(errno.EIO, "test read failure")
        inventory = discovery.Inventory()
        with self.assertRaises(OSError):
            inventory.load_leases(FailingStream(), 1000)
        self.assertFalse(inventory.devices)
        with self.assertRaises(OSError):
            inventory.load_neighbors("ts_missing_dev")

    def test_hostname_uses_byte_bound(self):
        inventory = discovery.Inventory()
        inventory.load_leases(io.StringIO(f"0 {MAC} 192.168.2.2 {'字' * 86} *\n"), 1000)
        self.assertEqual(inventory.devices["842f5729061f"]["hostname"], "")


def netlink_message(kind, payload=b"", flags=0, sequence=1):
    return discovery.NL_HEADER.pack(discovery.NL_HEADER.size + len(payload), kind, flags, sequence, 0) + payload


def neighbor_payload(ifindex=2):
    def attribute(kind, value):
        data = discovery.ATTRIBUTE.pack(4 + len(value), kind) + value
        return data + b"\0" * (-len(data) % 4)
    return (discovery.NEIGHBOR.pack(socket.AF_INET, 0, 0, ifindex, discovery.NUD_REACHABLE, 0, 0) +
            attribute(1, socket.inet_pton(socket.AF_INET, "192.0.2.2")) +
            attribute(2, bytes.fromhex("020000000002")))


class NetlinkTests(unittest.TestCase):
    def run_dump(self, packets, inventory=None):
        inventory = inventory or discovery.Inventory()
        with patch.object(socket, "if_nametoindex", return_value=2), patch.object(socket, "socket") as factory:
            connection = factory.return_value.__enter__.return_value
            connection.recvmsg.side_effect = packets
            inventory.load_neighbors("lan")
            request = connection.sendto.call_args.args[0]
            self.assertEqual(discovery.NL_HEADER.unpack_from(request)[1:4], (30, 0x301, 1))
        return inventory

    def test_native_dump_parses_kernel_attributes(self):
        inventory = self.run_dump([(netlink_message(28, neighbor_payload()) + netlink_message(3), [], 0, (0, 0))])
        self.assertEqual(inventory.bindings[("lan", socket.AF_INET, "192.0.2.2")]["device_id"], "020000000002")

    def test_other_interfaces_senders_and_sequences_are_ignored(self):
        inventory = self.run_dump([
            (netlink_message(28, neighbor_payload()), [], 0, (777, 0)),
            (netlink_message(28, neighbor_payload(), sequence=9), [], 0, (0, 0)),
            (netlink_message(28, neighbor_payload(3)) + netlink_message(3), [], 0, (0, 0))])
        self.assertFalse(inventory.devices)

    def test_failed_dump_keeps_previous_snapshot(self):
        failures = [
            (netlink_message(3, flags=0x10), 0),
            (netlink_message(3, struct.pack("=i", -errno.EBUSY)), 0),
            (netlink_message(2, b"\0"), 0),
            (netlink_message(4), 0),
            (netlink_message(28, b"short"), 0),
            (netlink_message(3), socket.MSG_TRUNC),
            (b"short", 0),
        ]
        for packet, flags in failures:
            with self.subTest(packet=packet, flags=flags):
                inventory = discovery.Inventory()
                with self.assertRaises(OSError):
                    self.run_dump([(netlink_message(28, neighbor_payload()), [], 0, (0, 0)),
                                   (packet, [], flags, (0, 0))], inventory)
                self.assertFalse(inventory.devices)


class NftTests(unittest.TestCase):
    def test_new_table_plan_and_address_conflicts(self):
        state = nft.parse({"nftables": [{"flowtable": {"family": "inet", "table": "fw4", "name": "ft"}}]})
        self.assertFalse(state.exists)
        self.assertTrue(state.flowtables_present)
        inventory = discovery.Inventory()
        inventory.add_neighbor(socket.AF_INET, "192.168.2.2", MAC, discovery.NUD_REACHABLE)
        inventory.add_neighbor(socket.AF_INET6, "2001:db8::1", MAC, discovery.NUD_REACHABLE)
        inventory.add_neighbor(socket.AF_INET6, "2001:db8::2", MAC, discovery.NUD_STALE)
        inventory.add_neighbor(socket.AF_INET, "192.168.2.99", MAC, discovery.NUD_STALE)
        inventory.add_neighbor(socket.AF_INET, "192.168.2.99", MAC2, discovery.NUD_STALE)
        plan = nft.plan(inventory, state, "br-lan", "macvlan0", GENERATION)
        self.assertIn(f'create table inet traffic_stats {{ comment "{nft.OWNER_PREFIX}{GENERATION}"; }}', plan)
        self.assertIn('up6 { "br-lan" . 2001:db8::1 : "d_842f5729061f_up" }', plan)
        self.assertNotIn("192.168.2.99", plan)
        self.assertIn("upload counter name iifname . ip saddr map @up4 return", plan)
        self.assertIn("download counter name unknown_down return", plan)

    def test_existing_counters_are_preserved_and_strings_remain_exact(self):
        state = nft.parse(fixture())
        self.assertEqual(state.generation, GENERATION)
        plan = nft.plan(discovery.Inventory(), state, "br-lan", "macvlan0", "")
        self.assertIn("delete map inet traffic_stats up4\n", plan)
        for forbidden in ("add counter", "reset", "delete counter", "delete table", "flush table"):
            self.assertNotIn(forbidden, plan)
        report = nft.report(state, discovery.Inventory())
        self.assertEqual(report["total_up"]["bytes"], str(nft.UINT64_MAX))
        self.assertEqual(report["devices"], [])
        self.assertEqual(nft.report(state)["devices"][0]["mac"], "00:11:22:33:44:55")
        self.assertEqual(report["reconciliation"]["up"], {"bytes_match": None, "packets_match": False, "sum_overflow": True})

    def test_reconciliation_includes_retained_devices(self):
        state = nft.parse(fixture())
        values = {"total_up": (nft.UINT64_MAX, 3), "unknown_up": (nft.UINT64_MAX - 100, 1),
                  "d_001122334455_up": (100, 2), "total_down": (30, 3), "unknown_down": (10, 1),
                  "d_001122334455_down": (20, 2)}
        for name, (bytes_, packets) in values.items():
            state.counters[name].update(bytes=bytes_, packets=packets)
        report = nft.report(state)
        self.assertFalse(report["discovery_available"])
        self.assertIsNone(report["devices"][0]["discovered"])
        for direction in ("up", "down"):
            self.assertEqual(report["reconciliation"][direction], {"bytes_match": True, "packets_match": True, "sum_overflow": False})
        state.counters["total_up"]["bytes"] -= 1
        state.counters["unknown_up"]["packets"] = nft.UINT64_MAX
        self.assertEqual(nft.report(state)["reconciliation"]["up"], {"bytes_match": False, "packets_match": None, "sum_overflow": True})

    def test_unowned_and_injectable_inputs_never_create_plan(self):
        state = nft.parse({"nftables": [{"table": {"family": "inet", "name": "traffic_stats"}}]})
        self.assertTrue(state.exists)
        self.assertFalse(state.owned)
        with self.assertRaises(FileExistsError):
            nft.plan(discovery.Inventory(), state, "lan", "wan", GENERATION)
        for lan, wan, generation in [('lan";', "wan", GENERATION), ("lan", "lan", GENERATION), ("lan", "wan", "bad")]:
            with self.subTest(lan=lan, wan=wan, generation=generation), self.assertRaises(ValueError):
                nft.plan(discovery.Inventory(), nft.NftState(), lan, wan, generation)

    def test_counter_numeric_bounds_and_types(self):
        for key in ("bytes", "packets", "handle"):
            for bad in (-1, 1.5, "123", None, True, nft.UINT64_MAX + 1):
                data = fixture()
                data["nftables"][8]["counter"][key] = bad
                with self.subTest(key=key, value=bad), self.assertRaises(OSError):
                    nft.parse(data)

    def test_incompatible_managed_structure_is_rejected(self):
        bad_data = []
        data = fixture(); data["nftables"].append(copy.deepcopy(data["nftables"][0])); bad_data.append(data)
        data = fixture(); data["nftables"].pop(4); bad_data.append(data)
        data = fixture(); data["nftables"].pop(); bad_data.append(data)
        data = fixture(); data["nftables"][1]["chain"]["prio"] = True; bad_data.append(data)
        data = fixture(); data["nftables"][2]["chain"]["hook"] = "input"; bad_data.append(data)
        data = fixture(); data["nftables"][4]["map"]["flags"] = []; bad_data.append(data)
        data = fixture(); data["nftables"].append({"set": {"family": "inet", "table": "traffic_stats", "name": "unexpected"}}); bad_data.append(data)
        data = fixture(); data["nftables"].append({"rule": {"family": "inet", "table": "traffic_stats", "chain": "unexpected"}}); bad_data.append(data)
        bad_data.extend(({}, {"nftables": {}}, {"nftables": [None]}))
        for data in bad_data:
            with self.subTest(data=data), self.assertRaises(OSError):
                nft.parse(data)

    def test_inventory_values_cannot_inject_nft_syntax(self):
        inventory = discovery.Inventory()
        inventory.add_neighbor(socket.AF_INET, "192.0.2.2", MAC, discovery.NUD_REACHABLE)
        inventory.bindings[(socket.AF_INET, "192.0.2.2")]["address"] = "192.0.2.2; flush ruleset"
        with self.assertRaises(ValueError):
            nft.plan(inventory, nft.NftState(), "lan", "wan", GENERATION)


if __name__ == "__main__":
    unittest.main()
