"""验证按接口归属流量及原子升级时保留已有计数."""
from copy import deepcopy
from pathlib import Path
import socket
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from traffic_stats import discovery, nft
from test_python_nft import fixture, GENERATION


def inventory():
    devices, bindings = {}, {}
    for interface, identity in [('lan', '020000000002'), ('guest', '020000000003')]:
        devices[identity] = {'id': identity, 'mac': ':'.join(identity[i:i + 2] for i in range(0, 12, 2)), 'hostname': ''}
        for family, address in ((socket.AF_INET, '192.0.2.2'), (socket.AF_INET6, '2001:db8::2')):
            bindings[interface, family, address] = {'interface': interface, 'family': family,
                'address': address, 'device_id': identity, 'conflict': False, 'priority': 2}
    return discovery.Inventory(devices, bindings)


def scoped_fixture():
    data = fixture()
    for entry in data['nftables']:
        if 'map' in entry:
            entry['map']['type'] = ['ifname', entry['map']['type']]
    return data


class MultiNftTests(unittest.TestCase):
    def test_overlapping_addresses_have_separate_interface_keys(self):
        plan = nft.plan(inventory(), nft.NftState(), ['lan', 'guest'], ['wan', 'wwan'], GENERATION)
        self.assertIn('iifname { "lan", "guest" } oifname { "wan", "wwan" }', plan)
        self.assertIn('iifname { "wan", "wwan" } oifname { "lan", "guest" }', plan)
        for interface, identity in [('lan', '020000000002'), ('guest', '020000000003')]:
            self.assertIn(f'up4 {{ "{interface}" . 192.0.2.2 : "d_{identity}_up" }}', plan)
            self.assertIn(f'down6 {{ "{interface}" . 2001:db8::2 : "d_{identity}_down" }}', plan)
        self.assertIn('upload counter name iifname . ip saddr map @up4', plan)
        self.assertIn('download counter name oifname . ip6 daddr map @down6', plan)
        self.assertNotIn('iifname { "lan", "guest" } oifname { "lan", "guest" }', plan)

    def test_legacy_map_upgrade_keeps_generation_and_named_counters(self):
        data = fixture()
        for chain in nft.LOCAL_CHAINS:
            data['nftables'].append({'chain': {'family': 'inet', 'table': nft.TABLE, 'name': chain,
                'type': 'filter', 'hook': chain, 'prio': 10, 'policy': 'accept'}})
        state = nft.parse(data)
        self.assertFalse(state.interface_maps)
        original = deepcopy(state.counters)
        plan = nft.plan(discovery.Inventory(), state, ['lan', 'guest'], ['wan', 'wwan'], '')
        self.assertLess(plan.index('flush chain inet traffic_stats output'), plan.index('delete map'))
        self.assertIn('add map inet traffic_stats up4 { type ifname . ipv4_addr : counter; }', plan)
        for token in ('reset', 'delete counter', 'delete table', 'flush table', 'add counter'):
            self.assertNotIn(token, plan)
        self.assertEqual(state.counters, original)
        self.assertEqual(state.generation, GENERATION)

    def test_existing_scoped_schema_uses_flush_and_rejects_mixed_schema(self):
        state = nft.parse(scoped_fixture())
        self.assertTrue(state.interface_maps)
        plan = nft.plan(discovery.Inventory(), state, ['lan', 'guest'], ['wan'], '')
        self.assertIn('flush map inet traffic_stats up4', plan)
        self.assertNotIn('delete map', plan)
        mixed = scoped_fixture()
        mixed['nftables'][4]['map']['type'] = 'ipv4_addr'
        with self.assertRaises(OSError):
            nft.parse(mixed)
        bad = scoped_fixture()
        bad['nftables'][4]['map']['type'] = ['ifname', 'inet_service']
        with self.assertRaises(OSError):
            nft.parse(bad)

    def test_invalid_interface_sets_and_unscoped_ambiguous_bindings_rejected(self):
        for lans, wans in (([], ['wan']), (['lan'], []), (['lan'], ['lan']),
                           (['lan', 'bad;'], ['wan']), (['lan'], ['wan', 'lan'])):
            with self.subTest(lans=lans, wans=wans), self.assertRaises(ValueError):
                nft.plan(inventory(), nft.NftState(), lans, wans, GENERATION)
        unscoped = discovery.Inventory()
        unscoped.add_neighbor(socket.AF_INET, '192.0.2.2', '02:00:00:00:00:02', discovery.NUD_REACHABLE)
        with self.assertRaises(ValueError):
            nft.plan(unscoped, nft.NftState(), ['lan', 'guest'], ['wan'], GENERATION)
        broken = inventory()
        key = next(iter(broken.bindings))
        broken.bindings[key]['interface'] = 'lan; flush ruleset'
        with self.assertRaises(ValueError):
            nft.plan(broken, nft.NftState(), ['lan', 'guest'], ['wan'], GENERATION)

    def test_proxy_and_local_paths_cover_all_configured_interfaces(self):
        devices = inventory()
        devices.devices['020000000001'] = {'id': '020000000001', 'hostname': 'OpenWrt', 'local': True}
        plan = nft.plan(devices, nft.NftState(), ['lan', 'guest'], ['wan', 'wwan'], GENERATION,
                        ['172.18.0.2'], [{'address': '0.0.0.0', 'protocol': 'tcp', 'port': 1080}])
        self.assertIn('iifname { "lan", "guest" } oifname != { "wan", "wwan" } ip daddr { 172.18.0.2 }', plan)
        self.assertIn('input iifname { "lan", "guest" } meta nfproto ipv4 tcp dport 1080', plan)
        self.assertIn('output oifname { "lan", "guest" } meta nfproto ipv4 tcp sport 1080', plan)
        self.assertIn('input iifname { "wan", "wwan" } counter name total_down counter name d_020000000001_down', plan)
        self.assertIn('output oifname { "wan", "wwan" } counter name total_up counter name d_020000000001_up', plan)


if __name__ == '__main__':
    unittest.main()
