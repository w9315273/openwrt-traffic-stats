"""验证按接口发现设备, 避免隔离 LAN 中的地址冲突."""
import errno
import io
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from traffic_stats import collector, discovery, nft


class MultiDiscoveryTests(unittest.TestCase):
    def test_overlapping_addresses_are_scoped_and_names_still_loaded(self):
        inventory = discovery.Inventory()
        inventory.load_leases(io.StringIO('0 02:00:00:00:00:02 192.0.2.2 guest *\n'), interface=None)
        self.assertEqual(inventory.bindings, {})
        inventory.load_neighbor_file(io.StringIO(
            '192.0.2.2 dev lan-a lladdr 02:00:00:00:00:02 REACHABLE\n'
            '192.0.2.2 dev lan-b lladdr 02:00:00:00:00:03 STALE\n'
            '192.0.2.2 dev wan lladdr 02:00:00:00:00:04 REACHABLE\n'
            '192.0.2.3 lladdr 02:00:00:00:00:05 REACHABLE\n'), ['lan-a', 'lan-b'])
        self.assertEqual(len(inventory.bindings), 2)
        self.assertEqual(inventory.bindings['lan-a',socket.AF_INET,'192.0.2.2']['device_id'], '020000000002')
        self.assertEqual(inventory.bindings['lan-b',socket.AF_INET,'192.0.2.2']['device_id'], '020000000003')
        self.assertEqual(inventory.devices['020000000002']['hostname'], 'guest')
        inventory.add_neighbor(socket.AF_INET, '192.0.2.2', '02:00:00:00:00:04', discovery.NUD_REACHABLE, 'lan-a')
        self.assertTrue(inventory.bindings['lan-a',socket.AF_INET,'192.0.2.2']['conflict'])
        self.assertFalse(inventory.bindings['lan-b',socket.AF_INET,'192.0.2.2']['conflict'])

    def test_report_interfaces_follow_valid_bindings(self):
        inventory = discovery.Inventory()
        for family, address, interface in [
            (socket.AF_INET, '192.0.2.2', 'br-lan'),
            (socket.AF_INET6, '2001:db8::2', 'br-lan'),
            (socket.AF_INET, '192.0.2.2', 'iot')]:
            inventory.add_neighbor(family, address, '02:00:00:00:00:02', discovery.NUD_REACHABLE, interface)
        inventory.add_neighbor(socket.AF_INET, '192.0.2.2', '02:00:00:00:00:03', discovery.NUD_REACHABLE, 'iot')
        devices = {item['id']: item for item in nft.report(nft.NftState(), inventory)['devices']}
        self.assertEqual(devices['020000000002']['interfaces'], ['br-lan'])
        self.assertEqual(devices['020000000003']['interfaces'], [])

    def test_missing_lan_does_not_stop_other_interfaces(self):
        options = collector.parse_options(['sync','--lan','absent','--lan','lan-b','--wan','wan','--leases','/dev/null'])
        def neighbors(inventory, interface):
            if interface == 'absent':
                raise OSError(errno.ENODEV, 'gone')
            inventory.add_neighbor(socket.AF_INET,'192.0.2.2','02:00:00:00:00:03',discovery.NUD_REACHABLE, interface)
        with patch.object(discovery.Inventory,'load_neighbors',neighbors), patch.object(discovery.Inventory,'load_local'):
            inventory, _ = collector.read_inventory(options)
        self.assertIn(('lan-b',socket.AF_INET,'192.0.2.2'), inventory.bindings)

    def test_cli_lists_deduplicate_and_reject_overlap(self):
        options = collector.parse_options(['sync','--lan','lan-a','--lan','lan-a','--lan','lan-b','--wan','wan-a','--wan','wan-b'])
        self.assertEqual(options.lan,['lan-a','lan-b'])
        self.assertEqual(options.wan,['wan-a','wan-b'])
        with self.assertRaises(SystemExit), patch('sys.stderr',io.StringIO()):
            collector.parse_options(['sync','--lan','lan-a','--wan','lan-a'])

    def test_local_identity_stays_one_across_different_lan_macs(self):
        inventory = discovery.Inventory()
        def ioctl(channel, operation, request):
            second = request.startswith(b'lan-b')
            value = bytearray(256)
            if operation == 0x8927:
                value[18:24] = bytes.fromhex('020000000020' if second else '020000000010')
            else:
                value[20:24] = socket.inet_pton(socket.AF_INET, '198.51.100.1' if second else '192.0.2.1')
            return bytes(value)
        with patch.object(discovery.socket,'socket'), patch.object(discovery.fcntl,'ioctl',ioctl), patch.object(Path,'read_text',return_value=''):
            inventory.load_local('lan-a')
            inventory.load_local('lan-b')
        self.assertEqual(list(inventory.devices), ['020000000010'])
        self.assertEqual({binding['device_id'] for binding in inventory.bindings.values()}, {'020000000010'})
        self.assertEqual({binding['interface'] for binding in inventory.bindings.values()}, {'lan-a','lan-b'})
