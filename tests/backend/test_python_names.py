"""验证名称匹配优先级, 保证不合并设备或改动原始名称与计数."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from traffic_stats import names

MAC = '84:2f:57:29:06:1f'


class NamingTests(unittest.TestCase):
    def rules(self, *entries):
        return names.rules(list(entries))[0]

    def test_combined_then_mac_then_ip_and_original_identity(self):
        rules=self.rules({'ip':'192.168.2.3','name':'IP 名称'},
                         {'mac':MAC,'name':'我的 MacBook Pro'},
                         {'mac':MAC,'ip':'192.168.2.3','name':'组合名称'})
        device={'mac':MAC,'hostname':'original','id':'842f5729061f','up':{'bytes':'123'},'addresses':['192.168.2.3']}
        names.apply([device],rules)
        self.assertEqual(device['custom_name'],'组合名称')
        names.apply([device],rules[:2])
        self.assertEqual(device['custom_name'],'我的 MacBook Pro')
        names.apply([device],rules[:1])
        self.assertEqual(device['custom_name'],'IP 名称')
        self.assertEqual((device['hostname'],device['id'],device['up']),('original','842f5729061f',{'bytes':'123'}))

    def test_combined_requires_both_and_offline_ip_does_not_match(self):
        device={'mac':MAC,'addresses':['192.168.2.4']}
        rules=self.rules({'mac':MAC,'ip':'192.168.2.3','name':'组合'})
        names.apply([device],rules)
        self.assertEqual(device['custom_name'],'')
        offline = {'mac':MAC}
        names.apply([offline],self.rules({'ip':'192.168.2.3','name':'IP'}))
        self.assertEqual(offline['custom_name'],'')

    def test_same_name_does_not_merge_distinct_devices(self):
        devices=[{'mac':MAC},{'mac':'90:4c:c5:42:8f:ac','addresses':['192.168.2.3']}]
        names.apply(devices,self.rules({'mac':MAC,'name':'我的笔记本'},{'ip':'192.168.2.3','name':'我的笔记本'}))
        self.assertEqual(len(devices),2)
        self.assertEqual([d['custom_name'] for d in devices],['我的笔记本']*2)

    def test_ipv6_mac_case_and_rule_order(self):
        rules=self.rules({'mac':MAC.upper(),'ip':'2001:0DB8:0:0::3','name':'首条'},
                         {'mac':MAC,'ip':'2001:db8::3','name':'后条'})
        device={'mac':MAC,'addresses':['2001:db8::3']}
        names.apply([device],rules)
        self.assertEqual(device['custom_name'],'首条')

    def test_bad_rules_are_skipped_not_used_as_wildcards(self):
        bad=[{'name':'缺少条件'},{'name':'a','mac':['bad']},{'name':'a','ip':'192.168.2.0/24'},
             {'name':'a','ip':'fe80::1%eth0'},{'name':'bad\nname','mac':MAC},{'name':'a','mac':'bad'},
             {'name':'x'*129,'mac':MAC}]
        result,invalid=names.rules(bad)
        self.assertEqual(result,[])
        self.assertEqual(len(invalid),len(bad))

    def test_removing_rule_clears_custom_name_and_preserves_hostname(self):
        device={'mac':MAC,'hostname':'原主机名'}
        names.apply([device],self.rules({'mac':MAC,'name':'自定义'}))
        names.apply([device],[])
        self.assertEqual(device['custom_name'],'')
        self.assertEqual(device['hostname'],'原主机名')

if __name__=='__main__':
    unittest.main()

class JsonStorageTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest.mock import patch
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.local/tmp')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'devices.json'
        patcher = patch.object(names, 'legacy', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unicode_crud_revision_and_permissions(self):
        entries, revision = names.read(self.path)
        first = {'name': "我的 '笔记本'", 'mac': MAC}
        self.assertEqual(names.save(self.path, 'save', '', first, revision), '0')
        self.assertEqual(names.read(self.path)[0], [first])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(names.Conflict):
            names.save(self.path, 'save', '', {'name':'stale','mac':MAC}, revision)
        revision = names.read(self.path)[1]
        names.save(self.path, 'save', '', {'name':'IPv6','ip':'2001:db8::1'}, revision)
        revision = names.read(self.path)[1]
        names.save(self.path, 'delete', '0', {}, revision)
        self.assertEqual(names.read(self.path)[0], [{'name':'IPv6','ip':'2001:db8::1'}])
        with self.assertRaises(names.Conflict):
            names.save(self.path, 'delete', '1', {}, revision)

    def test_failed_atomic_write_preserves_original(self):
        from unittest.mock import patch
        names.write(self.path, [{'name':'original','mac':MAC}])
        before = self.path.read_bytes()
        with patch.object(names.os, 'replace', side_effect=OSError('read-only filesystem')):
            with self.assertRaises(OSError):
                names.save(self.path, 'delete', '0', {}, names.read(self.path)[1])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('.devices-*')), [])

    def test_bad_json_is_never_overwritten(self):
        for raw in ('[', '{}', '[NaN]', '["\\ud800"]'):
            self.path.write_text(raw, errors='backslashreplace')
            before = self.path.read_bytes()
            with self.assertRaises((ValueError, UnicodeError)):
                names.save(self.path, 'save', '', {'name':'new','mac':MAC}, 'old')
            self.assertEqual(self.path.read_bytes(), before)

    def test_existing_destination_wins_on_move(self):
        source = self.path.with_name('old.json')
        names.write(source, [{'name':'old','mac':MAC}])
        names.initialize(self.path, source)
        self.assertEqual(names.read(self.path)[0][0]['name'], 'old')
        names.write(self.path, [{'name':'new','mac':MAC}])
        names.initialize(self.path, source)
        self.assertEqual(names.read(self.path)[0][0]['name'], 'new')
