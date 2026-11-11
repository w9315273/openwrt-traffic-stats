import errno
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from traffic_stats import discovery, nft


class RouterTests(unittest.TestCase):
    def setUp(self):
        sockets = patch.object(discovery.socket, 'socket')
        sockets.start()
        self.addCleanup(sockets.stop)

    def ioctl(self, channel, operation, request):
        value = bytearray(256)
        if operation == 0x8927:
            value[18:24] = bytes.fromhex('020000000001')
        else:
            value[20:24] = socket.inet_pton(socket.AF_INET, '192.0.2.1')
        return bytes(value)

    def test_local_identity_addresses_and_own_address_priority(self):
        inventory = discovery.Inventory()
        inventory.add_neighbor(socket.AF_INET, '192.0.2.1', '02:00:00:00:00:02', discovery.NUD_REACHABLE, 'lan')
        records = ('20010db8000000000000000000000001 02 40 00 00 lan\n'
                   'fe800000000000000000000000000001 02 40 20 00 lan\n'
                   '20010db8000000000000000000000002 02 40 00 40 lan\n')
        with patch.object(discovery.fcntl, 'ioctl', self.ioctl), patch.object(discovery.socket, 'gethostname', return_value='OpenWrt'), patch.object(Path, 'read_text', return_value=records):
            inventory.load_local('lan')
        device = inventory.devices['020000000001']
        self.assertTrue(device['local'])
        self.assertEqual(device['hostname'], 'OpenWrt')
        self.assertEqual(set(address for interface, family, address in inventory.bindings), {'192.0.2.1', '2001:db8::1'})
        self.assertEqual(inventory.bindings['lan', socket.AF_INET, '192.0.2.1']['device_id'], device['id'])
        transaction = nft.plan(inventory, nft.NftState(), 'lan', 'wan', '11111111-1111-1111-1111-111111111111')
        self.assertIn('input iifname "wan" counter name total_down counter name d_020000000001_down', transaction)
        self.assertIn('output oifname "wan" counter name total_up counter name d_020000000001_up', transaction)

    def test_ipv6_only_and_non_ethernet_do_not_break_discovery(self):
        inventory = discovery.Inventory()
        def ipv6_only(channel, operation, request):
            if operation == 0x8915:
                raise OSError(errno.EADDRNOTAVAIL, 'No IPv4 address')
            return self.ioctl(channel, operation, request)
        with patch.object(discovery.fcntl, 'ioctl', ipv6_only), patch.object(Path, 'read_text', return_value='20010db8000000000000000000000001 02 40 00 00 lan\n'):
            inventory.load_local('lan')
        self.assertTrue(inventory.devices['020000000001']['local'])
        self.assertEqual(len(inventory.bindings), 1)
        empty = discovery.Inventory()
        with patch.object(discovery.fcntl, 'ioctl', return_value=bytes(256)):
            empty.load_local('lan')
        self.assertEqual(empty.devices, {})


if __name__ == '__main__':
    unittest.main()
