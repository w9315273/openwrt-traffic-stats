#!/usr/bin/env python3
"""验证轮询频率, 规则修复及失败后的采集状态恢复."""

import copy
import io
import json
from pathlib import Path
import socket
import sqlite3
import sys
import time
import unittest
from unittest.mock import Mock, patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from traffic_stats import collector, discovery, nft, store


GENERATION = '12345678-1234-1234-1234-123456789abc'
NEW_GENERATION = '22345678-1234-1234-1234-123456789abc'
BOOT = '32345678-1234-1234-1234-123456789abc'
IDENTITY = '020000000002'
WALL_MS = 1793923200000


def inventory(address='192.0.2.2', hostname='Laptop'):
    result = discovery.Inventory()
    result.add_neighbor(socket.AF_INET, address, '02:00:00:00:00:02',
                        discovery.NUD_REACHABLE, 'lan')
    result.devices[IDENTITY]['hostname'] = hostname
    return result


def snapshot(devices, generation=GENERATION, previous=None, configuration=None):
    shared = {'family': 'inet', 'table': nft.TABLE}
    entries = [{'table': {'family': 'inet', 'name': nft.TABLE, 'handle': 1,
                           'comment': nft.OWNER_PREFIX + generation}}]
    for handle, name in enumerate(nft.CHAINS + nft.LOCAL_CHAINS, 2):
        item = dict(shared, name=name, handle=handle)
        if name in ('forward', 'input', 'output'):
            item.update(type='filter', hook=name, prio=10, policy='accept')
        entries.append({'chain': item})
    for handle, name in enumerate(nft.MAPS, 10):
        version = socket.AF_INET if name.endswith('4') else socket.AF_INET6
        direction = 'up' if name.startswith('up') else 'down'
        elements = [[{'concat': [binding['interface'], binding['address']]},
                     f"d_{binding['device_id']}_{direction}"]
                    for binding in devices.bindings.values()
                    if binding['family'] == version and not binding['conflict']]
        entries.append({'map': dict(shared, name=name, handle=handle,
                                    type=['ifname', 'ipv4_addr' if version == socket.AF_INET else 'ipv6_addr'],
                                    map='counter', elem=elements)})
    existing = copy.deepcopy(previous or {})
    names = set(existing) | set(nft.BASE_COUNTERS)
    names.update(f'd_{identity}_{direction}' for identity in devices.devices for direction in ('up', 'down'))
    for handle, name in enumerate(sorted(names), 20):
        value = existing.get(name, {'bytes': 0, 'packets': 0, 'handle': handle})
        entries.append({'counter': dict(shared, name=name, handle=value['handle'],
                                        bytes=value['bytes'], packets=value['packets'])})
    # 用独立规则内容表示已应用的接口和代理配置, 以检验缓存失效.
    entries.append({'rule': dict(shared, chain='forward', handle=50,
                                 expr=[{'counter': {'name': 'total_up'}}],
                                 comment=json.dumps(configuration or {}, sort_keys=True))})
    return {'nftables': entries}


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.mono = 1000
        self.source = inventory()
        self.listeners = []
        self.proxy_addresses = []
        self.extra_containers = []
        self.document = snapshot(self.source)
        self.flowtable = False
        self.calls = []
        self.failures = {}
        self.fail_after_apply = False
        self.discovery_error = False
        self.proxy_error = False
        self.pending_plan = None
        self.batches = []
        self.real_plan = nft.plan
        options = collector.parse_options(['watch', '--lan', 'lan', '--wan', 'wan',
                                           '--leases', '/dev/null', '--interval', '1', '--timezone', 'UTC0',
                                           '--proxy-container', 'warp'])
        mocks = (
            patch.object(collector, 'clock_ms', side_effect=lambda clock=None:
                         WALL_MS + self.mono if clock == time.CLOCK_REALTIME else self.mono),
            patch.object(collector, '_timezone', return_value='UTC0'),
            patch.object(collector, 'read_id', side_effect=lambda path:
                         BOOT if path.endswith('boot_id') else NEW_GENERATION),
            patch.object(collector, 'run', side_effect=self.run_nft),
            patch.object(collector, 'read_inventory', side_effect=self.discover),
            patch.object(collector.proxy, 'containers', side_effect=self.containers),
            patch.object(collector.nft, 'plan', side_effect=self.plan),
            patch('sys.stderr', new_callable=io.StringIO),
        )
        for replacement in mocks:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.worker = collector.Collector(options)
        self.worker.store = Mock()
        self.worker.store.stage.side_effect = lambda batch: self.batches.append(copy.deepcopy(batch))
        self.worker.store.month_usage.return_value = None

    def discover(self, *args):
        self.calls.append('discover')
        if self.discovery_error:
            raise OSError('discovery unavailable')
        return copy.deepcopy(self.source), True

    def containers(self, *args, **kwargs):
        self.calls.append('proxy')
        if self.proxy_error:
            raise OSError('proxy discovery unavailable')
        records = [{'name': 'warp', 'image': 'test/warp', 'state': 'running',
                    'addresses': self.proxy_addresses, 'listeners': self.listeners}, *self.extra_containers]
        return {'containers': copy.deepcopy(records), 'excluded_names': []}

    def plan(self, devices, state, lan, wan, generation, proxy_ips=(), published=()):
        self.calls.append('plan')
        self.pending_plan = (copy.deepcopy(devices), copy.deepcopy(state), generation,
                             {'lan': list(lan), 'wan': list(wan), 'proxy_ips': list(proxy_ips),
                              'published': list(published)})
        return self.real_plan(devices, state, lan, wan, generation, proxy_ips, published)

    def run_nft(self, args, **kwargs):
        if args == ['nft', '-f', '-']:
            operation = 'apply'
        elif args[-2:] == ['list', 'ruleset']:
            self.assertIn('-t', args)
            operation = 'full'
        elif args[-4:] == ['list', 'table', 'inet', nft.TABLE]:
            self.assertNotIn('-t', args, 'Element values must be present for structural drift detection')
            operation = 'table'
        else:
            raise AssertionError(args)
        self.calls.append(operation)
        if self.failures.get(operation, 0):
            self.failures[operation] -= 1
            raise OSError('forced ' + operation + ' failure')
        if operation == 'apply':
            devices, state, generation, configuration = self.pending_plan
            self.document = snapshot(devices, state.generation or generation,
                                     state.counters, configuration)
            if self.fail_after_apply:
                self.failures['table'] = 1
                self.fail_after_apply = False
            return ''
        if operation == 'table' and not self.document['nftables']:
            raise FileNotFoundError('Statistics table is absent')
        document = copy.deepcopy(self.document)
        if operation == 'full':
            for entry in document['nftables']:
                if 'map' in entry:
                    entry['map'].pop('elem', None)
            if self.flowtable:
                document['nftables'].append({'flowtable': {'family': 'inet', 'table': 'fw4', 'name': 'ft'}})
        return json.dumps(document)

    def collect(self, advance=0, expected=0):
        self.mono += advance
        start = len(self.calls)
        self.assertEqual(self.worker.collect(), expected, self.worker.sample_error)
        return self.calls[start:]

    def initialize(self):
        calls = self.collect()
        self.assertEqual(calls, ['full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        return calls

    def upload(self, amount):
        for entry in self.document['nftables']:
            counter = entry.get('counter')
            if counter and counter['name'] in ('total_up', f'd_{IDENTITY}_up'):
                counter['bytes'] += amount
                counter['packets'] += amount // 10

    def total_staged(self):
        return sum(row['bytes'] for batch in self.batches for row in batch['rows']
                   if row['counter'] == 'total_up')

    def test_one_table_read_per_fast_sample_and_exact_thirty_second_audit(self):
        self.initialize()
        for _ in range(29):
            self.upload(10)
            self.assertEqual(self.collect(1000), ['table'])
        self.assertEqual(self.total_staged(), 290)
        self.assertEqual(self.collect(1000), ['full', 'discover', 'proxy', 'table'])
        self.assertEqual(self.collect(1000), ['table'])
        self.assertEqual(self.total_staged(), 290)

    def test_hostname_refresh_updates_metadata_without_writing_nft(self):
        self.initialize()
        self.source.devices[IDENTITY]['hostname'] = 'Renamed laptop'
        self.assertEqual(self.collect(30000), ['full', 'discover', 'proxy', 'table'])
        device = next(item for item in self.worker.latest['devices'] if item['id'] == IDENTITY)
        self.assertEqual(device['hostname'], 'Renamed laptop')
        self.assertEqual(self.batches[-1]['inventory'].devices[IDENTITY]['hostname'], 'Renamed laptop')

    def test_binding_listener_and_interface_changes_apply_then_read(self):
        self.initialize()
        self.source = inventory('192.0.2.3')
        self.assertEqual(self.collect(30000), ['full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        self.assertEqual(self.worker.latest['devices'][0]['addresses'], ['192.0.2.3'])
        self.listeners = [{'address': '0.0.0.0', 'protocol': 'tcp', 'port': 1080}]
        self.assertEqual(self.collect(30000), ['full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        self.worker.options.wan.append('wan-backup')
        self.assertEqual(self.collect(30000), ['full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        self.worker.options.lan.append('lan-guest')
        self.assertEqual(self.collect(30000), ['full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        self.proxy_addresses.append('172.18.0.2')
        self.assertEqual(self.collect(30000), ['full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        self.assertEqual(self.collect(1000), ['table'])

    def test_empty_container_selection_never_queries_docker(self):
        self.worker.options.proxy_containers = []
        self.assertEqual(self.collect(), ['full', 'discover', 'plan', 'apply', 'table'])
        self.assertEqual(self.worker.latest['proxy_addresses'], [])
        self.assertEqual(self.worker.latest['proxy_listeners'], [])
        self.assertEqual(self.collect(1000), ['table'])
        self.assertEqual(self.collect(29000), ['full', 'discover', 'table'])

    def test_missing_selected_container_clears_endpoints_without_using_other_containers(self):
        self.proxy_addresses = ['172.18.0.2']
        self.listeners = [{'address': '0.0.0.0', 'protocol': 'tcp', 'port': 1080}]
        self.initialize()
        generation = self.worker.latest['generation']
        self.worker.options.proxy_containers = ['removed-container']
        self.assertEqual(self.collect(30000), ['full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        self.assertEqual(self.worker.latest['proxy_addresses'], [])
        self.assertEqual(self.worker.latest['proxy_listeners'], [])
        self.assertEqual(self.worker.latest['generation'], generation)
        self.assertEqual(self.collect(1000), ['table'])

    def test_selected_container_addresses_and_listeners_refresh_without_polling_docker_each_sample(self):
        self.proxy_addresses = ['172.18.0.2']
        self.listeners = [{'address': '0.0.0.0', 'protocol': 'tcp', 'port': 1080}]
        self.extra_containers = [{'name': 'unselected', 'image': 'test/unused', 'state': 'running',
                                 'addresses': ['172.18.0.99'],
                                 'listeners': [{'address': '0.0.0.0', 'protocol': 'tcp', 'port': 9999}]}]
        self.initialize()
        self.assertEqual(self.worker.latest['proxy_addresses'], ['172.18.0.2'])
        self.assertEqual(self.worker.latest['proxy_listeners'], self.listeners)
        self.proxy_addresses = ['172.18.0.3']
        self.listeners = [{'address': '0.0.0.0', 'protocol': 'tcp', 'port': 1081}]
        self.assertEqual(self.collect(1000), ['table'])
        self.assertEqual(self.worker.latest['proxy_addresses'], ['172.18.0.2'])
        self.assertEqual(self.collect(29000), ['full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        self.assertEqual(self.worker.latest['proxy_addresses'], ['172.18.0.3'])
        self.assertEqual(self.worker.latest['proxy_listeners'], self.listeners)
        self.proxy_addresses, self.listeners = [], []
        self.assertEqual(self.collect(30000), ['full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        self.assertEqual(self.worker.latest['proxy_addresses'], [])
        self.assertEqual(self.worker.latest['proxy_listeners'], [])
        self.worker.options.proxy_containers.append('unselected')
        self.assertEqual(self.collect(30000), ['full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        self.assertEqual(self.worker.latest['proxy_addresses'], ['172.18.0.99'])

    def test_rule_and_map_flush_repair_immediately_without_resetting_counters(self):
        self.initialize()
        self.upload(100)
        self.assertEqual(self.collect(1000), ['table'])
        original_generation = self.worker.latest['generation']
        self.document['nftables'] = [entry for entry in self.document['nftables'] if 'rule' not in entry]
        self.upload(50)
        self.assertEqual(self.collect(1000), ['table', 'plan', 'apply', 'table'])
        self.assertEqual(self.worker.latest['total_up']['bytes'], '150')
        self.assertEqual(self.worker.latest['generation'], original_generation)
        for entry in self.document['nftables']:
            if 'map' in entry:
                entry['map']['elem'] = []
        self.upload(70)
        self.assertEqual(self.collect(1000), ['table', 'plan', 'apply', 'table'])
        self.assertEqual(self.worker.latest['total_up']['bytes'], '220')
        self.assertEqual(self.total_staged(), 220)
        self.assertEqual(self.collect(1000), ['table'])

    def test_deleted_table_is_recreated_during_next_fast_sample(self):
        self.initialize()
        self.document = {'nftables': []}
        self.assertEqual(self.collect(1000), ['table', 'full', 'discover', 'proxy', 'plan', 'apply', 'table'])
        self.assertTrue(self.worker.latest['table_created'])
        self.assertTrue(self.worker.latest['generation_changed'])
        self.assertEqual(self.worker.latest['generation'], NEW_GENERATION)
        self.assertEqual(self.collect(1000), ['table'])

    def test_flowtable_audit_result_survives_table_only_samples(self):
        self.flowtable = True
        self.initialize()
        self.assertTrue(self.worker.latest['flowtables_present'])
        self.assertEqual(self.collect(1000), ['table'])
        self.assertTrue(self.worker.latest['flowtables_present'])
        self.flowtable = False
        self.assertEqual(self.collect(29000), ['full', 'discover', 'proxy', 'table'])
        self.assertFalse(self.worker.latest['flowtables_present'])

    def test_discovery_failures_retry_without_committing_partial_metadata(self):
        for failure in ('discovery_error',):
            with self.subTest(failure=failure):
                if self.worker.latest is None:
                    self.initialize()
                old_hostname = self.worker.latest['devices'][0]['hostname']
                old_previous = copy.deepcopy(self.worker.previous)
                self.source.devices[IDENTITY]['hostname'] = failure
                setattr(self, failure, True)
                self.upload(50)
                self.collect(30000, expected=1)
                self.assertEqual(self.worker.previous, old_previous)
                self.assertEqual(self.worker.latest['devices'][0]['hostname'], old_hostname)
                setattr(self, failure, False)
                calls = self.collect(1000)
                self.assertIn('discover', calls)
                self.assertIn('proxy', calls)
                self.assertEqual(self.worker.latest['devices'][0]['hostname'], failure)
        self.assertEqual(self.total_staged(), 50)

    def test_docker_unavailable_at_start_retries_and_recovers(self):
        self.proxy_error = True
        self.initialize()
        self.assertEqual(self.worker.proxy_ips, [])
        self.assertEqual(self.worker.sample_error, '')
        self.assertTrue(self.worker.proxy_error)
        self.upload(50)
        self.assertEqual(self.collect(1000), ['table'])
        self.assertEqual(self.total_staged(), 50)
        self.proxy_error = False
        self.proxy_addresses = ['172.18.0.2']
        self.assertIn('apply', self.collect(29000))
        self.assertEqual(self.worker.proxy_ips, ['172.18.0.2'])
        self.assertEqual(self.worker.proxy_error, '')

    def test_docker_outage_removes_stale_endpoints_without_losing_samples(self):
        self.proxy_addresses = ['172.18.0.2']
        self.listeners = [{'address': '0.0.0.0', 'protocol': 'tcp', 'port': 1080}]
        self.initialize()
        self.proxy_error = True
        self.upload(50)
        self.assertIn('apply', self.collect(30000))
        self.assertEqual(self.worker.proxy_ips, [])
        self.assertEqual(self.worker.listeners, [])
        self.assertEqual(self.total_staged(), 50)
        self.upload(25)
        self.collect(30000)
        self.assertEqual(self.total_staged(), 75)
        self.proxy_error = False
        self.proxy_addresses = ['172.18.0.3']
        self.assertIn('apply', self.collect(30000))
        self.assertEqual(self.worker.proxy_ips, ['172.18.0.3'])
        self.assertEqual(self.worker.listeners, self.listeners)

    def test_apply_failure_does_not_cache_the_unapplied_configuration(self):
        self.initialize()
        old_previous = copy.deepcopy(self.worker.previous)
        self.source = inventory('192.0.2.3')
        self.failures['apply'] = 1
        self.upload(50)
        self.collect(30000, expected=1)
        self.assertEqual(self.worker.previous, old_previous)
        self.assertEqual(self.worker.latest['devices'][0]['addresses'], ['192.0.2.2'])
        self.upload(70)
        calls = self.collect(1000)
        self.assertIn('apply', calls)
        self.assertEqual(self.worker.latest['devices'][0]['addresses'], ['192.0.2.3'])
        self.assertEqual(self.total_staged(), 120)
        self.assertEqual(self.collect(1000), ['table'])

    def test_failed_post_apply_read_does_not_publish_or_advance_checkpoints(self):
        self.initialize()
        old_previous = copy.deepcopy(self.worker.previous)
        old_latest = copy.deepcopy(self.worker.latest)
        self.source = inventory('192.0.2.3')
        self.fail_after_apply = True
        self.upload(50)
        self.collect(30000, expected=1)
        self.assertEqual(self.worker.previous, old_previous)
        self.assertEqual(self.worker.latest, old_latest)
        self.upload(70)
        self.collect(1000)
        self.assertEqual(self.worker.latest['devices'][0]['addresses'], ['192.0.2.3'])
        self.assertEqual(self.total_staged(), 120)
        self.assertEqual(self.collect(1000), ['table'])

    def test_failed_fast_and_fallback_reads_preserve_last_good_sample(self):
        self.initialize()
        old_previous = copy.deepcopy(self.worker.previous)
        self.upload(50)
        self.failures.update(table=1, full=1)
        self.collect(1000, expected=1)
        self.assertIn('Read nftables state', self.worker.sample_error)
        self.assertEqual(self.worker.previous, old_previous)
        self.upload(70)
        self.collect(1000)
        self.assertEqual(self.total_staged(), 120)
        self.assertEqual(self.collect(1000), ['table'])

    def test_stage_failure_retries_delta_and_discovery_metadata_together(self):
        self.initialize()
        old_previous = copy.deepcopy(self.worker.previous)
        self.source.devices[IDENTITY]['hostname'] = 'Pending name'
        self.upload(50)
        self.worker.store.stage.side_effect = ValueError('forced staging error')
        self.collect(30000, expected=1)
        self.assertEqual(self.worker.previous, old_previous)
        self.assertEqual(self.worker.latest['devices'][0]['hostname'], 'Laptop')
        self.worker.store.stage.side_effect = lambda batch: self.batches.append(copy.deepcopy(batch))
        self.upload(70)
        calls = self.collect(1000)
        self.assertIn('discover', calls)
        self.assertEqual(self.worker.latest['devices'][0]['hostname'], 'Pending name')
        self.assertEqual(self.total_staged(), 120)

    def test_monthly_query_failure_does_not_recount_staged_traffic(self):
        self.worker.store = store.Store(':memory:')
        self.addCleanup(self.worker.store.close)
        self.initialize()
        old_latest = copy.deepcopy(self.worker.latest)
        self.upload(50)
        with patch.object(self.worker.store, 'month_usage',
                          side_effect=sqlite3.OperationalError('disk I/O error')):
            self.collect(1000, expected=1)
        self.assertIn('Calculate monthly usage: disk I/O error', self.worker.sample_error)
        self.assertEqual(self.worker.latest, old_latest)
        self.assertEqual(self.worker.previous['total_up']['bytes'], 50)
        staged = lambda: self.worker.store.db.execute(
            "SELECT sum(bytes) FROM pending.usage_daily WHERE counter='total_up'").fetchone()[0]
        self.assertEqual(staged(), 50)
        self.upload(70)
        self.collect(1000)
        self.assertEqual(staged(), 120)
        self.assertEqual(self.worker.latest['rates']['total_up']['bytes_per_second'], 70)


if __name__ == '__main__':
    unittest.main()
