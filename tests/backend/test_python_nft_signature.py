"""验证规则指纹忽略计数变化, 但能识别统计规则被改动."""
from copy import deepcopy
import unittest
from test_python_multi_nft import scoped_fixture
from traffic_stats import nft


def fixture():
    data = scoped_fixture()
    data['nftables'][4]['map']['elem'] = [
        [{'concat': ['lan', '192.0.2.2']}, 'd_001122334455_up'],
        [{'concat': ['guest', '192.0.2.2']}, 'unknown_up'],
    ]
    table = {'family': 'inet', 'table': nft.TABLE, 'chain': 'upload'}
    data['nftables'].extend([
        {'rule': dict(table, handle=40, expr=[{'counter': {'map': {
            'key': {'concat': [{'meta': {'key': 'iifname'}}, {'payload': {'protocol': 'ip', 'field': 'saddr'}}]},
            'data': '@up4'}}}, {'return': None}])},
        {'rule': dict(table, handle=41, expr=[{'counter': 'unknown_up'}, {'return': None}])},
    ])
    return data


class SignatureTests(unittest.TestCase):
    def test_counts_and_other_tables_do_not_change_configuration_signature(self):
        original = fixture()
        changed = deepcopy(original)
        for entry in changed['nftables']:
            if 'counter' in entry:
                entry['counter'].update(bytes=1000, packets=30)
        changed['nftables'].extend([
            {'metainfo': {'version': 'other-version'}},
            {'rule': {'family': 'inet', 'table': 'fw4', 'chain': 'forward', 'expr': []}},
            {'flowtable': {'family': 'inet', 'table': 'fw4', 'name': 'ft'}},
        ])
        self.assertEqual(nft.parse(original).signature, nft.parse(changed).signature)
        self.assertNotEqual(nft.parse(original).flowtables_present, nft.parse(changed).flowtables_present)

    def test_maps_counters_and_rule_order_change_signature(self):
        original = fixture()
        baseline = nft.parse(original).signature
        cases = []
        data = deepcopy(original); data['nftables'][4]['map']['elem'][0][1] = 'unknown_up'; cases.append(data)
        data = deepcopy(original); data['nftables'][4]['map']['elem'][0][0]['concat'][0] = 'guest'; cases.append(data)
        data = deepcopy(original); data['nftables'][4]['map']['elem'].pop(); cases.append(data)
        data = deepcopy(original); data['nftables'][8]['counter']['handle'] = 50; cases.append(data)
        data = deepcopy(original); data['nftables'][0]['table']['comment'] = nft.OWNER_PREFIX + '11111111-1111-1111-1111-111111111111'; cases.append(data)
        data = deepcopy(original); data['nftables'][-1]['rule']['expr'][0]['counter'] = 'total_up'; cases.append(data)
        data = deepcopy(original); data['nftables'][-2:] = reversed(data['nftables'][-2:]); cases.append(data)
        data = deepcopy(original); data['nftables'].pop(); cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                self.assertNotEqual(nft.parse(data).signature, baseline)

    def test_object_and_element_iteration_order_are_not_configuration(self):
        original = fixture()
        changed = deepcopy(original)
        changed['nftables'][4]['map']['elem'].reverse()
        changed['nftables'] = list(reversed(changed['nftables'][:-2])) + changed['nftables'][-2:]
        self.assertEqual(nft.parse(original).signature, nft.parse(changed).signature)
        # 空映射可省略 elem, 也可显式返回空列表.
        changed = deepcopy(original)
        changed['nftables'][5]['map']['elem'] = []
        self.assertEqual(nft.parse(original).signature, nft.parse(changed).signature)

    def test_bad_map_elements_fail_validation(self):
        for elements in (None, {}, 'bad'):
            data = fixture()
            data['nftables'][4]['map']['elem'] = elements
            with self.subTest(elements=elements), self.assertRaises(OSError):
                nft.parse(data)


if __name__ == '__main__':
    unittest.main()
