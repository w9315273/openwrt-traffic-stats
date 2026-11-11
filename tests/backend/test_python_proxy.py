import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from traffic_stats import config, proxy, collector, nft, discovery


class ProxyTests(unittest.TestCase):
    def test_normalization_and_duplicate_addresses(self):
        self.assertEqual(proxy.addresses(['172.18.0.2', '2001:db8:0::2', '2001:db8::2']),
                         ['172.18.0.2', '2001:db8::2'])
        self.assertEqual(proxy.addresses('172.18.0.2'), ['172.18.0.2'])
        self.assertEqual(proxy.addresses([]), [])

    def test_invalid_targets_rejected_before_rule_generation(self):
        for value in ('', '127.0.0.1', '::1', '0.0.0.0', '::', '224.0.0.1', 'ff02::1',
                      'fe80::1', '169.254.1.1', '172.18.0.2/24', '172.18.0.2:1080',
                      '2001:db8::2%eth0', '172.18.0.2; flush ruleset', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                proxy.addresses([value])

    def test_uci_list_and_cli_use_same_container_names(self):
        values = ['WARP', 'proxy-2']
        with patch.object(config, 'package', side_effect=[{'main': {'proxy_containers': values}}, {}]):
            self.assertEqual(config.load_configuration()['proxy_containers'], values)
        args = ['plan', '--lan', 'lan', '--wan', 'wan']
        for value in values:
            args.extend(['--proxy-container', value])
        self.assertEqual(collector.parse_options(args).proxy_containers, values)

    def test_local_listener_removal_preserves_counters_and_empty_chains(self):
        state = nft.NftState(exists=True, owned=True, local_chains=True)
        plan = nft.plan(discovery.Inventory(), state, 'lan', 'wan', '')
        self.assertIn('flush chain inet traffic_stats input', plan)
        self.assertIn('flush chain inet traffic_stats output', plan)
        self.assertNotIn('reset', plan)
        self.assertNotIn('delete', plan)
        self.assertNotIn('add rule inet traffic_stats input', plan)


if __name__ == '__main__':
    unittest.main()
