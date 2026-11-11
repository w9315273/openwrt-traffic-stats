"""验证网络配置兼容旧版单设备选项并校验多设备列表."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from traffic_stats import config

ROOT = Path(__file__).resolve().parents[2]


class InterfaceConfigTests(unittest.TestCase):
    def load(self, values):
        with patch.object(config, 'package', side_effect=[{'main': values}, {}]):
            return config.load_configuration()

    def test_legacy_and_plural_precedence(self):
        legacy = self.load({'enabled': '1', 'lan_device': 'br-lan', 'wan_device': 'wan'})
        self.assertEqual(legacy['lan_devices'], ['br-lan'])
        self.assertEqual(legacy['wan_devices'], ['wan'])
        current = self.load({'enabled': '1', 'lan_devices': ['br-guest', 'br-iot', 'br-guest'],
                             'wan_devices': ['pppoe-wan', 'wwan0'], 'lan_device': 'old'})
        self.assertEqual(current['lan_devices'], ['br-guest', 'br-iot'])
        self.assertEqual(current['wan_devices'], ['pppoe-wan', 'wwan0'])

    def test_empty_disabled_valid_but_enabled_invalid(self):
        self.assertEqual(self.load({'lan_devices': '', 'wan_devices': []})['lan_devices'], [])
        with self.assertRaises(ValueError):
            self.load({'enabled': '1', 'lan_devices': [], 'wan_devices': ['wan']})
        with self.assertRaises(ValueError):
            self.load({'enabled': '1'})

    def test_overlaps_and_invalid_device_lists_rejected(self):
        with self.assertRaises(ValueError):
            self.load({'lan_devices': ['br-lan', 'eth2'], 'wan_devices': ['eth2']})
        for value in (['bad name'], ['eth0;reboot'], ['x' * 16], [7], {}, 5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load({'lan_devices': value})

    def init(self, setup):
        source = r'''
. "$1"
config_load() { :; }
config_get_bool() { eval "$1=1"; }
config_get() {
    eval "local cfg_result=\${cfg_${3}-${4-}}"
    eval "$1=\"\$cfg_result\""
}
config_list_foreach() {
    local index=1 length value
    config_get length "$1" "${2}_LENGTH"
    while [ "$index" -le "${length:-0}" ]; do
        config_get value "$1" "${2}_ITEM${index}"
        "$3" "$value"
        index=$((index + 1))
    done
}
uci() { printf '%s\n' UTC0; }
logger() { :; }
procd_open_instance() { :; }
procd_close_instance() { :; }
procd_set_param() { [ "$1" != command ] || printf '<%s>\n' "$@"; }
procd_append_param() { printf '<%s>\n' "$@"; }
cfg_storage_dir=/existing
'''
        source += setup + '\nstart_service\n'
        return subprocess.run(['sh', '-c', source, 'test', str(ROOT / 'openwrt/traffic-stats/files/traffic-stats.init')],
                              text=True, capture_output=True)

    def test_init_repeated_flags_and_legacy_fallback(self):
        legacy = self.init('cfg_lan_device=br-lan\ncfg_wan_device=pppoe-wan')
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertIn('<--lan>\n<br-lan>', legacy.stdout)
        self.assertIn('<--wan>\n<pppoe-wan>', legacy.stdout)
        multi = self.init('''
cfg_lan_devices='br-lan br-guest'
cfg_lan_devices_LENGTH=2
cfg_lan_devices_ITEM1=br-lan
cfg_lan_devices_ITEM2=br-guest
cfg_wan_devices='wan wwan0'
cfg_wan_devices_LENGTH=2
cfg_wan_devices_ITEM1=wan
cfg_wan_devices_ITEM2=wwan0
''')
        self.assertEqual(multi.returncode, 0, multi.stderr)
        self.assertEqual(multi.stdout.count('<--lan>'), 2)
        self.assertEqual(multi.stdout.count('<--wan>'), 2)
        self.assertIn('<br-guest>', multi.stdout)
        self.assertIn('<wwan0>', multi.stdout)

    def test_init_rejects_overlap_and_invalid_list_item(self):
        for setup in ('cfg_lan_devices=eth1\ncfg_wan_devices=eth1',
                      "cfg_lan_devices='bad name'\ncfg_wan_devices=wan",
                      'cfg_lan_devices=\ncfg_lan_device=br-lan\ncfg_wan_devices=wan'):
            with self.subTest(setup=setup):
                result = self.init(setup)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('<command>', result.stdout)


if __name__ == '__main__':
    unittest.main()
